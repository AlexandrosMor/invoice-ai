from __future__ import annotations
import json, logging, os, uuid
import time, re, secrets, hashlib, hmac
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Optional, Literal
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile, Depends, Request, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import create_engine, String, Text, select, inspect, text, update, ForeignKey, Index
from sqlalchemy import event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
import httpx
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError
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
if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine,"connect")
    def enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
        cursor=dbapi_connection.cursor();cursor.execute("PRAGMA foreign_keys=ON");cursor.close()
Session = sessionmaker(engine, expire_on_commit=False)

class Base(DeclarativeBase): pass
class User(Base):
    __tablename__="users"
    id: Mapped[str]=mapped_column(String,primary_key=True,default=lambda:str(uuid.uuid4()))
    first_name: Mapped[str]=mapped_column(String(100))
    last_name: Mapped[str]=mapped_column(String(100))
    company_name: Mapped[str]=mapped_column(String(200))
    email: Mapped[str]=mapped_column(String(320),unique=True,index=True)
    password_hash: Mapped[str]=mapped_column(String(512))
    created_at: Mapped[str]=mapped_column(String,default=lambda:datetime.now(timezone.utc).isoformat())
    updated_at: Mapped[str]=mapped_column(String,default=lambda:datetime.now(timezone.utc).isoformat())
    status: Mapped[str]=mapped_column(String(20),default="active")
class LoginSession(Base):
    __tablename__="sessions"
    id: Mapped[str]=mapped_column(String,primary_key=True,default=lambda:str(uuid.uuid4()))
    user_id: Mapped[str]=mapped_column(ForeignKey("users.id",ondelete="CASCADE"),index=True)
    token_hash: Mapped[str]=mapped_column(String(64),unique=True,index=True)
    created_at: Mapped[str]=mapped_column(String,default=lambda:datetime.now(timezone.utc).isoformat())
    expires_at: Mapped[str]=mapped_column(String,index=True)
    revoked_at: Mapped[Optional[str]]=mapped_column(String,nullable=True)
class ApiKey(Base):
    __tablename__="api_keys"
    id: Mapped[str]=mapped_column(String,primary_key=True,default=lambda:str(uuid.uuid4()))
    user_id: Mapped[str]=mapped_column(ForeignKey("users.id",ondelete="CASCADE"),index=True)
    name: Mapped[str]=mapped_column(String(100))
    key_prefix: Mapped[str]=mapped_column(String(16))
    key_hash: Mapped[str]=mapped_column(String(64),unique=True,index=True)
    scopes_json: Mapped[str]=mapped_column(Text)
    created_at: Mapped[str]=mapped_column(String,default=lambda:datetime.now(timezone.utc).isoformat())
    last_used_at: Mapped[Optional[str]]=mapped_column(String,nullable=True)
    expires_at: Mapped[Optional[str]]=mapped_column(String,nullable=True,index=True)
    revoked_at: Mapped[Optional[str]]=mapped_column(String,nullable=True)
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
    user_id: Mapped[Optional[str]]=mapped_column(ForeignKey("users.id",ondelete="SET NULL"),nullable=True,index=True)
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
if "user_id" not in {column["name"] for column in inspect(engine).get_columns("invoices")}:
    with engine.begin() as conn: conn.execute(text("ALTER TABLE invoices ADD COLUMN user_id VARCHAR REFERENCES users(id) ON DELETE SET NULL"))
with engine.begin() as conn: conn.execute(text("CREATE INDEX IF NOT EXISTS ix_invoices_user_id ON invoices(user_id)"))

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

app=FastAPI(title="InvoiceAI", version="0.4.0",lifespan=lifespan,openapi_tags=[{"name":"Developer API v1","description":"Versioned invoice API authenticated with scoped bearer API keys."}])
app.add_middleware(CORSMiddleware, allow_origins=[os.getenv("FRONTEND_ORIGIN","http://localhost:5173")], allow_credentials=True, allow_methods=["GET","POST","PATCH","DELETE","OPTIONS"], allow_headers=["Content-Type","Authorization","X-CSRF-Token"])

PASSWORDS=PasswordHasher()
SESSION_DAYS=14
SESSION_COOKIE="invoiceai_session"
CSRF_COOKIE="invoiceai_csrf"
AUTH_ATTEMPTS={}
API_RATE_EVENTS={}
EMAIL_PATTERN=re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$")
API_KEY_PATTERN=re.compile(r"^invai_[A-Za-z0-9_-]{43}$")
API_SCOPES={"invoices:read","invoices:write","invoices:document:read"}
API_BEARER=HTTPBearer(auto_error=False,scheme_name="InvoiceAI API Key",description="Use a scoped API key from Profile as `Bearer invai_…`.")

