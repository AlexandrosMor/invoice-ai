import os
os.environ["DATABASE_URL"]="sqlite://"

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import datetime,timezone,timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine,select
from sqlalchemy.orm import sessionmaker

import app as app_module
from app import ApiKey,Base,Record,User
from invoice_domain import Invoice


@pytest.fixture
def api_env(tmp_path,monkeypatch):
    engine=create_engine("sqlite:///"+str(tmp_path/"developer-api-test.db"),connect_args={"check_same_thread":False})
    Base.metadata.create_all(engine)
    factory=sessionmaker(engine,expire_on_commit=False)
    monkeypatch.setattr(app_module,"Session",factory)
    monkeypatch.setattr(app_module,"DATA",tmp_path)
    jobs=[]
    monkeypatch.setattr(app_module,"process_invoice",lambda *args:jobs.append(args))
    app_module.AUTH_ATTEMPTS.clear();app_module.API_RATE_EVENTS.clear()
    with TestClient(app_module.app) as client:
        yield client,factory,tmp_path,jobs
    engine.dispose()


def register(client,email="api-owner@example.test"):
    csrf=client.get("/api/auth/csrf").json()["csrf_token"]
    response=client.post("/api/auth/register",headers={"X-CSRF-Token":csrf},json={"first_name":"API","last_name":"Owner","company_name":"Example","email":email,"password":"InvoiceSecure123"})
    assert response.status_code==200,response.text
    return response.json()


def create_key(client,csrf_token,*,name="Integration",scopes=None,expires_in_days=90):
    response=client.post("/api/profile/api-keys",headers={"X-CSRF-Token":csrf_token},json={"name":name,"scopes":scopes or ["invoices:read"],"expires_in_days":expires_in_days})
    assert response.status_code==201,response.text
    return response.json()


def add_invoice(factory,tmp_path,user_id,invoice_id="invoice-1",status="NEEDS_REVIEW",invoice=None):
    path=tmp_path/(invoice_id+".pdf");path.write_bytes(b"%PDF-1.4 synthetic invoice")
    canonical=invoice or Invoice(supplier_name="Example BV",invoice_number="INV-1",invoice_date="2026-11-08",currency="EUR",subtotal="100.00",total_vat="21.00",invoice_total="121.00")
    with factory() as db:
        record=Record(id=invoice_id,filename=path.name,path=str(path),mime="application/pdf",status=status,invoice_json=canonical.model_dump_json() if isinstance(canonical,Invoice) else json.dumps(canonical),validation_json=json.dumps({"valid":True,"errors":[],"warnings":[]}),extraction_json=json.dumps({"status":"succeeded","model":"qwen3-vl:8b","duration_ms":1200}),user_id=user_id,created_at="2026-11-08T10:00:00+00:00")
        db.add(record);db.commit()
    return path


def user_id_for(factory,email):
    with factory() as db:
        return db.scalar(select(User).where(User.email==email)).id


def test_api_key_creation_hashing_listing_and_browser_session_independence(api_env):
    client,factory,_,_=api_env
    registered=register(client)
    created=create_key(client,registered["csrf_token"],scopes=["invoices:read","invoices:write"])
    secret=created["api_key"]
    assert secret.startswith("invai_") and len(secret.removeprefix("invai_"))>=43
    with factory() as db:
        row=db.get(ApiKey,created["id"])
        assert row.key_hash==app_module.token_hash(secret)
        assert secret not in row.key_hash and secret not in row.scopes_json
    listed=client.get("/api/profile/api-keys")
    assert listed.status_code==200 and len(listed.json())==1
    assert "api_key" not in listed.text and "key_hash" not in listed.text
    assert listed.json()[0]["scopes"]==["invoices:read","invoices:write"]
    assert client.get("/api/v1/invoices",headers={"Authorization":"Bearer "+secret}).status_code==200
    client.post("/api/auth/logout",headers={"X-CSRF-Token":registered["csrf_token"]})
    assert client.get("/api/v1/invoices",headers={"Authorization":"Bearer "+secret}).status_code==200


