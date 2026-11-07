from __future__ import annotations
import json, logging, os, uuid
import time
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import create_engine, String, Text, select, inspect, text, update
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from invoice_domain import Invoice
from document_processing import DocumentProcessor
from extraction import InvoiceExtractor, ExtractionError, IMPORTANT_FIELDS
from validation import validate

ROOT = Path(__file__).resolve().parent
DATA = Path(os.getenv("INVOICE_DATA_DIR", ROOT / "data"))
DATA.mkdir(parents=True, exist_ok=True)
MAX_BYTES = 10 * 1024 * 1024
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA / 'invoices.db'}")
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {})
Session = sessionmaker(engine, expire_on_commit=False)

class Base(DeclarativeBase): pass
class Record(Base):
    __tablename__ = "invoices"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    filename: Mapped[str] = mapped_column(String)
    path: Mapped[str] = mapped_column(String)
    mime: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="UPLOADED")
    invoice_json: Mapped[str] = mapped_column(Text, default="{}")
    validation_json: Mapped[str] = mapped_column(Text, default="{}")
    sync_json: Mapped[str] = mapped_column(Text, default="{}")
    extraction_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[str] = mapped_column(String, default=lambda: datetime.utcnow().isoformat())
class AuditEvent(Base):
    __tablename__="invoice_audit"
    id: Mapped[str]=mapped_column(String,primary_key=True,default=lambda:str(uuid.uuid4()))
    invoice_id: Mapped[str]=mapped_column(String,index=True)
    timestamp: Mapped[str]=mapped_column(String,default=lambda:datetime.now(timezone.utc).isoformat())
    event_type: Mapped[str]=mapped_column(String)
    metadata_json: Mapped[str]=mapped_column(Text,default="{}")
Base.metadata.create_all(engine)

# Additive SQLite migration: preserve all pre-existing invoice rows and files.
if "extraction_json" not in {column["name"] for column in inspect(engine).get_columns("invoices")}:
    with engine.begin() as conn: conn.execute(text("ALTER TABLE invoices ADD COLUMN extraction_json TEXT NOT NULL DEFAULT '{}'"))

class Status(str, Enum):
    UPLOADED="UPLOADED"; PROCESSING="PROCESSING"; NEEDS_REVIEW="NEEDS_REVIEW"; VALIDATED="VALIDATED"; APPROVED="APPROVED"; FAILED="FAILED"

def record_dict(r):
    return {"id":r.id,"filename":r.filename,"mime":r.mime,"status":r.status,"created_at":r.created_at,"invoice":json.loads(r.invoice_json or "{}"),"validation":json.loads(r.validation_json or "{}"),"sync":json.loads(r.sync_json or "{}"),"extraction":json.loads(r.extraction_json or "{}")}

def audit(db, invoice_id, event_type, metadata):
    db.add(AuditEvent(invoice_id=invoice_id,event_type=event_type,metadata_json=json.dumps(metadata,default=str)))

@asynccontextmanager
async def lifespan(app):
    with Session() as recovery_db:
        interrupted=recovery_db.scalars(select(Record).where(Record.status=="PROCESSING")).all()
        for row in interrupted:
            message="Processing was interrupted when the application stopped. Re-upload the document to retry extraction."
            row.status="FAILED"; row.extraction_json=json.dumps({"status":"failed","error":message,"pipeline_version":"2.0.0","timestamp":datetime.now(timezone.utc).isoformat()})
            row.validation_json=json.dumps({"valid":False,"severity":"ERROR","errors":[message],"warnings":[],"issues":[{"severity":"ERROR","code":"interrupted_processing","message":message}]})
            audit(recovery_db,row.id,"interrupted_processing_recovered",{})
        if interrupted: recovery_db.commit()
    yield

app=FastAPI(title="InvoiceAI", version="0.2.0",lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[os.getenv("FRONTEND_ORIGIN","http://localhost:5173")], allow_methods=["*"], allow_headers=["*"])

