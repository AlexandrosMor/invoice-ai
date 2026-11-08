"""Generate manually labeled synthetic documents for core invoice scenarios."""
import json
from pathlib import Path
import fitz

ROOT=Path(__file__).resolve().parents[1]/"tests"/"fixtures"/"core"

def item(description,quantity,unit,rate,total,**extra):
    return {"description":description,"quantity":str(quantity),"unit_price":str(unit),"vat_rate":str(rate),"line_total":str(total),**extra}

CASES=[
 {"id":"dutch_21","name":"Noordwind Kantoor B.V.","vat":"NL123456789B01","number":"NL-21001","date":"2026-08-01","date_text":"1 augustus 2026","due_date":"2026-08-31","due_date_text":"31 augustus 2026","currency":"EUR","subtotal":"100.00","vat_total":"21.00","total":"121.00","breakdown":[{"rate":"21","taxable_amount":"100.00","tax_amount":"21.00"}],"lines":[item("Office supplies",2,"50.00",21,"100.00")]},
 {"id":"dutch_9","name":"De Bloemenwinkel","vat":"NL987654321B01","number":"NL-9002","date":"2026-08-02","date_text":"02/08/2026","due_date":"2026-09-01","due_date_text":"01/09/2026","currency":"EUR","subtotal":"50.00","vat_total":"4.50","total":"54.50","breakdown":[{"rate":"9","taxable_amount":"50.00","tax_amount":"4.50"}],"lines":[item("Books and printed materials",5,"10.00",9,"50.00")]},
 {"id":"multiple_rates","name":"Northwind Office Supplies B.V.","vat":"NL123456789B01","number":"NW-26003","date":"2026-08-03","currency":"EUR","subtotal":"125.00","vat_total":"23.25","total":"148.25","breakdown":[{"rate":"9","taxable_amount":"25.00","tax_amount":"2.25"},{"rate":"21","taxable_amount":"100.00","tax_amount":"21.00"}],"lines":[item("Books",5,"5.00",9,"25.00"),item("Office supplies",2,"50.00",21,"100.00")]},
 {"id":"zero_rated","name":"Export Parts BV","vat":"NL123450000B01","number":"EX-26004","date":"2026-08-04","currency":"EUR","subtotal":"50.00","vat_total":"0.00","total":"50.00","breakdown":[{"rate":"0","taxable_amount":"50.00","tax_amount":"0.00","treatment":"zero_rated"}],"lines":[item("Export goods",1,"50.00",0,"50.00",vat_treatment="zero_rated")]},
 {"id":"reverse_charge","name":"EU Cloud Services GmbH","vat":"DE123456789","number":"DE-26005","date":"2026-08-05","currency":"EUR","subtotal":"200.00","vat_total":"0.00","total":"200.00","breakdown":[{"rate":"0","taxable_amount":"200.00","tax_amount":"0.00","treatment":"reverse_charge"}],"lines":[item("Cloud services",1,"200.00",0,"200.00",vat_treatment="reverse_charge")]},
 {"id":"credit_note","name":"Northwind Office Supplies B.V.","vat":"NL123456789B01","number":"CN-26006","date":"2026-08-06","currency":"EUR","document_type":"credit_note","subtotal":"-100.00","vat_total":"-21.00","total":"-121.00","breakdown":[{"rate":"21","taxable_amount":"-100.00","tax_amount":"-21.00"}],"lines":[item("Returned office supplies",-2,"50.00",21,"-100.00")]},
 {"id":"multi_page","name":"Pagewise Supplies BV","vat":"NL101010101B01","number":"MP-26007","date":"2026-08-07","currency":"EUR","subtotal":"60.00","vat_total":"12.60","total":"72.60","breakdown":[{"rate":"21","taxable_amount":"60.00","tax_amount":"12.60"}],"lines":[item("Page one item A",1,"20.00",21,"20.00"),item("Page one item B",1,"20.00",21,"20.00"),item("Page two item C",1,"20.00",21,"20.00")],"page_break_after":2},
 {"id":"discount","name":"Discounted Stationery BV","vat":"NL202020202B01","number":"DS-26008","date":"2026-08-08","currency":"EUR","subtotal":"100.00","discount_total":"10.00","vat_total":"18.90","total":"108.90","breakdown":[{"rate":"21","taxable_amount":"90.00","tax_amount":"18.90"}],"lines":[item("Paper cartons",2,"50.00",21,"90.00",discount="10.00")]},
 {"id":"shipping","name":"Freight and Goods BV","vat":"NL303030303B01","number":"FG-26009","date":"2026-08-09","currency":"EUR","subtotal":"100.00","additional_charges":"15.00","vat_total":"21.00","total":"136.00","breakdown":[{"rate":"21","taxable_amount":"100.00","tax_amount":"21.00"}],"lines":[item("Packing boxes",1,"100.00",21,"100.00")]},
 {"id":"gross_prices","name":"Gross Price Trading BV","vat":"NL404040404B01","number":"GP-26010","date":"2026-08-10","currency":"EUR","subtotal":"100.00","vat_total":"21.00","total":"121.00","breakdown":[{"rate":"21","taxable_amount":"100.00","tax_amount":"21.00"}],"lines":[item("Gross listed item",1,"121.00",21,"100.00",gross_total="121.00",price_includes_vat=True)]},
 {"id":"same_supplier_distinct_invoice","name":"Northwind Office Supplies B.V.","vat":"NL123456789B01","number":"NW-26011","date":"2026-08-11","currency":"EUR","subtotal":"100.00","vat_total":"21.00","total":"121.00","breakdown":[{"rate":"21","taxable_amount":"100.00","tax_amount":"21.00"}],"lines":[item("Office supplies",2,"50.00",21,"100.00")]},
 {"id":"duplicate_invoice_identity","name":"Noordwind Kantoor B.V.","vat":"NL123456789B01","number":"NL-21001","date":"2026-08-01","date_text":"1 augustus 2026","currency":"EUR","subtotal":"100.00","vat_total":"21.00","total":"121.00","breakdown":[{"rate":"21","taxable_amount":"100.00","tax_amount":"21.00"}],"lines":[item("Office supplies",2,"50.00",21,"100.00")]},
]

