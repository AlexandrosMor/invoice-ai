"""Workspace-scoped deterministic invoice duplicate classification."""
from decimal import Decimal, InvalidOperation
import re
import unicodedata

NO_DUPLICATE="NO_DUPLICATE"
EXACT_FILE_DUPLICATE="EXACT_FILE_DUPLICATE"
EXACT_INVOICE_DUPLICATE="EXACT_INVOICE_DUPLICATE"
POSSIBLE_DUPLICATE="POSSIBLE_DUPLICATE"

def normalize_identity(value):
    if value is None:return ""
    text=unicodedata.normalize("NFKD",str(value)).encode("ascii","ignore").decode().casefold()
    return re.sub(r"[^a-z0-9]", "", text)

def invoice_identity(invoice):
    supplier_vat=normalize_identity(invoice.get("supplier_vat_number"))
    supplier_name=normalize_identity(invoice.get("supplier_name"))
    number=normalize_identity(invoice.get("invoice_number"))
    kind=normalize_identity(invoice.get("document_type") or "invoice")
    return supplier_vat,supplier_name,number,kind

def classify_duplicate(invoice, file_hash, candidates):
    """Candidates must already be constrained to the requesting user's workspace."""
    new_vat,new_name,new_number,new_kind=invoice_identity(invoice)
    possible=[]
    for candidate in candidates:
        existing=candidate["invoice"]
        if file_hash and candidate.get("file_hash")==file_hash:
            return {"classification":EXACT_FILE_DUPLICATE,"matches":[candidate["id"]],"blocks_approval":True}
        old_vat,old_name,old_number,old_kind=invoice_identity(existing)
        same_number=bool(new_number and new_number==old_number and new_kind==old_kind)
        if same_number and new_vat and old_vat and new_vat==old_vat:
            return {"classification":EXACT_INVOICE_DUPLICATE,"matches":[candidate["id"]],"blocks_approval":True}
        if same_number and not(new_vat and old_vat and new_vat==old_vat) and new_name and new_name==old_name:
            possible.append(candidate["id"])
            continue
        same_supplier=bool((new_vat and old_vat and new_vat==old_vat) or (new_name and new_name==old_name))
        if same_supplier and new_kind==old_kind:
            same_date=bool(invoice.get("invoice_date") and invoice.get("invoice_date")==existing.get("invoice_date"))
            same_currency=bool(invoice.get("currency") and invoice.get("currency")==existing.get("currency"))
            same_total=False
            try:same_total=invoice.get("invoice_total") is not None and existing.get("invoice_total") is not None and Decimal(str(invoice["invoice_total"]))==Decimal(str(existing["invoice_total"]))
            except (InvalidOperation,ValueError,TypeError):pass
            if same_date and same_currency and same_total:possible.append(candidate["id"])
    if possible:return {"classification":POSSIBLE_DUPLICATE,"matches":sorted(set(possible)),"blocks_approval":False}
    return {"classification":NO_DUPLICATE,"matches":[],"blocks_approval":False}
