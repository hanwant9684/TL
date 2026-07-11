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
