"""Deterministic invoice validation; no model decisions are used here."""
from datetime import date
from decimal import Decimal, InvalidOperation
import os
import re
from typing import Optional
from invoice_domain import Invoice

ZERO_DECIMAL_CURRENCIES={"BIF","CLP","DJF","GNF","ISK","JPY","KMF","KRW","PYG","RWF","UGX","UYI","VND","VUV","XAF","XOF","XPF"}
THREE_DECIMAL_CURRENCIES={"BHD","IQD","JOD","KWD","LYD","OMR","TND"}
IMPORTANT=("supplier_name","invoice_number","invoice_date","currency","invoice_total")

def configured_tolerance(currency: Optional[str]=None) -> Decimal:
    try:
        configured=os.getenv("INVOICE_ROUNDING_TOLERANCE")
        if configured is None:
            code=(currency or "").upper()
            return Decimal("1") if code in ZERO_DECIMAL_CURRENCIES else Decimal("0.002") if code in THREE_DECIMAL_CURRENCIES else Decimal("0.02")
        value=Decimal(configured)
        return value if value.is_finite() and value>=0 else Decimal("0.02")
    except (InvalidOperation,ValueError):
        return Decimal("0.02")

def currency_tolerance(currency: Optional[str], configured: Optional[Decimal]=None) -> Decimal:
    """Return a precision-aware tolerance; an explicit configured value takes precedence."""
    if configured is not None:return configured
    if currency and currency.upper() in ZERO_DECIMAL_CURRENCIES:return Decimal("1")
    return configured_tolerance(currency)

