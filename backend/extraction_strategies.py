"""Composable invoice extraction strategies and conservative hybrid selection."""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Protocol

from document_processing import ProcessedDocument
from extraction import ExtractionError, InvoiceExtractor, PIPELINE_VERSION, derive_evidence
from invoice_domain import Invoice
from normalization import normalize_date, normalize_invoice_payload, parse_decimal
from validation import validate


class ExtractionStrategy(Protocol):
    """Common interface implemented by deterministic and model-backed extractors."""

    name: str

    def extract(self, document: ProcessedDocument) -> dict[str, Any]: ...


def analyze_document(document: ProcessedDocument) -> dict[str, Any]:
    """Return observable document facts; never infer supplier/template identity."""
    text = "\n".join(page.text for page in document.pages if page.text)
    sample = text.casefold()
    dutch = sum(word in sample for word in ("factuur", "leverancier", "btw", "betaal", "totaal"))
    english = sum(word in sample for word in ("invoice", "supplier", "vat", "payment", "total"))
    language = "nl" if dutch > english else "en" if english > dutch else "unknown"
    return {
        "document_kind": document.document_kind,
        "page_count": len(document.pages),
        "readable_text_pages": sum(bool(page.text.strip()) for page in document.pages),
        "language": language,
        "has_embedded_text": bool(text.strip()),
        "supplier_identity": "unknown",
        "template": "none",
    }


_NUMBER = r"\(?-?\d[\d., ]*\d?\)?"
_MONEY = re.compile(_NUMBER)
_LINE = re.compile(
    rf"^(?P<description>.+?)\s+\|\s*(?:Qty|Quantity)\s+(?P<quantity>{_NUMBER})"
    rf"\s+\|\s*(?:Unit|Price)\s+(?P<unit>{_NUMBER})\s+\|\s*VAT\s+"
    rf"(?P<rate>\d+(?:[.,]\d+)?)%\s+\|\s*(?:Net|Total)\s+(?P<net>{_NUMBER})\s*$",
    re.IGNORECASE,
)
_PLAIN_LINE = re.compile(
    rf"^(?P<description>.+?)\s+(?P<quantity>{_NUMBER})\s+(?P<unit>{_NUMBER})"
    rf"\s+(?P<rate>\d+(?:[.,]\d+)?)%\s+(?P<net>{_NUMBER})\s*$"
)
_VAT_ROW = re.compile(
    rf"^VAT\s+(?P<rate>\d+(?:[.,]\d+)?)%\s*"
    rf"(?:\((?P<treatment>[^)]+)\))?\s*:\s*taxable\s+(?P<base>{_NUMBER})"
    rf"\s+tax\s+(?P<tax>{_NUMBER})\s*$",
    re.IGNORECASE,
)


def _amount_from(value: str):
    match = _MONEY.search(value)
    return parse_decimal(match.group(0)) if match else None


