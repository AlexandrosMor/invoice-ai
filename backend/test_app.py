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
    from duplicate_detection import classify_duplicate
    result=classify_duplicate(a.model_dump(mode="json"),"hash",[{"id":"old","invoice":a.model_dump(mode="json"),"file_hash":"different"}])
    assert result["classification"]=="POSSIBLE_DUPLICATE" and not result["blocks_approval"]

def test_moneybird_mock():
    i=Invoice(supplier_name="Acme",invoice_number="X",currency="EUR",invoice_total=Decimal("12"))
    result=MoneybirdAdapter().sync(i,"abc",{})
    assert not result["success"] and result["mode"]=="mock" and result["missing_mappings"]