def correction_metadata(previous: dict, corrected: Invoice, metadata: dict) -> tuple[dict,dict]:
    updated=corrected.model_dump(mode="json"); changes={}
    for field in Invoice.model_fields:
        if previous.get(field)!=updated.get(field):
            changes[field]={"previous":previous.get(field),"corrected":updated.get(field)}
            evidence=metadata.setdefault("evidence",{}).setdefault(field,{})
            evidence["manually_corrected"]=True; evidence["needs_review"]=False
    if changes: metadata.setdefault("human_corrections",[]).append({"timestamp":datetime.now(timezone.utc).isoformat(),"changes":changes})
    return metadata,changes

def assess_invoice(invoice: Invoice, others: list[Invoice], metadata: dict) -> dict:
    result=validate(invoice,others)
    evidence=metadata.get("evidence",{})
    missing=[f for f in IMPORTANT_FIELDS if getattr(invoice,f) is not None and evidence.get(f,{}).get("needs_review",True)]
    if missing:
        message="Source text evidence is unavailable for: "+", ".join(missing)
        result["issues"].append({"severity":"WARNING","code":"missing_source_evidence","message":message,"field":None})
        result["warnings"].append(message)
        if result["review_assessment"]=="READY_FOR_REVIEW": result["review_assessment"]="NEEDS_ATTENTION"
        if result["severity"]=="INFO": result["severity"]="WARNING"
    for field,ev in evidence.items():
        ev["validation_warnings"]=[x["message"] for x in result["issues"] if x.get("field")==field]
    return result

def can_approve(status: str, validation_result: dict) -> bool:
    return status=="VALIDATED" and bool(validation_result.get("valid"))

def process_invoice(invoice_id: str):
    with Session() as db:
        claimed=db.execute(update(Record).where(Record.id==invoice_id,Record.status=="UPLOADED").values(status="PROCESSING"))
        if claimed.rowcount!=1: return
        db.commit()
        r=db.get(Record,invoice_id)
        if not r: return
        started=time.monotonic()
        try:
            document=DocumentProcessor.process(Path(r.path),r.mime)
            extractor=InvoiceExtractor()
            result=extractor.extract(document)
            result["total_processing_duration_ms"]=round((time.monotonic()-started)*1000,2)
            result["model_config"]={"temperature":0,"seed":42,"timeout_seconds":extractor.timeout,"max_retries":extractor.retries}
            invoice=result["invoice"]
            result["invoice"]=invoice.model_dump(mode="json")
            r.invoice_json=invoice.model_dump_json(); r.status="NEEDS_REVIEW"
            others=[Invoice.model_validate_json(x.invoice_json) for x in db.scalars(select(Record).where(Record.id!=r.id)).all() if x.invoice_json!="{}"]
            validation_result=assess_invoice(invoice,others,result)
            r.extraction_json=json.dumps(result,default=str)
            r.validation_json=json.dumps(validation_result)
            audit(db,r.id,"extraction_succeeded",{k:result[k] for k in ("model","pipeline_version","duration_ms","total_processing_duration_ms","timestamp","attempts","document_kind","status")})
            audit(db,r.id,"validation",{"severity":validation_result["severity"],"review_assessment":validation_result["review_assessment"],"issues":validation_result["issues"]})
            log_record={"event":"invoice_extraction_succeeded","invoice_id":r.id,"model":result["model"],"duration_ms":result["total_processing_duration_ms"],"document_kind":result["document_kind"]}
            logging.getLogger("invoiceai").info(json.dumps(log_record))
        except Exception as e:
            r.status="FAILED"; r.validation_json=json.dumps({"valid":False,"severity":"ERROR","errors":[str(e)],"warnings":[],"issues":[{"severity":"ERROR","code":"extraction_failed","message":str(e)}]})
            r.extraction_json=json.dumps({"status":"failed","error":str(e),"model":os.getenv("OLLAMA_MODEL","qwen3-vl:8b"),"pipeline_version":"2.0.0","duration_ms":round((time.monotonic()-started)*1000,2),"timestamp":datetime.now(timezone.utc).isoformat()})
            audit(db,r.id,"extraction_failed",{"error":str(e),"model":os.getenv("OLLAMA_MODEL","qwen3-vl:8b"),"pipeline_version":"2.0.0","duration_ms":round((time.monotonic()-started)*1000,2)})
            logging.getLogger("invoiceai").error(json.dumps({"event":"invoice_extraction_failed","invoice_id":r.id,"error_type":type(e).__name__}))
        db.commit()

