import hashlib
import json
from pathlib import Path

from invoice_domain import Invoice
from normalization import normalize_invoice_payload
from validation import validate

ROOT=Path(__file__).parent/"fixtures"/"core"


def test_synthetic_core_fixtures_are_manually_labeled_and_reconcile():
    truths=sorted((ROOT/"ground_truth").glob("*.json"))
    assert len(truths)>=12
    ids={path.stem for path in truths}
    assert {"dutch_21","dutch_9","multiple_rates","zero_rated","reverse_charge","credit_note","multi_page","discount","shipping","gross_prices","duplicate_invoice_identity","exact_file_reupload"}<=ids
    for truth_file in truths:
        raw=json.loads(truth_file.read_text())
        invoice=Invoice.model_validate(normalize_invoice_payload({key:value for key,value in raw.items() if key not in {"sequence","duplicate_expected","duplicate_reference"}}))
        result=validate(invoice,[])
        assert result["valid"],(truth_file.name,result["errors"])
        document=ROOT/"invoices"/(truth_file.stem+".pdf")
        assert document.is_file() and document.stat().st_size>100
    first=hashlib.sha256((ROOT/"invoices"/"dutch_21.pdf").read_bytes()).hexdigest()
    duplicate=hashlib.sha256((ROOT/"invoices"/"exact_file_reupload.pdf").read_bytes()).hexdigest()
    assert first==duplicate


def test_synthetic_duplicate_labels_and_multi_page_fixture():
    identity=json.loads((ROOT/"ground_truth"/"duplicate_invoice_identity.json").read_text())
    exact=json.loads((ROOT/"ground_truth"/"exact_file_reupload.json").read_text())
    assert identity["duplicate_expected"]=="EXACT_INVOICE_DUPLICATE" and identity["duplicate_reference"]=="dutch_21"
    assert exact["duplicate_expected"]=="EXACT_FILE_DUPLICATE" and exact["duplicate_reference"]=="dutch_21"
    from document_processing import DocumentProcessor
    document=DocumentProcessor.process(ROOT/"invoices"/"multi_page.pdf","application/pdf")
    assert len(document.pages)==2
