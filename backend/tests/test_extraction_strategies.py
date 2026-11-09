from pathlib import Path

from document_processing import DocumentProcessor, ProcessedDocument, ProcessedPage
from extraction_strategies import (
    DeterministicTextStrategy,
    HybridExtractionStrategy,
    analyze_document,
    create_extractor,
)
from invoice_domain import Invoice
from scripts.evaluate_invoices import evaluate_dataset
from scripts.compare_extraction_strategies import compare
from validation import validate


FIXTURES = Path(__file__).parent / "fixtures"


def test_document_analysis_reports_observable_facts_only():
    document = DocumentProcessor.process(FIXTURES / "core/invoices/dutch_21.pdf", "application/pdf")
    analysis = analyze_document(document)
    assert analysis["document_kind"] == "digital_pdf"
    assert analysis["page_count"] == 1
    assert analysis["has_embedded_text"] is True
    assert analysis["supplier_identity"] == "unknown"
    assert analysis["template"] == "none"


def test_deterministic_strategy_extracts_labeled_pdf_without_model():
    document = DocumentProcessor.process(FIXTURES / "invoices/invoice_001.pdf", "application/pdf")
    result = DeterministicTextStrategy().extract(document)
    invoice = result["invoice"]
    assert invoice.supplier_name == "Northwind Office Supplies B.V."
    assert invoice.invoice_number == "NW-2026-001"
    assert str(invoice.invoice_total) == "29.65"
    assert [line.description for line in invoice.line_items] == ["Paper (A4)", "Pens (blue)"]
    assert result["evidence"]["invoice_number"]["page"] == 1
    assert result["strategy"] == "deterministic"


def test_hybrid_short_circuits_only_when_deterministic_evidence_and_validation_are_ready(tmp_path):
    import fitz
    path = tmp_path / "hybrid-explicit-invoice.pdf"
    pdf = fitz.open()
    page = pdf.new_page()
    for y, line in enumerate((
        "Supplier: Example Office BV",
        "Invoice number: E-100",
        "Invoice date: 2026-01-15",
        "Currency: EUR",
        "Paper | Qty 1 | Unit 10.00 | VAT 21% | Net 10.00",
        "Subtotal: EUR 10.00",
        "VAT 21% (standard): taxable 10.00 tax 2.10",
        "Total VAT: EUR 2.10",
        "Invoice total: EUR 12.10",
    ), start=1):
        page.insert_text((50, 50 + y * 24), line)
    pdf.save(path)
    pdf.close()
    document = DocumentProcessor.process(path, "application/pdf")

    class ModelMustNotRun:
        def extract(self, _document):
            raise AssertionError("Qwen should not run for a complete, evidenced digital invoice")

    result = HybridExtractionStrategy(model=ModelMustNotRun()).extract(document)
    assert result["strategy_selected"] == "deterministic"
    assert result["fallback_reason"] is None
    assert result["invoice"].amount_due is None
    assert validate(result["invoice"], [])["valid"]


def test_hybrid_falls_back_for_unlabeled_supplier_identity():
    document = DocumentProcessor.process(FIXTURES / "invoices/invoice_001.pdf", "application/pdf")

    class FakeModel:
        def extract(self, _document):
            return {"invoice": Invoice(invoice_number="MODEL-1"), "evidence": {}, "model": "fake", "strategy": "ollama"}

    result = HybridExtractionStrategy(model=FakeModel()).extract(document)
    assert result["strategy_selected"] == "ollama"
    assert result["fallback_reason"] == "supplier_identity_not_explicitly_labeled"


def test_hybrid_uses_model_fallback_for_scanned_document():
    document = ProcessedDocument([ProcessedPage(1, b"image", "", False, [])], "image", "image/png")

    class FakeModel:
        def __init__(self):
            self.calls = 0

        def extract(self, _document):
            self.calls += 1
            return {"invoice": Invoice(invoice_number="OCR-1"), "evidence": {}, "model": "fake", "strategy": "ollama"}

    model = FakeModel()
    result = HybridExtractionStrategy(model=model).extract(document)
    assert model.calls == 1
    assert result["strategy_selected"] == "ollama"
    assert result["fallback_reason"] == "document_has_scanned_or_image_pages"
    assert result["document_analysis"]["has_embedded_text"] is False


def test_strategy_factory_is_configurable_and_defaults_to_ollama(monkeypatch):
    monkeypatch.delenv("INVOICE_EXTRACTION_STRATEGY", raising=False)
    assert create_extractor().__class__.__name__ == "InvoiceExtractor"
    assert create_extractor("deterministic").name == "deterministic"
    assert create_extractor("hybrid").name == "hybrid"
    try:
        create_extractor("unknown")
    except ValueError as error:
        assert "Unsupported INVOICE_EXTRACTION_STRATEGY" in str(error)
    else:
        raise AssertionError("Unknown strategy must fail clearly")


def test_deterministic_benchmark_reports_accuracy_latency_and_environment():
    report = evaluate_dataset(FIXTURES / "core", extractor=DeterministicTextStrategy())
    assert report["dataset_size"] == 13
    assert report["strategy"] == "deterministic"
    assert report["overall_field_accuracy"] is not None
    assert report["p50_processing_time_ms"] is not None
    assert report["runtime"]["python"]
    assert report["runtime"]["estimated_processing_cost"] is None
    assert report["average_inference_time_ms"] is None


def test_comparison_writes_per_strategy_reports_and_shared_summary(tmp_path):
    result = compare(FIXTURES / "core", tmp_path, ["deterministic"])
    assert result["dataset_size"] == 13
    assert result["strategies"]["deterministic"]["report_file"] == "deterministic.json"
    assert (tmp_path / "deterministic.json").is_file()
    assert (tmp_path / "comparison.json").is_file()
