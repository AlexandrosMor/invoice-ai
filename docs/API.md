# InvoiceAI Developer API

The versioned REST API lets you upload invoices and read extraction results. It uses scoped API keys, and every invoice request is restricted to the account that created the key. The interactive OpenAPI reference is available at `/docs` while the backend is running.

## Create an API key

Sign in and open **My Profile → API Keys**. Create a key with only the scopes your integration needs. The secret is displayed once; copy it to a secret manager or environment variable. InvoiceAI stores only a hash, so a lost secret must be revoked and replaced.

Available scopes:

| Scope | Permission |
| --- | --- |
| `invoices:read` | List invoices, inspect status/details, and read extracted invoice JSON |
| `invoices:write` | Upload invoices |
| `invoices:document:read` | Download the original uploaded document |

Send the key on each API request:

```bash
export INVOICEAI_API_KEY="invai_…"
export INVOICEAI_URL="http://localhost:8000"
curl "$INVOICEAI_URL/api/v1/invoices" \
  -H "Authorization: Bearer $INVOICEAI_API_KEY"
```

Keep keys out of source code, browser storage, and logs. Use HTTPS outside local development. Keys can be listed (metadata only) or revoked under **My Profile → API Keys**. The session-only management endpoints are `GET` and `POST /api/profile/api-keys` and `DELETE /api/profile/api-keys/{key_id}`; state-changing requests require the browser session's CSRF token.

## Upload and process an invoice

Upload one PDF, JPG, or PNG (maximum 10 MB). The API accepts the file, queues the existing extraction pipeline, and returns `202 Accepted`; extraction is asynchronous.

```bash
curl -X POST "$INVOICEAI_URL/api/v1/invoices" \
  -H "Authorization: Bearer $INVOICEAI_API_KEY" \
  -F "file=@invoice.pdf"
```

Example response:

```json
{
  "id": 42,
  "status": "queued",
  "message": "Invoice accepted for processing.",
  "status_url": "/api/v1/invoices/42"
}
```

Poll the status endpoint with a modest delay between requests. When status is `completed`, read the canonical extraction JSON. Do not repeat the upload just because extraction is still running; that creates another invoice.

```bash
curl "$INVOICEAI_URL/api/v1/invoices/42" \
  -H "Authorization: Bearer $INVOICEAI_API_KEY"

curl "$INVOICEAI_URL/api/v1/invoices/42/json" \
  -H "Authorization: Bearer $INVOICEAI_API_KEY"
```

The invoice JSON is extraction data and is not implicitly approved. Approval remains an explicit review action in the application.

## Endpoints

| Method and path | Scope | Description |
| --- | --- | --- |
| `POST /api/v1/invoices` | `invoices:write` | Upload and queue an invoice |
| `GET /api/v1/invoices` | `invoices:read` | List and filter the caller's invoices |
| `GET /api/v1/invoices/{invoice_id}` | `invoices:read` | Read status and safe summary fields |
| `GET /api/v1/invoices/{invoice_id}/json` | `invoices:read` | Read canonical invoice data after successful extraction |
| `GET /api/v1/invoices/{invoice_id}/document` | `invoices:document:read` | Download the original PDF or image |

### List and filter invoices

The list endpoint uses cursor pagination and sorts by newest first. `limit` defaults to 20 and must be between 1 and 100. Pass the returned `next_cursor` as `cursor` to fetch the next page. Optional `status` filters by invoice status; `search` matches invoice filenames and extracted JSON.

```bash
curl --get "$INVOICEAI_URL/api/v1/invoices" \
  -H "Authorization: Bearer $INVOICEAI_API_KEY" \
  --data-urlencode "limit=20" \
  --data-urlencode "status=completed" \
  --data-urlencode "search=acme"
```

Each list result includes its ID, filename, status, creation/update timestamps, and supplier/number/total/currency when available. The response includes `items` and `next_cursor` (`null` when there are no more results).

