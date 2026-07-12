---
name: TL-Latest FloodWait isolation
description: How to bisect Telegram FloodWait sources in the TL-Latest (Flask + Pyrogram/pyrofork) project, and the known live_updates+auto_reconnect trigger.
---

# FloodWait isolation in TL-Latest

The app (`TL-Latest/app.py`) has a DB-backed diagnostic feature-flag system
(`FeatureFlag` table, `is_feature_enabled()` / `set_feature_enabled()`,
`/api/feature-flags` route) built specifically for narrowing down FloodWait
sources one at a time, independent of the app's own routes. Flags as of this
writing: `auto_reconnect`, `preserved_media`, `thumbnails`, `peer_resync`,
`live_updates`.

`live_updates` controls whether the Pyrogram `Client` is created with
`no_updates=True` (off) or the default live-update engine (on). With it off,
`get_me`/`get_dialogs`/on-demand `get_messages` still work, but no
`on_message`/`on_raw_update`/`on_deleted_messages` handler fires and no
background update-catch-up traffic happens.

**Known trigger:** `live_updates=True` combined with `auto_reconnect=True`
(which reconnects all saved sessions at process boot) causes Pyrogram's
per-channel catch-up sync to fire simultaneously across many channels/groups
right after reconnect — observed as a burst of `channels.GetMessages` /
`updates.GetChannelDifference` calls hitting Telegram's rate limits within
~30s of boot, on an account with 100+ dialogs. Isolated by toggling flags
one at a time and force-reconnecting/restarting between each test.

**Why:** Pyrogram doesn't serialize or throttle its own multi-channel update
gap recovery; on an account with many channels, all of them try to catch up
at once on every reconnect, and Telegram flood-limits that burst.

**How to apply:** If FloodWait recurs after reconnect/restart with this app,
check `live_updates` first — it's the strongest known trigger, especially in
combination with `auto_reconnect`. Toggling flags requires a real
force-reconnect or full process restart to take effect (the Pyrogram
`Client` object is created once and cached in-memory; flipping the DB flag
alone doesn't affect an already-running client). Prefer a full workflow
restart over the in-app `/reconnect` endpoint when a client is already mid
flood-storm — the in-app stop()/restart can itself hang fighting the
flooding client's in-flight tasks.

## Root cause of the crash-loop (not just "FloodWait happens")

The real bug was `Client(..., sleep_threshold=0)`. Pyrogram's own internal
update-gap recovery (`Client.handle_updates()`/`recover_gaps()` calling
`updates.GetChannelDifference`) has no FloodWait handling of its own — it
relies entirely on the client's `sleep_threshold` to silently sleep-and-retry
short waits. With `sleep_threshold=0`, every one of those internal calls
raised immediately, crashing the in-flight asyncio task; Pyrogram then retried
on the next queued update with zero backoff, producing thousands of
FloodWait/`socket.send()` errors within seconds on a 100+ channel account.

**Fix:** raise the client's `sleep_threshold` to ~30. This lets Pyrogram
absorb short internal catch-up waits by sleeping once per channel as
designed. It does not weaken the app's own explicit call sites — Pyrogram
hardcodes `sleep_threshold=-1` (always raise) inside its own `get_messages()`
method (used for reply/pinned-message resolution during update parsing) and
`sleep_threshold=60` inside `get_dialogs()`/`get_chat_history()` — both
ignore the client-level value regardless.

**Residual behavior (expected, not a bug):** even after the fix, a
many-channel account reconnecting after being offline will still show
periodic `pyrogram.session.session - WARNING - Waiting for N seconds...`
lines and occasional individual `FloodWait` exceptions specifically from
`get_messages()`'s hardcoded reply-message resolution (sleep_threshold=-1 is
baked into that Pyrogram method, unfixable from client config). This is
normal backlog catch-up traffic settling over minutes, not a runaway loop —
confirm it's healthy by checking the rate is low/decreasing over time, not
thousands of errors per second.

## Correction: sleep_threshold alone did not fully fix it — two more real bugs

The `sleep_threshold=0→30` fix above is real but was not sufficient; a
sustained storm (thousands of `socket.send()` warnings/minute, continuous
`FloodWait` on `updates.GetChannelDifference`) kept recurring on reconnect
for a 100+ channel account with `live_updates=True`. Two further root causes,
found by reading Pyrogram's actual source rather than guessing from symptoms:

