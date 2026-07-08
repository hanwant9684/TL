#!/usr/bin/env bash
# One-shot VPS setup script for Telegram Viewer
# Run as root or with sudo on a fresh Ubuntu/Debian VPS
# Usage: bash setup.sh /opt/tgviewer

set -e

INSTALL_DIR="${1:-/opt/tgviewer}"

echo "==> Installing system packages..."
apt-get update -qq
apt-get install -y python3 python3-pip python3-venv nginx git

echo "==> Creating install directory: $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
mkdir -p /var/log/tgviewer

echo "==> Copying project files..."
# Assumes you're running this from the TL-Latest directory
cp -r . "$INSTALL_DIR/"

echo "==> Installing Python dependencies..."
cd "$INSTALL_DIR"
pip3 install -r requirements.txt

echo ""
echo "==> NEXT STEPS:"
echo ""
echo "  1. Create your .env file:"
echo "       cp $INSTALL_DIR/deploy/.env.example $INSTALL_DIR/.env"
echo "       nano $INSTALL_DIR/.env           # fill in API_ID, API_HASH, etc."
echo "       chmod 600 $INSTALL_DIR/.env"
echo ""
echo "  2. Install the systemd service:"
echo "       # Edit WorkingDirectory and EnvironmentFile in the service file first:"
echo "       nano $INSTALL_DIR/deploy/tgviewer.service"
echo "       cp $INSTALL_DIR/deploy/tgviewer.service /etc/systemd/system/"
echo "       systemctl daemon-reload"
echo "       systemctl enable tgviewer"
echo "       systemctl start tgviewer"
echo "       systemctl status tgviewer"
echo ""
echo "  3. Configure nginx:"
echo "       # Edit server_name in the nginx config first:"
echo "       nano $INSTALL_DIR/deploy/nginx.conf"
echo "       cp $INSTALL_DIR/deploy/nginx.conf /etc/nginx/sites-available/tgviewer"
echo "       ln -s /etc/nginx/sites-available/tgviewer /etc/nginx/sites-enabled/"
echo "       nginx -t && systemctl reload nginx"
echo ""
echo "  4. (Optional) Free HTTPS:"
echo "       apt-get install -y certbot python3-certbot-nginx"
echo "       certbot --nginx -d your-domain.com"
echo ""
echo "  5. Open your browser to http://YOUR_VPS_IP and log in with APP_PASSWORD"
echo ""
echo "Done. The app will auto-restart on reboot via systemd."