The invoice detail response includes an `extraction` object with the selected `strategy` (`ollama`, `deterministic`, or `hybrid`), the actual `strategy_selected` (`ollama` or `deterministic`), model name, duration, and a fallback reason when hybrid extraction had to use Ollama. These fields are additive; the canonical invoice JSON schema is unchanged.

### Status and extraction errors

Status values include `queued`, `processing`, `completed`, and `failed`. The detail endpoint provides a safe error summary for failed jobs. The JSON endpoint returns `409 Conflict` while extraction is queued or processing and when extraction failed; check the status endpoint before retrying.

## Errors and limits

Errors use a JSON `detail` field. Common HTTP statuses:

| Status | Meaning |
| --- | --- |
| `401` | API key missing, malformed, expired, revoked, or unknown |
| `403` | Key is missing the required scope |
| `404` | Invoice is unavailable to this key, or its document is missing |
| `409` | Invoice extraction is not ready or failed |
| `413` | Upload exceeds 10 MB |
| `415` | Unsupported file type or content does not match the filename |
| `422` | Invalid query or request parameters |
| `429` | API key exceeded the configured request rate |

The default limit is 120 requests per API key per minute. Set `API_RATE_LIMIT_PER_MINUTE` to change it. The current limiter stores counters in process memory, so deployments with multiple backend workers enforce the limit per worker. API keys can optionally be configured to expire when created; revoking a key takes effect immediately.

## Duplicate review and accounting mappings

The browser endpoints remain session-authenticated and organization-scoped. A session's active workspace is checked against active membership on every request. `POST /api/organizations/active` may select only a workspace the signed-in user belongs to. API keys are bound to one organization when created and cannot select a workspace.

| Method and path | Description |
| --- | --- |
| `GET /api/invoices/{id}/duplicates` | Return classification and safe summaries for matching invoices in the same workspace |
| `POST /api/invoices/{id}/duplicates/override` | Record a reason to proceed after a possible duplicate warning; confirmed duplicates cannot be overridden |
| `GET/POST/PATCH/DELETE /api/accounting/gl-accounts` | Manage user-configured GL accounts and categories; list supports `active` and `search` filters |
| `GET/POST/PATCH/DELETE /api/accounting/tax-codes` | Manage jurisdiction, transaction, treatment, rate, and optional provider ID; list supports active, jurisdiction, and transaction filters |
| `GET/POST /api/accounting/mapping-rules` | List or add supplier and description matching rules |
| `PATCH/DELETE /api/accounting/mapping-rules/{id}` | Update or remove a rule |
| `GET/POST /api/accounting/suppliers` | List or configure explicit external supplier/contact IDs |
| `GET /api/invoices/{id}/accounting-mapping` | Read the separate invoice accounting decision record |
| `POST /api/invoices/{id}/accounting-mapping/suggest` | Calculate deterministic, unconfirmed suggestions |
| `PATCH /api/invoices/{id}/accounting-mapping` | Save user-confirmed GL and tax code selections |

GL accounts, tax codes, supplier references, and rules are private to the signed-in user. Suggestions match exact supplier rules before general customer rules; tax defaults require an explicit jurisdiction and matching purchase VAT treatment. A percentage by itself is not enough. Mapping decisions are stored separately from the canonical invoice JSON and changes are audited. The mock Moneybird adapter only returns a successful draft when every line has a confirmed GL and tax mapping with explicitly configured external IDs; it never calls Moneybird.

An identical file upload within one workspace returns `409 EXACT_FILE_DUPLICATE` with the existing invoice ID. Uploading the same bytes into a different organization is allowed. Exact invoice identity (matching supplier VAT ID, invoice number, and document type) is confirmed and blocks approval. A supplier-name-only or similar document match is a possible duplicate warning; review the linked invoice and record an override reason when appropriate.

## OpenAPI

FastAPI serves the interactive API documentation at `/docs` and the OpenAPI schema at `/openapi.json`. The API is versioned under `/api/v1`; the browser's existing `/api` routes remain available for the application UI.
