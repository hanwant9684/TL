#!/usr/bin/env bash
# VPS setup script for Telegram Viewer
# Run from the directory CONTAINING TL-Latest/, not from inside it.
#
# Usage:
#   scp -r TL-Latest/ user@VPS_IP:/tmp/tgviewer-src
#   ssh user@VPS_IP
#   sudo bash /tmp/tgviewer-src/deploy/setup.sh [INSTALL_DIR]
#
# Default install directory: /opt/tgviewer
# The script copies the project there, creates a venv, sets permissions.

set -euo pipefail

INSTALL_DIR="${1:-/opt/tgviewer}"
SERVICE_USER="tgviewer"
LOG_DIR="/var/log/tgviewer"

# Resolve the directory this script lives in (i.e. TL-Latest/deploy/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Project root is one level up from deploy/
PROJECT_SRC="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: Please run as root (sudo bash deploy/setup.sh)" >&2
    exit 1
fi

if [ "$PROJECT_SRC" = "$INSTALL_DIR" ]; then
    echo "ERROR: Source and destination are the same directory ($INSTALL_DIR)." >&2
    echo "       Copy the project to a temp location first:" >&2
    echo "       scp -r TL-Latest/ user@VPS:/tmp/tgviewer-src" >&2
    exit 1
fi

echo "==> Installing system packages..."
apt-get update -qq
apt-get install -y python3 python3-venv nginx

echo "==> Creating service user: $SERVICE_USER"
id "$SERVICE_USER" &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"

echo "==> Installing project to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
rsync -a --delete "$PROJECT_SRC/" "$INSTALL_DIR/"

echo "==> Creating Python virtual environment..."
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

echo "==> Creating runtime directories..."
mkdir -p "$LOG_DIR"
mkdir -p "$INSTALL_DIR/preserved_media"
mkdir -p "$INSTALL_DIR/server_downloads"
mkdir -p "$INSTALL_DIR/server_exports"
mkdir -p "$INSTALL_DIR/instance"

echo "==> Setting ownership..."
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$LOG_DIR"

echo ""
echo "==> NEXT STEPS:"
echo ""
echo "  1. Create your .env file:"
echo "       sudo cp $INSTALL_DIR/deploy/.env.example $INSTALL_DIR/.env"
echo "       sudo nano $INSTALL_DIR/.env           # fill in API_ID, API_HASH, etc."
echo "       sudo chmod 600 $INSTALL_DIR/.env"
echo "       sudo chown $SERVICE_USER:$SERVICE_USER $INSTALL_DIR/.env"
echo ""
echo "  2. Install the systemd service:"
echo "       sudo cp $INSTALL_DIR/deploy/tgviewer.service /etc/systemd/system/"
echo "       sudo systemctl daemon-reload"
echo "       sudo systemctl enable tgviewer"
echo "       sudo systemctl start tgviewer"
echo "       sudo systemctl status tgviewer"
echo ""
echo "  3. Configure nginx:"
echo "       # Edit server_name in the nginx config:"
echo "       sudo nano $INSTALL_DIR/deploy/nginx.conf"
echo "       sudo cp $INSTALL_DIR/deploy/nginx.conf /etc/nginx/sites-available/tgviewer"
echo "       sudo ln -sf /etc/nginx/sites-available/tgviewer /etc/nginx/sites-enabled/"
echo "       sudo nginx -t && sudo systemctl reload nginx"
echo ""
echo "  4. (Optional) Free HTTPS:"
echo "       sudo apt-get install -y certbot python3-certbot-nginx"
echo "       sudo certbot --nginx -d your-domain.com"
echo ""
echo "  5. Open your browser to http://YOUR_VPS_IP and log in with APP_PASSWORD"
echo ""
echo "Done. The app will auto-restart on reboot via systemd."
