from decimal import Decimal

from accounting_mapping import select_mapping, SupplierMappingService
from duplicate_detection import classify_duplicate
from invoice_domain import Invoice, LineItem, TaxBreakdown
from normalization import normalize_invoice_payload
from validation import validate


def test_dates_are_normalized_only_when_unambiguous():
    data=normalize_invoice_payload({"invoice_date":"27/08/2026","due_date":"27 augustus 2026","delivery_date":"08/09/2026","payment_date":"2026-09-27"})
    assert data["invoice_date"]=="2026-08-27"
    assert data["due_date"]=="2026-08-27"
    assert data["delivery_date"]=="08/09/2026"
    assert data["payment_date"]=="2026-09-27"


def test_invoice_facts_keep_amount_due_credit_sign_and_line_ids():
    invoice=Invoice.model_validate(normalize_invoice_payload({"supplier_name":"Supplier BV","invoice_number":"CN-1","document_type":"credit_note","currency":"EUR","subtotal":"-100,00","discount_total":"0,00","total_vat":"-21,00","invoice_total":"-121,00","amount_paid":"-20,00","amount_due":"-101,00","line_items":[{"description":"Returned service","quantity":"1","unit_price":"-100,00","discount":"0,00","vat_rate":"21%","line_total":"-100,00"}]}))
    assert invoice.document_type=="credit_note" and invoice.invoice_total==Decimal("-121.00")
    assert invoice.amount_due==Decimal("-101.00") and invoice.line_items[0].line_id
    assert validate(invoice,[])["valid"]


def test_financial_validation_includes_discounts_shipping_and_currency_precision(monkeypatch):
    monkeypatch.delenv("INVOICE_ROUNDING_TOLERANCE",raising=False)
    invoice=Invoice(currency="EUR",subtotal=Decimal("100"),discount_total=Decimal("10"),additional_charges=Decimal("5"),total_vat=Decimal("19"),invoice_total=Decimal("114"),line_items=[LineItem(quantity=2,unit_price=50,discount=10,line_total=90)])
    codes={item["code"] for item in validate(invoice,[])["issues"]}
    assert "document_total_mismatch" not in codes
    assert "line_calculation_mismatch" not in codes
    jpy=Invoice(supplier_name="Supplier",invoice_number="J-1",currency="JPY",subtotal=Decimal("100"),total_vat=Decimal("0"),invoice_total=Decimal("100.50"))
    assert validate(jpy,[])["valid"]
    kwd=Invoice(supplier_name="Supplier",invoice_number="K-1",currency="KWD",subtotal=Decimal("100.000"),total_vat=Decimal("0"),invoice_total=Decimal("100.001"))
    assert validate(kwd,[])["valid"] and validate(kwd,[])["rounding_tolerance"]=="0.002"


def test_vat_treatments_are_not_collapsed_into_numeric_zero_rate():
    invoice=Invoice(supplier_name="Supplier",invoice_number="R-1",currency="EUR",subtotal=Decimal("50"),total_vat=Decimal("0"),invoice_total=Decimal("50"),vat_breakdown=[TaxBreakdown(rate=0,taxable_amount=50,tax_amount=0,treatment="reverse_charge")])
    assert validate(invoice,[])["valid"]
    assert invoice.vat_breakdown[0].treatment=="reverse_charge"
    ambiguous=Invoice(supplier_name="Supplier",invoice_number="Z-1",currency="EUR",subtotal=50,total_vat=0,invoice_total=50,vat_breakdown=[TaxBreakdown(rate=0,taxable_amount=50,tax_amount=0)])
    assert any(issue["code"]=="ambiguous_zero_vat_treatment" for issue in validate(ambiguous,[])["issues"])


