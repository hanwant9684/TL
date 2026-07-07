# Telegram Viewer

A Flask web app that lets you browse Telegram chats through a web UI. It connects to the Telegram API via Pyrogram, with AI-powered translation (Groq → Gemini → Google Translate fallback chain).

## Stack

- **Backend**: Python / Flask + Flask-SQLAlchemy
- **Telegram client**: Pyrogram 2.0
- **Database**: SQLite (default, `TL-Latest/instance/app.db`) or PostgreSQL via `DATABASE_URL`
- **Translation**: Groq AI → Gemini → Google Translate (unofficial)

## How to run

The workflow `Start application` runs `cd TL-Latest && python app.py` on port 5000.

## Required secrets

| Secret | Description |
|---|---|
| `API_ID` | Telegram app API ID from my.telegram.org |
| `API_HASH` | Telegram app API hash from my.telegram.org |
| `APP_PASSWORD` | Password to protect the web UI login page |
| `SESSION_SECRET` | Flask session secret key |

## Optional secrets

| Secret | Description |
|---|---|
| `GROQ_API_KEY` | Groq AI key for primary AI translation |
| `GEMINI_API_KEY` | Gemini key for fallback AI translation |
| `DATABASE_URL` | PostgreSQL URL (defaults to SQLite) |
| `PROXY_TYPE` | Proxy type: `socks5`, `socks4`, or `http` |
| `PROXY_HOST` / `PROXY_PORT` | Proxy host and port |
| `PROXY_USER` / `PROXY_PASS` | Proxy credentials (optional) |

## First-time use

1. Open the app, enter your `APP_PASSWORD` to log in.
2. On the main page, paste your Telegram session string to connect an account.
3. Browse dialogs, read messages, and download media.

## User preferences