def test_api_auth_rejects_missing_invalid_expired_and_revoked_keys(api_env):
    client,factory,_,_=api_env
    registered=register(client)
    created=create_key(client,registered["csrf_token"])
    secret=created["api_key"]
    assert client.get("/api/v1/invoices").status_code==401
    assert client.get("/api/v1/invoices",headers={"Authorization":"Bearer invai_invalid"}).status_code==401
    with factory() as db:
        row=db.get(ApiKey,created["id"]);row.expires_at=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat();db.commit()
    assert client.get("/api/v1/invoices",headers={"Authorization":"Bearer "+secret}).status_code==401
    with factory() as db:
        row=db.get(ApiKey,created["id"]);row.expires_at=(datetime.now(timezone.utc)+timedelta(days=5)).isoformat();db.commit()
    revoked=client.delete("/api/profile/api-keys/"+created["id"],headers={"X-CSRF-Token":registered["csrf_token"]})
    assert revoked.status_code==200
    assert client.get("/api/v1/invoices",headers={"Authorization":"Bearer "+secret}).status_code==401
    assert client.get("/api/auth/me").status_code==200
    assert client.delete("/api/profile/api-keys/"+created["id"],headers={"X-CSRF-Token":registered["csrf_token"]}).status_code==200


def test_invalid_bearer_does_not_fall_back_to_browser_session(api_env):
    client,_,_,_=api_env
    register(client)
    response=client.get("/api/v1/invoices",headers={"Authorization":"Bearer invai_invalid"})
    assert response.status_code==401


def test_scopes_upload_owner_assignment_and_processing_status(api_env):
    client,factory,tmp_path,jobs=api_env
    registered=register(client)
    owner_id=user_id_for(factory,"api-owner@example.test")
    write_key=create_key(client,registered["csrf_token"],scopes=["invoices:write"])
    write_headers={"Authorization":"Bearer "+write_key["api_key"]}
    response=client.post("/api/v1/invoices",headers=write_headers,files={"file":("upload.pdf",b"%PDF-1.4 synthetic","application/pdf")})
    assert response.status_code==202,response.text
    body=response.json();assert body["status"]=="UPLOADED" and body["status_url"].endswith(body["id"])
    assert jobs==[(body["id"],owner_id)]
    with factory() as db:
        record=db.get(Record,body["id"]);assert record.user_id==owner_id
    assert client.get("/api/v1/invoices",headers=write_headers).status_code==403
    assert client.get("/api/v1/invoices/"+body["id"],headers=write_headers).status_code==403
    read_key=create_key(client,registered["csrf_token"],scopes=["invoices:read"])
    read_headers={"Authorization":"Bearer "+read_key["api_key"]}
    detail=client.get(body["status_url"],headers=read_headers)
    assert detail.status_code==200 and detail.json()["status"]=="UPLOADED"
    assert detail.json()["extraction"]["model"] is None
    pending=client.get(body["status_url"]+"/json",headers=read_headers)
    assert pending.status_code==409 and pending.json()["detail"]["code"]=="processing"


def test_upload_rejects_unsupported_and_oversized_files(api_env):
    client,_,_,_=api_env
    registered=register(client)
    key=create_key(client,registered["csrf_token"],scopes=["invoices:write"])
    headers={"Authorization":"Bearer "+key["api_key"]}
    assert client.post("/api/v1/invoices",headers=headers,files={"file":("invoice.txt",b"not an invoice","text/plain")}).status_code==415
    oversized=b"%PDF"+(b"0"*(10*1024*1024))
    assert client.post("/api/v1/invoices",headers=headers,files={"file":("large.pdf",oversized,"application/pdf")}).status_code==413


def test_list_cursor_status_search_and_read_json_uses_decimal_strings(api_env):
    client,factory,tmp_path,_=api_env
    registered=register(client)
    owner_id=user_id_for(factory,"api-owner@example.test")
    first_invoice=Invoice(supplier_name="Alpha BV",invoice_number="A-1",currency="EUR",subtotal="100.00",total_vat="21.00",invoice_total="121.00")
    second_invoice=Invoice(supplier_name="Beta BV",invoice_number="B-1",currency="EUR",subtotal="10.00",total_vat="2.10",invoice_total="12.10")
    add_invoice(factory,tmp_path,owner_id,"alpha",invoice=first_invoice)
    add_invoice(factory,tmp_path,owner_id,"beta",status="APPROVED",invoice=second_invoice)
    key=create_key(client,registered["csrf_token"],scopes=["invoices:read"])
    headers={"Authorization":"Bearer "+key["api_key"]}
    page=client.get("/api/v1/invoices?limit=1",headers=headers).json()
    assert len(page["items"])==1 and page["next_cursor"]
    page2=client.get("/api/v1/invoices?limit=1&cursor="+page["next_cursor"],headers=headers).json()
    assert len(page2["items"])==1 and page2["next_cursor"] is None
    assert page["items"][0]["id"]!=page2["items"][0]["id"]
    assert client.get("/api/v1/invoices?status=APPROVED",headers=headers).json()["items"][0]["id"]=="beta"
    assert client.get("/api/v1/invoices?search=Alpha",headers=headers).json()["items"][0]["id"]=="alpha"
    bad_cursor=client.get("/api/v1/invoices?cursor=not-a-cursor",headers=headers)
    assert bad_cursor.status_code==422
    data=client.get("/api/v1/invoices/alpha/json",headers=headers).json()
    assert data["approved"] is False and data["invoice"]["invoice_total"]=="121.00"
    approved=client.get("/api/v1/invoices/beta/json",headers=headers).json()
    assert approved["approved"] is True and approved["invoice"]["subtotal"]=="10.00"