def create_case(case):
    pages=fitz.open();page=pages.new_page(width=595,height=842);y=54
    text=["SYNTHETIC SAMPLE — NOT A REAL INVOICE",case["name"],f"VAT: {case['vat']}","Credit note" if case.get("document_type")=="credit_note" else "INVOICE",f"Invoice number: {case['number']}",f"Invoice date: {case.get('date_text',case['date'])}"]
    if case.get("due_date_text"):text.append(f"Payment due date: {case['due_date_text']}")
    text.extend(["Bill to: Example Studio","Customer country: Netherlands","Supplier address: Examplelaan 12, Amsterdam","Customer address: Teststraat 2, Utrecht"])
    for line in text:page.insert_text((48,y),line,fontsize=11);y+=24
    for index,line in enumerate(case["lines"]):
        if case.get("page_break_after")==index:
            page=pages.new_page(width=595,height=842);page.insert_text((48,54),"SYNTHETIC SAMPLE — CONTINUED INVOICE TABLE",fontsize=11);y=90
        description=line["description"];page.insert_text((48,y),f"{description} | Qty {line['quantity']} | Unit {line['unit_price']} | VAT {line['vat_rate']}% | Net {line['line_total']}",fontsize=10);y+=24
        if line.get("discount"):page.insert_text((60,y),f"Line discount: {line['discount']}",fontsize=9);y+=20
        if line.get("vat_treatment"):page.insert_text((60,y),f"Tax treatment: {line['vat_treatment'].replace('_',' ')}",fontsize=9);y+=20
    for key,label in (("subtotal","Subtotal"),("discount_total","Discount total"),("additional_charges","Shipping / fees")):
        if key in case:page.insert_text((48,y),f"{label}: {case['currency']} {case[key]}",fontsize=10);y+=22
    for tax in case["breakdown"]:page.insert_text((48,y),f"VAT {tax['rate']}% ({tax.get('treatment','standard')}): taxable {tax['taxable_amount']} tax {tax['tax_amount']}",fontsize=10);y+=22
    page.insert_text((48,y),f"Total VAT: {case['currency']} {case['vat_total']}",fontsize=10);y+=22
    page.insert_text((48,y),f"Amount due: {case['currency']} {case['total']}",fontsize=11)
    out=ROOT/"invoices"/(case["id"]+".pdf");pages.save(out);pages.close()
    truth={"supplier_name":case["name"],"supplier_vat_number":case["vat"],"supplier_address":"Examplelaan 12, Amsterdam","customer_name":"Example Studio","customer_address":"Teststraat 2, Utrecht","customer_country":"Netherlands","invoice_number":case["number"],"invoice_date":case["date"],"due_date":case.get("due_date"),"currency":case["currency"],"document_type":case.get("document_type","invoice"),"subtotal":case["subtotal"],"discount_total":case.get("discount_total"),"additional_charges":case.get("additional_charges"),"total_vat":case["vat_total"],"invoice_total":case["total"],"amount_due":case["total"],"vat_breakdown":case["breakdown"],"line_items":case["lines"],"sequence":case.get("sequence",0),"duplicate_expected":case.get("duplicate_expected","NO_DUPLICATE"),"duplicate_reference":case.get("duplicate_reference")}
    (ROOT/"ground_truth"/(case["id"]+".json")).write_text(json.dumps(truth,indent=2,ensure_ascii=False)+"\n")

def main():
    (ROOT/"invoices").mkdir(parents=True,exist_ok=True);(ROOT/"ground_truth").mkdir(parents=True,exist_ok=True)
    for sequence,case in enumerate(CASES):
        case["sequence"]=sequence
        if case["id"]=="duplicate_invoice_identity":case.update({"duplicate_expected":"EXACT_INVOICE_DUPLICATE","duplicate_reference":"dutch_21"})
        create_case(case)
    duplicate=ROOT/"invoices"/"exact_file_reupload.pdf";duplicate.write_bytes((ROOT/"invoices"/"dutch_21.pdf").read_bytes())
    truth=json.loads((ROOT/"ground_truth"/"dutch_21.json").read_text());truth.update({"sequence":len(CASES),"duplicate_expected":"EXACT_FILE_DUPLICATE","duplicate_reference":"dutch_21"});(ROOT/"ground_truth"/"exact_file_reupload.json").write_text(json.dumps(truth,indent=2)+"\n")
    print(f"Created {len(CASES)+1} manually labeled synthetic invoices in {ROOT}")

if __name__=="__main__":main()
