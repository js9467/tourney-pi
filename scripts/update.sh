#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/bigrock-app"
SERVICE_NAME="bigrock.service"
LOCK_FILE="/run/bigrock-update.lock"
LOG_TAG="bigrock-update"

exec >> >(logger -t "$LOG_TAG") 2>&1

# Avoid concurrent runs
[ -e "$LOCK_FILE" ] && { echo "Updater already running"; exit 0; }
trap 'rm -f "$LOCK_FILE"' EXIT
touch "$LOCK_FILE"

cd "$APP_DIR"

CURRENT=$(git rev-parse HEAD || echo "none")
git fetch --tags origin

TARGET="origin/$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)"
if [ -f .desired_version ]; then
  DESIRED=$(tr -d ' \t\n\r' < .desired_version || true)
  [ -n "${DESIRED:-}" ] && TARGET="$DESIRED"    # e.g., refs/tags/v1.2.3 or a commit sha
fi

UPSTREAM=$(git rev-parse "$TARGET")
if [ "$UPSTREAM" = "$CURRENT" ]; then
  echo "No changes ($CURRENT)"; exit 0
fi

git reset --hard "$UPSTREAM"
echo "Updated to $UPSTREAM"

# Python deps (we'll generate requirements.txt in Step 2)
if [ -f requirements.txt ] && [ -x .venv/bin/pip ]; then
  . .venv/bin/activate
  pip install --upgrade -r requirements.txt
  deactivate || true
fi

# Optional healthcheck (define it below)
if [ -x scripts/healthcheck.sh ]; then
  if ! scripts/healthcheck.sh; then
    echo "Healthcheck FAILED; rolling back"
    git reset --hard "$CURRENT"
    if [ -f requirements.txt ] && [ -x .venv/bin/pip ]; then
      . .venv/bin/activate
      pip install --upgrade -r requirements.txt || true
      deactivate || true
    fi
    exit 1
  fi
fi

# Restart the app if service exists
if systemctl list-unit-files | grep -q "$SERVICE_NAME"; then
  systemctl restart "$SERVICE_NAME"
  echo "Restarted $SERVICE_NAME"
fi
