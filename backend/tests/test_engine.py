import os
os.environ["DATABASE_URL"]="sqlite://"
import json
from decimal import Decimal
from pathlib import Path
import pytest
from invoice_domain import Invoice, LineItem, TaxBreakdown
from document_processing import DocumentProcessor
from extraction import derive_evidence, InvoiceExtractor, ExtractionError
from validation import validate
from normalization import normalize_invoice_payload
from scripts.evaluate_invoices import field_equal, evaluate_dataset

FIXTURES=Path(__file__).parent/"fixtures"

def test_synthetic_pdf_digital_text_and_evidence():
    doc=DocumentProcessor.process(FIXTURES/"invoices"/"invoice_001.pdf","application/pdf")
    assert doc.document_kind=="digital_pdf" and "NW-2026-001" in doc.pages[0].text
    invoice=Invoice(supplier_name="Northwind Office Supplies B.V.",invoice_number="NW-2026-001",invoice_date="2026-04-12",currency="EUR",invoice_total=Decimal("29.65"))
    evidence=derive_evidence(invoice,doc)
    assert evidence["invoice_number"]["page"]==1
    assert evidence["invoice_number"]["bounding_box"] is not None

def test_scanned_pdf_page_is_rendered(tmp_path):
    import fitz
    image=tmp_path/"page.png"; d=fitz.open();p=d.new_page();p.insert_text((70,70),"scanned synthetic text");d.save(tmp_path/"source.pdf");d.close()
    d=fitz.open(tmp_path/"source.pdf");pix=d[0].get_pixmap();pix.save(image);d.close()
    pdf=fitz.open();page=pdf.new_page();page.insert_image(page.rect,filename=str(image));pdf.save(tmp_path/"scanned.pdf");pdf.close()
    processed=DocumentProcessor.process(tmp_path/"scanned.pdf","application/pdf")
    assert processed.document_kind=="scanned_pdf" and processed.pages[0].image.startswith(b"\x89PNG")

def test_multi_page_pdf_preserves_order(tmp_path):
    import fitz
    pdf=fitz.open()
    for label in ("FIRST PAGE", "SECOND PAGE"):
        p=pdf.new_page();p.insert_text((70,70),label+" synthetic invoice content")
    pdf.save(tmp_path/"multi.pdf");pdf.close()
    result=DocumentProcessor.process(tmp_path/"multi.pdf","application/pdf")
    assert [p.number for p in result.pages]==[1,2] and "SECOND PAGE" in result.pages[1].text

def test_decimal_vat_and_line_item_checks():
    invoice=Invoice(supplier_name="A",invoice_number="N",invoice_date="2026-01-01",currency="EUR",subtotal=Decimal("25"),total_vat=Decimal("4.65"),invoice_total=Decimal("29.65"),vat_breakdown=[TaxBreakdown(rate=21,taxable_amount=20,tax_amount=4.20),TaxBreakdown(rate=9,taxable_amount=5,tax_amount=.45)],line_items=[LineItem(quantity=2,unit_price=10,line_total=20),LineItem(quantity=1,unit_price=5,line_total=5)])
    assert validate(invoice,[])["valid"]
    invoice.invoice_total=Decimal("31")
    assert any(x["code"]=="document_total_mismatch" for x in validate(invoice,[])["issues"])

def test_credit_note_negative_amounts_are_allowed():
    invoice=Invoice(supplier_name="A",invoice_number="CN-1",document_type="credit_note",subtotal=-10,total_vat=-2.1,invoice_total=-12.1)
    assert not any(x["code"]=="negative_amount" for x in validate(invoice,[])["issues"])

def test_decimal_rounding_and_duplicate_detection():
    invoice=Invoice(supplier_name="A",invoice_number="1",subtotal=Decimal("100"),total_vat=Decimal("21"),invoice_total=Decimal("121.01"))
    assert validate(invoice,[])["valid"]
    assert any(x["code"]=="duplicate_invoice" for x in validate(invoice,[invoice])["issues"])

def test_model_numeric_normalization_dutch_amounts_and_percent_rates():
    normalized=normalize_invoice_payload({"subtotal":"EUR 1.234,56","invoice_total":"(1.234,56)","line_items":[{"vat_rate":"21%","unit_price":"€ 1.234,56"}],"vat_breakdown":[{"rate":"9 %","tax_amount":"0,45"}]})
    assert normalized["subtotal"]==Decimal("1234.56")
    assert normalized["invoice_total"]==Decimal("-1234.56")
    assert normalized["line_items"][0]["vat_rate"]==Decimal("21")
    assert normalized["vat_breakdown"][0]["tax_amount"]==Decimal("0.45")

def test_benchmark_field_metrics_numeric_and_null():
    assert field_equal("invoice_total","10.00","10.01")
    assert field_equal("invoice_number",None,None)
    assert not field_equal("supplier_name","Expected",None)

class FakeResponse:
    def __init__(self,content): self.content=content
    def raise_for_status(self): pass
    def json(self): return {"message":{"content":self.content}}