class RegisterPayload(BaseModel):
    first_name: str=Field(min_length=1,max_length=100)
    last_name: str=Field(min_length=1,max_length=100)
    company_name: str=Field(min_length=1,max_length=200)
    email: str=Field(min_length=3,max_length=320)
    password: str=Field(min_length=12,max_length=256)
class LoginPayload(BaseModel): email: str; password: str
class ProfilePayload(BaseModel): first_name: str=Field(min_length=1,max_length=100); last_name: str=Field(min_length=1,max_length=100); company_name: str=Field(min_length=1,max_length=200); email: str=Field(min_length=3,max_length=320)
class PasswordPayload(BaseModel): current_password: str; new_password: str=Field(min_length=12,max_length=256)
class ApiKeyCreatePayload(BaseModel):
    name: str=Field(min_length=1,max_length=100)
    scopes: list[Literal["invoices:read","invoices:write","invoices:document:read"]]=Field(default_factory=lambda:["invoices:read"],min_length=1)
    expires_in_days: Optional[int]=Field(default=90,ge=1,le=365)
    @field_validator("name")
    @classmethod
    def normalize_name(cls,value):
        value=value.strip()
        if not value: raise ValueError("Enter a name for this API key")
        return value
    @field_validator("scopes")
    @classmethod
    def unique_scopes(cls,value):
        if len(value)!=len(set(value)): raise ValueError("Do not select a permission more than once")
        return value
class DeveloperUploadResponse(BaseModel):
    id: str
    status: str
    message: str
    status_url: str
class DeveloperInvoiceSummary(BaseModel):
    id: str
    filename: str
    status: str
    created_at: str
    invoice_number: Optional[str]=None
    supplier_name: Optional[str]=None
    currency: Optional[str]=None
    total: Optional[str]=None
class DeveloperInvoicePage(BaseModel):
    items: list[DeveloperInvoiceSummary]
    next_cursor: Optional[str]=None
class DeveloperInvoiceDetail(BaseModel):
    id: str
    filename: str
    created_at: str
    status: str
    supplier_name: Optional[str]=None
    invoice_number: Optional[str]=None
    extraction: dict
    validation: dict
    approved: bool
    review_status: str
    links: dict[str,str]
class DeveloperInvoiceJson(BaseModel):
    id: str
    status: str
    approved: bool
    invoice: dict

def normalized_email(value: str) -> str:
    return value.strip().casefold()
def valid_email(value: str) -> bool:
    return len(value)<=320 and bool(EMAIL_PATTERN.fullmatch(value))
def password_policy(password: str) -> bool:
    return len(password)>=12 and any(c.isalpha() for c in password) and any(c.isdigit() for c in password)
def token_hash(token: str) -> str: return hashlib.sha256(token.encode("utf-8")).hexdigest()
def public_user(user: User) -> dict:
    return {"id":user.id,"first_name":user.first_name,"last_name":user.last_name,"company_name":user.company_name,"email":user.email,"created_at":user.created_at,"updated_at":user.updated_at,"status":user.status}
def cookie_secure(request: Request) -> bool:
    return request.url.scheme=="https" or os.getenv("COOKIE_SECURE","false").lower()=="true"
def issue_session(response, request: Request, db, user: User):
    raw=secrets.token_urlsafe(32); csrf=secrets.token_urlsafe(32)
    db.add(LoginSession(user_id=user.id,token_hash=token_hash(raw),expires_at=(datetime.now(timezone.utc)+timedelta(days=SESSION_DAYS)).isoformat()))
    db.commit()
    response.set_cookie(SESSION_COOKIE,raw,httponly=True,secure=cookie_secure(request),samesite="lax",max_age=SESSION_DAYS*86400,path="/")
    response.set_cookie(CSRF_COOKIE,csrf,httponly=False,secure=cookie_secure(request),samesite="lax",max_age=SESSION_DAYS*86400,path="/")
    return csrf
def rate_limit(request: Request, bucket: str):
    now=time.monotonic(); key=(bucket,request.client.host if request.client else "unknown")
    attempts=[t for t in AUTH_ATTEMPTS.get(key,[]) if now-t<600]
    if len(attempts)>=10: raise HTTPException(429,"Too many attempts. Wait a few minutes and try again.")
    attempts.append(now); AUTH_ATTEMPTS[key]=attempts
    if len(AUTH_ATTEMPTS)>2000:
        for old in [k for k,v in AUTH_ATTEMPTS.items() if not v or now-v[-1]>=600]: AUTH_ATTEMPTS.pop(old,None)
def csrf_protect(request: Request, x_csrf_token: Optional[str]=Header(default=None,alias="X-CSRF-Token")):
    cookie=request.cookies.get(CSRF_COOKIE)
    if not cookie or not x_csrf_token or not hmac.compare_digest(cookie,x_csrf_token): raise HTTPException(403,"CSRF token is missing or invalid. Reload the page and try again.")
