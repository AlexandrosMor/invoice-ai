#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

export OLLAMA_TIMEOUT="${OLLAMA_TIMEOUT:-600}"
export OLLAMA_RETRIES="${OLLAMA_RETRIES:-0}"

exec uvicorn app:app --reload --port "${PORT:-8000}"
