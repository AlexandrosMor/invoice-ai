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

The SQLAlchemy `Record` is the local repository boundary in this small PoC. It persists status, original path, canonical invoice JSON, validation, and synchronization result. The API layer handles HTTP validation and workflow transitions. Upload starts a background task and returns immediately. The task moves through `UPLOADED`, `PROCESSING`, and `NEEDS_REVIEW` or `FAILED`.

Approval requires a successful validation. Export and sync require `APPROVED`. The `AccountingAdapter` abstraction currently has a `MoneybirdAdapter` mock that maps canonical fields and optional customer mapping configuration into a simulated draft request. Successful sync results are saved and subsequent requests return the prior result, preventing duplicate mock creation. No undocumented external API behavior or credentials are assumed.

The local file root is generated from application configuration, and disk names use generated UUIDs rather than user filenames. User filenames are reduced to a basename for display. File size, extension, signature, PDF page count, and image decoding are checked. The UI does not display document content in backend logs.
