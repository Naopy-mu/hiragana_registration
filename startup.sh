#!/bin/sh
set -eu
exec gunicorn app.main:app \
  --worker-class uvicorn_worker.UvicornWorker \
  --bind 0.0.0.0:8000 \
  --workers 2 \
  --timeout 180 \
  --access-logfile - \
  --error-logfile -
