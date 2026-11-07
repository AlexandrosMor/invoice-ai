# InvoiceAI

Local-first invoice extraction and review PoC. The backend stores files and invoice records locally, sends rendered pages to a locally running Ollama vision model, and only permits export and mock accounting synchronization after explicit review and approval.

## Requirements

- Python 3.12+
- Node.js 20+
- Ollama installed locally

## Start Ollama

```bash
ollama pull qwen3-vl:8b
ollama serve
```

Copy `.env.example` to `.env` and export its values in the terminal used to start the backend, or configure the variables in your shell.

## Start the backend

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

The API docs are at http://localhost:8000/docs. SQLite and uploaded documents are created in `backend/data`.

## Start the frontend

In a second terminal:

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173. Vite forwards `/api` calls to the backend.

## Workflow

1. Upload a PDF, JPG, or PNG (up to 10 MB). Extraction runs in a FastAPI background task.
2. Open the invoice, compare the source and extracted JSON, and save corrections.
3. Run deterministic validation. Resolve errors, then explicitly approve.
4. Download canonical JSON or use **Sync Moneybird** to get a mock draft result. No document is sent to Moneybird.

The editor accepts the canonical invoice JSON. The schema and null behavior are visible in the API and extraction prompt. Invalid, unsupported, oversized, and extension/content-mismatched uploads are rejected.

## Tests

```bash
cd backend
pytest -q
```

Tests do not call Ollama or download a model.

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for processing, validation, persistence, and accounting boundaries.

## Current PoC limits

- The mock Moneybird adapter does not make live API calls. Live synchronization must wait for verified official API documentation and credentials.
- The UI uses a JSON editor for flexible field correction rather than schema-specific form controls.
- PDF preview uses the browser's built-in viewer; multi-page navigation and zoom are available there.
- FastAPI background tasks are process-local and are lost if the server stops mid-inference. Replace with a durable queue before multi-user or production use.
- A single SQLite database and local file storage are intended for one local user, not concurrent deployments.
- Extraction depends on a running Ollama server and downloaded vision model; the application reports connection/inference errors as failed records.