class FakeClient:
    def __init__(self,*args,**kwargs): pass
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def post(self,*args,**kwargs): return FakeResponse('{"supplier_name":"Northwind Office Supplies B.V.","invoice_number":"NW-2026-001","invoice_date":"2026-04-12","currency":"EUR","invoice_total":29.65}')

def test_mocked_extraction_parses_and_records_version(monkeypatch):
    import extraction
    monkeypatch.setattr(extraction.httpx,"Client",FakeClient)
    doc=DocumentProcessor.process(FIXTURES/"invoices"/"invoice_001.pdf","application/pdf")
    result=InvoiceExtractor().extract(doc)
    assert result["invoice"].invoice_number=="NW-2026-001" and result["duration_ms"]>=0 and result["pipeline_version"]

def test_invalid_json_from_model_fails_without_leaking_content(monkeypatch):
    import extraction
    class BadClient(FakeClient):
        def post(self,*args,**kwargs): return FakeResponse('{"invoice_number":')
    monkeypatch.setattr(extraction.httpx,"Client",BadClient)
    monkeypatch.setenv("OLLAMA_RETRIES","0")
    doc=DocumentProcessor.process(FIXTURES/"invoices"/"invoice_001.pdf","application/pdf")
    with pytest.raises(ExtractionError,match="invalid JSON"):
        InvoiceExtractor().extract(doc)

def test_invalid_model_response_is_retried_once(monkeypatch):
    import extraction
    class RetryClient(FakeClient):
        calls=0
        def post(self,*args,**kwargs):
            type(self).calls+=1
            if type(self).calls==1:return FakeResponse('{not-json')
            return FakeResponse('{"supplier_name":"Northwind Office Supplies B.V.","invoice_number":"NW-2026-001"}')
    monkeypatch.setattr(extraction.httpx,"Client",RetryClient)
    monkeypatch.setenv("OLLAMA_RETRIES","1")
    doc=DocumentProcessor.process(FIXTURES/"invoices"/"invoice_001.pdf","application/pdf")
    result=InvoiceExtractor().extract(doc)
    assert RetryClient.calls==2 and result["attempts"]==2

def test_timeout_is_bounded_and_reported(monkeypatch):
    import extraction, httpx
    class TimeoutClient(FakeClient):
        def post(self,*args,**kwargs): raise httpx.ReadTimeout("timed out")
    monkeypatch.setattr(extraction.httpx,"Client",TimeoutClient)
    monkeypatch.setenv("OLLAMA_TIMEOUT","5")
    monkeypatch.setenv("OLLAMA_RETRIES","0")
    doc=DocumentProcessor.process(FIXTURES/"invoices"/"invoice_001.pdf","application/pdf")
    with pytest.raises(ExtractionError,match="timed out after 5"):
        InvoiceExtractor().extract(doc)

def test_manual_correction_metadata_marks_audit_and_evidence():
    from app import correction_metadata, can_approve
    old={"supplier_name":"Old Supplier","invoice_number":"A-1"}
    corrected=Invoice(supplier_name="New Supplier",invoice_number="A-1")
    metadata={"evidence":{"supplier_name":{"page":1,"text":"Old Supplier","needs_review":False}}}
    updated,changes=correction_metadata(old,corrected,metadata)
    assert changes["supplier_name"]=={"previous":"Old Supplier","corrected":"New Supplier"}
    assert updated["evidence"]["supplier_name"]["manually_corrected"] is True
    assert updated["human_corrections"]
    assert not can_approve("NEEDS_REVIEW",{"valid":True})
    assert not can_approve("VALIDATED",{"valid":False})
    assert can_approve("VALIDATED",{"valid":True})

def test_benchmark_aggregates_per_invoice_metrics():
    class EvalExtractor:
        model="fake-qwen"
        def extract(self,document):
            return {"invoice":Invoice(supplier_name="Northwind Office Supplies B.V.",invoice_number="NW-2026-001",invoice_date="2026-04-12",due_date="2026-05-12",currency="EUR",subtotal=25,total_vat=4.65,invoice_total=29.65,vat_breakdown=[TaxBreakdown(rate=21,taxable_amount=20,tax_amount=4.2),TaxBreakdown(rate=9,taxable_amount=5,tax_amount=.45)],line_items=[LineItem(description="Paper (A4)",quantity=2,unit_price=10,vat_rate=21,line_total=20),LineItem(description="Pens (blue)",quantity=1,unit_price=5,vat_rate=9,line_total=5)]),"duration_ms":10,"model":"fake-qwen","pipeline_version":"test"}
    result=evaluate_dataset(FIXTURES,extractor=EvalExtractor())
    assert result["total_invoices"]==1 and result["overall_field_accuracy"]==1
    assert result["whole_invoice_exact_match_rate"]==1 and result["average_processing_time_ms"]>=10

def test_processing_claim_does_not_start_duplicate_task():
    from app import Session,Record,process_invoice
    with Session() as db:
        db.add(Record(id="already-processing-test",filename="synthetic.pdf",path="missing.pdf",mime="application/pdf",status="PROCESSING"));db.commit()
    process_invoice("already-processing-test")
    with Session() as db:
        assert db.get(Record,"already-processing-test").status=="PROCESSING"
