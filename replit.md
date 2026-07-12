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

## FloodWait / connection-stability fixes (live_updates engine)
Multiple independent Pyrogram storm sources were found and patched when
`live_updates` is ON (see `.agents/memory/telegram-viewer-floodwait-isolation.md`
for full root-cause detail): unscoped `recover_gaps()`/`handle_updates()`
per-channel catch-up, a `get_client()` create-race causing
`AUTH_KEY_DUPLICATED`, and (found only at 500+ chat scale) unscoped
reply-to-message resolution in Pyrogram's own message parser breaking the
socket under reply-heavy load. All three are now gated to only do the extra
network round-trip for chats in the 🛡 Protect (`ProtectedChat`) allowlist.
There's a 5th diagnostic feature flag, `live_updates`, alongside the existing
4 (`auto_reconnect`, `preserved_media`, `thumbnails`, `peer_resync`), toggled
via `/api/feature-flags` from the account page's settings — useful for
re-isolating a new storm source if one turns up again at even larger scale.

## User preferences
None recorded yet.