def current_user(request: Request) -> dict:
    raw=request.cookies.get(SESSION_COOKIE)
    if not raw: raise HTTPException(401,"Sign in to continue")
    with Session() as db:
        session=db.scalar(select(LoginSession).where(LoginSession.token_hash==token_hash(raw),LoginSession.revoked_at.is_(None)))
        if not session: raise HTTPException(401,"Sign in to continue")
        try: expired=datetime.fromisoformat(session.expires_at)<=datetime.now(timezone.utc)
        except ValueError: expired=True
        if expired: raise HTTPException(401,"Your session expired. Sign in again.")
        user=db.get(User,session.user_id)
        if not user or user.status!="active": raise HTTPException(401,"Sign in to continue")
        return public_user(user)

def api_key_metadata(key: ApiKey) -> dict:
    return {"id":key.id,"name":key.name,"prefix":key.key_prefix,"scopes":json.loads(key.scopes_json),"created_at":key.created_at,"last_used_at":key.last_used_at,"expires_at":key.expires_at,"revoked_at":key.revoked_at}

def check_api_rate_limit(key_id: str):
    try: limit=max(1,min(int(os.getenv("API_RATE_LIMIT_PER_MINUTE","120")),10000))
    except ValueError: limit=120
    now=time.monotonic()
    events=[stamp for stamp in API_RATE_EVENTS.get(key_id,[]) if now-stamp<60]
    if len(events)>=limit:
        API_RATE_EVENTS[key_id]=events
        raise HTTPException(429,"API rate limit exceeded. Wait before retrying.",headers={"Retry-After":"60"})
    events.append(now);API_RATE_EVENTS[key_id]=events
    if len(API_RATE_EVENTS)>5000:
        for expired in [key for key,stamps in API_RATE_EVENTS.items() if not stamps or now-stamps[-1]>=60]: API_RATE_EVENTS.pop(expired,None)

def get_api_key_context(credentials: Optional[HTTPAuthorizationCredentials]=Depends(API_BEARER)) -> dict:
    if not credentials or credentials.scheme.casefold()!="bearer": raise HTTPException(401,"A valid Bearer API key is required",headers={"WWW-Authenticate":"Bearer"})
    raw=credentials.credentials
    if not API_KEY_PATTERN.fullmatch(raw): raise HTTPException(401,"A valid Bearer API key is required",headers={"WWW-Authenticate":"Bearer"})
    digest=token_hash(raw)
    with Session() as db:
        key=db.scalar(select(ApiKey).where(ApiKey.key_hash==digest))
        if not key or not hmac.compare_digest(key.key_hash,digest) or key.revoked_at:
            raise HTTPException(401,"A valid Bearer API key is required",headers={"WWW-Authenticate":"Bearer"})
        if key.expires_at:
            try: expired=datetime.fromisoformat(key.expires_at)<=datetime.now(timezone.utc)
            except ValueError: expired=True
            if expired: raise HTTPException(401,"This API key has expired",headers={"WWW-Authenticate":"Bearer"})
        user=db.get(User,key.user_id)
        if not user or user.status!="active": raise HTTPException(401,"A valid Bearer API key is required",headers={"WWW-Authenticate":"Bearer"})
        scopes=json.loads(key.scopes_json)
        check_api_rate_limit(key.id)
        now=datetime.now(timezone.utc)
        try: last_used=datetime.fromisoformat(key.last_used_at) if key.last_used_at else None
        except ValueError: last_used=None
        if last_used is None or (now-last_used).total_seconds()>=60:
            key.last_used_at=now.isoformat();db.commit()
        return {"user":public_user(user),"key_id":key.id,"scopes":scopes}

def require_api_scope(scope: str):
    def dependency(context: dict=Depends(get_api_key_context)):
        if scope not in context["scopes"]: raise HTTPException(403,"This API key does not have the required scope: "+scope)
        return context
    return dependency

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

def process_invoice(invoice_id: str,user_id: Optional[str]=None):
    with Session() as db:
        claimed=db.execute(update(Record).where(Record.id==invoice_id,Record.user_id==user_id,Record.status=="UPLOADED").values(status="PROCESSING"))
        if claimed.rowcount!=1: return
        db.commit()
        r=db.get(Record,invoice_id)
        if not r: return
        started=time.monotonic()
        try:
            preprocess_started=time.monotonic()
            document=DocumentProcessor.process(Path(r.path),r.mime)
            preprocessing_duration=round((time.monotonic()-preprocess_started)*1000,2)
            extractor=InvoiceExtractor()
            result=extractor.extract(document)
            result["preprocessing_duration_ms"]=preprocessing_duration
            result["total_processing_duration_ms"]=round((time.monotonic()-started)*1000,2)
            result["model_config"]={"temperature":0,"seed":42,"timeout_seconds":extractor.timeout,"max_retries":extractor.retries}
            invoice=result["invoice"]
            result["invoice"]=invoice.model_dump(mode="json")
            r.invoice_json=invoice.model_dump_json(); r.status="NEEDS_REVIEW"
            others=[Invoice.model_validate_json(x.invoice_json) for x in db.scalars(select(Record).where(Record.id!=r.id,Record.user_id==r.user_id)).all() if x.invoice_json!="{}"]
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

