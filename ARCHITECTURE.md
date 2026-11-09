# Architecture and data flow

```text
Browser or developer API → FastAPI upload/controller → local file + SQLite record
                                      ↓ background task
                    DocumentProcessor (PDF pages / image validation)
                                      ↓ document analysis
                    Extraction strategy (configured)
                    ├─ deterministic embedded-text baseline
                    ├─ hybrid: deterministic when evidenced/valid, else Ollama
                    └─ Ollama Qwen3-VL (backwards-compatible default)
                                      ↓ canonical Pydantic Invoice
                    InvoiceRepository (SQLite record) → review UI
                                      ↓ corrections
                    InvoiceValidator (deterministic checks) → approval gate
                                      ↓ explicit approval
                    JSON export OR AccountingAdapter → mock Moneybird draft
```

`DocumentProcessor` verifies image readability, extracts embedded PDF text/spans, and renders PDF pages with PyMuPDF. `analyze_document` records page count, document kind, readable-text availability, and a lightweight language hint; supplier templates are not configured and supplier identity is not treated as known by this stage. `InvoiceExtractor` sends all pages as images to local Qwen3-VL, marks document instructions as untrusted, and validates the returned JSON against the canonical Pydantic schema. `DeterministicTextStrategy` is an intentionally conservative baseline for explicit labels and common digital-PDF table shapes; it does not OCR images or invent missing amounts. `HybridExtractionStrategy` uses that baseline only when its required values have embedded-text evidence and deterministic validation returns ready for review. Otherwise it invokes the existing Qwen strategy. The default remains `ollama` until the strategies are compared on an appropriate labeled dataset.

All strategies normalize into the same Pydantic `Invoice` and use the same downstream duplicate detection and validation. Mathematical reconciliation is only one signal and does not prove extraction correctness. Normalization accepts Dutch/English written months and unambiguous numeric dates; ambiguous numeric dates stay unchanged and trigger review/fallback.

The SQLAlchemy `Record` is the local repository boundary in this small PoC. It persists status, original path, SHA-256 file hash, normalized invoice identity, canonical invoice JSON, validation, duplicate decisions, synchronization result, extraction metadata, and an optional user owner. Additive SQLite setup adds new columns and tables without dropping invoice rows or files. SQLite also stores user accounts and hashed server-side sessions. Invoice queries always filter by the authenticated user; legacy records remain unassigned until explicitly assigned with the documented command. The API layer handles HTTP validation and workflow transitions. Upload starts a background task and returns immediately. The task moves through `UPLOADED`, `PROCESSING`, and `NEEDS_REVIEW` or `FAILED`.

Authentication uses Argon2id password hashes, opaque random session tokens stored only as hashes, HttpOnly same-site cookies, CSRF token checks on writes, and a basic process-local authentication rate limit. This is a local multi-user foundation, not production identity infrastructure: email is not verified, rate limits are not shared across workers, and recovery/admin workflows are not provided.

Duplicate classification is a separate workspace-scoped service. An exact file hash is protected by a partial unique SQLite index on `(user_id, file_hash)` and repeated uploads return the existing same-user invoice reference. Invoice identity compares normalized supplier VAT, invoice number, and document type; supplier-name-only matches remain possible warnings. Confirmed duplicates block approval. Possible duplicates require a recorded reason before approval and generate an audit event. Cross-user candidates are never queried or returned.

Accounting configuration is separate from canonical invoice facts. `GLAccount`, `TaxCode`, `MappingRule`, `SupplierMapping`, and `InvoiceAccountingMapping` are user-owned records. Deterministic suggestions favor exact supplier rules, then customer rules, and remain unconfirmed; VAT defaults require explicit jurisdiction and treatment context. Manual choices are associated with stable line IDs and audited. The mock `MoneybirdAdapter` uses configured external GL and tax IDs and reports an actionable error when a mapping is missing. It never calls Moneybird or invents provider IDs.

Approval requires successful validation and no confirmed duplicate. Possible duplicates require an override reason before approval. Export and sync require `APPROVED`. Successful mock sync results are saved and subsequent requests return the prior result, preventing duplicate mock creation. No undocumented external API behavior or credentials are assumed.

The local file root is generated from application configuration, and disk names use generated UUIDs rather than user filenames. User filenames are reduced to a basename for display. File size, extension, signature, PDF page count, and image decoding are checked. The UI does not display document content in backend logs.


## Extraction engine (v2)

The incremental engine is split across `document_processing.py`, `extraction.py`, `normalization.py`, `invoice_domain.py`, and `validation.py`. PDFs are classified page by page from embedded text; all pages are rendered with PyMuPDF so mixed and scanned invoices retain visual layout. Rotated page rendering is normalized by PyMuPDF, image EXIF rotation is applied, and embedded text is passed as context without treating it as a reliable table parser. Numeric strings such as Dutch decimal-comma values and rates suffixed with `%` are normalized before canonical Pydantic validation; invalid numeric text fails extraction instead of being guessed.

The canonical invoice is stored separately from extraction metadata. Metadata includes model/pipeline versions, deterministic options, inference and total processing durations, status, page kind, evidence, and correction history. Evidence snippets and boxes are emitted only where a value is matched against actual embedded PDF spans; boxes use PDF page-point coordinates. Scanned pages receive no invented OCR evidence and require review. SQLite gets an additive `extraction_json` migration and a separate audit table. On restart, records left in `PROCESSING` are marked failed with a recovery event; they are not silently reprocessed. A conditional SQLite update claims uploaded records to prevent concurrent duplicate processing.

Validation uses `Decimal`, configurable `INVOICE_ROUNDING_TOLERANCE`, currency precision defaults, discount/fee conventions, gross-line support, and ERROR/WARNING/INFO issues. It never overwrites extracted amounts with calculated values. It runs immediately after extraction and again after a user requests validation. Warnings remain visible while explicit approval remains a separate human action. `READY_FOR_REVIEW`, `NEEDS_ATTENTION`, and `VALIDATION_FAILED` are assessment labels and do not replace persisted workflow statuses.

The benchmark command compares output to manually maintained ground truth. `scripts/compare_extraction_strategies.py` runs selected strategies against the same sorted dataset and writes detailed reports plus an aggregate comparison. The core fixture generator creates 13 synthetic invoices for VAT, credit notes, multi-page layouts, discounts, fees, gross prices, identity duplicates, and exact file re-uploads. Metrics include per-field and grouped accuracy, exact invoice match, financial/VAT/line-item accuracy, latency percentiles, duplicate precision/recall, and critical false acceptance. The deterministic baseline measured 99.1% field accuracy on this synthetic set, while Qwen has not yet been evaluated on that same dataset. Mapping suggestion correctness stays null until labeled mapping examples exist. Synthetic fixtures do not establish general-world accuracy. Keep private documents and reports outside version control.
