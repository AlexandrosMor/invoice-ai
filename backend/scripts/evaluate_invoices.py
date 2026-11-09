"""Evaluate local Ollama invoice extraction against manually verified JSON."""
from __future__ import annotations
import argparse, json, os, platform, statistics, sys, time
import hashlib
from pathlib import Path
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone

from document_processing import DocumentProcessor
from extraction import PIPELINE_VERSION
from extraction_strategies import create_extractor
from invoice_domain import Invoice
from validation import validate
from duplicate_detection import classify_duplicate

FIELDS=("supplier_name","supplier_address","supplier_vat_number","supplier_kvk_number","supplier_iban","customer_name","customer_address","customer_vat_number","invoice_number","invoice_date","due_date","delivery_date","payment_date","purchase_order_reference","currency","document_type","subtotal","discount_total","additional_charges","total_vat","invoice_total","amount_paid","amount_due","vat_breakdown","line_items")
NUMERIC_FIELDS={"subtotal","discount_total","additional_charges","total_vat","invoice_total","amount_paid","amount_due"}
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
            if "treatment" in x and norm(x.get("treatment"))!=norm(y.get("treatment")):return False
        return True
    if field=="line_items":
        e=expected or []; a=actual or []
        if len(e)!=len(a):return False
        for x,y in zip(e,a):
            for key in ("description","quantity","unit_price","vat_rate","line_total","gross_total","discount"):
                xv,yv=x.get(key),y.get(key)
                if key in ("quantity","unit_price","vat_rate","line_total"):
                    if not close_numeric(xv,yv,tolerance):return False
                elif norm(xv)!=norm(yv):return False
            for key in ("vat_treatment","price_includes_vat"):
                if key in x and norm(x.get(key))!=norm(y.get(key)):return False
        return True
    return norm(expected)==norm(actual)

