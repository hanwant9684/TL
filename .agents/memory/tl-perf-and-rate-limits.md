---
name: TL performance and Telegram rate-limit hardening
description: Speed improvements and anti-rate-limit measures applied to the Flask+Pyrogram Telegram viewer
---

## Changes made (2026-07-07)

### Speed improvements
- **Dialog snapshot cache** (`_dialog_snapshots`): Pyrogram `get_dialogs()` has no server-side offset, so every paginated scroll re-scanned from the start. Now fetches ALL dialogs once per session and slices in memory. TTL=15 min, invalidated on new message.
- **In-flight guard** (`_dialog_fetch_inflight`): Prevents concurrent cold-cache requests from each firing a full Telegram scan simultaneously.
- **Chat info cache** (`_chat_info_cache`, 1h TTL): Eliminated the extra `get_chat()` round-trip before every `get_chat_history()` call.
- **Double get_messages fixed**: `media_stream` and `download_media_route` each called `get_messages()` twice (once for metadata, once inside the streaming producer). Now fetches once and passes the object via closure.
- **Gzip compression**: `after_request` middleware compresses JSON/text responses > 500 bytes.
- **Thumbnail disk read**: removed `os.path.exists` pre-checks — now opens directly and catches `FileNotFoundError`.
- **IntersectionObserver rootMargin**: 400px → 120px, reducing initial thumbnail flood.
- **Translation cache** (`_translation_cache`, 500 entries FIFO): avoids re-hitting AI APIs for identical text+target pairs.

### Telegram rate-limit fixes
- **FloodWait-aware retry** in `run_with_reconnect`: catches `pyrogram.errors.FloodWait`, sleeps exact wait + random jitter (1-3s), max 3 retries. Broken-pipe tracked separately (`broken_pipe_retried`) so reconnect still works after a FloodWait.
- **`save_outgoing` filter**: previously ran `_auto_preserve` on ALL outgoing messages. Now short-circuits if chat is not in the protected set — no Telegram download on every sent message.
- **`_auto_preserve` protected-chat check**: replaced `ProtectedChat.query` DB call on every incoming message with in-memory set (`_protected_chats_mem`). DB queried once per session, then cached.
- **`protect_chat`/`unprotect_chat` routes** now update the in-memory set immediately after DB commit.
- **media_only history scan**: reduced from 1500 to 200 messages per window to avoid large Telegram history requests triggering FloodWait.

**Why:** Single-client Pyrogram accounts are very sensitive to burst Telegram API calls. Large `get_dialogs()` re-scans and per-message DB-backed checks were the main latency and rate-limit triggers. The FloodWait handler was completely missing before.

**How to apply:** Any future feature that loops over Telegram history should use small page sizes (≤200). Any per-message work should use in-memory checks, not DB queries, in async handlers.
