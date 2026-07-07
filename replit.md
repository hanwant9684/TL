# Telegram Viewer

A Flask + Pyrogram web app for browsing your Telegram account.

## Stack
- **Python / Flask** — web server (`TL-Latest/app.py`, 3000+ lines)
- **Pyrogram** — Telegram client library (uses session strings, not interactive login)
- **SQLite** (default) or **PostgreSQL** — stores sessions, downloads, preserved media
- **Jinja2 templates** — `TL-Latest/templates/`

## How to run
The workflow `Start application` runs:
```
cd TL-Latest && python app.py
```
The app serves on port 5000.

## Required secrets (Replit Secrets)
| Secret | Purpose |
|---|---|
| `SESSION_SECRET` | Flask session signing key |
| `API_ID` | Telegram app ID (from my.telegram.org) |
| `API_HASH` | Telegram app hash (from my.telegram.org) |
| `APP_PASSWORD` | Password to access the web UI |

## Optional secrets
| Secret | Purpose |
|---|---|
| `GROQ_API_KEY` | AI translation via Groq (Llama 3) |
| `GEMINI_API_KEY` | AI translation via Gemini (fallback) |
| `DATABASE_URL` | PostgreSQL URL (defaults to SQLite `instance/app.db`) |

## Usage
1. Open the app — you'll see a password login screen.
2. Enter your `APP_PASSWORD` to sign in.
3. Paste a Pyrogram session string (`BQC...`) to connect your Telegram account.
4. Browse chats, download media, translate messages.

## User preferences
