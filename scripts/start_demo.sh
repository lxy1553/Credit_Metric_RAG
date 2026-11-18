#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
if [[ ! -x .venv/bin/python ]]; then
  python3.12 -m venv .venv
fi
.venv/bin/python -m pip install -e '.[dev,rag]' >/dev/null
.venv/bin/python -m credit.demo --db var/credit.db --sources var/sources >/dev/null
export CREDIT_DEMO_MODE=1 CREDIT_DB=var/credit.db CREDIT_MEMORY="${CREDIT_MEMORY:-memory}"
export CREDIT_RESPONSE_CACHE="${CREDIT_RESPONSE_CACHE:-memory}"
export CREDIT_API_WORKERS="${CREDIT_API_WORKERS:-1}"
if [[ "${CREDIT_RETRIEVAL:-elasticsearch}" == "elasticsearch" ]]; then
  docker compose up -d --wait elasticsearch >/dev/null
  .venv/bin/python -m credit.search_index --output var/index-publication.json >/dev/null
  export CREDIT_RETRIEVAL=elasticsearch CREDIT_ELASTICSEARCH_URL="${CREDIT_ELASTICSEARCH_URL:-http://127.0.0.1:9200}"
  export CREDIT_ELASTICSEARCH_INDEX="${CREDIT_ELASTICSEARCH_INDEX:-credit-knowledge-current}"
  export CREDIT_MODEL_CACHE="${CREDIT_MODEL_CACHE:-var/models}"
fi
exec .venv/bin/python -m uvicorn credit.api:from_environment --factory --workers "${CREDIT_API_WORKERS}" \
  --host 127.0.0.1 --port "${CREDIT_PORT:-8000}"
