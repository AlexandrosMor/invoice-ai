"""Normalize model strings into canonical Decimal values without guessing fields."""
from decimal import Decimal, InvalidOperation

AMOUNT_KEYS={"subtotal","total_vat","invoice_total","quantity","unit_price","line_total","rate","taxable_amount","tax_amount","vat_rate"}

def parse_decimal(value, percent=False):
    if value is None or isinstance(value,(int,float,Decimal)): return value
    text=str(value).strip()
    if not text:return None
    if percent and text.endswith("%"): text=text[:-1].strip()
    negative=text.startswith("(") and text.endswith(")")
    if negative:text=text[1:-1].strip()
    # Discard currency labels/symbols while preserving separators and sign.
    text="".join(ch for ch in text if ch.isdigit() or ch in ",.+-")
    if not text or text in ("+","-",".",",","+.","-,"): raise ValueError("Invalid monetary value")
    if "," in text and "." in text:
        if text.rfind(",")>text.rfind("."): text=text.replace(".","").replace(",",".")
        else:text=text.replace(",","")
    elif "," in text:
        pieces=text.split(",")
        if len(pieces)>2: text="".join(pieces[:-1])+"."+pieces[-1]
        elif len(pieces[-1]) in (1,2): text=".".join(pieces)
        else:text="".join(pieces)
    try:
        number=Decimal(text)
        if not number.is_finite(): raise ValueError("Non-finite monetary value")
        return -number if negative else number
    except InvalidOperation as e: raise ValueError("Invalid monetary value") from e

def normalize_invoice_payload(data: dict) -> dict:
    normalized=dict(data)
    for key in ("subtotal","total_vat","invoice_total"):
        if key in normalized: normalized[key]=parse_decimal(normalized[key])
    if isinstance(normalized.get("line_items"),list):
        normalized["line_items"]=[{k:(parse_decimal(v,k=="vat_rate") if k in AMOUNT_KEYS else v) for k,v in row.items()} for row in normalized["line_items"]]
    if isinstance(normalized.get("vat_breakdown"),list):
        normalized["vat_breakdown"]=[{k:(parse_decimal(v,k=="rate") if k in AMOUNT_KEYS else v) for k,v in row.items()} for row in normalized["vat_breakdown"]]
    return normalized
