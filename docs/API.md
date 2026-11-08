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

## OpenAPI

FastAPI serves the interactive API documentation at `/docs` and the OpenAPI schema at `/openapi.json`. The API is versioned under `/api/v1`; the browser's existing `/api` routes remain available for the application UI.
