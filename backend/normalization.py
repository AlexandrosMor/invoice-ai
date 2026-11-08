"""Normalize model strings into canonical Decimal values without guessing fields."""
from decimal import Decimal, InvalidOperation
from datetime import date
import re

AMOUNT_KEYS={"subtotal","discount_total","additional_charges","total_vat","invoice_total","amount_paid","amount_due","quantity","unit_price","line_total","gross_total","discount","rate","taxable_amount","tax_amount","vat_rate"}

MONTHS={"januari":1,"january":1,"februari":2,"february":2,"maart":3,"march":3,"april":4,"mei":5,"may":5,"juni":6,"june":6,"juli":7,"july":7,"augustus":8,"august":8,"september":9,"oktober":10,"october":10,"november":11,"december":12}

def normalize_date(value):
    """Normalize only unambiguous dates; ambiguous numeric dates stay untouched for review."""
    if value is None:return None
    raw=str(value).strip()
    if not raw:return None
    try:return date.fromisoformat(raw).isoformat()
    except ValueError:pass
    written=re.fullmatch(r"(\d{1,2})\s+([A-Za-z]+)\.?\s+(\d{4})",raw)
    if written:
        month=MONTHS.get(written.group(2).casefold())
        if month:
            try:return date(int(written.group(3)),month,int(written.group(1))).isoformat()
            except ValueError:return raw
    numeric=re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})",raw)
    if numeric:
        first,second,year=map(int,numeric.groups())
        if first>12 and second<=12:day,month=first,second
        elif second>12 and first<=12:day,month=second,first
        elif first>12 or second>12:return raw
        else:return raw
        try:return date(year,month,day).isoformat()
        except ValueError:return raw
    return raw

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
    for key in ("invoice_date","due_date","delivery_date","payment_date"):
        if key in normalized: normalized[key]=normalize_date(normalized[key])
    for key in ("subtotal","discount_total","additional_charges","total_vat","invoice_total","amount_paid","amount_due"):
        if key in normalized: normalized[key]=parse_decimal(normalized[key])
    if isinstance(normalized.get("line_items"),list):
        normalized["line_items"]=[{k:(parse_decimal(v,k=="vat_rate") if k in AMOUNT_KEYS else v) for k,v in row.items()} for row in normalized["line_items"]]
    if isinstance(normalized.get("vat_breakdown"),list):
        normalized["vat_breakdown"]=[{k:(parse_decimal(v,k=="rate") if k in AMOUNT_KEYS else v) for k,v in row.items()} for row in normalized["vat_breakdown"]]
    for field in ("supplier_vat_number","customer_vat_number","supplier_kvk_number"):
        if normalized.get(field) is not None:normalized[field]=str(normalized[field]).strip().upper()
    if normalized.get("supplier_iban") is not None:
        normalized["supplier_iban"]="".join(str(normalized["supplier_iban"]).upper().split()) or None
    if normalized.get("currency") is not None:
        currency=str(normalized["currency"]).strip().upper()
        normalized["currency"]=currency if re.fullmatch(r"[A-Z]{3}",currency) else normalized["currency"]
    return normalized
