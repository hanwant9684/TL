---
name: View-once media preservation — root causes and fix
description: How Telegram sends view-once media in MTProto and how to reliably preserve it in Pyrogram
---

## Root cause: photo=null in update

For "true view-once" in newer Telegram API layers (Layer 158+), the server sends
`MessageMediaPhoto` with flags.0 (photo) UNSET but flags.2 (ttl_seconds) SET.
Pyrogram parses this as `msg.photo = None`.

Old code:
```python
media_obj = getattr(msg, media_type, None)  # → None
is_view_once = bool(getattr(media_obj, 'ttl_seconds', None))  # → False — SILENT MISS
```

**Why:** TeleVip (Xposed module) confirmed this by hooking `SecretMediaViewer.openMedia()`
to grab the *already-cached local file* via `fileLoader.getLocalFile()`. The Telegram app
pre-downloads view-once media to local cache before the viewer opens. A Python userbot must
call `get_messages()` immediately to fetch the full message from the server.

**Key insight from TeleVip PreventMedia.java:** The self-destruct timer starts only AFTER
`sendSecretMessageRead` / `messages.readMessageContents` is called. Since a userbot never
calls `readMessageContents`, the file stays on Telegram's server and is downloadable.

## Fix applied

1. **`_auto_preserve` fallback**: when `media_obj is None` and media_type is a visual type,
   call `get_messages(chat_id, msg.id)` immediately to fetch from server. The file is still
   there because no client has called `readMessageContents`.

2. **Three-strategy download waterfall**:
   - S1: `download_media(msg, file_name=dest)` — standard
   - S2: `download_media(msg, in_memory=True)` — write bytes ourselves
   - S3: `download_media(file_id, in_memory=True)` — bypass message reference

3. **Raw update safety-net handler** (`@client.on_raw_update()`): catches
   `UpdateNewMessage` where `media.photo is None` but `media.ttl_seconds` is set.
   Uses a bounded retry loop (8 × 0.5 s = 4 s) to give `on_message` priority,
   then calls `get_messages` + `_auto_preserve` if still not preserved.

4. **DB unique constraint** on `(session_key, chat_id, message_id)` closes the race
   between `on_message` and the raw handler. `IntegrityError` is caught and treated
   as a no-op (duplicate = already preserved, which is fine).

## What TeleVip does (Xposed / app-level) vs what we do (API-level)

TeleVip works by hooking INTO the running Telegram Android process. It doesn't use
the MTProto API at all — it grabs the locally cached file. A Python userbot cannot
do this; it must use the API. The key distinction is that `readMessageContents` never
fires for a userbot, keeping the file accessible server-side indefinitely.

**How to apply:** If `_auto_preserve` still can't download (all 3 strategies fail and
logs show "server may have already expired"), the media was viewed by the recipient
before the userbot could save it, OR Telegram changed the server-side restriction.
In that case there's nothing the API can do — an Xposed hook is the only option.