@app.get("/api/auth/csrf")
def auth_csrf(request: Request):
    value=secrets.token_urlsafe(32)
    response=JSONResponse({"csrf_token":value})
    response.set_cookie(CSRF_COOKIE,value,httponly=False,secure=cookie_secure(request),samesite="lax",max_age=SESSION_DAYS*86400,path="/")
    return response

@app.post("/api/auth/register",dependencies=[Depends(csrf_protect)])
def register(payload: RegisterPayload,request: Request):
    rate_limit(request,"register")
    email=normalized_email(payload.email)
    if not valid_email(email): raise HTTPException(422,"Enter a valid email address")
    if not password_policy(payload.password): raise HTTPException(422,"Password must be at least 12 characters and include letters and numbers")
    with Session() as db:
        if db.scalar(select(User.id).where(User.email==email)): raise HTTPException(409,"An account with this email already exists")
        user=User(first_name=payload.first_name.strip(),last_name=payload.last_name.strip(),company_name=payload.company_name.strip(),email=email,password_hash=PASSWORDS.hash(payload.password))
        db.add(user)
        try: db.commit()
        except Exception:
            db.rollback(); raise HTTPException(409,"An account with this email already exists")
        db.refresh(user)
        response=JSONResponse({})
        csrf=issue_session(response,request,db,user)
        response.body=json.dumps({"user":public_user(user),"csrf_token":csrf}).encode();response.headers["content-length"]=str(len(response.body))
        return response

@app.post("/api/auth/login",dependencies=[Depends(csrf_protect)])
def login(payload: LoginPayload,request: Request):
    rate_limit(request,"login")
    email=normalized_email(payload.email)
    with Session() as db:
        user=db.scalar(select(User).where(User.email==email)) if valid_email(email) else None
        try: verified=bool(user and PASSWORDS.verify(user.password_hash,payload.password))
        except (VerifyMismatchError,VerificationError): verified=False
        if not verified or not user or user.status!="active": raise HTTPException(401,"Email or password is incorrect")
        response=JSONResponse({})
        csrf=issue_session(response,request,db,user)
        response.body=json.dumps({"user":public_user(user),"csrf_token":csrf}).encode();response.headers["content-length"]=str(len(response.body))
        return response

@app.post("/api/auth/logout",dependencies=[Depends(csrf_protect)])
def logout(request: Request,user=Depends(current_user)):
    raw=request.cookies.get(SESSION_COOKIE)
    with Session() as db:
        session=db.scalar(select(LoginSession).where(LoginSession.token_hash==token_hash(raw or ""),LoginSession.user_id==user["id"],LoginSession.revoked_at.is_(None)))
        if session: session.revoked_at=datetime.now(timezone.utc).isoformat(); db.commit()
    response=JSONResponse({"ok":True}); response.delete_cookie(SESSION_COOKIE,path="/"); response.delete_cookie(CSRF_COOKIE,path="/")
    return response

@app.get("/api/auth/me")
def auth_me(user=Depends(current_user)): return {"user":user}

@app.get("/api/profile")
def get_profile(user=Depends(current_user)): return user

@app.patch("/api/profile",dependencies=[Depends(csrf_protect)])
def update_profile(payload: ProfilePayload,user=Depends(current_user)):
    email=normalized_email(payload.email)
    if not valid_email(email): raise HTTPException(422,"Enter a valid email address")
    with Session() as db:
        record=db.get(User,user["id"])
        duplicate=db.scalar(select(User.id).where(User.email==email,User.id!=record.id))
        if duplicate: raise HTTPException(409,"An account with this email already exists")
        record.first_name=payload.first_name.strip();record.last_name=payload.last_name.strip();record.company_name=payload.company_name.strip();record.email=email;record.updated_at=datetime.now(timezone.utc).isoformat()
        db.commit();db.refresh(record);return public_user(record)

@app.post("/api/profile/change-password",dependencies=[Depends(csrf_protect)])
def change_password(payload: PasswordPayload,request: Request,user=Depends(current_user)):
    if not password_policy(payload.new_password): raise HTTPException(422,"Password must be at least 12 characters and include letters and numbers")
    with Session() as db:
        record=db.get(User,user["id"])
        try: verified=PASSWORDS.verify(record.password_hash,payload.current_password)
        except (VerifyMismatchError,VerificationError): verified=False
        if not verified: raise HTTPException(400,"Current password is incorrect")
        record.password_hash=PASSWORDS.hash(payload.new_password);record.updated_at=datetime.now(timezone.utc).isoformat()
        now=datetime.now(timezone.utc).isoformat()
        current_hash=token_hash(request.cookies.get(SESSION_COOKIE,""))
        db.execute(update(LoginSession).where(LoginSession.user_id==record.id,LoginSession.token_hash!=current_hash,LoginSession.revoked_at.is_(None)).values(revoked_at=now))
        db.commit()
    return {"ok":True,"message":"Password changed. Sign in again on your devices."}

