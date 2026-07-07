# Telegram Viewer

A Flask web app that lets you browse your Telegram account (chats, messages, media) through a browser-based UI. Built with Pyrogram (MTProto) and Flask.

## How to run

The workflow **"Start application"** runs `cd TL-Latest && python app.py` on port 5000.

## Required secrets (set in Replit Secrets)

| Secret | Description |
|--------|-------------|
| `SESSION_SECRET` | Flask session signing key |
| `API_ID` | Telegram API ID from [my.telegram.org](https://my.telegram.org) |
| `API_HASH` | Telegram API hash from [my.telegram.org](https://my.telegram.org) |
| `APP_PASSWORD` | Password to log in to the web UI |

## Optional secrets

| Secret | Description |
|--------|-------------|
| `GROQ_API_KEY` | Groq API key for AI-powered translation (Llama 3) |
| `GEMINI_API_KEY` | Gemini API key for AI-powered translation (fallback) |
| `DATABASE_URL` | Postgres connection string (defaults to SQLite at `instance/app.db`) |
| `PROXY_TYPE` | Proxy type: `socks5`, `socks4`, or `http` |
| `PROXY_HOST` | Proxy host |
| `PROXY_PORT` | Proxy port |
| `PROXY_USER` | Proxy username (optional) |
| `PROXY_PASS` | Proxy password (optional) |

## Features

- Browse Telegram chats/dialogs
- Read messages with inline media previews and thumbnails
- Download files from Telegram to the server
- Auto-translation via Groq → Gemini → Google Translate fallback chain
- Preserved media cache and thumbnail disk cache
- Rate-limit lockout on login (5 attempts → 5 min lockout)

## Deployment

The `.replit` deployment config uses Gunicorn:
```
cd TL-Latest && gunicorn --bind=0.0.0.0:5000 --reuse-port --workers=1 --threads=8 --timeout=120 app:app
```

## User preferences
