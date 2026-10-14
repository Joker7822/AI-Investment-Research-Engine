#!/bin/sh
set -eu

PORT_VALUE="${PORT:-8000}"
WORKERS_VALUE="${WEB_CONCURRENCY:-1}"

echo "Starting Investment Research API on 0.0.0.0:${PORT_VALUE} with ${WORKERS_VALUE} worker(s)"
exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT_VALUE}" \
  --workers "${WORKERS_VALUE}" \
  --proxy-headers \
  --forwarded-allow-ips="${FORWARDED_ALLOW_IPS:-*}"
