# Telegram Viewer

A Flask web app that connects to Telegram via the [Pyrogram](https://pyrogram.org/) MTProto client. It lets you browse chats, download media, translate messages, and preserve view-once / protected content through a password-protected web UI.

## Stack
- **Backend:** Python / Flask + Flask-SQLAlchemy
- **Telegram client:** Pyrogram 2.0 (MTProto)
- **Database:** SQLite by default (`instance/app.db`); set `DATABASE_URL` for Postgres
- **Translation:** Groq AI → Gemini → Google Translate (fallback chain)

## How to run
The app starts with:
```
cd TL-Latest && python app.py
```
It binds to `0.0.0.0:5000` and is managed by the **Start application** workflow.

## Required secrets (Replit Secrets)
| Secret | Description |
|---|---|
| `SESSION_SECRET` | Flask session signing key |
| `API_ID` | Telegram API ID from [my.telegram.org](https://my.telegram.org) |
| `API_HASH` | Telegram API hash from [my.telegram.org](https://my.telegram.org) |
| `APP_PASSWORD` | Password for the web UI login screen |

## Optional secrets
| Secret | Description |
|---|---|
| `DATABASE_URL` | Postgres connection string (defaults to SQLite) |
| `GROQ_API_KEY` | Groq AI key for AI-powered translation |
| `GEMINI_API_KEY` | Google Gemini key for AI-powered translation |
| `PROXY_TYPE` | `socks5` / `socks4` / `http` |
| `PROXY_HOST` | Proxy hostname |
| `PROXY_PORT` | Proxy port |
| `PROXY_USER` / `PROXY_PASS` | Proxy credentials (optional) |

## User preferences
