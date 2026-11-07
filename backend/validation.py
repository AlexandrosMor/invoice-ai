"""Deterministic invoice validation; no model decisions are used here."""
from datetime import date
from decimal import Decimal, InvalidOperation
import os
from typing import Optional
from invoice_domain import Invoice

SUPPORTED_CURRENCIES={"EUR","USD","GBP","CHF","CAD","AUD","NZD","SEK","NOK","DKK","JPY","CNY","PLN","CZK"}
IMPORTANT=("supplier_name","invoice_number","invoice_date","currency","invoice_total")

def configured_tolerance() -> Decimal:
    try:
        value=Decimal(os.getenv("INVOICE_ROUNDING_TOLERANCE","0.02"))
        return value if value.is_finite() and value>=0 else Decimal("0.02")
    except (InvalidOperation,ValueError):
        return Decimal("0.02")

def validate(invoice: Invoice, all_invoices: list[Invoice], tolerance: Optional[Decimal]=None) -> dict:
    tolerance=configured_tolerance() if tolerance is None else tolerance
    issues=[]
    def add(severity,code,message,field=None): issues.append({"severity":severity,"code":code,"message":message,"field":field})
    for f in ("supplier_name","invoice_number","invoice_date","currency","invoice_total"):
        if getattr(invoice,f) is None: add("ERROR" if f in ("supplier_name","invoice_number","invoice_total") else "WARNING", "missing_"+f, f"{f.replace('_',' ').capitalize()} is missing",f)
    dates={}
    for f in ("invoice_date","due_date"):
        v=getattr(invoice,f)
        if v:
            try: dates[f]=date.fromisoformat(v)
            except (ValueError,TypeError): add("ERROR","invalid_date",f"{f.replace('_',' ').capitalize()} must use YYYY-MM-DD",f)
    if dates.get("invoice_date") and dates.get("due_date") and dates["due_date"]<dates["invoice_date"]: add("WARNING","due_before_invoice","Due date is earlier than invoice date","due_date")
    if invoice.currency and invoice.currency.upper() not in SUPPORTED_CURRENCIES: add("WARNING","unsupported_currency",f"Currency code {invoice.currency!r} is not in the supported ISO currency set","currency")
    vals=[invoice.subtotal,invoice.total_vat,invoice.invoice_total]
    for f,v in zip(("subtotal","total_vat","invoice_total"),vals):
        if v is not None and (not v.is_finite()): add("ERROR","non_finite_amount",f"{f.replace('_',' ').capitalize()} must be finite",f)
    credit=invoice.document_type=="credit_note"
    for f,v in zip(("subtotal","total_vat","invoice_total"),vals):
        if v is not None and not credit and v<0: add("WARNING","negative_amount",f"Negative {f.replace('_',' ')}; confirm this is a credit note",f)
    if all(v is not None and v.is_finite() for v in vals) and abs(invoice.subtotal+invoice.total_vat-invoice.invoice_total)>tolerance:
        add("ERROR","document_total_mismatch","Subtotal plus VAT does not match invoice total","invoice_total")
    line_sum=sum((x.line_total for x in invoice.line_items if x.line_total is not None),Decimal(0))
    complete_lines=bool(invoice.line_items) and all(x.line_total is not None for x in invoice.line_items)
    if complete_lines and invoice.subtotal is not None and abs(line_sum-invoice.subtotal)>tolerance: add("WARNING","line_subtotal_mismatch","Sum of line items does not match subtotal","subtotal")
    for i,line in enumerate(invoice.line_items,1):
        if all(v is not None for v in (line.quantity,line.unit_price,line.line_total)) and abs(line.quantity*line.unit_price-line.line_total)>tolerance:
            add("WARNING","line_calculation_mismatch",f"Line {i}: quantity × unit price does not match line total", "line_items")
    for tax in invoice.vat_breakdown:
        if tax.taxable_amount is not None and tax.rate is not None and tax.tax_amount is not None:
            expected=tax.taxable_amount*tax.rate/Decimal(100)
            if abs(expected-tax.tax_amount)>tolerance: add("WARNING","vat_rate_mismatch",f"VAT at {tax.rate}% does not reconcile with taxable amount", "vat_breakdown")
    if invoice.vat_breakdown and invoice.total_vat is not None and all(x.tax_amount is not None for x in invoice.vat_breakdown):
        vat_sum=sum((x.tax_amount for x in invoice.vat_breakdown),Decimal(0))
        if abs(vat_sum-invoice.total_vat)>tolerance: add("ERROR","vat_breakdown_mismatch","VAT breakdown does not equal total VAT","total_vat")
    if invoice.invoice_number and invoice.supplier_name:
        for other in all_invoices:
            if (other.supplier_name or "").casefold()==invoice.supplier_name.casefold() and (other.invoice_number or "").casefold()==invoice.invoice_number.casefold():
                add("ERROR","duplicate_invoice","Possible duplicate: supplier and invoice number already exist","invoice_number"); break
    errors=[x for x in issues if x["severity"]=="ERROR"]
    warnings=[x for x in issues if x["severity"]=="WARNING"]
    missing_evidence=[f for f in IMPORTANT if getattr(invoice,f) is not None]
    return {"valid":not errors,"severity":"ERROR" if errors else "WARNING" if warnings else "INFO","issues":issues,"errors":[x["message"] for x in errors],"warnings":[x["message"] for x in warnings],"review_assessment":"VALIDATION_FAILED" if errors else "NEEDS_ATTENTION" if warnings else "READY_FOR_REVIEW","rounding_tolerance":str(tolerance)}