1. **`skip_updates` defaults to `True`**, and Pyrogram's `Dispatcher.start()`
   only calls `client.recover_gaps()` when `skip_updates is False`. This app
   never overrides `skip_updates`, so `recover_gaps()` (and any monkeypatch
   of it) never runs at all — it is not the storm source and patching it is a
   no-op. The real per-update storm source is `Client.handle_updates()`
   itself: any incoming update batch containing a user/chat not yet in
   Pyrogram's local peer cache (`is_min=True` — common right after reconnect,
   and *guaranteed* every restart here because the app uses
   `in_memory=True` sessions, so the peer cache is never persisted) makes
   Pyrogram fire one `updates.GetChannelDifference` per
   `UpdateNewChannelMessage` in that batch, with no filtering. On a
   100+-channel account with recent activity, Telegram delivers a burst of
   these the instant the connection reconnects, and that burst alone is
   enough to flood-limit the single MTProto connection.
   **Fix applied:** monkeypatch `client.handle_updates` (assigned as a plain
   instance attribute, exactly like the `recover_gaps` patch — Pyrogram's
   `session.py` calls it via `self.client.handle_updates(...)`, an attribute
   lookup, so an instance override without `self` in its signature works) to
   reproduce the original method but only issue the extra
   `GetChannelDifference` resolution for channels in a caller-supplied
   allowlist; every other channel's update is still queued/dispatched
   normally, just without the extra network round-trip to fully resolve a
   `is_min` peer. This is also how "instant live preservation for only a
   few selected chats" (an explicit product requirement, not just a
   perf fix) was implemented — the allowlist is the existing `ProtectedChat`
   table that already backs the UI's per-chat "🛡 Protect" toggle.

2. **The real dominant cause of the *sustained* (not just one-time) storm**
   turned out to be unrelated to update-gap recovery at all:
   `get_client(session_string)` had a classic check-then-act race. It checked
   `session_string not in telegram_clients`, then `await client.start()`
   (which yields to the event loop for the whole MTProto handshake), and only
   *after* that wrote the new `Client` into the `telegram_clients` cache dict.
   Two coroutines calling `get_client()` for the same session close together
   (e.g. the startup auto-reconnect sweep racing a browser request resuming
   the same session right after a restart) would both pass the "not in
   telegram_clients" check and each start a *separate* `Client` with the same
   auth key. Telegram detects that as **`AUTH_KEY_DUPLICATED`** and starts
   forcibly resetting one of the connections — which shows up as a
   self-sustaining loop of socket resets → reconnect → fresh
   `is_min`/`GetChannelDifference` catch-up → FloodWait, that never settles
   because a new duplicate keeps getting created on each retry.
   **Fix applied:** an `asyncio.Lock` per `session_string` around the
   check-and-create block in `get_client()`, so only one `Client` is ever
   started for a given session at a time.
   **Why this matters generally:** any "get-or-create and cache in a dict"
   pattern across an `await` boundary in async Python needs a lock around
   the whole check-create-store sequence, not just around the dict write —
   the window between the check and the store is exactly where the race
   lives, and with a slow `await` (like a network handshake) in between,
   that window is wide enough to hit often, not just in theory.

**How to tell which of these you're hitting:** grep the logs for
`AUTH_KEY_DUPLICATED` — if present, it's cause #2 (the client-creation race)
and matters more than any update-gap tuning; a storm with no
`AUTH_KEY_DUPLICATED` but heavy `is_min`-triggered `GetChannelDifference`
volume right after reconnect is cause #1 (unscoped live catch-up).

## Fourth cause: blocking preserved-media I/O on the shared event loop

All Telegram sessions share one asyncio event loop/thread (`_loop` in
`app.py`) that also drives every session's Pyrogram network I/O (ping,
keepalive, socket read/write). Any synchronous (blocking) disk or DB call
made inline from an `async` update handler running on that loop — not just
per-channel catch-up volume — can starve Pyrogram's own keepalive task long
enough for Telegram to reset the connection, producing a symptom that looks
identical to a FloodWait storm (`socket.send() raised exception`,
`Connection lost`, endless reconnect) but has nothing to do with update/catch
-up volume: it reproduces even with only one protected/view-once chat and
zero flood-worthy traffic, and toggling `live_updates` off "fixes" it only
because it stops the handler from ever firing, not because the update volume
was the problem.

**Where it hid:** `_auto_preserve()`'s download-write (`open(...,'wb')`) and
DB-persist (`db.session.add`/`commit`, plus a full-file `open(...,'rb').read()`
fallback) ran inline in the coroutine. `log_message()` right next to it
already offloaded its own (much smaller) DB write via
`asyncio.to_thread(_log_message_sync, ...)` specifically to avoid this class
of bug — `_auto_preserve()`'s file/DB I/O just never got the same treatment,
and it handles much larger payloads (video files), making the stall far more
likely to exceed Telegram's keepalive tolerance.

**Fix applied:** wrap the blocking write (`_write_file_sync`) and the
read+DB-persist step (`_persist_preserved_media_sync`) in
`asyncio.to_thread(...)` from `_auto_preserve()`, exactly matching the
existing `log_message`/`_log_message_sync` pattern.

**How to apply generally:** in this app, any new code added to an
`on_message`/`on_raw_update`/`on_raw_update`-style handler that does disk I/O,
a DB commit, or any other blocking call must go through
`asyncio.to_thread(...)` — never call it inline — because it runs on the one
shared loop serving every session's live connection, not a per-session loop.
