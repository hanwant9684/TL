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

## FloodWait diagnostics (in progress)
The account was hitting a FloodWait storm from Pyrogram's own background update
engine (`updates.GetChannelDifference` retrying in a tight loop — unrelated to
any of the app's own routes). Added a 5th diagnostic feature flag,
`live_updates`, alongside the existing 4 (`auto_reconnect`, `preserved_media`,
`thumbnails`, `peer_resync`). All 5 are currently OFF in the database, so the
app is running in "account-opening only" mode: login + viewing the dialog
list/messages on demand works, but no background listening, auto-preserve,
thumbnails, or auto-reconnect run. Turn features back on one at a time via
`/api/feature-flags` (from the account page's settings) to find which one
triggers FloodWait.

## User preferences
None recorded yet.