def validate(invoice: Invoice, all_invoices: list[Invoice], tolerance: Optional[Decimal]=None) -> dict:
    tolerance=currency_tolerance(invoice.currency,tolerance)
    issues=[]
    def add(severity,code,message,field=None): issues.append({"severity":severity,"code":code,"message":message,"field":field})
    for f in ("supplier_name","invoice_number","invoice_date","currency","invoice_total"):
        if getattr(invoice,f) is None: add("ERROR" if f in ("supplier_name","invoice_number","invoice_total") else "WARNING", "missing_"+f, f"{f.replace('_',' ').capitalize()} is missing",f)
    dates={}
    for f in ("invoice_date","due_date","delivery_date","payment_date"):
        v=getattr(invoice,f)
        if v:
            try: dates[f]=date.fromisoformat(v)
            except (ValueError,TypeError): add("ERROR","invalid_date",f"{f.replace('_',' ').capitalize()} must use YYYY-MM-DD",f)
    if dates.get("invoice_date") and dates.get("due_date") and dates["due_date"]<dates["invoice_date"]: add("WARNING","due_before_invoice","Due date is earlier than invoice date","due_date")
    if invoice.currency and not re.fullmatch(r"[A-Z]{3}",invoice.currency.upper()): add("WARNING","invalid_currency_code",f"Currency {invoice.currency!r} must be a three-letter ISO 4217 code","currency")
    vals=[invoice.subtotal,invoice.total_vat,invoice.invoice_total]
    for f,v in zip(("subtotal","total_vat","invoice_total"),vals):
        if v is not None and (not v.is_finite()): add("ERROR","non_finite_amount",f"{f.replace('_',' ').capitalize()} must be finite",f)
    credit=(invoice.document_type or "").casefold().replace(" ","_") in {"credit_note","creditnote","credit"}
    for f,v in zip(("subtotal","total_vat","invoice_total"),vals):
        if v is not None and not credit and v<0: add("WARNING","negative_amount",f"Negative {f.replace('_',' ')}; confirm this is a credit note",f)
    if all(v is not None and v.is_finite() for v in vals):
        # The invoice convention is net subtotal - discounts + VAT + explicit charges.
        expected=invoice.subtotal-(invoice.discount_total or Decimal(0))+invoice.total_vat+(invoice.additional_charges or Decimal(0))
        if abs(expected-invoice.invoice_total)>tolerance:
            message="Subtotal plus VAT does not match invoice total" if not invoice.discount_total and not invoice.additional_charges else "Subtotal less discounts plus VAT and additional charges does not match invoice total"
            add("ERROR","document_total_mismatch",message,"invoice_total")
    elif invoice.invoice_total is not None and (invoice.subtotal is None or invoice.total_vat is None):
        add("WARNING","incomplete_financial_breakdown","Subtotal or total VAT is missing, so the payable total cannot be reconciled","invoice_total")
    line_sum=sum((x.line_total for x in invoice.line_items if x.line_total is not None),Decimal(0))
    line_discounts=sum((x.discount for x in invoice.line_items if x.discount is not None),Decimal(0))
    complete_lines=bool(invoice.line_items) and all(x.line_total is not None for x in invoice.line_items)
    if invoice.line_items and not complete_lines:add("WARNING","incomplete_line_items","Some line items do not have an extracted net total and need review","line_items")
    expected_subtotal=invoice.subtotal
    if complete_lines and expected_subtotal is not None and abs(line_sum+line_discounts-expected_subtotal)>tolerance: add("WARNING","line_subtotal_mismatch","Sum of line items, before line discounts, does not match subtotal","subtotal")
    for i,line in enumerate(invoice.line_items,1):
        if line.vat_rate==0 and not line.vat_treatment:add("WARNING","ambiguous_zero_vat_treatment",f"Line {i}: a zero VAT rate needs an explicit tax treatment","line_items")
        calculated=(line.quantity*line.unit_price-(line.discount or Decimal(0))) if all(v is not None for v in (line.quantity,line.unit_price)) else None
        if calculated is not None and line.price_includes_vat and line.vat_rate is not None and line.vat_rate>=0:
            calculated=calculated/(Decimal(1)+line.vat_rate/Decimal(100))
        if calculated is not None and line.line_total is not None and abs(calculated-line.line_total)>tolerance:
            add("WARNING","line_calculation_mismatch",f"Line {i}: quantity × unit price does not match line total", "line_items")
        if line.gross_total is not None and line.line_total is not None and line.vat_rate is not None:
            expected_gross=line.line_total*(Decimal(1)+line.vat_rate/Decimal(100))
            if abs(expected_gross-line.gross_total)>tolerance:add("WARNING","line_gross_mismatch",f"Line {i}: net and gross values do not reconcile with its VAT rate","line_items")
    for tax in invoice.vat_breakdown:
        if tax.rate==0 and not tax.treatment:add("WARNING","ambiguous_zero_vat_treatment","A zero VAT rate does not identify zero-rated, exempt, or reverse-charge treatment","vat_breakdown")
        if tax.taxable_amount is not None and tax.rate is not None and tax.tax_amount is not None:
            treatment=(tax.treatment or "").casefold().replace("-","_").replace(" ","_")
            if treatment in {"reverse_charge","exempt","vat_exempt","out_of_scope"} and tax.tax_amount==0:continue
            expected=tax.taxable_amount*tax.rate/Decimal(100)
            if abs(expected-tax.tax_amount)>tolerance: add("WARNING","vat_rate_mismatch",f"VAT at {tax.rate}% does not reconcile with taxable amount", "vat_breakdown")
    if invoice.vat_breakdown and invoice.total_vat is not None and all(x.tax_amount is not None for x in invoice.vat_breakdown):
        vat_sum=sum((x.tax_amount for x in invoice.vat_breakdown),Decimal(0))
        if abs(vat_sum-invoice.total_vat)>tolerance: add("ERROR","vat_breakdown_mismatch","VAT breakdown does not equal total VAT","total_vat")
    if invoice.amount_due is not None and invoice.amount_paid is not None and invoice.invoice_total is not None:
        if abs(invoice.invoice_total-invoice.amount_paid-invoice.amount_due)>tolerance:add("WARNING","amount_due_mismatch","Invoice total less amount paid does not match amount due","amount_due")
    errors=[x for x in issues if x["severity"]=="ERROR"]
    warnings=[x for x in issues if x["severity"]=="WARNING"]
    missing_evidence=[f for f in IMPORTANT if getattr(invoice,f) is not None]
    return {"valid":not errors,"severity":"ERROR" if errors else "WARNING" if warnings else "INFO","issues":issues,"errors":[x["message"] for x in errors],"warnings":[x["message"] for x in warnings],"review_assessment":"VALIDATION_FAILED" if errors else "NEEDS_ATTENTION" if warnings else "READY_FOR_REVIEW","rounding_tolerance":str(tolerance),"currency":invoice.currency}
