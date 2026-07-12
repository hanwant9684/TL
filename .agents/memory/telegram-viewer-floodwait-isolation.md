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

## Third real bug: found only at large scale (500+ chats)

Both fixes above only gate Pyrogram's *catch-up/gap-recovery* machinery. A
third, independent storm source is Pyrogram's own **per-message reply
resolution**: `Dispatcher`'s `message_parser`/`edited_message_parser` call
`Message._parse(..., replies=1)` (the hardcoded default) for every dispatched
message, and `_parse` then calls `client.get_messages(...)` to fetch
`reply_to_message` whenever a message is itself a reply — completely
independent of the `handle_updates`/`recover_gaps` patches and the
`ProtectedChat` allowlist used there. That `get_messages()` call hardcodes
`sleep_threshold=-1` inside Pyrogram (always raise; ignores the client-level
`sleep_threshold=30`), so it has zero flood/backoff protection.

**Symptom:** on an account with ~100 chats this never showed up (not enough
concurrent reply traffic to matter). On a 500+ chat account with real
reply-heavy activity, a burst of these fire concurrently on the single MTProto
connection right after reconnect and can break the socket itself —
`asyncio - WARNING - socket.send() raised exception` repeated rapidly, then
`pyrogram.dispatcher - ERROR - Connection lost` with an `OSError` traceback
through `message_parser -> Message._parse -> get_messages`, then several
minutes of `Unable to connect due to network issues: Connection timed out`
before it self-heals. This is a distinct failure mode from causes #1/#2 above
— no `AUTH_KEY_DUPLICATED`, and it hits during steady-state live traffic, not
just at boot/reconnect.

**Fix applied:** monkeypatch `client.dispatcher.update_parsers` (an instance
dict keyed by raw update type, populated in `Dispatcher.__init__` from
`client.dispatcher = Dispatcher(self)` inside `Client.__init__` — available
immediately after `Client(...)` construction, no `.start()` needed) for the
`UpdateNewMessage`/`UpdateNewChannelMessage`/`UpdateNewScheduledMessage`/
`UpdateEditMessage`/`UpdateEditChannelMessage` keys, to call `Message._parse`
with `replies=0` (skip the extra network fetch, `reply_to_message_id` is still
set so the UI can still show "replying to #N") for any chat not in the
preserved/🛡 Protect set, and `replies=1` only for preserved chats.

**Why this matters generally:** a fix validated at one scale (100 chats) does
not necessarily hold at 5x that scale — the underlying bug class (a "cheap
per-item extra network round-trip" baked into a hot per-update code path with
no gating) can have multiple independent instances in different code paths,
and only enough concurrent volume exposes each one.
