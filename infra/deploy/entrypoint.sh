#!/usr/bin/env bash
set -euo pipefail

echo "Running migrations..."
uv run alembic -c /app/alembic.ini upgrade head

echo "Starting dashboard on ${ASLAN_DASHBOARD_HOST:-127.0.0.1}:${ASLAN_DASHBOARD_PORT:-8585}"
exec uv run aslan dashboard serve \
    --host "${ASLAN_DASHBOARD_HOST:-0.0.0.0}" \
    --port "${ASLAN_DASHBOARD_PORT:-8585}" \
    --i-know-this-is-unsafe