@app.get("/api/profile/api-keys")
def list_api_keys(user=Depends(current_user)):
    with Session() as db:
        keys=db.scalars(select(ApiKey).where(ApiKey.user_id==user["id"]).order_by(ApiKey.created_at.desc(),ApiKey.id.desc())).all()
        return [api_key_metadata(key) for key in keys]

@app.post("/api/profile/api-keys",status_code=201,dependencies=[Depends(csrf_protect)])
def create_api_key(payload: ApiKeyCreatePayload,request: Request,user=Depends(current_user)):
    rate_limit(request,"api_key_create")
    raw="invai_"+secrets.token_urlsafe(32)
    now=datetime.now(timezone.utc)
    expires_at=(now+timedelta(days=payload.expires_in_days)).isoformat() if payload.expires_in_days is not None else None
    key=ApiKey(user_id=user["id"],name=payload.name,key_prefix=raw[:14],key_hash=token_hash(raw),scopes_json=json.dumps(payload.scopes),created_at=now.isoformat(),expires_at=expires_at)
    with Session() as db:
        db.add(key);db.commit();db.refresh(key)
        result=api_key_metadata(key)
    result["api_key"]=raw
    result["warning"]="Copy this API key now. It will not be shown again."
    return result

@app.delete("/api/profile/api-keys/{key_id}",dependencies=[Depends(csrf_protect)])
def revoke_api_key(key_id: str,user=Depends(current_user)):
    with Session() as db:
        key=db.scalar(select(ApiKey).where(ApiKey.id==key_id,ApiKey.user_id==user["id"]))
        if not key: raise HTTPException(404,"API key not found")
        if not key.revoked_at: key.revoked_at=datetime.now(timezone.utc).isoformat();db.commit()
        return {"ok":True,"key":api_key_metadata(key)}

@app.get("/api/health")
def health():
    ollama_url=os.getenv("OLLAMA_URL","http://localhost:11434").rstrip("/")
    model=os.getenv("OLLAMA_MODEL","qwen3-vl:8b")
    ollama_available=False
    model_available=False
    try:
        response=httpx.get(ollama_url+"/api/tags",timeout=httpx.Timeout(2.0,connect=1.0))
        response.raise_for_status()
        ollama_available=True
        models=response.json().get("models",[])
        model_available=any(item.get("name")==model for item in models if isinstance(item,dict))
    except (httpx.HTTPError,ValueError):
        pass
    return {"ok":True,"ollama_url":ollama_url,"ollama_available":ollama_available,"model":model,"model_available":model_available}

@app.get("/api/benchmarks/latest")
def latest_benchmark():
    path=ROOT/"tests"/"fixtures"/"results.json"
    return json.loads(path.read_text()) if path.exists() else {"available":False,"message":"No benchmark results yet. Run the documented benchmark command.","invoices":[]}

@app.get("/api/invoices/{invoice_id}/audit")
def invoice_audit(invoice_id: str,user=Depends(current_user)):
    with Session() as db:
        if not db.scalar(select(Record.id).where(Record.id==invoice_id,Record.user_id==user["id"])): raise HTTPException(404,"Invoice not found")
        rows=db.scalars(select(AuditEvent).where(AuditEvent.invoice_id==invoice_id).order_by(AuditEvent.timestamp)).all()
        return [{"timestamp":x.timestamp,"event_type":x.event_type,"metadata":json.loads(x.metadata_json)} for x in rows]

async def store_invoice_upload(file: UploadFile,user_id: str) -> Record:
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
        r=Record(id=ident,filename=clean_name,path=str(path),mime=mime,status="UPLOADED",user_id=user_id); db.add(r); db.commit();db.refresh(r)
        return r

def enqueue_invoice_processing(background: BackgroundTasks,record: Record):
    background.add_task(process_invoice,record.id,record.user_id)

@app.post("/api/invoices",status_code=202)
async def upload(background: BackgroundTasks,file: UploadFile=File(...),user=Depends(current_user),_csrf=Depends(csrf_protect)):
    record=await store_invoice_upload(file,user["id"])
    enqueue_invoice_processing(background,record)
    return {"id":record.id,"filename":record.filename,"status":record.status}

