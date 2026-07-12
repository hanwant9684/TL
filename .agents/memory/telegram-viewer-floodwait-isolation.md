---
name: Telegram viewer live_updates / FloodWait / connection-storm isolation
description: Root causes (and fixes) for the "socket.send() raised exception / Connection lost" storm and account-won't-open hangs on the Telegram Viewer app when live_updates is enabled on large accounts.
---

Bisected via DB-backed feature flags. Real causes found so far, in the order
they were uncovered — all still apply, they're independent and additive:

1. **sleep_threshold=0** made Pyrogram's own internal per-channel gap-recovery
   (`recover_gaps()`/`handle_updates()`) raise FloodWait immediately instead of
   sleeping, causing a tight retry loop. Fixed by raising `sleep_threshold=30`
   on the Client (does not weaken explicit call sites that hardcode their own
   sleep_threshold, e.g. get_messages()=-1, get_dialogs()=60).

2. **recover_gaps() network catch-up ran for every stored channel** on
   process start/reconnect — floods Telegram on 100+ channel accounts even
   with a sane sleep_threshold, purely from call volume. Fixed by patching
   `recover_gaps()` to only do the real network catch-up for chat id==0
   (private/small-group combined GetDifference, cheap) and the small
   user-selected "protected" chat set; every other channel's pending state is
   marked caught-up locally with no network call (those chats still get live
   updates going forward, they just skip catch-up on anything missed while
   offline).

3. **AUTH_KEY_DUPLICATED races**: `get_client()` used to check-then-act
   without a lock, so two coroutines racing to (re)create a Client for the
   same session_string could each start their own Client with the same auth
   key, and Telegram resets both connections repeatedly. Fixed with a
   per-session `asyncio.Lock` (`_client_creation_locks`) serializing Client
   creation in `get_client()`.

4. **Blocking I/O on the shared event loop**: `_auto_preserve()` (view-once /
   protected-chat auto-save) used to do synchronous file writes and a
   SQLAlchemy commit inline, on the same asyncio loop that drives every
   session's MTProto keepalive/ping and socket reads. A large media write
   blocks that loop long enough for Telegram to reset the connection —
   looks identical to a FloodWait storm but reproducible with just one
   preserved chat, no real flood traffic. Fixed by moving both the file write
   and the DB persist through `asyncio.to_thread(...)`. General rule: any new
   blocking call added inside an `on_message`/`on_raw_update` handler in this
   app must go through `asyncio.to_thread(...)`.

5. **Stale-client deadlock ("account won't open")**: even after 1-4, a real
   network blip (socket timeout) can trigger Pyrogram's *own* internal
   `Session.restart()` retry task; if that retry touches the client's sqlite
   storage after something else in our code has already called
   `client.stop()` on it (closing storage), every call on that client then
   raises `"Connection lost"` or `"Cannot operate on a closed database"`.
   Those error strings didn't match `_is_broken_pipe()`, so
   `run_with_reconnect()` just re-raised immediately instead of force-
   reconnecting — the dead Client stayed cached in `telegram_clients` forever
   and every future request hit the same error, which looks exactly like
   "the account won't open" from the outside.
   **Why:** two problems compounded — (a) our reconnect-worthy error
   detection was too narrow (only "Broken pipe"), and (b) more than one code
   path called `client.stop()` / deleted `telegram_clients[...]` directly
   without going through the same per-session lock `get_client()` uses,
   so teardown could race a concurrent `get_client()`/`force_reconnect()` for
   the same session.
   **How to apply:** (a) treat "Connection lost", "closed database", and
   "Not connected" as reconnect-worthy in `run_with_reconnect()` alongside
   broken-pipe (see `_is_connection_dead()`); (b) always tear down a Client
   through `clear_client()` (which now takes the same `_client_creation_lock`
   as `get_client()`) — never call `.stop()` / mutate `telegram_clients`
   inline at a new call site.

Toggling `live_updates` ON force-reconnects **every** currently-connected
session at once (not just the one with a preserved chat) since `no_updates=`
is baked into the Client at construction time — on an account with several
active sessions this itself is a moment of elevated collision risk with #5.