@app.get("/api/health")
def health(): return {"ok":True,"ollama_url":os.getenv("OLLAMA_URL","http://localhost:11434"),"model":os.getenv("OLLAMA_MODEL","qwen3-vl:8b")}

@app.get("/api/benchmarks/latest")
def latest_benchmark():
    path=ROOT/"tests"/"fixtures"/"results.json"
    return json.loads(path.read_text()) if path.exists() else {"available":False,"message":"No benchmark results yet. Run the documented benchmark command.","invoices":[]}

@app.get("/api/invoices/{invoice_id}/audit")
def invoice_audit(invoice_id: str):
    with Session() as db:
        if not db.get(Record,invoice_id): raise HTTPException(404,"Invoice not found")
        rows=db.scalars(select(AuditEvent).where(AuditEvent.invoice_id==invoice_id).order_by(AuditEvent.timestamp)).all()
        return [{"timestamp":x.timestamp,"event_type":x.event_type,"metadata":json.loads(x.metadata_json)} for x in rows]

@app.post("/api/invoices",status_code=202)
async def upload(background: BackgroundTasks,file: UploadFile=File(...)):
    ext=Path(file.filename or "").suffix.lower(); allowed={".pdf":"application/pdf",".jpg":"image/jpeg",".jpeg":"image/jpeg",".png":"image/png"}
    if ext not in allowed: raise HTTPException(415,"Only PDF, JPG, and PNG are supported")
    content=await file.read(MAX_BYTES+1)
    if len(content)>MAX_BYTES: raise HTTPException(413,"Maximum file size is 10 MB")
    if not content: raise HTTPException(400,"Empty upload")
    mime=allowed[ext]
    if (ext==".pdf" and not content.startswith(b"%PDF")) or (ext in (".jpg",".jpeg") and not content.startswith(b"\xff\xd8\xff")) or (ext==".png" and not content.startswith(b"\x89PNG\r\n\x1a\n")):
        raise HTTPException(415,"File contents do not match the extension")
    ident=str(uuid.uuid4()); clean_name=Path(file.filename or "invoice").name.replace("/","_").replace("\\","_")[:150]; path=DATA/(ident+ext); path.write_bytes(content)
    with Session() as db:
        r=Record(id=ident,filename=clean_name,path=str(path),mime=mime,status="UPLOADED"); db.add(r); db.commit()
    background.add_task(process_invoice,ident)
    return {"id":ident,"filename":clean_name,"status":"UPLOADED"}

@app.get("/api/invoices")
def list_invoices(search: str=""):
    with Session() as db:
        records=db.scalars(select(Record).order_by(Record.created_at.desc())).all()
        result=[record_dict(r) for r in records]
    if search: result=[r for r in result if search.casefold() in json.dumps(r.get("invoice",{}),ensure_ascii=False).casefold() or search.casefold() in r["filename"].casefold()]
    return result

@app.get("/api/invoices/{invoice_id}")
def get_invoice(invoice_id: str):
    with Session() as db:
        r=db.get(Record,invoice_id)
        if not r: raise HTTPException(404,"Invoice not found")
        return record_dict(r)

@app.get("/api/invoices/{invoice_id}/document")
def document(invoice_id: str):
    with Session() as db:
        r=db.get(Record,invoice_id)
        if not r: raise HTTPException(404,"Invoice not found")
        return FileResponse(r.path,media_type=r.mime,filename=r.filename,content_disposition_type="inline")

