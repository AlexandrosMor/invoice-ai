import os
os.environ["DATABASE_URL"]="sqlite://"
from datetime import datetime, timezone, timedelta
from pathlib import Path
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
import app as app_module
from app import Base, User, LoginSession, Record
app=app_module.app

@pytest.fixture
def web(tmp_path,monkeypatch):
    engine=create_engine("sqlite:///"+str(tmp_path/"phase2-test.db"),connect_args={"check_same_thread":False})
    Base.metadata.create_all(engine)
    factory=sessionmaker(engine,expire_on_commit=False)
    monkeypatch.setattr(app_module,"Session",factory)
    monkeypatch.setattr(app_module,"process_invoice",lambda *_args:None)
    app_module.AUTH_ATTEMPTS.clear()
    with TestClient(app) as client:
        yield client,factory,tmp_path
    engine.dispose()

def csrf(client): return client.get("/api/auth/csrf").json()["csrf_token"]
def register(client,email="owner@example.test",password="InvoiceSecure123"):
    token=csrf(client)
    response=client.post("/api/auth/register",headers={"X-CSRF-Token":token},json={"first_name":"Alex","last_name":"Mor","company_name":"Northwind","email":email,"password":password})
    assert response.status_code==200,response.text
    return response.json()

def test_registration_hashes_password_and_starts_session(web):
    client,factory,_=web
    token=csrf(client)
    response=client.post("/api/auth/register",headers={"X-CSRF-Token":token},json={"first_name":"Alex","last_name":"Mor","company_name":"Northwind","email":" OWNER@Example.test ","password":"InvoiceSecure123"})
    result=response.json()
    assert response.status_code==200 and result["user"]["email"]=="owner@example.test"
    assert "httponly" in response.headers.get("set-cookie","").lower()
    assert client.get("/api/auth/me").json()["user"]["first_name"]=="Alex"
    raw=client.cookies.get("invoiceai_session")
    assert raw
    with factory() as db:
        account=db.scalar(select(User).where(User.email=="owner@example.test"))
        session=db.scalar(select(LoginSession).where(LoginSession.user_id==account.id))
        assert account.password_hash.startswith("$argon2id$") and account.password_hash!="InvoiceSecure123"
        assert session.token_hash!=raw and len(session.token_hash)==64
    token=result["csrf_token"]
    assert client.post("/api/auth/logout",headers={"X-CSRF-Token":token}).status_code==200
    assert client.get("/api/auth/me").status_code==401

def test_registration_validation_duplicate_and_csrf(web):
    client,_,_=web
    assert client.post("/api/auth/register",json={}).status_code==403
    token=csrf(client); headers={"X-CSRF-Token":token}
    body={"first_name":"A","last_name":"B","company_name":"Co","email":"bad","password":"InvoiceSecure123"}
    assert client.post("/api/auth/register",headers=headers,json=body).status_code==422
    body["email"]="valid@example.test";body["password"]="short"
    assert client.post("/api/auth/register",headers=headers,json=body).status_code==422
    body["password"]="InvoiceSecure123"
    assert client.post("/api/auth/register",headers=headers,json=body).status_code==200
    token=client.get("/api/auth/csrf").json()["csrf_token"]
    assert client.post("/api/auth/register",headers={"X-CSRF-Token":token},json=body).status_code==409

def test_login_profile_password_and_session_expiration(web):
    client,factory,_=web
    result=register(client)
    client.post("/api/auth/logout",headers={"X-CSRF-Token":result["csrf_token"]})
    token=csrf(client)
    bad=client.post("/api/auth/login",headers={"X-CSRF-Token":token},json={"email":"owner@example.test","password":"wrong"})
    assert bad.status_code==401
    good=client.post("/api/auth/login",headers={"X-CSRF-Token":token},json={"email":" OWNER@example.test ","password":"InvoiceSecure123"})
    assert good.status_code==200
    token=good.json()["csrf_token"]
    updated=client.patch("/api/profile",headers={"X-CSRF-Token":token},json={"first_name":"Alexandra","last_name":"Mor","company_name":"New Company","email":"new@example.test"})
    assert updated.status_code==200 and updated.json()["email"]=="new@example.test"
    changed=client.post("/api/profile/change-password",headers={"X-CSRF-Token":token},json={"current_password":"InvoiceSecure123","new_password":"DifferentSecure456"})
    assert changed.status_code==200 and client.get("/api/auth/me").status_code==200
    client.post("/api/auth/logout",headers={"X-CSRF-Token":token})
    token=csrf(client)
    assert client.post("/api/auth/login",headers={"X-CSRF-Token":token},json={"email":"new@example.test","password":"DifferentSecure456"}).status_code==200
    with factory() as db:
        session=db.scalar(select(LoginSession).where(LoginSession.revoked_at.is_(None)))
        session.expires_at=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat();db.commit()
    assert client.get("/api/auth/me").status_code==401

def test_invoice_operations_are_isolated_by_session_user(web):
    client_a,factory,tmp=web
    result_a=register(client_a,"a@example.test")
    client_b=TestClient(app)
    result_b=register(client_b,"b@example.test")
    path=tmp/"private.pdf";path.write_bytes(b"%PDF synthetic")
    with factory() as db:
        owner_a=db.scalar(select(User).where(User.email=="a@example.test"));owner_b=db.scalar(select(User).where(User.email=="b@example.test"))
        db.add_all([Record(id="a-invoice",filename="a.pdf",path=str(path),mime="application/pdf",status="APPROVED",invoice_json=json.dumps({"supplier_name":"A","invoice_number":"A-1","invoice_total":"12.00"}),user_id=owner_a.id),Record(id="b-invoice",filename="b.pdf",path=str(path),mime="application/pdf",status="APPROVED",invoice_json=json.dumps({"supplier_name":"B","invoice_number":"B-1","invoice_total":"8.00"}),user_id=owner_b.id)])
        db.commit()
    assert [x["id"] for x in client_a.get("/api/invoices").json()]==["a-invoice"]
    assert [x["id"] for x in client_b.get("/api/invoices").json()]==["b-invoice"]
    assert client_a.get("/api/invoices/b-invoice").status_code==404
    assert client_b.get("/api/invoices/a-invoice/document").status_code==404
    headers={"X-CSRF-Token":result_b["csrf_token"]}
    assert client_b.patch("/api/invoices/a-invoice",headers=headers,json={}).status_code==404
    for path in ("/validate","/approve","/sync/moneybird"):
        response=client_b.post("/api/invoices/a-invoice"+path,headers=headers,json={})
        assert response.status_code==404,(path,response.text)
    assert client_b.get("/api/invoices/a-invoice/export").status_code==404
    assert client_b.get("/api/invoices/a-invoice/audit").status_code==404
    client_b.close()

