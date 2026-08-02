#!/usr/bin/env bash
# One-shot deploy for a fresh Ubuntu 22.04/24.04 DigitalOcean droplet.
#
# Usage (on the droplet):
#   # option A — clone from GitHub (download first, then run so REPO reaches sudo):
#   curl -fsSL https://raw.githubusercontent.com/rasajdak/hrrr-portal/main/deploy/bootstrap.sh -o /tmp/bootstrap.sh
#   sudo REPO=https://github.com/rasajdak/hrrr-portal bash /tmp/bootstrap.sh
#
#   # option B — code already rsync'd to /opt/hrrr-portal:
#   sudo bash /opt/hrrr-portal/deploy/bootstrap.sh
#
# Idempotent: safe to re-run (it updates code, rebuilds env if missing, restarts).
set -euo pipefail

APP_DIR=/opt/hrrr-portal
MF_DIR=/opt/miniforge3
ENV_NAME=hrrr-portal
SVC_USER=hrrr
REPO="${REPO:-}"

echo "==> packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq nginx git curl ca-certificates

echo "==> code -> $APP_DIR"
if [ -n "$REPO" ] && [ ! -f "$APP_DIR/app.py" ]; then
  git clone --depth 1 "$REPO" "$APP_DIR"
elif [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only || true
fi
if [ ! -f "$APP_DIR/app.py" ]; then
  echo "!! No code at $APP_DIR. Set REPO=... or rsync the folder there first." >&2
  exit 1
fi

echo "==> service user"
id "$SVC_USER" &>/dev/null || useradd -r -d "$APP_DIR" -s /usr/sbin/nologin "$SVC_USER"

echo "==> miniforge + conda env ($ENV_NAME)"
if [ ! -x "$MF_DIR/bin/mamba" ]; then
  curl -fsSL -o /tmp/mf.sh \
    https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh
  bash /tmp/mf.sh -b -p "$MF_DIR"
fi
if [ ! -x "$MF_DIR/envs/$ENV_NAME/bin/gunicorn" ]; then
  "$MF_DIR/bin/mamba" env create -y -f "$APP_DIR/environment.yml"
fi

echo "==> permissions"
mkdir -p "$APP_DIR/cache" "$APP_DIR/data"
chown -R "$SVC_USER:$SVC_USER" "$APP_DIR"

echo "==> systemd service"
install -m644 "$APP_DIR/deploy/hrrr-portal.service" /etc/systemd/system/hrrr-portal.service
systemctl daemon-reload
systemctl enable --now hrrr-portal
systemctl restart hrrr-portal

echo "==> nginx reverse proxy"
install -m644 "$APP_DIR/deploy/nginx.conf" /etc/nginx/sites-available/hrrr-portal
ln -sf /etc/nginx/sites-available/hrrr-portal /etc/nginx/sites-enabled/hrrr-portal
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

IP=$(curl -fsSL https://ifconfig.me 2>/dev/null || echo "<droplet-ip>")
echo
echo "==================================================================="
echo " HRRR portal is up.  Open:  http://$IP"
echo " Logs:      journalctl -u hrrr-portal -f"
echo " Restart:   systemctl restart hrrr-portal"
echo " HTTPS:     sudo apt-get install -y certbot python3-certbot-nginx && \\"
echo "            sudo certbot --nginx -d your.domain"
echo "==================================================================="