def test_failed_invoice_returns_safe_error(api_env):
    client,factory,tmp_path,_=api_env
    registered=register(client)
    owner_id=user_id_for(factory,"api-owner@example.test")
    add_invoice(factory,tmp_path,owner_id,"failed",status="FAILED",invoice={})
    with factory() as db:
        row=db.get(Record,"failed");row.extraction_json=json.dumps({"status":"failed","error":"Cannot connect to secret-host:11434"});row.validation_json=json.dumps({"errors":["Cannot connect to secret-host:11434"]});db.commit()
    key=create_key(client,registered["csrf_token"],scopes=["invoices:read"])
    headers={"Authorization":"Bearer "+key["api_key"]}
    response=client.get("/api/v1/invoices/failed/json",headers=headers)
    assert response.status_code==409 and "secret-host" not in response.text
    detail=client.get("/api/v1/invoices/failed",headers=headers)
    assert detail.status_code==200 and "secret-host" not in detail.text


def test_document_scope_and_safe_attachment_download(api_env):
    client,factory,tmp_path,_=api_env
    registered=register(client)
    owner_id=user_id_for(factory,"api-owner@example.test")
    path=add_invoice(factory,tmp_path,owner_id,"document")
    read_key=create_key(client,registered["csrf_token"],scopes=["invoices:read"])
    read_headers={"Authorization":"Bearer "+read_key["api_key"]}
    assert client.get("/api/v1/invoices/document/document",headers=read_headers).status_code==403
    document_key=create_key(client,registered["csrf_token"],scopes=["invoices:document:read"])
    response=client.get("/api/v1/invoices/document/document",headers={"Authorization":"Bearer "+document_key["api_key"]})
    assert response.status_code==200 and response.content==path.read_bytes()
    assert "attachment" in response.headers["content-disposition"].lower()
    with factory() as db:
        record=db.get(Record,"document");record.path=str(tmp_path.parent/"outside.pdf");db.commit()
    assert client.get("/api/v1/invoices/document/document",headers={"Authorization":"Bearer "+document_key["api_key"]}).status_code==404


def test_two_users_cannot_access_each_others_invoices_or_keys(api_env):
    client_a,factory,tmp_path,_=api_env
    registered_a=register(client_a,"a@example.test")
    client_b=TestClient(app_module.app)
    try:
        registered_b=register(client_b,"b@example.test")
        owner_a=user_id_for(factory,"a@example.test");owner_b=user_id_for(factory,"b@example.test")
        add_invoice(factory,tmp_path,owner_a,"only-a")
        add_invoice(factory,tmp_path,owner_b,"only-b")
        key_a=create_key(client_a,registered_a["csrf_token"],scopes=["invoices:read","invoices:document:read"])
        key_b=create_key(client_b,registered_b["csrf_token"],name="B key",scopes=["invoices:read","invoices:document:read"])
        headers_a={"Authorization":"Bearer "+key_a["api_key"]}
        headers_b={"Authorization":"Bearer "+key_b["api_key"]}
        assert [x["id"] for x in client_a.get("/api/v1/invoices",headers=headers_a).json()["items"]]==["only-a"]
        assert [x["id"] for x in client_b.get("/api/v1/invoices",headers=headers_b).json()["items"]]==["only-b"]
        for path in ("/api/v1/invoices/only-b","/api/v1/invoices/only-b/json","/api/v1/invoices/only-b/document"):
            assert client_a.get(path,headers=headers_a).status_code==404,path
        assert client_a.get("/api/profile/api-keys").json()[0]["id"]==key_a["id"]
        assert client_a.delete("/api/profile/api-keys/"+key_b["id"],headers={"X-CSRF-Token":registered_a["csrf_token"]}).status_code==404
    finally:
        client_b.close()


