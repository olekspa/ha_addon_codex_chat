#!/usr/bin/with-contenv bash
set -euo pipefail

export PYTHONUNBUFFERED=1
exec /opt/venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8099 --app-dir /app