class DeterministicTextStrategy:
    """Conservative label/table parser for digitally readable PDF text.

    It is a measurable baseline, not a general invoice parser. It does not OCR
    scanned documents or derive missing values from arithmetic.
    """

    name = "deterministic"

    def extract(self, document: ProcessedDocument) -> dict[str, Any]:
        started = time.monotonic()
        invoice_data: dict[str, Any] = {"vat_breakdown": [], "line_items": []}
        supplier_name_source = "missing"
        last_line_index: int | None = None
        lines = [line.strip() for page in document.pages for line in page.text.splitlines() if line.strip()]
        for line in lines:
            lowered = line.casefold()
            if "credit note" in lowered:
                invoice_data["document_type"] = "credit_note"
            elif lowered in {"invoice", "factuur"}:
                invoice_data.setdefault("document_type", "invoice")

            supplier_label = re.match(r"^(?:Supplier(?: name)?|From|Vendor):\s*(.+)$", line, re.IGNORECASE)
            if supplier_label and "supplier_name" not in invoice_data:
                invoice_data["supplier_name"] = supplier_label.group(1).strip()
                supplier_name_source = "explicit_label"

            if "supplier_name" not in invoice_data and not any(
                marker in lowered for marker in ("synthetic sample", "continued invoice", "continued factuur")
            ) and lowered not in {"invoice", "factuur", "credit note"} and not re.match(
                r"^(vat|invoice\s+(?:number|date)|due date|payment due date|bill to|customer|supplier|subtotal|total|currency|description)\b",
                line,
                re.IGNORECASE,
            ) and not _LINE.match(line) and not _PLAIN_LINE.match(line):
                # The first unlabelled heading is only a supplier-name candidate.
                # It is accepted as deterministic evidence only if the exact text
                # can be located in the original document text below.
                invoice_data["supplier_name"] = line
                supplier_name_source = "unlabeled_heading_candidate"

            match = re.match(r"^(?:VAT|Supplier VAT(?: number| ID)?):\s*(\S+)\s*$", line, re.IGNORECASE)
            if match and "vat " not in lowered[:5]:
                invoice_data["supplier_vat_number"] = match.group(1)
            match = re.match(r"^Supplier address:\s*(.+)$", line, re.IGNORECASE)
            if match:
                invoice_data["supplier_address"] = match.group(1).strip()
            match = re.match(r"^(?:Bill to|Billed to|Customer):\s*(.+)$", line, re.IGNORECASE)
            if match:
                invoice_data["customer_name"] = match.group(1).strip()
            match = re.match(r"^Customer address:\s*(.+)$", line, re.IGNORECASE)
            if match:
                invoice_data["customer_address"] = match.group(1).strip()
            match = re.match(r"^Customer country:\s*(.+)$", line, re.IGNORECASE)
            if match:
                invoice_data["customer_country"] = match.group(1).strip()

            match = re.match(r"^Invoice\s*(?:number|no\.?|#)\s*:\s*(.+)$", line, re.IGNORECASE)
            if match:
                invoice_data["invoice_number"] = match.group(1).strip()
            match = re.match(r"^Invoice date:\s*(.+)$", line, re.IGNORECASE)
            if match:
                invoice_data["invoice_date"] = normalize_date(match.group(1).strip())
            match = re.match(r"^(?:Payment )?due date:\s*(.+)$", line, re.IGNORECASE)
            if match:
                invoice_data["due_date"] = normalize_date(match.group(1).strip())
            match = re.match(r"^Currency:\s*([A-Za-z]{3})\s*$", line, re.IGNORECASE)
            if match:
                invoice_data["currency"] = match.group(1).upper()

            for label, field in (
                (r"subtotal", "subtotal"),
                (r"discount total", "discount_total"),
                (r"(?:shipping\s*/\s*fees|additional charges|fees)", "additional_charges"),
                (r"total vat", "total_vat"),
                (r"amount paid", "amount_paid"),
                (r"amount due|total due|grand total|invoice total", "invoice_total"),
            ):
                match = re.match(rf"^(?:{label})\s*:\s*(.+)$", line, re.IGNORECASE)
                if match:
                    amount = _amount_from(match.group(1))
                    if amount is not None:
                        invoice_data[field] = amount
                        if field == "invoice_total" and re.match(r"^(?:amount due|total due):", line, re.IGNORECASE):
                            invoice_data["amount_due"] = amount
                        code = re.match(r"\s*([A-Z]{3}|€|\$|£)", match.group(1), re.IGNORECASE)
                        if code and "currency" not in invoice_data:
                            invoice_data["currency"] = {"€": "EUR", "$": "USD", "£": "GBP"}.get(code.group(1), code.group(1).upper())
                    break

            tax = _VAT_ROW.match(line)
            if tax:
                invoice_data["vat_breakdown"].append({
                    "rate": tax.group("rate"),
                    "taxable_amount": tax.group("base"),
                    "tax_amount": tax.group("tax"),
                    "treatment": (tax.group("treatment") or "").strip().replace(" ", "_") or None,
                })
                continue
            tax = re.match(rf"^VAT\s+(\d+(?:[.,]\d+)?)%\s*:\s*(.+)$", line, re.IGNORECASE)
            if tax:
                invoice_data["vat_breakdown"].append({"rate": tax.group(1), "tax_amount": _amount_from(tax.group(2))})

            line_match = _LINE.match(line) or _PLAIN_LINE.match(line)
            if line_match:
                row = line_match.groupdict()
                line_item = {
                    "description": row["description"].strip(),
                    "quantity": row["quantity"],
                    "unit_price": row["unit"],
                    "vat_rate": row["rate"],
                    "line_total": row["net"],
                }
                if re.search(r"\b(gross|including\s+vat|incl\.?\s*vat)\b", line, re.IGNORECASE):
                    quantity = parse_decimal(row["quantity"])
                    unit_price = parse_decimal(row["unit"])
                    line_item["gross_total"] = quantity * unit_price
                    line_item["price_includes_vat"] = True
                invoice_data["line_items"].append(line_item)
                last_line_index = len(invoice_data["line_items"]) - 1
                continue

            if last_line_index is not None:
                line_discount = re.match(rf"^Line discount:\s*(.+)$", line, re.IGNORECASE)
                if line_discount:
                    invoice_data["line_items"][last_line_index]["discount"] = _amount_from(line_discount.group(1))
                treatment = re.match(r"^Tax treatment:\s*(.+)$", line, re.IGNORECASE)
                if treatment:
                    invoice_data["line_items"][last_line_index]["vat_treatment"] = treatment.group(1).strip().casefold().replace(" ", "_")

        # A currency symbol/code is accepted only when printed next to a total.
        if "currency" not in invoice_data:
            for line in lines:
                if re.search(r"(?:EUR|USD|GBP|€|\$|£)\s*" + _NUMBER, line, re.IGNORECASE):
                    token = re.search(r"EUR|USD|GBP|€|\$|£", line, re.IGNORECASE).group(0)
                    invoice_data["currency"] = {"€": "EUR", "$": "USD", "£": "GBP"}.get(token, token.upper())
                    break

        invoice = Invoice.model_validate(normalize_invoice_payload(invoice_data))
        return {
            "invoice": invoice,
            "evidence": derive_evidence(invoice, document),
            "model": "deterministic-pdf-text",
            "pipeline_version": PIPELINE_VERSION,
            "strategy": self.name,
            "strategy_selected": self.name,
            "duration_ms": round((time.monotonic() - started) * 1000, 2),
            "strategy_duration_ms": round((time.monotonic() - started) * 1000, 2),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "attempts": 1,
            "document_kind": document.document_kind,
            "document_analysis": analyze_document(document),
            "strategy_diagnostics": {"supplier_name_source": supplier_name_source},
            "status": "succeeded",
        }


