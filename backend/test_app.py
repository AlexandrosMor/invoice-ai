import os
os.environ["DATABASE_URL"]="sqlite://"
from decimal import Decimal
from app import Invoice, validate, MoneybirdAdapter

def test_totals_and_vat_validation():
    i=Invoice(supplier_name="Acme",invoice_number="1",currency="EUR",subtotal=Decimal("100"),total_vat=Decimal("21"),invoice_total=Decimal("121"))
    assert validate(i,[]) ["valid"]
    i.invoice_total=Decimal("120")
    assert "Subtotal plus VAT does not match invoice total" in validate(i,[])["errors"]

def test_duplicate_detection():
    a=Invoice(supplier_name="Acme",invoice_number="X")
    assert any("duplicate" in x for x in validate(a,[a])["errors"])

def test_moneybird_mock():
    i=Invoice(supplier_name="Acme",invoice_number="X",currency="EUR",invoice_total=Decimal("12"))
    result=MoneybirdAdapter().sync(i,"abc",{})
    assert result["success"] and result["mode"]=="mock" and result["external_document_id"]=="mock-abc"