@app.patch("/api/invoices/{invoice_id}")
def correct(invoice_id: str, payload: dict):
    try: inv=Invoice.model_validate(payload)
    except Exception as e: raise HTTPException(422,str(e))
    with Session() as db:
        r=db.get(Record,invoice_id)
        if not r: raise HTTPException(404,"Invoice not found")
        if r.status in ("APPROVED",): raise HTTPException(409,"Approved invoices cannot be edited")
        metadata,changes=correction_metadata(json.loads(r.invoice_json or "{}"),inv,json.loads(r.extraction_json or "{}"))
        if changes: audit(db,invoice_id,"manual_correction",{"changes":changes})
        r.extraction_json=json.dumps(metadata,default=str)
        r.invoice_json=inv.model_dump_json(); r.validation_json="{}"; r.status="NEEDS_REVIEW"; db.commit(); return record_dict(r)

@app.post("/api/invoices/{invoice_id}/validate")
def validate_endpoint(invoice_id: str):
    with Session() as db:
        r=db.get(Record,invoice_id)
        if not r: raise HTTPException(404,"Invoice not found")
        inv=Invoice.model_validate_json(r.invoice_json)
        others=[Invoice.model_validate_json(x.invoice_json) for x in db.scalars(select(Record).where(Record.id!=invoice_id)).all() if x.invoice_json!="{}"]
        metadata=json.loads(r.extraction_json or "{}")
        result=assess_invoice(inv,others,metadata)
        r.extraction_json=json.dumps(metadata,default=str)
        r.validation_json=json.dumps(result); r.status="VALIDATED" if result["valid"] else "NEEDS_REVIEW"
        audit(db,invoice_id,"validation",{"severity":result["severity"],"review_assessment":result["review_assessment"],"issues":result["issues"]})
        db.commit(); return result

@app.post("/api/invoices/{invoice_id}/approve")
def approve(invoice_id: str):
    with Session() as db:
        r=db.get(Record,invoice_id)
        if not r: raise HTTPException(404,"Invoice not found")
        if not can_approve(r.status,json.loads(r.validation_json)): raise HTTPException(409,"Run validation and resolve errors before approval")
        r.status="APPROVED"; audit(db,invoice_id,"approved",{}); db.commit(); return record_dict(r)

@app.get("/api/invoices/{invoice_id}/export")
def export(invoice_id: str):
    with Session() as db:
        r=db.get(Record,invoice_id)
        if not r: raise HTTPException(404,"Invoice not found")
        if r.status!="APPROVED": raise HTTPException(409,"Only approved invoices can be exported")
        return JSONResponse(json.loads(r.invoice_json),headers={"Content-Disposition":f'attachment; filename="{invoice_id}.json"'})

class AccountingAdapter:
    def sync(self, invoice: Invoice, invoice_id: str, config: dict) -> dict: raise NotImplementedError
class MoneybirdAdapter(AccountingAdapter):
    def sync(self,invoice,invoice_id,config):
        # Mock only: no undocumented live API behavior is assumed.
        return {"success":True,"mode":"mock","external_document_id":"mock-"+invoice_id,"payload":{"contact":{"name":invoice.supplier_name,"id":config.get("supplier_ids",{}).get(invoice.supplier_name)},"invoice_number":invoice.invoice_number,"date":invoice.invoice_date,"due_date":invoice.due_date,"currency":invoice.currency,"total":str(invoice.invoice_total),"details":[{"description":x.description,"amount":str(x.line_total),"tax_rate":str(x.vat_rate),"ledger_account_id":config.get("ledger_accounts",{}).get(str(x.vat_rate))} for x in invoice.line_items]}}

@app.post("/api/invoices/{invoice_id}/sync/moneybird")
def sync_moneybird(invoice_id: str, config: dict={}):
    with Session() as db:
        r=db.get(Record,invoice_id)
        if not r: raise HTTPException(404,"Invoice not found")
        if r.status!="APPROVED": raise HTTPException(409,"Approve invoice before synchronization")
        previous=json.loads(r.sync_json or "{}")
        if previous.get("success"): return previous
        result=MoneybirdAdapter().sync(Invoice.model_validate_json(r.invoice_json),invoice_id,config); r.sync_json=json.dumps(result); db.commit(); return result