class HybridExtractionStrategy:
    """Use deterministic text only when critical fields and validation support it."""

    name = "hybrid"
    REQUIRED_FIELDS = ("supplier_name", "invoice_number", "invoice_date", "currency", "subtotal", "total_vat", "invoice_total")

    def __init__(self, deterministic: ExtractionStrategy | None = None, model: ExtractionStrategy | None = None):
        self.deterministic = deterministic or DeterministicTextStrategy()
        self.model = model or InvoiceExtractor()

    def extract(self, document: ProcessedDocument) -> dict[str, Any]:
        started = time.monotonic()
        deterministic = self.deterministic.extract(document)
        invoice = deterministic["invoice"]
        missing = [field for field in self.REQUIRED_FIELDS if getattr(invoice, field) is None]
        if not document.all_digital:
            reason = "document_has_scanned_or_image_pages"
        elif deterministic.get("strategy_diagnostics", {}).get("supplier_name_source") != "explicit_label":
            reason = "supplier_identity_not_explicitly_labeled"
        elif missing:
            reason = "missing_required_fields:" + ",".join(missing)
        elif not invoice.line_items or any(
            any(getattr(line, field) is None for field in ("description", "quantity", "unit_price", "line_total"))
            for line in invoice.line_items
        ):
            reason = "line_item_evidence_incomplete"
        elif any(deterministic["evidence"].get(field, {}).get("needs_review", True) for field in self.REQUIRED_FIELDS):
            reason = "required_field_evidence_incomplete"
        elif any(
            value is not None and not any(str(value).casefold() in page.text.casefold() for page in document.pages)
            for line in invoice.line_items
            for value in (line.description, line.quantity, line.unit_price, line.line_total)
        ):
            reason = "line_item_source_text_incomplete"
        elif any(
            value is not None and not any(str(value).casefold() in page.text.casefold() for page in document.pages)
            for tax in invoice.vat_breakdown
            for value in (tax.rate, tax.taxable_amount, tax.tax_amount)
        ):
            reason = "vat_source_text_incomplete"
        else:
            assessment = validate(invoice, [])
            reason = "deterministic_validation_requires_review" if assessment["review_assessment"] != "READY_FOR_REVIEW" else None
        if reason is None:
            deterministic["strategy"] = self.name
            deterministic["strategy_selected"] = "deterministic"
            deterministic["fallback_reason"] = None
            deterministic["strategy_duration_ms"] = round((time.monotonic() - started) * 1000, 2)
            return deterministic
        result = self.model.extract(document)
        model_duration = result.get("duration_ms")
        result["strategy"] = self.name
        result["strategy_selected"] = "ollama"
        result["fallback_reason"] = reason
        result["model_inference_duration_ms"] = model_duration
        result["strategy_duration_ms"] = round((time.monotonic() - started) * 1000, 2)
        result["document_analysis"] = analyze_document(document)
        return result


def create_extractor(strategy: str | None = None) -> ExtractionStrategy:
    """Create the configured strategy; defaults to the backwards-compatible Ollama path."""
    selected = (strategy or os.getenv("INVOICE_EXTRACTION_STRATEGY", "ollama")).strip().lower()
    if selected == "ollama":
        return InvoiceExtractor()
    if selected == "deterministic":
        return DeterministicTextStrategy()
    if selected == "hybrid":
        return HybridExtractionStrategy()
    raise ValueError(f"Unsupported INVOICE_EXTRACTION_STRATEGY: {selected}")
