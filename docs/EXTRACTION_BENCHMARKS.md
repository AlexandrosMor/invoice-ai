# Extraction strategy benchmark

This document records the current baseline and how to make an apples-to-apples comparison. Benchmark documents and detailed reports may contain sensitive financial data. Keep customer datasets and generated reports private and outside this repository.

## Current baseline

The deterministic parser was run against the 13 manually labeled synthetic core fixtures on 2026-11-08. The run used CPython 3.9.6 on macOS 27.2 arm64; additional hardware details were not recorded.

| Metric | Deterministic text baseline |
| --- | ---: |
| Dataset size | 13 synthetic invoices |
| Overall scored field accuracy | 99.1% |
| Whole-invoice exact match | 92.3% |
| Line-item accuracy | 100% |
| Supplier / invoice number / currency accuracy | 100% each |
| Date accuracy | 92.3% |
| Critical false-accept rate | 0% |
| Manual-review rate | 30.8% |
| Processing p50 / p95 | 28.9 / 49.3 ms |
| Estimated processing cost | Not measured |

These results describe a small, purpose-built synthetic fixture set. They are not estimates of commercial or real-supplier accuracy. The deterministic parser has explicit label and table-shape rules that overlap the synthetic fixture layouts, so its scores should not be generalized.

The checked-in Qwen3-VL report covers only the original single synthetic fixture and reports 60% field accuracy and 0% whole-invoice exact match. It is **not** directly comparable to the deterministic 13-document result. A completed Qwen3-VL run on the same 13 fixtures is not available, so no model-vs-baseline improvement is claimed. Hardware-energy cost, correction counts, and real human correction time are also not measured.

## Strategies

- `ollama`: current Qwen3-VL vision extraction. This remains the default for compatibility.
- `deterministic`: conservative extraction from explicit labels and supported digital-PDF table shapes. It does not OCR scanned documents, apply supplier templates, or calculate missing facts.
- `hybrid`: uses deterministic extraction only when required fields have embedded-text evidence and the shared validator reports `READY_FOR_REVIEW`; otherwise it falls back to the full Ollama extraction. It is opt-in and is not a targeted page/crop recheck.

Document analysis currently reports page count, digital/scanned/mixed kind, embedded-text availability, a lightweight language hint, and that supplier/template identity is unknown. Supplier-specific templates and an OCR adapter for scanned documents are not implemented. Source evidence and coordinates are emitted only when extracted values match actual embedded PDF text spans.

All strategies return the canonical `Invoice` schema and pass through the existing downstream validation and duplicate checks. A valid arithmetic result is not evidence that all extracted facts are correct. No LLM self-confidence score is used.

## Run a strategy

From `backend/`, select the application strategy with `INVOICE_EXTRACTION_STRATEGY=ollama`, `deterministic`, or `hybrid`. The default is `ollama`.

```bash
python -m scripts.evaluate_invoices \
  --dataset /private/path/invoice-dataset \
  --strategy deterministic \
  --output /private/path/reports/deterministic.json
```

Dataset layout:

```text
invoice-dataset/
  invoices/       # PDF, PNG, or JPG documents
  ground_truth/   # Manually verified JSON with matching basenames
```

Each ground-truth JSON should contain only fields that were reviewed by a person. Do not generate ground truth from the strategy under evaluation. The benchmark can read private local datasets; do not commit customer documents, labels, or generated reports.

## Compare strategies on the same dataset

```bash
python -m scripts.compare_extraction_strategies \
  --dataset /private/path/invoice-dataset \
  --output-dir /private/path/reports/strategy-comparison \
  --strategies deterministic,ollama,hybrid
```

The comparison command processes the same sorted dataset once per selected strategy, then writes a detailed JSON report for each strategy and `comparison.json` with the summarized metrics. Ollama and hybrid require the configured local model and may take a long time. For a lower-cost first pass, compare only `deterministic`; do not infer model performance from that run.

## Metrics and limitations

- Field accuracy compares manually labeled fields with normalized output. Numeric fields use the evaluator's configured per-field tolerance; line items and VAT rows are compared in document order.
- Whole-invoice exact match counts invoices with no incorrect scored fields. Failed extractions count as non-matches.
- Critical false-accept rate measures critical supplier/number/total mismatches among successfully extracted invoices that pass validation. It is `null` when no eligible invoices exist.
- Manual-review rate counts validation not marked `READY_FOR_REVIEW` or missing evidence for populated critical fields.
- p50 and p95 processing latency include preprocessing plus extraction; model inference duration is reported separately.
- Estimated local processing cost remains `null`; the project does not measure power consumption or apply a machine-specific energy tariff.
- Verification-conflict detection, reusable correction suggestions and their acceptance/rejection, batch throughput, and real human correction time are not yet measured because the corresponding product workflows are not implemented.

Build a larger, manually verified and privacy-authorized dataset before using these measurements to choose a production default. The plan calls for at least 100 diverse real-world invoices when such documents can be obtained and evaluated lawfully; no such dataset is included here.