@app.get("/api/invoices")
def list_invoices(search: str="",user=Depends(current_user)):
    with Session() as db:
        records=db.scalars(select(Record).where(Record.user_id==user["id"]).order_by(Record.created_at.desc())).all()
        result=[record_dict(r) for r in records]
    if search: result=[r for r in result if search.casefold() in json.dumps(r.get("invoice",{}),ensure_ascii=False).casefold() or search.casefold() in r["filename"].casefold()]
    return result

@app.get("/api/invoices/{invoice_id}")
def get_invoice(invoice_id: str,user=Depends(current_user)):
    with Session() as db:
        r=db.scalar(select(Record).where(Record.id==invoice_id,Record.user_id==user["id"]))
        if not r: raise HTTPException(404,"Invoice not found")
        return record_dict(r)

@app.get("/api/invoices/{invoice_id}/document")
def document(invoice_id: str,user=Depends(current_user)):
    with Session() as db:
        r=db.scalar(select(Record).where(Record.id==invoice_id,Record.user_id==user["id"]))
        if not r: raise HTTPException(404,"Invoice not found")
        return FileResponse(r.path,media_type=r.mime,filename=r.filename,content_disposition_type="inline")

def api_invoice_summary(record: Record) -> DeveloperInvoiceSummary:
    try: invoice=json.loads(record.invoice_json or "{}")
    except ValueError: invoice={}
    total=invoice.get("invoice_total")
    return DeveloperInvoiceSummary(id=record.id,filename=record.filename,status=record.status,created_at=record.created_at,invoice_number=invoice.get("invoice_number"),supplier_name=invoice.get("supplier_name"),currency=invoice.get("currency"),total=str(total) if total is not None else None)

def encode_invoice_cursor(record: Record) -> str:
    raw=json.dumps([record.created_at,record.id],separators=(",",":" )).encode("utf-8")
    return __import__("base64").urlsafe_b64encode(raw).decode("ascii").rstrip("=")

def decode_invoice_cursor(cursor: str) -> tuple[str,str]:
    import base64, binascii
    try:
        raw=base64.urlsafe_b64decode(cursor+"="*((4-len(cursor)%4)%4))
        value=json.loads(raw)
        if not isinstance(value,list) or len(value)!=2 or not all(isinstance(part,str) and part for part in value): raise ValueError()
        return value[0],value[1]
    except (ValueError,TypeError,binascii.Error):
        raise HTTPException(422,"Invalid cursor")

def api_document_path(record: Record) -> Path:
    path=Path(record.path).resolve()
    if not path.is_relative_to(DATA.resolve()) or not path.is_file(): raise HTTPException(404,"Invoice document not found")
    return path

@app.post("/api/v1/invoices",status_code=202,response_model=DeveloperUploadResponse,tags=["Developer API v1"],summary="Upload an invoice",description="Required scope: `invoices:write`. Accepts one PDF, JPG, or PNG and queues the existing extraction pipeline.",responses={401:{"description":"Missing or invalid API key"},403:{"description":"API key lacks invoices:write"},413:{"description":"Upload exceeds 10 MB"},415:{"description":"Unsupported type or content mismatch"},429:{"description":"Per-key rate limit exceeded"}})
async def developer_upload_invoice(background: BackgroundTasks,file: UploadFile=File(...),auth: dict=Depends(require_api_scope("invoices:write"))):
    record=await store_invoice_upload(file,auth["user"]["id"])
    enqueue_invoice_processing(background,record)
    return DeveloperUploadResponse(id=record.id,status=record.status,message="Invoice accepted for processing",status_url=f"/api/v1/invoices/{record.id}")

@app.get("/api/v1/invoices",response_model=DeveloperInvoicePage,tags=["Developer API v1"],summary="List invoices",description="Required scope: `invoices:read`. Results are scoped to the API key owner and ordered by creation time and ID descending.",responses={401:{"description":"Missing or invalid API key"},403:{"description":"API key lacks invoices:read"},422:{"description":"Invalid cursor or query parameter"},429:{"description":"Per-key rate limit exceeded"}})
def developer_list_invoices(limit: int=Query(default=20,ge=1,le=100),cursor: Optional[str]=None,status: Optional[Status]=None,search: Optional[str]=Query(default=None,max_length=200),auth: dict=Depends(require_api_scope("invoices:read"))):
    filters=[Record.user_id==auth["user"]["id"]]
    if status is not None: filters.append(Record.status==status.value)
    if search:
        pattern="%"+search.strip()+"%"
        filters.append((Record.filename.ilike(pattern))|(Record.invoice_json.ilike(pattern)))
    if cursor:
        created_at,record_id=decode_invoice_cursor(cursor)
        filters.append((Record.created_at<created_at)|((Record.created_at==created_at)&(Record.id<record_id)))
    with Session() as db:
        records=db.scalars(select(Record).where(*filters).order_by(Record.created_at.desc(),Record.id.desc()).limit(limit+1)).all()
    has_more=len(records)>limit
    page=records[:limit]
    return DeveloperInvoicePage(items=[api_invoice_summary(record) for record in page],next_cursor=encode_invoice_cursor(page[-1]) if has_more and page else None)

