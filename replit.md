# Telegram Viewer

A Flask + Pyrogram web app that lets you browse your Telegram chats through a browser-based UI. Supports message translation (via Groq/Gemini AI) and chat export.

## How to run

The app starts automatically via the **Start application** workflow:

```
cd TL-Latest && python app.py
```

It binds to `0.0.0.0:5000`. The preview pane shows the login screen.

## Required secrets (set in Replit Secrets)

| Secret | Purpose |
|---|---|
| `API_ID` | Telegram API ID from https://my.telegram.org |
| `API_HASH` | Telegram API hash from https://my.telegram.org |
| `APP_PASSWORD` | Password to protect the web UI |
| `SESSION_SECRET` | Flask session signing key |

## Optional secrets

| Secret | Purpose |
|---|---|
| `GROQ_API_KEY` | AI translation via Groq (llama-3.1-8b-instant) |
| `GEMINI_API_KEY` | AI translation via Gemini (fallback) |
| `DATABASE_URL` | PostgreSQL URL; defaults to SQLite (`app.db`) if unset |

## Stack

- **Flask** — web framework
- **Pyrogram** — Telegram MTProto client
- **Flask-SQLAlchemy** — ORM (SQLite by default, PostgreSQL optional)
- **gunicorn** — WSGI server (used in production/deployment)

## Deployment

The `deploy/` folder contains a VPS deployment guide, nginx config, and systemd unit. For Replit deployment, use the configured VM deployment target with gunicorn.

## User preferences
