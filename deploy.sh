#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="frammer-backend"
APP_ROOT="/opt/frammer-backend"
RELEASES_DIR="$APP_ROOT/releases"
SHARED_DIR="$APP_ROOT/shared"
CURRENT_LINK="$APP_ROOT/current"
TIMESTAMP="$(date +%Y%m%d%H%M%S)"
RELEASE_DIR="$RELEASES_DIR/$TIMESTAMP"
SERVICE_NAME="frammer-backend"
PYTHON_BIN="python3"

mkdir -p "$RELEASES_DIR" "$SHARED_DIR"
mkdir -p "$RELEASE_DIR"

tar -xzf /tmp/frammer-release.tar.gz -C "$RELEASE_DIR"

if [ ! -f "$SHARED_DIR/.env" ]; then
  echo "Missing $SHARED_DIR/.env on server"
  exit 1
fi

if [ ! -d "$APP_ROOT/venv" ]; then
  $PYTHON_BIN -m venv "$APP_ROOT/venv"
fi

source "$APP_ROOT/venv/bin/activate"
pip install --upgrade pip wheel

if [ -f "$RELEASE_DIR/requirements-prod.txt" ]; then
  pip install -r "$RELEASE_DIR/requirements-prod.txt"
elif [ -f "$RELEASE_DIR/requirements.txt" ]; then
  pip install -r "$RELEASE_DIR/requirements.txt"
else
  echo "No requirements file found"
  exit 1
fi

ln -sfn "$SHARED_DIR/.env" "$RELEASE_DIR/.env"

if [ -f "$RELEASE_DIR/alembic.ini" ] && [ -d "$RELEASE_DIR/alembic" ]; then
  echo "Running database migrations..."
  if (cd "$RELEASE_DIR" && alembic -c alembic.ini upgrade head); then
    echo "Migrations applied successfully."
  else
    echo "WARNING: Alembic migration failed (DB may be unreachable). Continuing deployment with existing schema."
  fi
fi

ln -sfn "$RELEASE_DIR" "$CURRENT_LINK"
sudo systemctl daemon-reload
sudo cp "$RELEASE_DIR/frammer-backend.service" /etc/systemd/system/frammer-backend.service
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo "Waiting for service to start..."
for i in $(seq 1 12); do
  if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
    echo "Health check passed."
    break
  fi
  echo "Attempt $i/12: service not ready yet, waiting 5s..."
  sleep 5
  if [ "$i" -eq 12 ]; then
    echo "ERROR: Service failed to start after 60s."
    sudo systemctl status frammer-backend --no-pager || true
    sudo journalctl -u frammer-backend --no-pager -n 50 || true
    exit 1
  fi
done

find "$RELEASES_DIR" -mindepth 1 -maxdepth 1 -type d | sort | head -n -5 | xargs -r rm -rf

# ── Install / refresh cron jobs ────────────────────────────────────────────────
VENV_PYTHON="$APP_ROOT/venv/bin/python"
SCRIPTS_DIR="$CURRENT_LINK"

# Build a fresh crontab by removing any stale Frammer jobs then adding new ones
(crontab -l 2>/dev/null | grep -v "frammer-backend\|run_digest\|evaluate_alerts") \
  > /tmp/frammer_cron_tmp || true

cat >> /tmp/frammer_cron_tmp <<CRON
# frammer-backend: Leadership digest – every Monday 08:00 UTC
0 8 * * 1  cd $SCRIPTS_DIR && $VENV_PYTHON scripts/run_digest.py leadership >> /var/log/frammer-digest.log 2>&1
# frammer-backend: Ops digest – every Monday 08:05 UTC
5 8 * * 1  cd $SCRIPTS_DIR && $VENV_PYTHON scripts/run_digest.py ops >> /var/log/frammer-digest.log 2>&1
# frammer-backend: DQ digest – every Monday 08:10 UTC
10 8 * * 1  cd $SCRIPTS_DIR && $VENV_PYTHON scripts/run_digest.py dq >> /var/log/frammer-digest.log 2>&1
# frammer-backend: Client health digest – every Monday 08:15 UTC
15 8 * * 1  cd $SCRIPTS_DIR && $VENV_PYTHON scripts/run_digest.py client_health >> /var/log/frammer-digest.log 2>&1
# frammer-backend: Alert evaluation – every 15 minutes
*/15 * * * *  cd $SCRIPTS_DIR && $VENV_PYTHON scripts/evaluate_alerts.py >> /var/log/frammer-alerts.log 2>&1
CRON

crontab /tmp/frammer_cron_tmp
rm /tmp/frammer_cron_tmp
echo "Cron jobs installed."

echo "Deployment successful: $TIMESTAMP"
