---
name: Postgres startup recovery causes transient 500s
description: Why a burst of DB-connection errors right after a workflow restart is often not a real bug, and how this app now handles it.
---

Replit's managed Postgres can still be finishing its own recovery/startup for
up to a few minutes after the app process starts (e.g. right after a workflow
restart). During that window, SQLAlchemy raises `OperationalError` with
messages like "the database system is not yet accepting connections /
Consistent recovery state has not been yet reached."

**Why it matters:** if the frontend polls an endpoint that touches the DB
(e.g. a status/tray poll every few seconds), this transient window can produce
a visible burst of dozens of 500s that looks like a serious regression but is
actually just the DB warming up — it resolves on its own once Postgres
finishes recovery.

**How to apply:** before assuming a burst of DB errors early after a restart
is a code bug, check the timestamps against the workflow start time and look
for "not yet accepting connections" / "recovery state" in the error text. In
TL-Latest/app.py this is mitigated with: `pool_pre_ping` + `pool_recycle` on
the SQLAlchemy engine, a startup readiness retry loop before `db.create_all()`,
try/except-with-rollback around DB-touching download routes that degrade
gracefully instead of 500ing, and client-side polling backoff (in
account.html) that slows down on repeated failures instead of hammering the
server.