@app.get("/api/v1/invoices/{invoice_id}",response_model=DeveloperInvoiceDetail,tags=["Developer API v1"],summary="Get invoice details",description="Required scope: `invoices:read`. Sensitive paths and raw model output are not included.",responses={401:{"description":"Missing or invalid API key"},403:{"description":"API key lacks invoices:read"},404:{"description":"Invoice not found"},429:{"description":"Per-key rate limit exceeded"}})
def developer_get_invoice(invoice_id: str,auth: dict=Depends(require_api_scope("invoices:read"))):
    with Session() as db:
        record=db.scalar(select(Record).where(Record.id==invoice_id,Record.user_id==auth["user"]["id"]))
        if not record: raise HTTPException(404,"Invoice not found")
        try: invoice=json.loads(record.invoice_json or "{}")
        except ValueError: invoice={}
        try: extraction=json.loads(record.extraction_json or "{}")
        except ValueError: extraction={}
        try: validation=json.loads(record.validation_json or "{}")
        except ValueError: validation={}
        links={"json":f"/api/v1/invoices/{record.id}/json"}
        if "invoices:document:read" in auth["scopes"]: links["document"]=f"/api/v1/invoices/{record.id}/document"
        failed=record.status=="FAILED"
        return DeveloperInvoiceDetail(id=record.id,filename=record.filename,created_at=record.created_at,status=record.status,supplier_name=invoice.get("supplier_name"),invoice_number=invoice.get("invoice_number"),extraction={"status":extraction.get("status") or ("failed" if failed else record.status.casefold()),"model":extraction.get("model"),"duration_ms":extraction.get("duration_ms")},validation={"valid":validation.get("valid") if not failed else False,"errors":["Invoice extraction failed. Check backend logs or upload the document again."] if failed else validation.get("errors",[]),"warnings":[] if failed else validation.get("warnings",[])},approved=record.status=="APPROVED",review_status=record.status,links=links)

@app.get("/api/v1/invoices/{invoice_id}/json",response_model=DeveloperInvoiceJson,tags=["Developer API v1"],summary="Get canonical invoice JSON",description="Required scope: `invoices:read`. Returns the latest saved invoice data as unapproved unless the invoice has been explicitly approved.",responses={401:{"description":"Missing or invalid API key"},403:{"description":"API key lacks invoices:read"},404:{"description":"Invoice not found"},409:{"description":"Extraction is still running or failed"},429:{"description":"Per-key rate limit exceeded"}})
def developer_invoice_json(invoice_id: str,auth: dict=Depends(require_api_scope("invoices:read"))):
    with Session() as db:
        record=db.scalar(select(Record).where(Record.id==invoice_id,Record.user_id==auth["user"]["id"]))
        if not record: raise HTTPException(404,"Invoice not found")
        if record.status in ("UPLOADED","PROCESSING"):
            raise HTTPException(409,{"code":"processing","status":record.status,"message":"Invoice extraction is still in progress. Poll the status URL before requesting JSON."})
        if record.status=="FAILED":
            raise HTTPException(409,{"code":"extraction_failed","status":record.status,"message":"Invoice extraction failed. Check backend logs or upload the document again."})
        try: invoice=Invoice.model_validate_json(record.invoice_json).model_dump(mode="json")
        except Exception:
            raise HTTPException(409,{"code":"invoice_data_unavailable","status":record.status,"message":"Canonical invoice data is not available yet."})
        return DeveloperInvoiceJson(id=record.id,status=record.status,approved=record.status=="APPROVED",invoice=invoice)

@app.get("/api/v1/invoices/{invoice_id}/document",tags=["Developer API v1"],summary="Download original invoice",description="Required scope: `invoices:document:read`. Returns the original uploaded file as an attachment.",responses={200:{"description":"Original PDF or image","content":{"application/pdf":{},"image/jpeg":{},"image/png":{}}},401:{"description":"Missing or invalid API key"},403:{"description":"API key lacks invoices:document:read"},404:{"description":"Invoice or document not found"},429:{"description":"Per-key rate limit exceeded"}})
def developer_invoice_document(invoice_id: str,auth: dict=Depends(require_api_scope("invoices:document:read"))):
    with Session() as db:
        record=db.scalar(select(Record).where(Record.id==invoice_id,Record.user_id==auth["user"]["id"]))
        if not record: raise HTTPException(404,"Invoice not found")
        path=api_document_path(record)
        mime=record.mime if record.mime in {"application/pdf","image/jpeg","image/png"} else "application/octet-stream"
        return FileResponse(path,media_type=mime,filename=Path(record.filename).name,content_disposition_type="attachment")