def test_api_keys_require_session_for_management_and_rate_limit_is_configurable(api_env,monkeypatch):
    client,_,_,_=api_env
    registered=register(client)
    key=create_key(client,registered["csrf_token"],scopes=["invoices:read"])
    bearer={"Authorization":"Bearer "+key["api_key"]}
    assert client.get("/api/profile/api-keys",headers=bearer).status_code==200 # Existing browser cookie remains present.
    api_client=TestClient(app_module.app)
    assert api_client.get("/api/profile/api-keys",headers=bearer).status_code==401
    monkeypatch.setenv("API_RATE_LIMIT_PER_MINUTE","1")
    assert client.get("/api/v1/invoices",headers=bearer).status_code==200
    limited=client.get("/api/v1/invoices",headers=bearer)
    assert limited.status_code==429 and limited.headers.get("retry-after")=="60"
    api_client.close()


def test_exact_document_duplicate_is_blocked_per_workspace_without_cross_user_disclosure(api_env):
    client_a,_,_,_=api_env
    registered_a=register(client_a,"duplicate-a@example.test")
    first=client_a.post("/api/invoices",headers={"X-CSRF-Token":registered_a["csrf_token"]},files={"file":("one.pdf",b"%PDF-1.4 same contents","application/pdf")})
    assert first.status_code==202
    second=client_a.post("/api/invoices",headers={"X-CSRF-Token":registered_a["csrf_token"]},files={"file":("renamed.pdf",b"%PDF-1.4 same contents","application/pdf")})
    assert second.status_code==409
    assert second.json()["detail"]["code"]=="EXACT_FILE_DUPLICATE"
    assert second.json()["detail"]["invoice_id"]==first.json()["id"]
    client_b=TestClient(app_module.app)
    try:
        registered_b=register(client_b,"duplicate-b@example.test")
        allowed=client_b.post("/api/invoices",headers={"X-CSRF-Token":registered_b["csrf_token"]},files={"file":("same.pdf",b"%PDF-1.4 same contents","application/pdf")})
        assert allowed.status_code==202
    finally:client_b.close()


def test_database_constraint_closes_concurrent_exact_file_upload_race(api_env,tmp_path):
    client,factory,_,_=api_env
    register(client,"race@example.test");owner=user_id_for(factory,"race@example.test");barrier=Barrier(2)
    def insert(index):
        try:
            with factory() as db:
                barrier.wait()
                db.add(Record(id=f"race-{index}",filename=f"{index}.pdf",path=str(tmp_path/f"{index}.pdf"),mime="application/pdf",status="UPLOADED",user_id=owner,file_hash="a"*64))
                db.commit();return "inserted"
        except Exception:return "rejected"
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(insert,(1,2)))
    assert sorted(results)==["inserted","rejected"]


def test_database_constraint_closes_concurrent_vat_invoice_identity_race(api_env,tmp_path):
    client,factory,_,_=api_env
    register(client,"identity-a@example.test");register(client,"identity-b@example.test")
    owner=user_id_for(factory,"identity-a@example.test");other=user_id_for(factory,"identity-b@example.test");barrier=Barrier(2)
    def insert(index):
        try:
            with factory() as db:
                barrier.wait()
                db.add(Record(id=f"identity-{index}",filename=f"{index}.pdf",path=str(tmp_path/f"identity-{index}.pdf"),mime="application/pdf",status="NEEDS_REVIEW",user_id=owner,normalized_supplier_vat="nl123",normalized_invoice_number="inv7",document_type="invoice"))
                db.commit();return "inserted"
        except Exception:return "rejected"
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(insert,(1,2)))
    assert sorted(results)==["inserted","rejected"]
    with factory() as db:
        db.add(Record(id="other-owner-identity",filename="other.pdf",path=str(tmp_path/"other.pdf"),mime="application/pdf",user_id=other,normalized_supplier_vat="nl123",normalized_invoice_number="inv7",document_type="invoice"));db.commit()


