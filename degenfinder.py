#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# One-time setup script for a fresh Ubuntu DigitalOcean droplet.
# Run as root (or with sudo) on the droplet:
#
#   curl -O https://raw.githubusercontent.com/<you>/<repo>/main/setup_droplet.sh
#   chmod +x setup_droplet.sh
#   ./setup_droplet.sh
#
# Edit the CONFIG section below first (repo URL, telegram token/chat id).
# ---------------------------------------------------------------------------
set -euo pipefail

# ── CONFIG ──────────────────────────────────────────────────────────────
REPO_URL="https://github.com/<you>/<repo>.git"
APP_DIR="/opt/degenfinder"
TELEGRAM_BOT_TOKEN="REPLACE_ME"
TELEGRAM_CHAT_ID="REPLACE_ME"
SCAN_INTERVAL_MIN=15     # how often to run a fresh scan
TRACK_INTERVAL_MIN=5     # how often to check the watchlist for exit signals
RUN_AS_USER="degenfinder"

# ── SYSTEM DEPS ─────────────────────────────────────────────────────────
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git cron

# ── APP USER (don't run as root) ───────────────────────────────────────
id -u "$RUN_AS_USER" &>/dev/null || useradd -r -m -s /bin/bash "$RUN_AS_USER"

# ── CLONE / UPDATE REPO ─────────────────────────────────────────────────
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" pull
else
    git clone "$REPO_URL" "$APP_DIR"
fi
chown -R "$RUN_AS_USER":"$RUN_AS_USER" "$APP_DIR"

# ── VENV + DEPS ──────────────────────────────────────────────────────────
sudo -u "$RUN_AS_USER" python3 -m venv "$APP_DIR/venv"
sudo -u "$RUN_AS_USER" "$APP_DIR/venv/bin/pip" install --upgrade pip -q
sudo -u "$RUN_AS_USER" "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q

# ── ENV FILE (keeps secrets out of crontab / git) ───────────────────────
cat > "$APP_DIR/.env" <<EOF
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID}
EOF
chown "$RUN_AS_USER":"$RUN_AS_USER" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"

# ── WRAPPER SCRIPT (loads .env, then runs python) ───────────────────────
cat > "$APP_DIR/run.sh" <<'EOF'
#!/usr/bin/env bash
cd "$(dirname "$0")"
set -a
source ./.env
set +a
./venv/bin/python3 degenfinder.py "$@" >> logs/degenfinder.log 2>&1
EOF
chmod +x "$APP_DIR/run.sh"
mkdir -p "$APP_DIR/logs"
chown -R "$RUN_AS_USER":"$RUN_AS_USER" "$APP_DIR/logs"

# ── CRON JOBS (scan + track on separate cadences) ───────────────────────
CRON_FILE="/etc/cron.d/degenfinder"
cat > "$CRON_FILE" <<EOF
*/${SCAN_INTERVAL_MIN} * * * * ${RUN_AS_USER} ${APP_DIR}/run.sh
*/${TRACK_INTERVAL_MIN} * * * * ${RUN_AS_USER} ${APP_DIR}/run.sh --track
EOF
chmod 644 "$CRON_FILE"
systemctl restart cron

echo "Done. Scans run every ${SCAN_INTERVAL_MIN}min, tracking every ${TRACK_INTERVAL_MIN}min."
echo "Logs: ${APP_DIR}/logs/degenfinder.log"
echo "Test manually with: sudo -u ${RUN_AS_USER} ${APP_DIR}/run.sh"