def evaluate_dataset(dataset:Path, output:Path|None=None, extractor=None, strategy:str|None=None):
    invoice_dir=dataset/"invoices"; truth_dir=dataset/"ground_truth"
    truth_files=list(truth_dir.glob("*.json"))
    truth_files.sort(key=lambda path:(json.loads(path.read_text()).get("sequence",10**9),path.name))
    results=[];durations=[];preprocessing_durations=[];strategy_durations=[];inference_durations=[];field_hits={f:0 for f in FIELDS};field_counts={f:0 for f in FIELDS}
    extraction_failures=0;manual_reviews=0;validation_failures=0;successful=0;false_accept=0;false_accept_denominator=0;extractor=extractor or create_extractor(strategy);prior_duplicate_candidates=[];duplicate_cases=[]
    for truth_file in truth_files:
        invoice_id=truth_file.stem; ground=json.loads(truth_file.read_text()); doc_path=next((invoice_dir/f"{invoice_id}{ext}" for ext in (".pdf",".png",".jpg",".jpeg") if (invoice_dir/f"{invoice_id}{ext}").exists()),None)
        if doc_path is None:
            results.append({"id":invoice_id,"status":"failed","error":"Invoice fixture is missing"}); extraction_failures+=1; continue
        t=time.perf_counter()
        try:
            document=DocumentProcessor.process(doc_path,"application/pdf" if doc_path.suffix==".pdf" else "image/jpeg" if doc_path.suffix in (".jpg",".jpeg") else "image/png")
            preprocessing_ms=round((time.perf_counter()-t)*1000,2)
            extracted=extractor.extract(document); invoice=extracted["invoice"].model_dump(mode="json"); elapsed=round((time.perf_counter()-t)*1000,2)
            strategy_name=getattr(extractor,"name",None) or strategy or "ollama"
            actual_strategy=extracted.get("strategy_selected",strategy_name)
            inference_ms=extracted.get("model_inference_duration_ms")
            if inference_ms is None and actual_strategy=="ollama":inference_ms=extracted.get("duration_ms",round(elapsed-preprocessing_ms,2))
            strategy_ms=extracted.get("strategy_duration_ms",round(elapsed-preprocessing_ms,2))
            successful+=1; durations.append(float(elapsed)); preprocessing_durations.append(float(preprocessing_ms)); strategy_durations.append(float(strategy_ms))
            if inference_ms is not None:inference_durations.append(float(inference_ms))
            validation=validate(Invoice.model_validate(invoice),[])
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
            hash_value=hashlib.sha256(doc_path.read_bytes()).hexdigest()
            duplicate=classify_duplicate(invoice,hash_value,prior_duplicate_candidates)
            if ground.get("duplicate_expected"):
                duplicate_cases.append({"expected":ground["duplicate_expected"],"predicted":duplicate["classification"]})
            results.append({"id":invoice_id,"status":"succeeded","duration_ms":elapsed,"preprocessing_duration_ms":preprocessing_ms,"strategy_duration_ms":strategy_ms,"inference_duration_ms":inference_ms,"strategy":extracted.get("strategy",strategy_name),"strategy_selected":actual_strategy,"fallback_reason":extracted.get("fallback_reason"),"expected":ground,"extracted":invoice,"incorrect_fields":incorrect,"validation":validation,"duplicate_detection":duplicate,"model":extracted["model"],"pipeline_version":extracted["pipeline_version"]})
            prior_duplicate_candidates.append({"id":invoice_id,"invoice":invoice,"file_hash":hash_value})
        except Exception as e:
            extraction_failures+=1;hash_value=hashlib.sha256(doc_path.read_bytes()).hexdigest();duplicate=classify_duplicate({},hash_value,prior_duplicate_candidates)
            if ground.get("duplicate_expected"):duplicate_cases.append({"expected":ground["duplicate_expected"],"predicted":duplicate["classification"]})
            results.append({"id":invoice_id,"status":"failed","error":str(e),"duplicate_detection":duplicate});prior_duplicate_candidates.append({"id":invoice_id,"invoice":{},"file_hash":hash_value})
    total=len(truth_files); all_fields=sum(field_counts.values()); exact=sum(field_hits.values())
    sorted_times=sorted(durations); p95=sorted_times[max(0,int(.95*len(sorted_times)+.999999)-1)] if sorted_times else None
    duplicate_tp=sum(c["expected"]!="NO_DUPLICATE" and c["predicted"]!="NO_DUPLICATE" for c in duplicate_cases);duplicate_fp=sum(c["expected"]=="NO_DUPLICATE" and c["predicted"]!="NO_DUPLICATE" for c in duplicate_cases);duplicate_fn=sum(c["expected"]!="NO_DUPLICATE" and c["predicted"]=="NO_DUPLICATE" for c in duplicate_cases)
    p50=statistics.median(durations) if durations else None
    field_accuracy={f:(field_hits[f]/field_counts[f] if field_counts[f] else None) for f in FIELDS}
    accuracy_groups={"identity":("supplier_name","supplier_vat_number","invoice_number"),"dates":("invoice_date","due_date","delivery_date","payment_date"),"currency":("currency",),"financial_totals":("subtotal","discount_total","additional_charges","total_vat","invoice_total","amount_paid","amount_due"),"tax":("vat_breakdown",),"line_items":("line_items",)}
    grouped={name:(sum(field_hits[f] for f in fields)/sum(field_counts[f] for f in fields) if sum(field_counts[f] for f in fields) else None) for name,fields in accuracy_groups.items()}
    selected_strategy=getattr(extractor,"name",None) or strategy or "ollama"
    report={"benchmark_version":"1.3.0","dataset":str(dataset),"dataset_size":total,"strategy":selected_strategy,"model":getattr(extractor,"model","deterministic-pdf-text" if selected_strategy=="deterministic" else "unknown"),"pipeline_version":PIPELINE_VERSION,"generated_at":datetime.now(timezone.utc).isoformat(),"runtime":{"python":platform.python_version(),"platform":platform.platform(),"hardware_description":os.getenv("INVOICE_BENCHMARK_HARDWARE","not supplied"),"estimated_processing_cost":None,"estimated_processing_cost_note":"Local inference cost is not estimated; no hardware or energy tariff was supplied."},"total_invoices":total,"successful_invoices":successful,"field_accuracy":field_accuracy,"field_accuracy_by_type":grouped,"supplier_accuracy":field_hits["supplier_name"]/field_counts["supplier_name"] if field_counts["supplier_name"] else None,"invoice_number_accuracy":field_hits["invoice_number"]/field_counts["invoice_number"] if field_counts["invoice_number"] else None,"currency_accuracy":field_hits["currency"]/field_counts["currency"] if field_counts["currency"] else None,"date_accuracy":{f:(field_hits[f]/field_counts[f] if field_counts[f] else None) for f in ("invoice_date","due_date","delivery_date","payment_date")},"financial_total_accuracy":{f:(field_hits[f]/field_counts[f] if field_counts[f] else None) for f in ("subtotal","total_vat","invoice_total","amount_due")},"vat_breakdown_accuracy":field_hits["vat_breakdown"]/field_counts["vat_breakdown"] if field_counts["vat_breakdown"] else None,"line_item_accuracy":field_hits["line_items"]/field_counts["line_items"] if field_counts["line_items"] else None,"overall_field_accuracy":exact/all_fields if all_fields else None,"whole_invoice_exact_match_rate":sum(1 for x in results if x.get("status")=="succeeded" and not x.get("incorrect_fields"))/total if total else None,"extraction_failure_rate":extraction_failures/total if total else None,"average_preprocessing_time_ms":statistics.mean(preprocessing_durations) if preprocessing_durations else None,"average_strategy_time_ms":statistics.mean(strategy_durations) if strategy_durations else None,"average_inference_time_ms":statistics.mean(inference_durations) if inference_durations else None,"average_processing_time_ms":statistics.mean(durations) if durations else None,"p50_processing_time_ms":p50,"p95_processing_time_ms":p95,"critical_false_accept_rate":false_accept/false_accept_denominator if false_accept_denominator else None,"validation_failure_rate":validation_failures/successful if successful else None,"manual_review_rate":manual_reviews/successful if successful else None,"duplicate_detection":{"labeled_examples":len(duplicate_cases),"class_accuracy":sum(c["expected"]==c["predicted"] for c in duplicate_cases)/len(duplicate_cases) if duplicate_cases else None,"precision":duplicate_tp/(duplicate_tp+duplicate_fp) if duplicate_tp+duplicate_fp else None,"recall":duplicate_tp/(duplicate_tp+duplicate_fn) if duplicate_tp+duplicate_fn else None,"true_positives":duplicate_tp,"false_positives":duplicate_fp,"false_negatives":duplicate_fn},"mapping_suggestion_correctness":{"labeled_examples":0,"accuracy":None,"message":"No labeled mapping-rule dataset is configured."},"invoices":results}
    if output:
        output.parent.mkdir(parents=True,exist_ok=True); output.write_text(json.dumps(report,indent=2,ensure_ascii=False))
    return report

def main():
    parser=argparse.ArgumentParser();parser.add_argument("--dataset",type=Path,required=True);parser.add_argument("--output",type=Path,default=Path("tests/fixtures/results.json"));parser.add_argument("--strategy",choices=("ollama","deterministic","hybrid"),default=None,help="Extraction strategy; defaults to INVOICE_EXTRACTION_STRATEGY (ollama).");args=parser.parse_args()
    result=evaluate_dataset(args.dataset,args.output,strategy=args.strategy); print(json.dumps({k:v for k,v in result.items() if k!="invoices"},indent=2))
if __name__=="__main__":main()
