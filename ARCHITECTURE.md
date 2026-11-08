# Architecture and data flow

```text
Browser upload → FastAPI upload/controller → local file + SQLite record
                                      ↓ background task
                    DocumentProcessor (PDF pages / image validation)
                                      ↓ page images
                    InvoiceExtractor (Ollama vision, schema-constrained JSON)
                                      ↓ canonical Pydantic Invoice
                    InvoiceRepository (SQLite record) → review UI
                                      ↓ corrections
                    InvoiceValidator (deterministic checks) → approval gate
                                      ↓ explicit approval
                    JSON export OR AccountingAdapter → mock Moneybird draft
```

`DocumentProcessor` verifies image readability and renders PDF pages with PyMuPDF. `InvoiceExtractor` sends all pages as images, marks document instructions as untrusted, and validates the returned JSON against the canonical Pydantic schema. `InvoiceValidator` is independent of model output and reports missing required fields, invalid dates, arithmetic mismatches, duplicate supplier/invoice identities, and currency omissions.

The SQLAlchemy `Record` is the local repository boundary in this small PoC. It persists status, original path, canonical invoice JSON, validation, synchronization result, extraction metadata, and an optional user owner. SQLite also stores user accounts and hashed server-side sessions. Invoice queries always filter by the authenticated user; legacy records remain unassigned until explicitly assigned with the documented command. The API layer handles HTTP validation and workflow transitions. Upload starts a background task and returns immediately. The task moves through `UPLOADED`, `PROCESSING`, and `NEEDS_REVIEW` or `FAILED`.

Authentication uses Argon2id password hashes, opaque random session tokens stored only as hashes, HttpOnly same-site cookies, CSRF token checks on writes, and a basic process-local authentication rate limit. This is a local multi-user foundation, not production identity infrastructure: email is not verified, rate limits are not shared across workers, and recovery/admin workflows are not provided.

Approval requires a successful validation. Export and sync require `APPROVED`. The `AccountingAdapter` abstraction currently has a `MoneybirdAdapter` mock that maps canonical fields and optional customer mapping configuration into a simulated draft request. Successful sync results are saved and subsequent requests return the prior result, preventing duplicate mock creation. No undocumented external API behavior or credentials are assumed.

The local file root is generated from application configuration, and disk names use generated UUIDs rather than user filenames. User filenames are reduced to a basename for display. File size, extension, signature, PDF page count, and image decoding are checked. The UI does not display document content in backend logs.


## Extraction engine (v2)

The incremental engine is split across `document_processing.py`, `extraction.py`, `normalization.py`, `invoice_domain.py`, and `validation.py`. PDFs are classified page by page from embedded text; all pages are rendered with PyMuPDF so mixed and scanned invoices retain visual layout. Rotated page rendering is normalized by PyMuPDF, image EXIF rotation is applied, and embedded text is passed as context without treating it as a reliable table parser. Numeric strings such as Dutch decimal-comma values and rates suffixed with `%` are normalized before canonical Pydantic validation; invalid numeric text fails extraction instead of being guessed.

The canonical invoice is stored separately from extraction metadata. Metadata includes model/pipeline versions, deterministic options, inference and total processing durations, status, page kind, evidence, and correction history. Evidence snippets and boxes are emitted only where a value is matched against actual embedded PDF spans; boxes use PDF page-point coordinates. Scanned pages receive no invented OCR evidence and require review. SQLite gets an additive `extraction_json` migration and a separate audit table. On restart, records left in `PROCESSING` are marked failed with a recovery event; they are not silently reprocessed. A conditional SQLite update claims uploaded records to prevent concurrent duplicate processing.

Validation uses `Decimal`, configurable `INVOICE_ROUNDING_TOLERANCE`, and ERROR/WARNING/INFO issues. It runs immediately after extraction and again after a user requests validation. Warnings remain visible while explicit approval remains a separate human action. `READY_FOR_REVIEW`, `NEEDS_ATTENTION`, and `VALIDATION_FAILED` are assessment labels and do not replace persisted workflow statuses.

The benchmark command invokes the configured local model and compares output to manually maintained ground truth. It writes measured results to `tests/fixtures/results.json`; absence of this file is the true empty state in the Evaluation page. See README for metric and null-handling definitions. The included dataset contains one synthetic invoice only and provides no evidence of general-world accuracy.
