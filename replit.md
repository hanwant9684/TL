# Telegram Viewer (TL-Latest)

## Overview
Imported from GitHub. A Flask + Pyrogram (pyrofork) web app for viewing/exporting Telegram messages, originally built for VPS deployment (see `deploy/` for the original systemd/nginx setup). Now running on Replit.

## Stack
- Python 3 / Flask, Flask-SQLAlchemy
- Pyrogram (`pyrofork`) for Telegram MTProto access
- SQLite by default (`app.db`); set `DATABASE_URL` to use Postgres instead
- Optional AI-assisted translation: Groq / Gemini API keys, falls back to unofficial Google Translate if unset

## Running on Replit
- Workflow: `Start application` runs `python app.py`, serving on port 5000 (`0.0.0.0`).
- Required secrets (already configured): `SESSION_SECRET`, `API_ID`, `API_HASH`, `APP_PASSWORD`.
- Optional secrets: `GROQ_API_KEY`, `GEMINI_API_KEY` (better translation quality), `DATABASE_URL` (use Postgres instead of SQLite), `PROXY_*` (outbound proxy for Telegram connections).
- The app gates access behind the `APP_PASSWORD` login screen shown at `/`.

## User preferences
None recorded yet.