def test_duplicate_classification_scopes_strength_and_document_kind():
    base={"supplier_name":"Example BV","supplier_vat_number":"NL 123","invoice_number":"INV-1","invoice_date":"2026-01-01","currency":"EUR","invoice_total":"121.00","document_type":"invoice"}
    existing={"id":"other-user-id","invoice":base,"file_hash":"same"}
    assert classify_duplicate(base,"same",[existing])["classification"]=="EXACT_FILE_DUPLICATE"
    new={**base,"supplier_vat_number":"NL123"}
    assert classify_duplicate(new,"different",[existing])["classification"]=="EXACT_INVOICE_DUPLICATE"
    weak={k:v for k,v in base.items() if k!="supplier_vat_number"}
    assert classify_duplicate(weak,"different",[{"id":"same-name","invoice":weak,"file_hash":"old"}])["classification"]=="POSSIBLE_DUPLICATE"
    credit={**new,"document_type":"credit_note"}
    assert classify_duplicate(credit,"different",[existing])["classification"]=="NO_DUPLICATE"


def test_accounting_mapping_is_configured_contextual_and_never_confirms_suggestions():
    invoice={"supplier_name":"Microsoft","customer_country":"NL","line_items":[{"line_id":"line-1","description":"Microsoft 365 subscription","vat_rate":"21"}]}
    accounts=[{"id":"gl-1","code":"4400","name":"Subscriptions","category":"Software","active":True}]
    taxes=[{"id":"tax-1","code":"NL-PURCHASE-21","jurisdiction":"NL","transaction_type":"purchase","treatment":"standard","rate":"21","active":True}]
    rules=[{"id":"general","description_contains":"subscription","gl_account_id":"gl-1","tax_code_id":"tax-1","priority":0},{"id":"supplier","supplier_key":"Microsoft","gl_account_id":"gl-1","tax_code_id":"tax-1","priority":-1}]
    suggested=select_mapping(invoice,rules,accounts,taxes)
    assert suggested["lines"][0]["source"]=="supplier_rule"
    assert suggested["lines"][0]["mapping_status"]=="SUGGESTED" and not suggested["lines"][0]["confirmed"]
    missing_context=select_mapping({**invoice,"customer_country":None},[],accounts,taxes)
    assert missing_context["lines"][0]["tax_code_id"] is None
    confirmed=select_mapping(invoice,rules,accounts,taxes,{"lines":[{"line_id":"line-1","confirmed":True,"gl_account_id":"gl-1","tax_code_id":"tax-1"}]})
    assert confirmed["mapping_status"]=="MAPPED" and confirmed["lines"][0]["source"]=="manual"


def test_supplier_reference_does_not_merge_distinct_vat_entities_by_name():
    configured=[{"supplier_name_key":"examplebv","supplier_vat_key":"nl111","external_id":"contact-1"}]
    exact_vat=SupplierMappingService.resolve({"supplier_name":"Example B.V.","supplier_vat_number":"NL 111"},configured)
    different_vat=SupplierMappingService.resolve({"supplier_name":"Example B.V.","supplier_vat_number":"NL 222"},configured)
    no_vat=SupplierMappingService.resolve({"supplier_name":"Example B.V.","supplier_vat_number":None},configured)
    assert exact_vat["external_id"]=="contact-1"
    assert different_vat is None and no_vat is None


def test_mock_moneybird_uses_explicit_external_mappings_only():
    from app import MoneybirdAdapter
    line=LineItem(description="Software",quantity=1,unit_price=10,vat_rate=21,line_total=10)
    invoice=Invoice(supplier_name="Vendor",invoice_number="V-1",currency="EUR",invoice_total=Decimal("12.10"),line_items=[line])
    missing=MoneybirdAdapter().sync(invoice,"invoice-1",{},[],[])
    assert not missing["success"] and "external provider IDs" in missing["error"]
    mapping={"mapping_status":"MAPPED","lines":[{"line_id":line.line_id,"confirmed":True,"gl_account_id":"gl-1","tax_code_id":"tax-1"}]}
    result=MoneybirdAdapter().sync(invoice,"invoice-1",mapping,[{"id":"gl-1","external_id":"provider-ledger-9"}],[{"id":"tax-1","external_id":"provider-tax-21"}],[{"supplier_name_key":"vendor","supplier_vat_key":"","external_id":"provider-contact-1"}])
    assert result["success"] and result["mode"]=="mock"
    assert result["payload"]["details"][0]["ledger_account_id"]=="provider-ledger-9"
    assert result["payload"]["details"][0]["tax_rate_id"]=="provider-tax-21"
    assert result["payload"]["contact"]["id"]=="provider-contact-1"