@app.patch("/api/invoices/{invoice_id}",dependencies=[Depends(csrf_protect)])
def correct(invoice_id: str, payload: dict,user=Depends(current_user)):
    try: inv=Invoice.model_validate(payload)
    except Exception as e: raise HTTPException(422,str(e))
    with Session() as db:
        r=db.scalar(select(Record).where(Record.id==invoice_id,Record.user_id==user["id"]))
        if not r: raise HTTPException(404,"Invoice not found")
        if r.status in ("APPROVED",): raise HTTPException(409,"Approved invoices cannot be edited")
        metadata,changes=correction_metadata(json.loads(r.invoice_json or "{}"),inv,json.loads(r.extraction_json or "{}"))
        if changes: audit(db,invoice_id,"manual_correction",{"changes":changes})
        r.extraction_json=json.dumps(metadata,default=str)
        r.invoice_json=inv.model_dump_json(); r.validation_json="{}"; r.status="NEEDS_REVIEW"; db.commit(); return record_dict(r)

@app.post("/api/invoices/{invoice_id}/validate",dependencies=[Depends(csrf_protect)])
def validate_endpoint(invoice_id: str,user=Depends(current_user)):
    with Session() as db:
        r=db.scalar(select(Record).where(Record.id==invoice_id,Record.user_id==user["id"]))
        if not r: raise HTTPException(404,"Invoice not found")
        inv=Invoice.model_validate_json(r.invoice_json)
        others=[Invoice.model_validate_json(x.invoice_json) for x in db.scalars(select(Record).where(Record.id!=invoice_id,Record.user_id==user["id"])).all() if x.invoice_json!="{}"]
        metadata=json.loads(r.extraction_json or "{}")
        result=assess_invoice(inv,others,metadata)
        r.extraction_json=json.dumps(metadata,default=str)
        r.validation_json=json.dumps(result); r.status="VALIDATED" if result["valid"] else "NEEDS_REVIEW"
        audit(db,invoice_id,"validation",{"severity":result["severity"],"review_assessment":result["review_assessment"],"issues":result["issues"]})
        db.commit(); return result

@app.post("/api/invoices/{invoice_id}/approve",dependencies=[Depends(csrf_protect)])
def approve(invoice_id: str,user=Depends(current_user)):
    with Session() as db:
        r=db.scalar(select(Record).where(Record.id==invoice_id,Record.user_id==user["id"]))
        if not r: raise HTTPException(404,"Invoice not found")
        if not can_approve(r.status,json.loads(r.validation_json)): raise HTTPException(409,"Run validation and resolve errors before approval")
        r.status="APPROVED"; audit(db,invoice_id,"approved",{}); db.commit(); return record_dict(r)

@app.get("/api/invoices/{invoice_id}/export")
def export(invoice_id: str,user=Depends(current_user)):
    with Session() as db:
        r=db.scalar(select(Record).where(Record.id==invoice_id,Record.user_id==user["id"]))
        if not r: raise HTTPException(404,"Invoice not found")
        if r.status!="APPROVED": raise HTTPException(409,"Only approved invoices can be exported")
        return JSONResponse(json.loads(r.invoice_json),headers={"Content-Disposition":f'attachment; filename="{invoice_id}.json"'})

class AccountingAdapter:
    def sync(self, invoice: Invoice, invoice_id: str, config: dict) -> dict: raise NotImplementedError
class MoneybirdAdapter(AccountingAdapter):
    def sync(self,invoice,invoice_id,config):
        # Mock only: no undocumented live API behavior is assumed.
        return {"success":True,"mode":"mock","external_document_id":"mock-"+invoice_id,"payload":{"contact":{"name":invoice.supplier_name,"id":config.get("supplier_ids",{}).get(invoice.supplier_name)},"invoice_number":invoice.invoice_number,"date":invoice.invoice_date,"due_date":invoice.due_date,"currency":invoice.currency,"total":str(invoice.invoice_total),"details":[{"description":x.description,"amount":str(x.line_total),"tax_rate":str(x.vat_rate),"ledger_account_id":config.get("ledger_accounts",{}).get(str(x.vat_rate))} for x in invoice.line_items]}}

@app.post("/api/invoices/{invoice_id}/sync/moneybird",dependencies=[Depends(csrf_protect)])
def sync_moneybird(invoice_id: str, config: dict={},user=Depends(current_user)):
    with Session() as db:
        r=db.scalar(select(Record).where(Record.id==invoice_id,Record.user_id==user["id"]))
        if not r: raise HTTPException(404,"Invoice not found")
        if r.status!="APPROVED": raise HTTPException(409,"Approve invoice before synchronization")
        previous=json.loads(r.sync_json or "{}")
        if previous.get("success"): return previous
        result=MoneybirdAdapter().sync(Invoice.model_validate_json(r.invoice_json),invoice_id,config); r.sync_json=json.dumps(result); db.commit(); return result
