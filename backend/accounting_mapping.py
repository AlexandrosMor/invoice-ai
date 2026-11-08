"""Deterministic mapping suggestions kept separate from extracted invoice facts."""
from decimal import Decimal, InvalidOperation
import re
import unicodedata
from sqlalchemy import select

def _norm(value):return " ".join(str(value or "").casefold().split())
def _country(value):
    normalized=_norm(value)
    return {"netherlands":"nl","nederland":"nl","belgium":"be","belgie":"be","germany":"de","deutschland":"de","france":"fr","united kingdom":"gb","uk":"gb"}.get(normalized,normalized)
def _identity(value):return re.sub(r"[^a-z0-9]","",unicodedata.normalize("NFKD",str(value or "")).encode("ascii","ignore").decode().casefold())

class SupplierMappingService:
    @staticmethod
    def resolve(invoice, supplier_mappings):
        vat=_identity(invoice.get("supplier_vat_number"))
        name=_identity(invoice.get("supplier_name"))
        if vat:
            return next((row for row in supplier_mappings if _identity(row.get("supplier_vat_key"))==vat),None)
        return next((row for row in supplier_mappings if not row.get("supplier_vat_key") and row.get("supplier_name_key")==name),None)

class GeneralLedgerMappingService:
    @staticmethod
    def resolve(rule, gl_accounts):
        if not rule:return None
        return next((account for account in gl_accounts if account["id"]==rule.get("gl_account_id") and account.get("active",True)),None)

class TaxCodeMappingService:
    @staticmethod
    def resolve(rule,line,invoice,tax_codes):
        if rule and rule.get("tax_code_id"):
            return next((item for item in tax_codes if item["id"]==rule["tax_code_id"] and item.get("active",True)),None)
        if line.get("vat_rate") is None:return None
        try:rate=Decimal(str(line["vat_rate"]))
        except (InvalidOperation,ValueError):return None
        treatment=(line.get("vat_treatment") or "standard").casefold()
        jurisdiction=_country(invoice.get("customer_country"))
        if not jurisdiction:return None
        matches=[]
        for item in tax_codes:
            try:configured=Decimal(str(item.get("rate"))) if item.get("rate") is not None else None
            except (InvalidOperation,ValueError):configured=None
            if configured==rate and _country(item.get("jurisdiction"))==jurisdiction and (item.get("treatment") or "standard").casefold()==treatment and item.get("transaction_type")=="purchase" and item.get("active",True):matches.append(item)
        return matches[0] if len(matches)==1 else None

class MappingRepository:
    """Small SQLAlchemy read boundary for user-owned mapping configuration."""
    def __init__(self,session,user_id):self.session=session;self.user_id=user_id
    def list_for(self,model,active_only=False):
        filters=[model.user_id==self.user_id]
        if active_only:filters.append(model.active.is_(True))
        return self.session.scalars(select(model).where(*filters)).all()

def select_mapping(invoice, rules, gl_accounts, tax_codes, existing=None):
    existing=existing or {}
    existing_by_id={line.get("line_id"):line for line in existing.get("lines",[]) if line.get("line_id")}
    output=[]
    for index,line in enumerate(invoice.get("line_items",[])):
        line_id=line.get("line_id") or f"legacy-{index}"
        manual=existing_by_id.get(line_id,{})
        if manual.get("confirmed"):
            output.append({**manual,"line_id":line_id,"source":"manual","confirmed":True,"mapping_status":"MAPPED"});continue
        candidates=[]
        for rule in rules:
            supplier=rule.get("supplier_key")
            description=rule.get("description_contains")
            supplier_match=bool(supplier and _norm(supplier)==_norm(invoice.get("supplier_name")))
            description_match=bool(description and _norm(description) in _norm(line.get("description")))
            if (supplier or description) and ((not supplier or supplier_match) and (not description or description_match)):
                candidates.append((0 if supplier_match else 1,-int(rule.get("priority",0)),rule))
        rule=min(candidates,key=lambda item:(item[0],item[1],item[2].get("id","")))[2] if candidates else None
        gl=GeneralLedgerMappingService.resolve(rule,gl_accounts)
        tax=TaxCodeMappingService.resolve(rule,line,invoice,tax_codes)
        confirmed=bool(manual)
        source="manual" if confirmed else "supplier_rule" if rule and rule.get("supplier_key") else "customer_rule" if rule else "unmapped"
        complete=bool(gl and tax)
        status="MAPPED" if complete and confirmed else "SUGGESTED" if complete else "NEEDS_REVIEW" if tax and not gl else "UNMAPPED"
        output.append({"line_id":line_id,"category":gl.get("category") if gl else None,"gl_account_id":gl.get("id") if gl else None,"gl_account_code":gl.get("code") if gl else None,"tax_code_id":tax.get("id") if tax else None,"tax_code":tax.get("code") if tax else None,"external_tax_id":tax.get("external_id") if tax else None,"source":source,"applied_rule_id":rule.get("id") if rule else None,"confirmed":confirmed,"mapping_status":status})
    statuses={line["mapping_status"] for line in output}
    status="UNMAPPED" if not output or "UNMAPPED" in statuses else "NEEDS_REVIEW" if "NEEDS_REVIEW" in statuses else "SUGGESTED" if "SUGGESTED" in statuses else "MAPPED"
    return {"mapping_status":status,"lines":output}

class AccountingMappingService:
    @staticmethod
    def suggest(invoice,rules,gl_accounts,tax_codes,existing=None):
        return select_mapping(invoice,rules,gl_accounts,tax_codes,existing)
