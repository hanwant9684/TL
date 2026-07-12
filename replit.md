# TL Telegram Viewer

A private web interface for browsing your Telegram messages, built with Flask + Pyrogram (pyrofork).

## Stack

- **Backend**: Python / Flask
- **Telegram client**: pyrofork (Pyrogram fork) via MTProto
- **Database**: SQLite by default (PostgreSQL optional via `DATABASE_URL`)
- **UI**: Jinja2 templates
- **Production server**: Gunicorn

## How to run

```bash
cd TL-Latest && python app.py
```

The app listens on port 5000. The workflow "Start application" is configured to start it automatically.

## Required secrets (set in Replit Secrets)

| Secret | Description |
|--------|-------------|
| `API_ID` | Telegram API ID — get from https://my.telegram.org |
| `API_HASH` | Telegram API Hash — from https://my.telegram.org |
| `APP_PASSWORD` | Password to log in to the web UI |
| `SESSION_SECRET` | Flask session signing key (any long random string) |

## Optional secrets

| Secret | Description |
|--------|-------------|
| `GROQ_API_KEY` | Enables AI translation via Groq (llama-3.1-8b-instant) |
| `GEMINI_API_KEY` | Enables AI translation via Gemini (fallback) |
| `DATABASE_URL` | PostgreSQL connection string (omit to use SQLite) |
| `PROXY_TYPE` / `PROXY_HOST` / `PROXY_PORT` | SOCKS proxy for Telegram connection |

## Project structure

```
TL-Latest/
  app.py              # Main Flask application (all routes + Pyrogram logic)
  requirements.txt    # Python dependencies
  templates/          # Jinja2 HTML templates
    index.html        # Main chat browser
    login.html        # Login page
    account.html      # Account settings
  logs/               # App logs
  deploy/             # VPS deployment helpers (nginx, systemd, setup.sh)
```

## GitHub

Remote: https://github.com/hanwant9684/TL

## User preferences

- Use SQLite (not PostgreSQL) for local/Replit use.
