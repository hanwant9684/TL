# VPS Deployment Guide

## Files in this folder

| File | Purpose |
|------|---------|
| `.env.example` | Template for all required environment variables |
| `tgviewer.service` | Systemd unit — keeps the app running 24/7, auto-restarts on crash/reboot |
| `nginx.conf` | Nginx reverse proxy — cleaner URLs, optional HTTPS |
| `setup.sh` | One-shot install script for Ubuntu/Debian |

---

## Quick start (Ubuntu/Debian VPS)

```bash
# 1. Copy the project to your VPS
scp -r TL-Latest/ user@YOUR_VPS_IP:/opt/tgviewer

# 2. SSH in and run the setup script
ssh user@YOUR_VPS_IP
cd /opt/tgviewer
bash deploy/setup.sh /opt/tgviewer

# 3. Follow the printed NEXT STEPS
```

---

## How the session stays connected

```
Your browser tab
      │  HTTP  (closes when you close the tab — that's fine)
      ▼
Flask/Gunicorn process  ──── systemd keeps this alive 24/7
      │
      │  MTProto TCP  (stays open as long as the process runs)
      ▼
Telegram servers
```

Closing the browser tab **only** ends your HTTP session with the web UI.
The Pyrogram client inside the Flask process keeps its own live connection
to Telegram completely independently. Background downloads and exports
continue running even when you're not watching.

When you reopen the browser, the session string is loaded from the database
and reconnects automatically.

---

## Accessing the app

- **Direct IP**: `http://YOUR_VPS_IP:5000` (open port 5000 in your firewall first)
- **Via nginx** (recommended): `http://your-domain.com` after nginx setup
- **With HTTPS**: Run `certbot --nginx -d your-domain.com` after nginx setup

---

## Useful commands

```bash
# Check app status
systemctl status tgviewer

# View live logs
journalctl -u tgviewer -f

# Restart the app (e.g. after updating code)
systemctl restart tgviewer

# Stop the app
systemctl stop tgviewer
```

---

## Database

By default the app uses **SQLite** (`app.db` file in the project folder) —
zero configuration needed, fine for personal use.

To switch to PostgreSQL, set in your `.env`:
```
DATABASE_URL=postgresql://user:password@localhost/tgviewer
```
Then create the DB and run once to initialize tables:
```bash
createdb tgviewer
systemctl restart tgviewer   # db.create_all() runs at startup automatically
```

---

## Secret chats

Telegram secret chats use device-specific end-to-end encryption.
The keys are generated on and locked to the original device (your phone/desktop app)
and are never sent to Telegram's servers. This app (like all API-based clients)
**cannot read secret chat messages** — this is a Telegram protocol limitation,
not a bug.
