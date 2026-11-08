"""Evaluate local Ollama invoice extraction against manually verified JSON."""
from __future__ import annotations
import argparse, json, statistics, sys, time
from pathlib import Path
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone

from document_processing import DocumentProcessor
from extraction import InvoiceExtractor, PIPELINE_VERSION
from invoice_domain import Invoice
from validation import validate

FIELDS=("invoice_number","supplier_name","invoice_date","due_date","currency","subtotal","total_vat","invoice_total","vat_breakdown","line_items")
NUMERIC_FIELDS={"subtotal","total_vat","invoice_total"}
def norm(v):
    if isinstance(v,dict): return {k:norm(x) for k,x in sorted(v.items())}
    if isinstance(v,list): return [norm(x) for x in v]
    if v is None:return None
    if isinstance(v,str):return v.strip().casefold()
    return v
def close_numeric(a,b,tol):
    if a is None or b is None:return a is b
    try:return abs(Decimal(str(a))-Decimal(str(b)))<=tol
    except (InvalidOperation,TypeError):return False
def field_equal(field,expected,actual,tolerance=Decimal("0.01")):
    if field in NUMERIC_FIELDS:return close_numeric(expected,actual,tolerance) if expected is not None and actual is not None else expected is actual
    if field=="vat_breakdown":
        e=expected or []; a=actual or []
        if len(e)!=len(a):return False
        for x,y in zip(e,a):
            for key in ("rate","taxable_amount","tax_amount"):
                xv,yv=x.get(key),y.get(key)
                if key=="rate":
                    if not close_numeric(xv,yv,tolerance):return False
                elif not close_numeric(xv,yv,tolerance):return False
        return True
    if field=="line_items":
        e=expected or []; a=actual or []
        if len(e)!=len(a):return False
        for x,y in zip(e,a):
            for key in ("description","quantity","unit_price","vat_rate","line_total"):
                xv,yv=x.get(key),y.get(key)
                if key in ("quantity","unit_price","vat_rate","line_total"):
                    if not close_numeric(xv,yv,tolerance):return False
                elif norm(xv)!=norm(yv):return False
        return True
    return norm(expected)==norm(actual)

def evaluate_dataset(dataset:Path, output:Path|None=None, extractor=None):
    invoice_dir=dataset/"invoices"; truth_dir=dataset/"ground_truth"
    truth_files=sorted(truth_dir.glob("*.json")); results=[]; durations=[]; preprocessing_durations=[]; inference_durations=[]; field_hits={f:0 for f in FIELDS}; field_counts={f:0 for f in FIELDS}
    extraction_failures=0; manual_reviews=0; validation_failures=0; successful=0; false_accept=0; false_accept_denominator=0; extractor=extractor or InvoiceExtractor()
    for truth_file in truth_files:
        invoice_id=truth_file.stem; ground=json.loads(truth_file.read_text()); doc_path=next((invoice_dir/f"{invoice_id}{ext}" for ext in (".pdf",".png",".jpg",".jpeg") if (invoice_dir/f"{invoice_id}{ext}").exists()),None)
        if doc_path is None:
            results.append({"id":invoice_id,"status":"failed","error":"Invoice fixture is missing"}); extraction_failures+=1; continue
        t=time.perf_counter()
        try:
            document=DocumentProcessor.process(doc_path,"application/pdf" if doc_path.suffix==".pdf" else "image/jpeg" if doc_path.suffix in (".jpg",".jpeg") else "image/png")
            preprocessing_ms=round((time.perf_counter()-t)*1000,2)
            extracted=extractor.extract(document); invoice=extracted["invoice"].model_dump(mode="json"); elapsed=round((time.perf_counter()-t)*1000,2)
            inference_ms=extracted.get("duration_ms",round(elapsed-preprocessing_ms,2))
            successful+=1; durations.append(float(elapsed)); preprocessing_durations.append(float(preprocessing_ms)); inference_durations.append(float(inference_ms)); validation=validate(Invoice.model_validate(invoice),[])
            incorrect=[]
            for field in FIELDS:
                if field not in ground: continue
                field_counts[field]+=1; ok=field_equal(field,ground.get(field),invoice.get(field))
                if ok:field_hits[field]+=1
                else:incorrect.append(field)
            critical=[f for f in ("supplier_name","invoice_number","invoice_total") if f in ground]
            if critical and validation["valid"]:
                false_accept_denominator+=1
                if any(not field_equal(f,ground[f],invoice.get(f)) for f in critical): false_accept+=1
            evidence=extracted.get("evidence",{})
            missing_evidence=any(invoice.get(f) is not None and evidence.get(f,{}).get("needs_review",True) for f in ("supplier_name","invoice_number","invoice_date","currency","invoice_total"))
            if validation["review_assessment"]!="READY_FOR_REVIEW" or missing_evidence:manual_reviews+=1
            if not validation["valid"]:validation_failures+=1
            results.append({"id":invoice_id,"status":"succeeded","duration_ms":elapsed,"preprocessing_duration_ms":preprocessing_ms,"inference_duration_ms":inference_ms,"expected":ground,"extracted":invoice,"incorrect_fields":incorrect,"validation":validation,"model":extracted["model"],"pipeline_version":extracted["pipeline_version"]})
        except Exception as e:
            extraction_failures+=1; results.append({"id":invoice_id,"status":"failed","error":str(e)})
    total=len(truth_files); all_fields=sum(field_counts.values()); exact=sum(field_hits.values())
    sorted_times=sorted(durations); p95=sorted_times[max(0,int(.95*len(sorted_times)+.999999)-1)] if sorted_times else None
    report={"benchmark_version":"1.1.0","dataset":str(dataset),"model":getattr(extractor,"model","unknown"),"pipeline_version":PIPELINE_VERSION,"generated_at":datetime.now(timezone.utc).isoformat(),"total_invoices":total,"successful_invoices":successful,"field_accuracy":{f:(field_hits[f]/field_counts[f] if field_counts[f] else None) for f in FIELDS},"overall_field_accuracy":exact/all_fields if all_fields else None,"whole_invoice_exact_match_rate":sum(1 for x in results if x.get("status")=="succeeded" and not x.get("incorrect_fields"))/total if total else None,"extraction_failure_rate":extraction_failures/total if total else None,"average_preprocessing_time_ms":statistics.mean(preprocessing_durations) if preprocessing_durations else None,"average_inference_time_ms":statistics.mean(inference_durations) if inference_durations else None,"average_processing_time_ms":statistics.mean(durations) if durations else None,"p95_processing_time_ms":p95,"critical_false_accept_rate":false_accept/false_accept_denominator if false_accept_denominator else None,"validation_failure_rate":validation_failures/successful if successful else None,"manual_review_rate":manual_reviews/successful if successful else None,"invoices":results}
    if output:
        output.parent.mkdir(parents=True,exist_ok=True); output.write_text(json.dumps(report,indent=2,ensure_ascii=False))
    return report

def main():
    parser=argparse.ArgumentParser();parser.add_argument("--dataset",type=Path,required=True);parser.add_argument("--output",type=Path,default=Path("tests/fixtures/results.json"));args=parser.parse_args()
    result=evaluate_dataset(args.dataset,args.output); print(json.dumps({k:v for k,v in result.items() if k!="invoices"},indent=2))
if __name__=="__main__":main()
