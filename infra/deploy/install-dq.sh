#!/usr/bin/env sh
# install-dq.sh — idempotent dq subsystem installer for Hetzner.
#
# Run from /home/emersus/aslan-core after `git pull`:
#
#   bash infra/deploy/install-dq.sh
#
# Re-running is safe: cron entries are de-duplicated, builds are
# cached, services are reconciled to "up", smoke is best-effort.
#
# Native deps (WeasyPrint) are baked into the docker image, so this
# script does NOT call `sudo apt install`. If you want the host CLI
# (`uv run aslan ...`) to render PDFs too, run the apt step from the
# deploy runbook once.
set -eu

# Resolve repo root (the parent of infra/deploy where this script lives).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

COMPOSE="docker compose -f infra/deploy/docker-compose.yml"

echo "==> Verifying .env exists and has dq vars set..."
if [ ! -f infra/deploy/.env ]; then
    echo "ERROR: infra/deploy/.env not found. Copy .env.example and fill values." >&2
    exit 1
fi
# shellcheck disable=SC1091
. ./infra/deploy/.env
for v in DQ_GLITCHTIP_DSN DQ_ALERT_SMTP_URL DQ_ALERT_TO DQ_SLACK_WEBHOOK_URL ASLAN_PUBLIC_STATUS_DSN; do
    eval val="\${$v:-}"
    if [ -z "${val:-}" ]; then
        echo "WARN: $v is empty; corresponding sink/feature will be suppressed."
    fi
done

echo "==> Building dq images..."
$COMPOSE build dq-alert-dispatch public-status

echo "==> Applying migrations through head (via dashboard container)..."
$COMPOSE run --rm dashboard /app/.venv/bin/aslan migrate up || {
    echo "WARN: migrate via dashboard failed; trying alembic directly..."
    $COMPOSE run --rm dashboard uv run alembic -c /app/alembic.ini upgrade head
}

echo "==> Installing cron entries (idempotent)..."
# Strip any existing dq cron lines (header + any aslan audit invocation)
# to avoid duplicates, then append the canonical fragment.
TMP_BEFORE="$(mktemp)"
trap 'rm -f "$TMP_BEFORE"' EXIT
crontab -l 2>/dev/null \
    | grep -v '^# dq subsystem cron' \
    | grep -v 'aslan audit' \
    > "$TMP_BEFORE" || true
cat "$TMP_BEFORE" infra/deploy/cron.dq | crontab -
echo "    crontab updated; verify with: crontab -l | grep -E 'aslan audit|dq subsystem'"

echo "==> Starting services..."
$COMPOSE up -d dq-alert-dispatch public-status

echo "==> Smoke testing (best-effort; non-fatal failures are warnings)..."
$COMPOSE exec -T dashboard /app/.venv/bin/aslan audit test-alert --sink glitchtip \
    || echo "WARN: glitchtip smoke failed (DSN unset or unreachable?)"
$COMPOSE exec -T dashboard /app/.venv/bin/aslan audit recency-sweep \
    || echo "WARN: recency-sweep smoke failed"

# Verify the public-status HTTP endpoint is up.
sleep 2
if command -v curl >/dev/null 2>&1; then
    if curl -fsS -o /dev/null http://127.0.0.1:8081/status; then
        echo "    public-status responded 200 on 127.0.0.1:8081/status"
    else
        echo "WARN: public-status did not respond on 127.0.0.1:8081/status"
    fi
fi

echo "==> Done. Check /tmp/dq-*.log for cron output once the next tick fires."
