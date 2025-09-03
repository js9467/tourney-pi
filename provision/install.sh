#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

APP_DIR="/opt/bigrock-app"
REPO_URL="https://github.com/js9467/tourney-pi.git"
APP_SERVICE="bigrock.service"
UPDATE_SERVICE="bigrock-update.service"
UPDATE_TIMER="bigrock-update.timer"

echo "[1/7] Update apt + install must-have OS packages"
sudo apt-get update
if [ -f "$PWD/provision/apt.musthave.list" ]; then
  xargs -a provision/apt.musthave.list sudo apt-get install -y
else
  # fallback minimal
  sudo apt-get install -y git python3-venv curl ca-certificates
fi

echo "[2/7] Ensure canonical path and clone/pull"
sudo mkdir -p /opt
sudo chown -R pi:pi /opt
if [ -d "$APP_DIR/.git" ]; then
  cd "$APP_DIR"
  git fetch --all --tags
  git reset --hard origin/$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)
else
  git clone "$REPO_URL" "$APP_DIR"
  sudo chown -R pi:pi "$APP_DIR"
  cd "$APP_DIR"
fi

echo "[3/7] Build/refresh venv and install Python deps"
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
if [ -s requirements.txt ]; then
  # Strip apt-only packages if someone accidentally froze them
  sed -i '/^cupshelpers==/d;/^python-apt==/d;/^dbus==/d;/^PyGObject==/d;/^RPi\.GPIO==/d' requirements.txt
  pip install -r requirements.txt
else
  pip install Flask requests beautifulsoup4 python-dateutil Pillow
fi
deactivate

echo "[4/7] Install updater script"
mkdir -p "$APP_DIR/scripts"
cat > "$APP_DIR/scripts/update.sh" <<'EOS'
#!/usr/bin/env bash
set -euo pipefail
APP_DIR="/opt/bigrock-app"
SERVICE_NAME="bigrock.service"
LOCK_FILE="/run/bigrock-update.lock"
LOG_TAG="bigrock-update"
exec >> >(logger -t "$LOG_TAG") 2>&1
[ -e "$LOCK_FILE" ] && { echo "Updater already running"; exit 0; }
trap 'rm -f "$LOCK_FILE"' EXIT
touch "$LOCK_FILE"
cd "$APP_DIR"
CURRENT=$(git rev-parse HEAD || echo "none")
git fetch --tags origin
TARGET="origin/$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)"
UPSTREAM=$(git rev-parse "$TARGET" 2>/dev/null || echo "")
[ -z "$UPSTREAM" ] && { echo "No upstream"; exit 1; }
if [ "$UPSTREAM" != "$CURRENT" ]; then
  git reset --hard "$UPSTREAM"
  if [ -f requirements.txt ] && [ -x .venv/bin/pip ]; then
    . .venv/bin/activate
    pip install --upgrade -r requirements.txt
    deactivate
  fi
  systemctl restart "$SERVICE_NAME" || true
  echo "Updated to $UPSTREAM"
else
  echo "No changes ($CURRENT)"
fi
EOS
chmod +x "$APP_DIR/scripts/update.sh"

echo "[5/7] Install systemd units"
sudo tee /etc/systemd/system/"$APP_SERVICE" >/dev/null <<'EOS'
[Unit]
Description=BigRock App (Flask)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/opt/bigrock-app
ExecStart=/opt/bigrock-app/.venv/bin/python /opt/bigrock-app/app.py
Environment=PYTHONUNBUFFERED=1
# Uncomment to silence PulseAudio warnings if headless:
# Environment=SDL_AUDIODRIVER=dummy
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOS

sudo tee /etc/systemd/system/"$UPDATE_SERVICE" >/dev/null <<'EOS'
[Unit]
Description=BigRock Updater
[Service]
Type=oneshot
# run as root so it can restart services
ExecStart=/opt/bigrock-app/scripts/update.sh
EOS

sudo tee /etc/systemd/system/"$UPDATE_TIMER" >/dev/null <<'EOS'
[Unit]
Description=Run BigRock Updater 4 times daily
[Timer]
OnCalendar=*-*-* 03,09,15,21:00:00
Persistent=true
Unit=bigrock-update.service
[Install]
WantedBy=timers.target
EOS

echo "[6/7] Enable and start services"
sudo systemctl daemon-reload
sudo systemctl enable --now "$APP_SERVICE"
sudo systemctl enable --now "$UPDATE_TIMER"

echo "[7/7] Verify"
systemctl status "$APP_SERVICE" --no-pager || true
systemctl list-timers | grep bigrock || true
echo "Installer complete."

echo "[8/7] Write deploy stamp"
COMMIT=$(git -C "$APP_DIR" rev-parse HEAD 2>/dev/null || echo "unknown")
DATE=$(date -Is)
HOST=$(hostname)
REQ_SHA=$( [ -f "$APP_DIR/requirements.txt" ] && sha256sum "$APP_DIR/requirements.txt" | awk '{print $1}' || echo "none" )

cat > "$APP_DIR/installed-ok.txt" <<EOFSTAMP
date=$DATE
host=$HOST
commit=$COMMIT
requirements_sha256=$REQ_SHA
EOFSTAMP

echo "Wrote $APP_DIR/installed-ok.txt"
