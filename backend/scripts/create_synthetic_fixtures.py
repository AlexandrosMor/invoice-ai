"""Create openly synthetic, non-customer invoice benchmark fixtures."""
import json
from pathlib import Path
import fitz

ROOT=Path(__file__).resolve().parents[1]/"tests"/"fixtures"
ROOT.joinpath("invoices").mkdir(parents=True,exist_ok=True);ROOT.joinpath("ground_truth").mkdir(parents=True,exist_ok=True)
page=fitz.open();p=page.new_page(width=595,height=842)
lines=["SYNTHETIC SAMPLE — NOT A REAL INVOICE","Northwind Office Supplies B.V.","Prinsengracht 100, 1015 EA Amsterdam","VAT: NL123456789B01","INVOICE","Invoice number: NW-2026-001","Invoice date: 2026-04-12","Due date: 2026-05-12","Bill to: Example Studio","Description                 Qty     Unit     VAT     Total","Paper (A4)                   2      10.00    21%     20.00","Pens (blue)                  1       5.00     9%      5.00","Subtotal: EUR 25.00","VAT 21%: EUR 4.20","VAT 9%: EUR 0.45","Total VAT: EUR 4.65","TOTAL DUE: EUR 29.65","Currency: EUR"]
for i,line in enumerate(lines):p.insert_text((52,60+i*27),line,fontsize=11)
page.save(ROOT/"invoices"/"invoice_001.pdf");page.close()
truth={"supplier_name":"Northwind Office Supplies B.V.","invoice_number":"NW-2026-001","invoice_date":"2026-04-12","due_date":"2026-05-12","currency":"EUR","subtotal":"25.00","total_vat":"4.65","invoice_total":"29.65","vat_breakdown":[{"rate":"21","taxable_amount":"20.00","tax_amount":"4.20"},{"rate":"9","taxable_amount":"5.00","tax_amount":"0.45"}],"line_items":[{"description":"Paper (A4)","quantity":"2","unit_price":"10.00","vat_rate":"21","line_total":"20.00"},{"description":"Pens (blue)","quantity":"1","unit_price":"5.00","vat_rate":"9","line_total":"5.00"}]}
(ROOT/"ground_truth"/"invoice_001.json").write_text(json.dumps(truth,indent=2)+"\n")