def test_startup_additively_backfills_hashes_and_stable_line_ids(api_env):
    client,factory,tmp_path,_=api_env
    register(client,"migration@example.test");owner=user_id_for(factory,"migration@example.test")
    path=tmp_path/"legacy.pdf";content=b"%PDF-1.4 prior invoice";path.write_bytes(content)
    legacy={"supplier_name":"Legacy Supplier","invoice_number":"L-1","line_items":[{"description":"Existing line","line_total":"10.00"}]}
    with factory() as db:
        db.add(Record(id="legacy-row",filename=path.name,path=str(path),mime="application/pdf",status="NEEDS_REVIEW",invoice_json=json.dumps(legacy),user_id=owner));db.commit()
    with factory() as db:
        app_module.migrate_workspace_duplicate_metadata(db);db.commit()
        row=db.get(Record,"legacy-row");stored=json.loads(row.invoice_json)
        assert row.file_hash==app_module.hashlib.sha256(content).hexdigest()
        assert row.normalized_supplier=="legacysupplier" and row.normalized_invoice_number=="l1" and row.document_type=="invoice"
        assert stored["supplier_name"]=="Legacy Supplier" and stored["line_items"][0]["description"]=="Existing line"
        assert stored["line_items"][0]["line_id"]


def test_accounting_configuration_suggestions_confirmation_and_user_isolation(api_env):
    client_a,factory,tmp_path,_=api_env
    registered=register(client_a,"mapping-a@example.test");owner=user_id_for(factory,"mapping-a@example.test")
    gl=client_a.post("/api/accounting/gl-accounts",headers={"X-CSRF-Token":registered["csrf_token"]},json={"code":"4400","name":"Software subscriptions","category":"Software"})
    tax=client_a.post("/api/accounting/tax-codes",headers={"X-CSRF-Token":registered["csrf_token"]},json={"code":"NL_PURCHASE_21","name":"Dutch purchase standard","jurisdiction":"NL","transaction_type":"purchase","treatment":"standard","rate":"21"})
    assert gl.status_code==200 and tax.status_code==200,(gl.text,tax.text)
    rule=client_a.post("/api/accounting/mapping-rules",headers={"X-CSRF-Token":registered["csrf_token"]},json={"name":"Microsoft subscriptions","supplier_key":"Microsoft","description_contains":"subscription","gl_account_id":gl.json()["id"],"tax_code_id":tax.json()["id"],"priority":10})
    assert rule.status_code==200,rule.text
    supplier=client_a.post("/api/accounting/suppliers",headers={"X-CSRF-Token":registered["csrf_token"]},json={"supplier_name":"Microsoft","supplier_vat_number":"NL123","external_id":"configured-contact-1"})
    assert supplier.status_code==200 and supplier.json()["external_id"]=="configured-contact-1"
    invoice=Invoice(supplier_name="Microsoft",customer_country="NL",invoice_number="M-1",currency="EUR",line_items=[{"description":"Microsoft 365 subscription","quantity":"1","unit_price":"10","vat_rate":"21","line_total":"10"}])
    add_invoice(factory,tmp_path,owner,"mapping-invoice",invoice=invoice)
    suggested=client_a.post("/api/invoices/mapping-invoice/accounting-mapping/suggest",headers={"X-CSRF-Token":registered["csrf_token"]})
    assert suggested.status_code==200,suggested.text
    item=suggested.json()["lines"][0]
    assert item["source"]=="supplier_rule" and item["mapping_status"]=="SUGGESTED" and item["confirmed"] is False
    saved=client_a.patch("/api/invoices/mapping-invoice/accounting-mapping",headers={"X-CSRF-Token":registered["csrf_token"]},json={"lines":[{"line_id":item["line_id"],"gl_account_id":gl.json()["id"],"tax_code_id":tax.json()["id"]}]})
    assert saved.status_code==200 and saved.json()["mapping_status"]=="MAPPED"
    other=TestClient(app_module.app)
    try:
        register(other,"mapping-b@example.test")
        assert other.get("/api/accounting/gl-accounts").json()==[]
        assert other.get("/api/accounting/suppliers").json()==[]
        assert other.get("/api/invoices/mapping-invoice/accounting-mapping").status_code==404
        assert other.patch("/api/accounting/mapping-rules/"+rule.json()["id"],headers={"X-CSRF-Token":other.get("/api/auth/csrf").json()["csrf_token"]},json={"active":False}).status_code==404
    finally:other.close()
