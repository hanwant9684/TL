import os
import uuid
import math
import time
import asyncio
import concurrent.futures
import threading
import logging
import hashlib
import random
import gzip as _gzip
from functools import wraps
from datetime import timedelta, datetime
import base64

import requests as _http
from flask import Flask, render_template, request, jsonify, redirect, url_for, session, send_file, Response
from flask_sqlalchemy import SQLAlchemy

# ── Translation ───────────────────────────────────────────────────────────────
# Chain: Groq AI → Gemini → Google Translate (unofficial, no key)

_AI_SYSTEM_PROMPT = (
    "You are an expert multilingual translator specializing in informal, slang-heavy text. "
    "Translate the given text into the specified target language.\n\n"
    "RULES:\n"
    "1. Translate ALL content including vulgar, explicit, offensive, or sexual words — NEVER censor.\n"
    "2. Understand slang and Gen-Z language from any language:\n"
    "   - Indonesian: gw/gue=I, lo/lu=you, gabut=bored, anjir/anjg/anjing=damn/fuck, "
    "wkwk=lol, mager=lazy, baper=emotional, kepo=nosy, mantul=awesome, gasken=let's go, "
    "cok/cuk=dude(vulgar), kontol=dick(vulgar), ngab=bro, "
    "yg=yang, dgn=dengan, krn=karena, jg=juga, tp=tapi, udh/udah=sudah, blm=belum, "
    "hrs=harus, sm=sama, dl=dulu, emg=emang, bntr=bentar, kyk=kayak, gak/ngga/nggak=tidak\n"
    "   - English Gen-Z: lowkey, highkey, bussin, no cap, fr fr, slay, sus, goated, bet, rizz\n"
    "   - Hindi: yaar=friend, bhai=brother, bindaas=carefree, jugaad=hack\n"
    "3. Handle code-switching (mixed languages) naturally.\n"
    "4. If already in target language, return as-is.\n"
    "5. Return ONLY the translation — no notes, no quotes, no explanation."
)

def _try_groq(text, target_name):
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        return None
    try:
        resp = _http.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [
                    {"role": "system", "content": _AI_SYSTEM_PROMPT},
                    {"role": "user", "content": f"Translate to {target_name}:\n{text}"}
                ],
                "temperature": 0.3,
                "max_tokens": 1024
            },
            timeout=20
        )
        if resp.status_code == 200:
            result = resp.json()["choices"][0]["message"]["content"].strip()
            return result if result else None
        if resp.status_code in (429, 503):
            return None
    except Exception:
        pass
    return None

def _try_gemini(text, target_name):
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return None
    prompt = f"{_AI_SYSTEM_PROMPT}\n\nTranslate to {target_name}:\n{text}"
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    headers = {"Content-Type": "application/json"}
    for model in ("gemini-2.0-flash", "gemini-2.0-flash-lite"):
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:generateContent?key={key}")
        try:
            resp = _http.post(url, json=payload, headers=headers, timeout=20)
            if resp.status_code == 200:
                return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            if resp.status_code in (429, 503):
                continue
        except Exception:
            pass
    return None

def _try_google_translate(text, target_lang):
    try:
        resp = _http.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": target_lang, "dt": "t", "q": text},
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            result = "".join(part[0] for part in data[0] if part[0])
            if result and result.lower().strip() != text.lower().strip():
                return result
    except Exception:
        pass
    return None

def translate_with_fallback(text, target_lang, target_name):
    result = _try_groq(text, target_name)
    if result:
        return result, "Groq AI"

    result = _try_gemini(text, target_name)
    if result:
        return result, "Gemini"

    result = _try_google_translate(text, target_lang)
    if result:
        return result, "Google Translate"

    raise ValueError("All translation engines unavailable. Try again later.")

# ── App setup ─────────────────────────────────────────────────────────────────

DOWNLOADS_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server_downloads")
PRESERVED_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "preserved_media")
THUMBNAILS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "thumbnail_cache")
EXPORTS_DIR    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server_exports")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)
os.makedirs(PRESERVED_DIR, exist_ok=True)
os.makedirs(THUMBNAILS_DIR, exist_ok=True)
os.makedirs(EXPORTS_DIR,    exist_ok=True)

download_queue = {}
export_jobs    = {}   # {job_id: job_dict}

_MEDIA_EXT_MAP = {
    "photo": ".jpg", "video": ".mp4", "audio": ".mp3",
    "voice": ".ogg", "animation": ".mp4", "sticker": ".webp",
    "video_note": ".mp4", "document": "",
}

LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'

# ── Rotating file handler — writes to logs/app.log, keeps last 5 × 5 MB ──────
import os as _os
from logging.handlers import RotatingFileHandler as _RotatingFileHandler
_LOGS_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "logs")
_os.makedirs(_LOGS_DIR, exist_ok=True)
_file_handler = _RotatingFileHandler(
    _os.path.join(_LOGS_DIR, "app.log"),
    maxBytes=5 * 1024 * 1024,   # 5 MB per file
    backupCount=5,               # keep app.log + 5 rotated backups = 30 MB max
    encoding="utf-8",
)
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(logging.Formatter(LOG_FORMAT))

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    handlers=[logging.StreamHandler(), _file_handler]
)
# basicConfig is a no-op if any handlers already exist on the root logger,
# so attach the file handler explicitly to guarantee it always runs.
_root_logger = logging.getLogger()
_root_logger.setLevel(logging.INFO)
if _file_handler not in _root_logger.handlers:
    _root_logger.addHandler(_file_handler)
if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, _RotatingFileHandler)
           for h in _root_logger.handlers):
    _root_logger.addHandler(logging.StreamHandler())
logger = logging.getLogger(__name__)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# ── In-memory log buffer (last 500 entries) — readable from /api/logs ─────────
import collections
_LOG_BUFFER_SIZE = 500
_log_buffer = collections.deque(maxlen=_LOG_BUFFER_SIZE)

class _BufferHandler(logging.Handler):
    """Appends every log record from our app logger into _log_buffer."""
    def emit(self, record):
        try:
            _log_buffer.append({
                "ts":    self.formatTime(record, "%Y-%m-%d %H:%M:%S"),
                "level": record.levelname,
                "name":  record.name,
                "msg":   record.getMessage(),
            })
        except Exception:
            pass

_buf_handler = _BufferHandler()
_buf_handler.setLevel(logging.DEBUG)
logging.getLogger(__name__).addHandler(_buf_handler)
# Also capture root-level app logs (werkzeug excluded above)
logging.getLogger().addHandler(_buf_handler)

try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from pyrogram import Client, filters as pyro_filters
try:
    from pyrogram.errors import FloodWait as _FloodWait
except ImportError:
    _FloodWait = None

app = Flask(__name__, static_folder='static', template_folder='templates')
app.secret_key = os.environ.get("SESSION_SECRET") or os.environ.get("SECRET_KEY")
if not app.secret_key:
    raise RuntimeError("SESSION_SECRET environment variable is not set. Set it in Replit Secrets before starting the app.")
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)

telegram_clients = {}

# ── DB ────────────────────────────────────────────────────────────────────────

_db_url = os.environ.get("DATABASE_URL", "sqlite:///app.db")
if _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql://", 1)
app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
if _db_url.startswith("postgresql"):
    # pool_pre_ping: validates a connection with a cheap SELECT 1 before handing
    # it to a request, so stale/dropped connections (e.g. after the DB restarts
    # or recycles) are silently replaced instead of surfacing as a 500.
    # pool_recycle: proactively retires connections older than 5 min, matching
    # typical managed-Postgres idle/connection limits.
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
        "connect_args": {"connect_timeout": 5},
    }

db = SQLAlchemy(app)

def _db_is_transient_error(exc):
    """True if exc looks like a transient DB connectivity issue (DB still
    booting/recovering, connection dropped, etc.) rather than a real bug."""
    from sqlalchemy.exc import OperationalError, DBAPIError
    return isinstance(exc, (OperationalError, DBAPIError))

class MessageStore(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.BigInteger)
    chat_id    = db.Column(db.BigInteger)
    user_id    = db.Column(db.BigInteger)
    text       = db.Column(db.Text)
    date       = db.Column(db.DateTime)

class PreservedMedia(db.Model):
    id               = db.Column(db.Integer, primary_key=True)
    session_key      = db.Column(db.String(32), nullable=False, index=True)
    chat_id          = db.Column(db.BigInteger, nullable=False)
    message_id       = db.Column(db.BigInteger, nullable=False)
    file_path        = db.Column(db.Text)
    file_name        = db.Column(db.String(512))
    file_size        = db.Column(db.BigInteger)
    media_type       = db.Column(db.String(32))
    reason           = db.Column(db.String(32))   # view_once / protected / secret
    saved_at         = db.Column(db.DateTime, default=datetime.utcnow)
    original_deleted = db.Column(db.Boolean, default=False)
    file_data        = db.Column(db.LargeBinary)  # binary copy stored in DB — survives redeploys

    __table_args__ = (
        # Unique guard: prevents two concurrent handlers from both inserting the
        # same message (race between on_message and the raw update safety-net).
        # _auto_preserve catches IntegrityError from this and treats it as a no-op.
        db.UniqueConstraint('session_key', 'chat_id', 'message_id',
                            name='uq_preserved_media_identity'),
    )

class ProtectedChat(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    session_key = db.Column(db.String(32), nullable=False)
    chat_id     = db.Column(db.BigInteger, nullable=False)

class ServerDownload(db.Model):
    """Persists server-side downloads so files survive redeploys."""
    id          = db.Column(db.Integer, primary_key=True)
    download_id = db.Column(db.String(16), unique=True, nullable=False, index=True)
    session_key = db.Column(db.String(32), nullable=False)
    chat_id     = db.Column(db.BigInteger, nullable=False)
    message_id  = db.Column(db.BigInteger, nullable=False)
    file_name   = db.Column(db.String(512))
    file_path   = db.Column(db.String(1024))
    file_size   = db.Column(db.BigInteger)
    file_data   = db.Column(db.LargeBinary)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    status      = db.Column(db.String(16), default="queued")
    error       = db.Column(db.Text)

class StoredSession(db.Model):
    """Persists Telegram session strings so clients stay connected across restarts."""
    id               = db.Column(db.Integer, primary_key=True)
    session_key      = db.Column(db.String(32), unique=True, nullable=False, index=True)
    session_string   = db.Column(db.Text, nullable=False)
    label            = db.Column(db.String(256))
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)
    last_seen        = db.Column(db.DateTime, default=datetime.utcnow)
    reconnect_failed = db.Column(db.Boolean, default=False)

def _wait_for_db_ready(max_attempts=8, base_delay=1.5):
    """Managed Postgres can still be finishing its own recovery/startup when
    this app boots (e.g. right after a workflow restart), which raises
    OperationalError until it's ready. Retry with backoff instead of letting
    the very first request after boot crash with a raw 500."""
    from sqlalchemy import text
    for attempt in range(1, max_attempts + 1):
        try:
            with db.engine.connect() as _conn:
                _conn.execute(text("SELECT 1"))
            return True
        except Exception as e:
            if attempt == max_attempts:
                logger.warning(f"DB not ready after {max_attempts} attempts: {e}")
                return False
            delay = base_delay * attempt
            logger.warning(f"DB not ready yet (attempt {attempt}/{max_attempts}): {e}. Retrying in {delay:.1f}s...")
            time.sleep(delay)
    return False

with app.app_context():
    _wait_for_db_ready()
    db.create_all()
    # Runtime migration: add any new columns to existing tables
    try:
        from sqlalchemy import text
        with db.engine.connect() as _conn:
            for _stmt in [
                "ALTER TABLE preserved_media ADD COLUMN IF NOT EXISTS file_data bytea",
                "ALTER TABLE stored_session ADD COLUMN IF NOT EXISTS reconnect_failed boolean DEFAULT false",
                "ALTER TABLE server_download ADD COLUMN IF NOT EXISTS file_data bytea",
                # Unique constraint added to prevent duplicate inserts from concurrent
                # on_message + raw_update handlers for the same view-once message.
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_preserved_media_identity "
                "ON preserved_media (session_key, chat_id, message_id)",
            ]:
                try:
                    _conn.execute(text(_stmt))
                except Exception:
                    pass
            _conn.commit()
    except Exception as _me:
        logger.warning(f"Migration note: {_me}")

# ── Telegram config ───────────────────────────────────────────────────────────

_api_id_raw = os.environ.get("API_ID", "").strip()
if not _api_id_raw or not _api_id_raw.isdigit():
    raise RuntimeError("API_ID environment variable is not set or invalid. Set it in Replit Secrets before starting the app.")
API_ID = int(_api_id_raw)

API_HASH = os.environ.get("API_HASH", "").strip()
if not API_HASH or API_HASH == "your_api_hash_here":
    raise RuntimeError("API_HASH environment variable is not set. Set it in Replit Secrets before starting the app.")

APP_PASSWORD = os.environ.get("APP_PASSWORD", "").strip()
if not APP_PASSWORD:
    raise RuntimeError("APP_PASSWORD environment variable is not set. Set it in Replit Secrets before starting the app.")

def format_file_size(size_bytes):
    if not size_bytes or size_bytes <= 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    i = min(int(math.floor(math.log(size_bytes, 1024))), len(units) - 1)
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {units[i]}"

import re as _re
def _sanitize_filename(name: str) -> str:
    """Return a safe basename — strip path separators, null bytes, and
    control characters so Telegram-supplied file names cannot escape
    the intended download/preserve directory."""
    # Take only the final component (basename), stripping any directory parts
    name = os.path.basename(name)
    # Remove null bytes and ASCII control characters
    name = _re.sub(r'[\x00-\x1f\x7f]', '', name)
    # Replace Windows-style directory separators that os.path.basename may miss
    name = name.replace('\\', '').replace('/', '')
    # Collapse leading dots to avoid hidden-file tricks on Unix
    name = name.lstrip('.') or 'file'
    # Trim to a sane length
    return name[:200]

def get_proxy_config():
    ptype = os.environ.get("PROXY_TYPE", "").lower()
    host  = os.environ.get("PROXY_HOST", "")
    port  = int(os.environ.get("PROXY_PORT", "0") or "0")
    if not ptype or not host or not port:
        return None
    proxy = {"scheme": ptype, "hostname": host, "port": port}
    if os.environ.get("PROXY_USER") and os.environ.get("PROXY_PASS"):
        proxy["username"] = os.environ.get("PROXY_USER")
        proxy["password"] = os.environ.get("PROXY_PASS")
    return proxy

_PRESERVE_EXT = {
    "photo": ".jpg", "video": ".mp4", "audio": ".mp3",
    "voice": ".ogg", "animation": ".mp4", "sticker": ".webp",
    "video_note": ".mp4", "document": ".bin",
}

async def _auto_preserve(client, msg, session_key):
    """Download and record media that should be preserved (view-once or protected chat).

    Three-strategy download waterfall:
      S1 — download_media(msg, file_name=path)   standard path-based download
      S2 — download_media(msg, in_memory=True)   memory download, we write bytes
      S3 — download_media(file_id, in_memory=True) direct file-id bypass

    View-once detection has two layers:
      Primary  — ttl_seconds on the parsed Pyrogram media object (covers timer
                 mode and most view-once where the server sent the photo object)
      Fallback — when msg.photo / msg.video is None (Telegram sends photo=null
                 in MessageMediaPhoto for true view-once in newer API layers),
                 we immediately call get_messages() to fetch the full message
                 from the server — the file is still there until the recipient
                 opens it and readMessageContents fires, which we never call.
    """
    if not msg or not msg.media:
        return
    media_type = msg.media.value if msg.media else None
    if not media_type or media_type not in _PRESERVE_EXT:
        return

    chat_id = msg.chat.id if msg.chat else None
    if not chat_id:
        return

    media_obj = getattr(msg, media_type, None)

    # ── Primary view-once detection ───────────────────────────────────────────
    is_view_once = bool(getattr(media_obj, 'ttl_seconds', None))

    # ── Fallback: photo=null in update (true view-once in newer Telegram) ─────
    # When Telegram sends MessageMediaPhoto with flags.0 (photo) unset but
    # flags.2 (ttl_seconds) set, Pyrogram sets msg.photo = None.  We detect
    # this as (media_obj is None AND media type is a visual type) and fetch the
    # full message immediately.  The server still has the file because no client
    # has called readMessageContents yet.
    if not is_view_once and media_obj is None and media_type in ('photo', 'video', 'voice', 'video_note'):
        try:
            fresh = await asyncio.wait_for(
                client.get_messages(chat_id, msg.id),
                timeout=15
            )
            if fresh and fresh.media and fresh.media.value == media_type:
                fresh_obj = getattr(fresh, media_type, None)
                if fresh_obj is not None:
                    media_obj   = fresh_obj
                    msg         = fresh
                    is_view_once = bool(getattr(media_obj, 'ttl_seconds', None))
                    if is_view_once:
                        logger.info(f"[preserve] recovered view-once {media_type} "
                                    f"for msg {msg.id} via get_messages (photo was null in update)")
        except Exception as fe:
            logger.debug(f"[preserve] get_messages fallback failed for msg {msg.id}: {fe}")

    # ── Protected-chat detection ──────────────────────────────────────────────
    is_protected = chat_id in _get_protected_set(session_key)

    if not is_view_once and not is_protected:
        return

    # ── Duplicate guard ───────────────────────────────────────────────────────
    with app.app_context():
        if PreservedMedia.query.filter_by(
            session_key=session_key, chat_id=chat_id, message_id=msg.id
        ).first():
            return

    reason   = "view_once" if is_view_once else "protected"
    raw_name = (getattr(media_obj, 'file_name', None) if media_obj else None)
    if not raw_name:
        raw_name = f"{media_type}_{msg.id}{_PRESERVE_EXT.get(media_type, '.bin')}"
    safe_name = f"{session_key[:8]}_{chat_id}_{msg.id}_{_sanitize_filename(raw_name)}"
    dest      = os.path.join(PRESERVED_DIR, safe_name)

    # ── Multi-strategy download ───────────────────────────────────────────────
    file_bytes = None
    downloaded = None

    # S1: standard path-based download
    try:
        result = await asyncio.wait_for(
            client.download_media(msg, file_name=dest),
            timeout=90
        )
        if result and os.path.exists(str(result)):
            downloaded = str(result)
            logger.debug(f"[preserve] S1 success msg {msg.id}")
    except Exception as e1:
        logger.warning(f"[preserve] S1 failed msg {msg.id} "
                       f"({media_type}, view_once={is_view_once}): "
                       f"{type(e1).__name__}: {e1}")

    # S2: download into memory, write ourselves — avoids partial-file edge cases
    if not downloaded:
        try:
            data = await asyncio.wait_for(
                client.download_media(msg, in_memory=True),
                timeout=90
            )
            if data:
                file_bytes = data.getvalue() if hasattr(data, 'getvalue') else bytes(data)
                with open(dest, 'wb') as fh:
                    fh.write(file_bytes)
                downloaded = dest
                logger.debug(f"[preserve] S2 (in_memory) success msg {msg.id}")
        except Exception as e2:
            logger.warning(f"[preserve] S2 failed msg {msg.id}: "
                           f"{type(e2).__name__}: {e2}")

    # S3: download via file_id directly — bypasses the message reference,
    # useful when the message's file_reference has expired but the file itself
    # is still accessible (common for TTL media)
    if not downloaded and media_obj:
        file_id = getattr(media_obj, 'file_id', None)
        if file_id:
            try:
                data = await asyncio.wait_for(
                    client.download_media(file_id, in_memory=True),
                    timeout=90
                )
                if data:
                    file_bytes = data.getvalue() if hasattr(data, 'getvalue') else bytes(data)
                    with open(dest, 'wb') as fh:
                        fh.write(file_bytes)
                    downloaded = dest
                    logger.debug(f"[preserve] S3 (file_id) success msg {msg.id}")
            except Exception as e3:
                logger.warning(f"[preserve] S3 failed msg {msg.id}: "
                               f"{type(e3).__name__}: {e3}")

    if not downloaded:
        logger.error(
            f"[preserve] ALL strategies failed — msg {msg.id} "
            f"chat {chat_id} type={media_type} view_once={is_view_once} "
            f"media_obj={'present' if media_obj else 'NULL (photo was absent in update)'}"
        )
        return

    # ── Persist to DB ─────────────────────────────────────────────────────────
    try:
        size = os.path.getsize(downloaded)
        if file_bytes is None:
            try:
                with open(downloaded, 'rb') as fh:
                    file_bytes = fh.read()
            except Exception:
                file_bytes = None
        with app.app_context():
            db.session.add(PreservedMedia(
                session_key      = session_key,
                chat_id          = chat_id,
                message_id       = msg.id,
                file_path        = downloaded,
                file_name        = raw_name,
                file_size        = size,
                media_type       = media_type,
                reason           = reason,
                saved_at         = msg.date or datetime.utcnow(),
                original_deleted = False,
                file_data        = file_bytes,
            ))
            db.session.commit()
        logger.info(f"[preserve] saved [{reason}] {raw_name} ({format_file_size(size)})")
    except Exception as dbe:
        from sqlalchemy.exc import IntegrityError as _IntegrityError
        if isinstance(dbe, _IntegrityError):
            # Duplicate — another handler (on_message vs raw safety-net race)
            # already saved this message.  Treat as a successful no-op.
            logger.debug(f"[preserve] duplicate insert ignored for msg {msg.id} "
                         f"(uq_preserved_media_identity) — already preserved")
            try:
                db.session.rollback()
            except Exception:
                pass
        else:
            logger.error(f"[preserve] DB write failed msg {msg.id}: {dbe}")
            try:
                db.session.rollback()
            except Exception:
                pass


def create_telegram_client(session_string):
    session_key = session_string[:16]
    client = Client(
        name=f"session_{hash(session_string)}",
        session_string=session_string,
        api_id=API_ID,
        api_hash=API_HASH,
        proxy=get_proxy_config(),
        in_memory=True
    )

    @client.on_message()
    async def log_message(c, m):
        try:
            with app.app_context():
                db.session.add(MessageStore(
                    message_id=m.id,
                    chat_id=m.chat.id,
                    user_id=m.from_user.id if m.from_user else None,
                    text=m.text or m.caption or "",
                    date=m.date
                ))
                db.session.commit()
        except Exception as e:
            logger.error(f"Error logging message: {e}")
        # Invalidate caches so new messages appear immediately.
        # Also wipe the dialog snapshot — new message may have moved a dialog
        # to the top of the list.
        try:
            chat_id = m.chat.id if m.chat else None
            if chat_id:
                _invalidate_dialog_cache(session_key)
                _api_cache.delete(f"msgs:{session_key}:{chat_id}")
                _api_cache.delete(f"acct:{session_key}")
        except Exception:
            pass
        try:
            await _auto_preserve(c, m, session_key)
        except Exception as e:
            logger.error(f"Auto-preserve error: {e}")

    @client.on_message(pyro_filters.outgoing)
    async def save_outgoing(c, m):
        """Preserve YOUR OWN sent media — in protected chats, OR whenever it's
        view-once media (self-destructing photos/videos you send yourself also
        disappear after being opened, so they need saving too).

        We check the in-memory protected-chat set first (cheap) and only fall
        back to inspecting the media object for ttl_seconds when the chat isn't
        protected, to avoid unnecessary work on ordinary outgoing messages.

        Note: media_obj can be None for true view-once in newer Telegram layers
        (photo=null in update). _auto_preserve handles the fallback get_messages()
        call to recover the media object in that case.
        """
        try:
            chat_id = m.chat.id if m.chat else None
            if not chat_id:
                return
            is_protected = chat_id in _get_protected_set(session_key)
            is_view_once = False
            if not is_protected and m.media:
                media_type = m.media.value
                media_obj  = getattr(m, media_type, None)
                # media_obj can be None for view-once (photo=null in update).
                # Check ttl_seconds if present, or treat None media_obj on a
                # visual type as potentially view-once and let _auto_preserve decide.
                is_view_once = bool(getattr(media_obj, 'ttl_seconds', None))
                if not is_view_once and media_obj is None and media_type in ('photo', 'video', 'voice', 'video_note'):
                    is_view_once = True  # treat as potentially view-once; _auto_preserve will confirm
            if not is_protected and not is_view_once:
                return
            await _auto_preserve(c, m, session_key)
        except Exception as e:
            logger.error(f"Auto-preserve outgoing error: {e}")

    @client.on_raw_update()
    async def _raw_preserve_handler(c, update, users, chats):
        """Raw-level safety net for view-once media.

        Pyrogram's on_message can silently miss view-once photos where Telegram
        sends MessageMediaPhoto with photo=null (flags.0 unset) but ttl_seconds
        set.  At the raw MTProto level we can still see ttl_seconds, and we
        trigger a server-side get_messages() fetch before the file expires.

        Flow:
          1. Check if update is UpdateNewMessage / UpdateNewChannelMessage
          2. Check if the raw media has ttl_seconds but the photo/document is absent
          3. Sleep briefly so on_message fires first (it handles the normal case)
          4. If still not preserved, fetch full message and call _auto_preserve
        """
        from pyrogram import raw as _raw
        try:
            if not isinstance(update, (_raw.types.UpdateNewMessage,
                                        _raw.types.UpdateNewChannelMessage)):
                return

            raw_msg = update.message
            if not isinstance(raw_msg, _raw.types.Message):
                return
            if not raw_msg.media:
                return

            media      = raw_msg.media
            ttl_seconds = getattr(media, 'ttl_seconds', None)
            if not ttl_seconds:
                return  # not a TTL / view-once message — ignore

            # If the raw media has a non-null photo or document, on_message
            # already has a valid media_obj and will handle it via _auto_preserve.
            # We only need to intervene when photo/document is absent (photo=null case).
            has_content = bool(
                getattr(media, 'photo', None) or getattr(media, 'document', None)
            )
            if has_content:
                return  # on_message has the full object; nothing to do here

            # Derive integer chat_id from the raw peer
            peer = getattr(raw_msg, 'peer_id', None)
            if peer is None:
                return
            if hasattr(peer, 'channel_id'):
                chat_id = int(f"-100{peer.channel_id}")
            elif hasattr(peer, 'chat_id'):
                chat_id = -peer.chat_id
            elif hasattr(peer, 'user_id'):
                chat_id = peer.user_id
            else:
                return

            msg_id = raw_msg.id
            logger.info(f"[raw_preserve] detected view-once with photo=null "
                        f"msg={msg_id} chat={chat_id} ttl={ttl_seconds}")

            # Bounded retry: poll the DB for up to 4 s in 0.5 s intervals to give
            # on_message time to fire first (it handles the normal path).  If it
            # hasn't preserved by then, we take over.
            already_saved = False
            for _attempt in range(8):           # 8 × 0.5 s = 4 s total
                await asyncio.sleep(0.5)
                with app.app_context():
                    already_saved = bool(PreservedMedia.query.filter_by(
                        session_key=session_key,
                        chat_id=chat_id,
                        message_id=msg_id,
                    ).first())
                if already_saved:
                    logger.debug(f"[raw_preserve] msg {msg_id} preserved by on_message after "
                                 f"{(_attempt+1)*0.5:.1f}s")
                    break

            if already_saved:
                return

            # Not preserved yet — fetch full message from Telegram's server.
            # The file is still accessible because no client has called
            # readMessageContents yet (we never call it).
            try:
                full_msg = await asyncio.wait_for(
                    c.get_messages(chat_id, msg_id),
                    timeout=20
                )
                if full_msg and full_msg.media:
                    logger.info(f"[raw_preserve] fetched full msg {msg_id} — calling _auto_preserve")
                    await _auto_preserve(c, full_msg, session_key)
                else:
                    logger.warning(f"[raw_preserve] get_messages returned empty for msg {msg_id} "
                                   f"— server may have already expired the view-once media")
            except Exception as fe:
                logger.warning(f"[raw_preserve] get_messages failed for msg {msg_id}: {fe}")

        except Exception as e:
            logger.debug(f"[raw_preserve] handler error: {e}")

    @client.on_deleted_messages()
    async def on_deleted(c, messages):
        try:
            msg_ids = [m.id for m in messages]
            with app.app_context():
                rows = PreservedMedia.query.filter(
                    PreservedMedia.session_key == session_key,
                    PreservedMedia.message_id.in_(msg_ids)
                ).all()
                for row in rows:
                    row.original_deleted = True
                if rows:
                    db.session.commit()
                    logger.info(f"Marked {len(rows)} preserved media as originally deleted")
        except Exception as e:
            logger.error(f"on_deleted error: {e}")

    return client

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('app_authenticated'):
            return redirect(url_for('app_login'))
        return f(*args, **kwargs)
    return decorated

def api_login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('app_authenticated'):
            return jsonify({"error": "Unauthorized", "code": "AUTH_REQUIRED"}), 401
        return f(*args, **kwargs)
    return decorated

# ── Async runner ──────────────────────────────────────────────────────────────

_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True).start()

def run_async(coro):
    return asyncio.run_coroutine_threadsafe(coro, _loop).result()

# ── Startup: restore download queue + auto-reconnect saved Telegram sessions ──

def _startup_restore():
    """On app start, restore the in-memory download_queue from the DB so
    previously queued/completed downloads are visible again after a redeploy."""
    with app.app_context():
        # DB may still be finishing its own boot/recovery right when this
        # thread starts; retry a few times instead of silently giving up.
        rows = None
        for attempt in range(1, 6):
            try:
                rows = ServerDownload.query.all()
                break
            except Exception as e:
                db.session.rollback()
                if attempt == 5:
                    logger.warning(f"[startup] Giving up restoring downloads from DB after {attempt} attempts: {e}")
                    return
                delay = 1.5 * attempt
                logger.warning(f"[startup] DB not ready for download restore (attempt {attempt}/5): {e}. Retrying in {delay:.1f}s...")
                time.sleep(delay)
        for row in rows or []:
            download_queue[row.download_id] = {
                "status":    row.status or "done",
                "chat_id":   str(row.chat_id),
                "msg_id":    str(row.message_id),
                "filename":  row.file_name,
                "safe_name": row.file_name,
                "path":      None,   # disk path gone after redeploy; DB copy used
                "error":     row.error,
                "size":      format_file_size(row.file_size) if row.file_size else None,
            }
        logger.info(f"[startup] Restored {len(download_queue)} download record(s) from DB.")

threading.Thread(target=_startup_restore, daemon=True).start()

def _startup_reconnect_sessions():
    """Auto-reconnect every saved Telegram session on process start, so
    message logging / view-once & protected-chat capture run continuously
    on the server — with no browser tab, cookie, or manual click required.

    Runs in the background dedicated event loop (_loop) since Pyrogram
    clients must be started from an async context.
    """
    with app.app_context():
        rows = None
        for attempt in range(1, 6):
            try:
                rows = StoredSession.query.all()
                break
            except Exception as e:
                db.session.rollback()
                if attempt == 5:
                    logger.warning(f"[startup] Giving up auto-reconnecting sessions after {attempt} attempts: {e}")
                    return
                delay = 1.5 * attempt
                logger.warning(f"[startup] DB not ready for session reconnect (attempt {attempt}/5): {e}. Retrying in {delay:.1f}s...")
                time.sleep(delay)

        for row in rows or []:
            try:
                run_async(get_client(row.session_string))
                row.reconnect_failed = False
                row.last_seen = datetime.utcnow()
                db.session.commit()
                logger.info(f"[startup] Auto-reconnected saved session '{row.label}'")
            except Exception as e:
                db.session.rollback()
                err = str(e)
                if any(k in err.upper() for k in (
                    "AUTH_KEY_DUPLICATED", "SESSION_REVOKED", "SESSION_EXPIRED",
                    "AUTH_KEY_INVALID", "USER_DEACTIVATED"
                )):
                    try:
                        row.reconnect_failed = True
                        db.session.commit()
                    except Exception:
                        db.session.rollback()
                logger.warning(f"[startup] Auto-reconnect failed for '{row.label}': {e}")

# Delay slightly so _startup_restore's DB retries aren't racing this thread
# for the same connection pool right at cold start.
threading.Timer(3.0, lambda: threading.Thread(target=_startup_reconnect_sessions, daemon=True).start()).start()

# ── General-purpose TTL cache (dialogs, messages, account info) ───────────────

class _TTLCache:
    """Thread-safe in-memory cache with per-entry TTL (seconds)."""
    def __init__(self):
        self._data = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            value, expires = entry
            if datetime.utcnow().timestamp() > expires:
                del self._data[key]
                return None
            return value

    def set(self, key, value, ttl=120):
        with self._lock:
            self._data[key] = (value, datetime.utcnow().timestamp() + ttl)

    def delete(self, key):
        with self._lock:
            self._data.pop(key, None)

    def delete_prefix(self, prefix):
        with self._lock:
            for k in [k for k in self._data if k.startswith(prefix)]:
                del self._data[k]

_api_cache = _TTLCache()

# ── Dialog snapshot — one full fetch per session, sliced for pagination ────────
# Pyrogram has no server-side offset for get_dialogs(), so every paginated call
# used to re-scan from the beginning. We fetch ALL dialogs once, cache the full
# list, and serve slices from memory. Invalidated on new message or manual refresh.
_dialog_snapshots: dict = {}       # session_key -> {"items": [...], "expires": float}
_dialog_snapshot_lock = threading.Lock()
_DIALOG_SNAPSHOT_TTL = 900         # 15 min

# In-flight guard: prevents concurrent requests from all firing a full
# get_dialogs() scan simultaneously when the snapshot is cold.  The first
# request that finds no snapshot sets its Future here; others wait on it.
_dialog_fetch_inflight: dict = {}  # session_key -> concurrent.futures.Future
_dialog_fetch_inflight_lock = threading.Lock()

def _invalidate_dialog_cache(session_key: str):
    """Clear both the per-page TTL cache and the full-list snapshot."""
    _api_cache.delete_prefix(f"dialogs:{session_key}")
    with _dialog_snapshot_lock:
        _dialog_snapshots.pop(session_key, None)

def _is_peer_invalid(e) -> bool:
    return "PEER_ID_INVALID" in str(e)

async def _resync_peers(client, session_key=None):
    """Force a fresh get_dialogs() scan to re-resolve peer access hashes.

    Pyrogram (especially with in_memory=True sessions) only learns a peer's
    access_hash by seeing it in get_dialogs() or incoming updates. Right after
    a fresh login, or after the process restarts, that cache is empty — so
    calling get_chat()/get_chat_history() by numeric ID can fail with
    PEER_ID_INVALID even for chats the account is still a normal member of.
    Re-scanning dialogs repopulates the cache; if the chat is still present
    there, the retry succeeds.
    """
    try:
        if session_key:
            _invalidate_dialog_cache(session_key)
        async for _ in client.get_dialogs():
            pass
    except Exception as exc:
        logger.warning(f"_resync_peers failed: {exc}")

# ── Chat info cache — avoids an extra get_chat() round-trip per message load ──
_chat_info_cache: dict = {}        # (session_key, chat_id) -> {"name":str,"can_manage":bool,"exp":float}
_chat_info_lock = threading.Lock()
_CHAT_INFO_TTL = 3600              # 1 hour

# ── Protected chats — memory set so _auto_preserve skips DB on every message ──
_protected_chats_mem: dict = {}    # session_key -> set[int] | None=unloaded
_protected_chats_mem_lock = threading.Lock()

def _get_protected_set(session_key: str) -> set:
    """Return the set of protected chat_ids for session_key (loads from DB once)."""
    with _protected_chats_mem_lock:
        s = _protected_chats_mem.get(session_key)
        if s is not None:
            return s
    with app.app_context():
        rows = ProtectedChat.query.filter_by(session_key=session_key).all()
        result = {r.chat_id for r in rows}
    with _protected_chats_mem_lock:
        _protected_chats_mem[session_key] = result
    return result

# ── Translation cache — avoids re-hitting AI APIs for identical requests ──────
_translation_cache: dict = {}      # (text_md5, target_lang) -> (translated, engine)
_translation_cache_lock = threading.Lock()
_TRANSLATION_CACHE_MAX = 500

# ── Thumbnail cache & concurrency guards ──────────────────────────────────────

THUMB_CACHE_MAX = 300
THUMB_TIMEOUT   = 25   # seconds per thumb download — Telegram's own flood-wait on
                       # upload.GetFile can force multi-second delays server-side,
                       # so this must be generous enough to survive that wait
                       # instead of aborting a request Telegram was about to honor.
_thumb_cache      = {}
_thumb_cache_lock = threading.Lock()

def _cache_get(key):
    with _thumb_cache_lock:
        return _thumb_cache.get(key)

def _cache_set(key, value):
    with _thumb_cache_lock:
        if len(_thumb_cache) >= THUMB_CACHE_MAX:
            for k in list(_thumb_cache.keys())[:60]:
                del _thumb_cache[k]
        _thumb_cache[key] = value

# ── Disk thumbnail cache ───────────────────────────────────────────────────────
# Thumbnails fetched from Telegram are written to THUMBNAILS_DIR so they
# survive app restarts and don't require another Telegram API round-trip.
# Two tiny sidecar files per thumbnail: {hash}.bin (raw bytes) + {hash}.mime.

def _thumb_disk_key(cache_key):
    return hashlib.md5(cache_key.encode()).hexdigest()

def _thumb_disk_read(cache_key):
    """Return (data_bytes, mime_str) from disk cache, or (None, None) on miss.

    Uses try/open directly instead of os.path.exists + open — removes two extra
    syscalls on every cache miss and avoids a TOCTOU race.
    """
    h = _thumb_disk_key(cache_key)
    bin_path  = os.path.join(THUMBNAILS_DIR, f"{h}.bin")
    mime_path = os.path.join(THUMBNAILS_DIR, f"{h}.mime")
    try:
        with open(bin_path, "rb") as f:
            data = f.read()
        with open(mime_path, "r") as f:
            mime = f.read().strip()
        return data, mime
    except (FileNotFoundError, OSError):
        pass
    except Exception:
        pass
    return None, None

def _thumb_disk_write(cache_key, data, mime):
    """Write thumbnail bytes + mime to disk cache (best-effort, never raises)."""
    h = _thumb_disk_key(cache_key)
    try:
        with open(os.path.join(THUMBNAILS_DIR, f"{h}.bin"), "wb") as f:
            f.write(data)
        with open(os.path.join(THUMBNAILS_DIR, f"{h}.mime"), "w") as f:
            f.write(mime)
    except Exception as e:
        logger.debug(f"thumb disk write failed: {e}")

async def _make_semaphore(n):
    return asyncio.Semaphore(n)

# Kept modest on purpose: Telegram enforces its own per-account flood control on
# upload.GetFile independent of our concurrency. Firing more requests at once only
# makes Telegram impose longer waits on *every* request, which is what caused the
# timeout storm — so a smaller, steadier concurrency actually loads faster overall.
_thumb_sem  = asyncio.run_coroutine_threadsafe(_make_semaphore(3), _loop).result()
_stream_sem = asyncio.run_coroutine_threadsafe(_make_semaphore(3), _loop).result()

# In-flight de-duplication: if the same thumbnail is already being fetched from
# Telegram, concurrent/retried requests await that same result instead of firing
# another upload.GetFile call and adding to the flood-wait queue.
_thumb_inflight = {}
_thumb_inflight_lock = threading.Lock()

# ── Client management ─────────────────────────────────────────────────────────

def _is_broken_pipe(e):
    return "Broken pipe" in str(e) or "BrokenPipeError" in type(e).__name__

def _is_auth_key_duplicated(e):
    return "AUTH_KEY_DUPLICATED" in str(e)

async def clear_client(session_string):
    if session_string in telegram_clients:
        try:
            await telegram_clients[session_string].stop()
        except Exception:
            pass
        del telegram_clients[session_string]

async def get_client(session_string, force_reconnect=False):
    if not session_string:
        return None
    if force_reconnect and session_string in telegram_clients:
        try:
            await telegram_clients[session_string].stop()
        except Exception:
            pass
        del telegram_clients[session_string]
    if session_string not in telegram_clients:
        client = create_telegram_client(session_string)
        await client.start()
        telegram_clients[session_string] = client
    return telegram_clients[session_string]

async def run_with_reconnect(session_string, coro_factory):
    """Run coro_factory(client) with automatic retry on broken-pipe and FloodWait.

    FloodWait strategy: sleep the exact wait Telegram demands + a small random
    jitter (1-3 s), then retry — up to 3 times before giving up.

    Broken-pipe strategy: force-reconnect the client once.  Tracked independently
    from FloodWait so a broken-pipe that follows a FloodWait sleep still gets its
    reconnect attempt (prior code only allowed it on attempt == 0).
    """
    flood_attempts = 0
    broken_pipe_retried = False
    for attempt in range(7):
        try:
            # Force-reconnect on broken-pipe retry; NOT on FloodWait retries
            # (those reuse the same connection — force-reconnect is unnecessary
            # and would waste the session-start handshake time).
            force = broken_pipe_retried and attempt > 0
            client = await get_client(session_string, force_reconnect=force)
            return await coro_factory(client)
        except Exception as e:
            if _FloodWait and isinstance(e, _FloodWait):
                flood_attempts += 1
                if flood_attempts > 3:
                    raise
                wait_s = max(getattr(e, 'value', 30), 1) + random.uniform(1, 3)
                logger.warning(f"FloodWait {getattr(e, 'value', '?')}s — sleeping {wait_s:.1f}s "
                               f"(flood attempt {flood_attempts}/3)")
                await asyncio.sleep(wait_s)
                continue
            if _is_broken_pipe(e) and not broken_pipe_retried:
                broken_pipe_retried = True
                continue
            raise

# ── Download (server-side) ────────────────────────────────────────────────────

async def _download_to_server(download_id, session_str, chat_id, msg_id):
    entry = download_queue[download_id]
    sk = session_str[:16]
    try:
        entry["status"] = "downloading"
        try:
            peer_id = int(chat_id)
        except Exception:
            peer_id = chat_id

        async def _do(client):
            msg = await client.get_messages(peer_id, int(msg_id))
            if not msg or not msg.media:
                entry["status"] = "failed"
                entry["error"] = "No downloadable media"
                return
            media_obj = getattr(msg, msg.media.value, None)
            file_name = getattr(media_obj, "file_name", None)
            if not file_name:
                ext = ".file"
                if msg.photo:        ext = ".jpg"
                elif msg.video:      ext = ".mp4"
                elif msg.audio:      ext = ".mp3"
                elif msg.voice:      ext = ".ogg"
                elif msg.animation:  ext = ".mp4"
                elif msg.sticker:    ext = ".webp"
                elif msg.video_note: ext = ".mp4"
                file_name = f"file_{msg_id}{ext}"
            safe_name = f"{download_id}_{_sanitize_filename(file_name)}"
            dest = os.path.join(DOWNLOADS_DIR, safe_name)
            entry["filename"] = file_name
            entry["safe_name"] = safe_name
            await client.download_media(msg, file_name=dest)
            if os.path.exists(dest):
                file_size = os.path.getsize(dest)
                entry["size"] = format_file_size(file_size)
                entry["path"] = dest
                entry["status"] = "done"
                # Persist metadata to DB — no file_data blob, disk file is the source of truth
                try:
                    with app.app_context():
                        existing = ServerDownload.query.filter_by(download_id=download_id).first()
                        if existing:
                            existing.file_name = file_name
                            existing.file_size = file_size
                            existing.file_data = None   # never store blob — disk file is enough
                            existing.file_path = dest
                            existing.status    = "done"
                        else:
                            db.session.add(ServerDownload(
                                download_id = download_id,
                                session_key = sk,
                                chat_id     = int(chat_id),
                                message_id  = int(msg_id),
                                file_name   = file_name,
                                file_size   = file_size,
                                file_data   = None,     # never store blob — disk file is enough
                                file_path   = dest,
                                status      = "done",
                            ))
                        db.session.commit()
                    logger.info(f"Download {download_id} persisted to DB ({format_file_size(file_size)}): {file_name}")
                except Exception as db_err:
                    # Roll back so this session isn't left in a dirty/broken
                    # state for the next DB operation on this thread — without
                    # this, one transient failure here could cascade into
                    # unrelated later queries failing too.
                    db.session.rollback()
                    logger.warning(f"Could not persist download {download_id} to DB: {db_err}")
            else:
                entry["status"] = "failed"
                entry["error"] = "File not written"

        await run_with_reconnect(session_str, _do)
    except Exception as e:
        logger.error(f"Server download error [{download_id}]: {e}")
        entry["status"] = "failed"
        entry["error"] = str(e)

# ── Telegram data helpers ─────────────────────────────────────────────────────

DIALOGS_PAGE_SIZE = 100  # Fetch chats/channels in bigger batches so each scroll
                          # doesn't burn a separate Telegram API call — 100 per
                          # request instead of 20 means 5x fewer round trips.

async def get_dialogs_list(client, offset=0, limit=DIALOGS_PAGE_SIZE, session_key=None):
    """Return a page of dialogs.

    Uses a full-list snapshot so every paginated scroll request is served from
    memory instead of re-scanning from the beginning of get_dialogs() each time.
    The snapshot is invalidated by _invalidate_dialog_cache() whenever a new
    message arrives or the cache is manually cleared.

    In-flight guard: only ONE coroutine fires the full Telegram get_dialogs()
    scan at a time.  Concurrent requests that miss the snapshot wait for the
    in-progress fetch to complete, then read the result from the snapshot.
    This prevents a burst of simultaneous cache misses (e.g. on first load)
    from each triggering a full scan — which would hammer Telegram's rate limits.
    """
    if session_key:
        now = datetime.utcnow().timestamp()
        with _dialog_snapshot_lock:
            snap = _dialog_snapshots.get(session_key)
            if snap and snap["expires"] > now:
                return snap["items"][offset:offset + limit]

        # Check/register in-flight guard
        with _dialog_fetch_inflight_lock:
            fut = _dialog_fetch_inflight.get(session_key)
            is_owner = fut is None
            if is_owner:
                fut = concurrent.futures.Future()
                _dialog_fetch_inflight[session_key] = fut

        if not is_owner:
            # Another coroutine is already fetching — wait for it, then slice
            try:
                all_dialogs = fut.result(timeout=120)
            except Exception:
                all_dialogs = []
            return all_dialogs[offset:offset + limit]

    try:
        # No valid snapshot and we are the owner — fetch ALL dialogs from Telegram.
        all_dialogs = []
        async for dialog in client.get_dialogs():
            all_dialogs.append({
                "name": dialog.chat.title or dialog.chat.first_name or "Unknown",
                "id": dialog.chat.id,
                "unread_count": dialog.unread_messages_count,
                "is_channel": dialog.chat.type.value == "channel",
                "is_group": dialog.chat.type.value in ["group", "supergroup"],
                "can_manage": dialog.chat.type.value in ["channel", "group", "supergroup"],
                "username": getattr(dialog.chat, "username", None) or "",
            })

        if session_key:
            # Pre-fill chat info cache from dialog data so the first openChat()
            # → loadMessages() call for any dialog skips the get_chat() round-trip
            # entirely — we already know the name and type from get_dialogs().
            _now = time.time()
            with _chat_info_lock:
                for _d in all_dialogs:
                    _ck = (session_key, _d["id"])
                    if _ck not in _chat_info_cache or _chat_info_cache[_ck].get("exp", 0) < _now:
                        _chat_info_cache[_ck] = {
                            "name": _d["name"],
                            "can_manage": _d["can_manage"],
                            "exp": _now + _CHAT_INFO_TTL,
                        }

            with _dialog_snapshot_lock:
                _dialog_snapshots[session_key] = {
                    "items": all_dialogs,
                    "expires": datetime.utcnow().timestamp() + _DIALOG_SNAPSHOT_TTL,
                }
            with _dialog_fetch_inflight_lock:
                if session_key in _dialog_fetch_inflight:
                    _dialog_fetch_inflight[session_key].set_result(all_dialogs)
                    del _dialog_fetch_inflight[session_key]

        return all_dialogs[offset:offset + limit]
    except Exception as exc:
        if session_key:
            with _dialog_fetch_inflight_lock:
                f = _dialog_fetch_inflight.pop(session_key, None)
            if f:
                f.set_exception(exc)
        raise

async def get_account_info(session_string):
    sk = session_string[:16]
    cache_key = f"acct:{sk}"
    cached = _api_cache.get(cache_key)
    if cached:
        return cached
    try:
        async def _do(client):
            me = await client.get_me()
            # Parallelize profile photo download + dialog fetch — both are
            # independent of each other after get_me() resolves.
            # Use small_file_id: the sidebar avatar renders at 38×38 px; the
            # big photo (640×640) wastes bandwidth and a Telegram call quota slot.
            async def _get_photo():
                if not me.photo:
                    return None
                try:
                    data = await client.download_media(me.photo.small_file_id, in_memory=True)
                    if data:
                        return base64.b64encode(data.getvalue()).decode('utf-8')
                except Exception:
                    pass
                return None

            profile_photo, dialogs = await asyncio.gather(
                _get_photo(),
                get_dialogs_list(client, offset=0, session_key=sk),
            )
            return {
                "id": me.id,
                "first_name": me.first_name or "",
                "last_name": me.last_name or "",
                "username": me.username or "No username",
                "phone": me.phone_number or "Hidden",
                "profile_photo": profile_photo,
                "dialogs": dialogs,
                "has_more_dialogs": len(dialogs) == DIALOGS_PAGE_SIZE
            }
        result = await run_with_reconnect(session_string, _do)
        if "error" not in result:
            _api_cache.set(cache_key, result, ttl=1800)  # 30 min
        return result
    except Exception as e:
        return {"error": str(e)}

DOWNLOADABLE_MEDIA = {"photo", "video", "document", "audio", "voice", "animation", "video_note", "sticker"}

def _extract_forward(msg):
    # Use the non-deprecated forward_origin API (Pyrofork ≥ 2.x).
    # Falls back gracefully to None if origin is absent.
    origin = getattr(msg, 'forward_origin', None)
    if origin is None:
        return None
    # MessageOriginUser — forwarded from a visible user
    sender_user = getattr(origin, 'sender_user', None)
    if sender_user:
        name = (
            f"{getattr(sender_user, 'first_name', '') or ''} "
            f"{getattr(sender_user, 'last_name', '') or ''}"
        ).strip()
        return name or "Unknown"
    # MessageOriginChannel (has .chat) or MessageOriginChat (has .sender_chat)
    sender_chat = getattr(origin, 'sender_chat', None) or getattr(origin, 'chat', None)
    if sender_chat:
        return (
            getattr(sender_chat, 'title', None)
            or getattr(sender_chat, 'first_name', None)
            or "Unknown"
        )
    # MessageOriginHiddenUser — privacy-protected sender name
    sender_user_name = getattr(origin, 'sender_user_name', None)
    if sender_user_name:
        return sender_user_name
    return None

def _extract_sender(msg):
    """Return (display_name, sender_key, username_or_None) for incoming messages."""
    u = getattr(msg, 'from_user', None)
    if u:
        parts = [p for p in [getattr(u, 'first_name', None), getattr(u, 'last_name', None)] if p]
        name = " ".join(parts) if parts else (f"@{u.username}" if getattr(u, 'username', None) else f"User {u.id}")
        return name, str(u.id), getattr(u, 'username', None)
    sc = getattr(msg, 'sender_chat', None)
    if sc:
        name = getattr(sc, 'title', None) or getattr(sc, 'username', None) or f"Chat {sc.id}"
        return name, str(sc.id), getattr(sc, 'username', None)
    sig = getattr(msg, 'author_signature', None) or getattr(msg, 'post_author', None)
    if sig:
        return sig, sig, None
    return None, None, None

PREVIEW_KINDS = {"photo", "video", "audio", "voice", "sticker", "animation", "video_note"}

def _build_msg_dict(msg):
    media_type = msg.media.value if msg.media else None
    downloadable = media_type in DOWNLOADABLE_MEDIA if media_type else False
    sender_name, sender_key, sender_username = _extract_sender(msg) if not msg.outgoing else (None, None, None)

    preview_kind = media_type if media_type in PREVIEW_KINDS else None
    mime_type    = None
    is_view_once = False
    ttl_seconds  = None

    m = {
        "id":           msg.id,
        "date":         msg.date.isoformat() if msg.date else None,
        "text":         msg.text or msg.caption or "",
        "is_outgoing":  msg.outgoing,
        "has_media":    downloadable,
        "media_type":   media_type,
        "preview_kind": preview_kind,
        "forward_from": _extract_forward(msg),
        "sender_name":  sender_name,
        "sender_key":   sender_key,
        "sender_username": sender_username,
    }
    if downloadable:
        try:
            obj = getattr(msg, media_type)
            if obj:
                if hasattr(obj, 'file_size') and obj.file_size:
                    m["file_size"] = format_file_size(obj.file_size)
                if hasattr(obj, 'file_name') and obj.file_name:
                    m["file_name"] = obj.file_name
                if hasattr(obj, 'duration') and obj.duration:
                    m["duration"] = obj.duration
                mime_type   = getattr(obj, 'mime_type', None)
                ttl_seconds = getattr(obj, 'ttl_seconds', None)
                is_view_once = bool(ttl_seconds)
        except Exception:
            pass
    m["mime_type"]    = mime_type
    m["is_view_once"] = is_view_once
    m["ttl_seconds"]  = ttl_seconds
    return m

async def get_messages_from_chat(session_string, chat_id, limit=100, offset_id=0, query=None, media_only=False):
    try:
        chat_id = int(chat_id)
    except Exception:
        pass

    sk = session_string[:16]
    # Only cache standard (non-search, non-media-only, first page) requests
    use_cache = (not query and not media_only and offset_id == 0)
    cache_key = f"msgs:{sk}:{chat_id}" if use_cache else None
    if cache_key:
        cached = _api_cache.get(cache_key)
        if cached is not None:
            return cached

    try:
        async def _do(client, _resynced=False):
            # Chat metadata (name + can_manage) is cached for 1 hour so we don't
            # fire an extra get_chat() round-trip on every uncached message fetch.
            now = time.time()
            ck = (sk, chat_id)
            with _chat_info_lock:
                ci = _chat_info_cache.get(ck)
            if ci and ci["exp"] > now:
                chat_name  = ci["name"]
                can_manage = ci["can_manage"]
            else:
                chat = None
                try:
                    chat = await client.get_chat(chat_id)
                except Exception as e:
                    logger.warning(f"get_chat failed for {chat_id}: {e}")
                    # get_chat can fail with PEER_ID_INVALID if the Pyrogram session
                    # hasn't cached the access hash yet — but get_chat_history often
                    # still works. Continue with a fallback name instead of bailing.
                chat_name = (
                    (getattr(chat, "title", None) or getattr(chat, "first_name", None))
                    if chat else None
                ) or "Chat"
                can_manage = (
                    getattr(chat.type, "value", "") in ["channel", "group", "supergroup"]
                    if chat else False
                )
                with _chat_info_lock:
                    _chat_info_cache[ck] = {
                        "name": chat_name,
                        "can_manage": can_manage,
                        "exp": now + _CHAT_INFO_TTL,
                    }

            messages = []
            has_more = False

            if query:
                try:
                    async for msg in client.search_messages(chat_id, query=query, limit=limit):
                        if media_only and not msg.media:
                            continue
                        messages.append(_build_msg_dict(msg))
                    messages.sort(key=lambda x: x["id"], reverse=True)
                except Exception as e:
                    if _is_peer_invalid(e) and not _resynced:
                        logger.warning(f"search_messages PEER_ID_INVALID for {chat_id} — resyncing dialogs and retrying")
                        await _resync_peers(client, session_key=sk)
                        return await _do(client, _resynced=True)
                    logger.warning(f"search_messages failed for {chat_id}: {e}")
                    return {"error": f"Could not search messages: {e}"}
            elif media_only:
                count = 0
                # Walk backwards in batches of 200 until we have `limit` media
                # items or exhaust history.  A single 200-message window is
                # almost never enough in chatty groups where media is sparse.
                _MEDIA_BATCH = 200
                cur_offset = offset_id if offset_id > 0 else 0
                try:
                    while count < limit:
                        batch_seen = 0
                        last_id = None
                        async for msg in client.get_chat_history(
                            chat_id, limit=_MEDIA_BATCH, offset_id=cur_offset
                        ):
                            batch_seen += 1
                            last_id = msg.id
                            if not msg.media:
                                continue
                            if (msg.media.value if msg.media else None) not in DOWNLOADABLE_MEDIA:
                                continue
                            messages.append(_build_msg_dict(msg))
                            count += 1
                            if count >= limit:
                                has_more = True
                                break

                        if count >= limit:
                            break  # done — hit the page limit
                        if batch_seen < _MEDIA_BATCH or last_id is None:
                            has_more = False  # short batch → end of history
                            break  # reached start of history
                        # Advance the offset to continue from where we left off
                        cur_offset = last_id
                except Exception as e:
                    if _is_peer_invalid(e) and not _resynced:
                        logger.warning(f"get_chat_history (media) PEER_ID_INVALID for {chat_id} — resyncing dialogs and retrying")
                        await _resync_peers(client, session_key=sk)
                        return await _do(client, _resynced=True)
                    logger.warning(f"get_chat_history (media) failed for {chat_id}: {e}")
                    if not messages:
                        return {"error": f"Could not load media: {e}"}
            else:
                try:
                    async for msg in client.get_chat_history(chat_id, limit=limit, offset_id=offset_id if offset_id > 0 else 0):
                        messages.append(_build_msg_dict(msg))
                    has_more = len(messages) >= limit
                except Exception as e:
                    if _is_peer_invalid(e) and not _resynced:
                        logger.warning(f"get_chat_history PEER_ID_INVALID for {chat_id} — resyncing dialogs and retrying")
                        await _resync_peers(client, session_key=sk)
                        return await _do(client, _resynced=True)
                    logger.warning(f"get_chat_history failed for {chat_id}: {e}")
                    if not messages:
                        return {
                            "error": (
                                "This chat is no longer reachable on your Telegram "
                                "account (you may have left it, been removed, or it "
                                "was deleted). Telegram error: " + str(e)
                            ) if _is_peer_invalid(e) else f"Could not load messages: {e}"
                        }

            return {
                "messages": messages,
                "chat_name": chat_name,
                "has_more": has_more,
                "can_manage": can_manage,
            }

        result = await run_with_reconnect(session_string, _do)
        if cache_key and "error" not in result:
            _api_cache.set(cache_key, result, ttl=120)  # 2 min
        return result
    except Exception as e:
        logger.error(f"Error in get_messages_from_chat: {e}", exc_info=True)
        return {"error": str(e)}

# ── Media helpers ─────────────────────────────────────────────────────────────

def _guess_mime(media_type, mime_from_obj=None):
    if mime_from_obj:
        return mime_from_obj
    return {
        "photo": "image/jpeg", "sticker": "image/webp",
        "audio": "audio/mpeg", "voice": "audio/ogg",
        "video": "video/mp4", "video_note": "video/mp4", "animation": "video/mp4",
    }.get(media_type, "application/octet-stream")

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/api/media-thumb/<chat_id>/<message_id>")
@api_login_required
def media_thumb(chat_id, message_id):
    session_str = session.get("session_string")
    if not session_str:
        return jsonify({"error": "No session", "code": "AUTH_REQUIRED"}), 401

    cache_key = f"{session_str[:16]}:{chat_id}:{message_id}"
    cached = _cache_get(cache_key)
    if cached:
        data, mime = cached
        return Response(data, mimetype=mime, headers={"Cache-Control": "private, max-age=7200"})

    # Disk cache — survives restarts; much faster than a Telegram round-trip.
    disk_data, disk_mime = _thumb_disk_read(cache_key)
    if disk_data:
        _cache_set(cache_key, (disk_data, disk_mime))  # warm in-memory cache too
        return Response(disk_data, mimetype=disk_mime, headers={"Cache-Control": "private, max-age=7200"})

    async def get_thumb():
        async with _thumb_sem:
            try:
                peer_id = int(chat_id)
            except Exception:
                peer_id = chat_id

            async def _do(client):
                msg = await asyncio.wait_for(
                    client.get_messages(peer_id, int(message_id)), timeout=8
                )
                if not msg or not msg.media:
                    return None, None, "NO_MEDIA"
                media_type = msg.media.value if msg.media else None

                if media_type == "photo":
                    obj = msg.photo
                    if obj is None:
                        # View-once photo (photo=null in update): try to serve
                        # the preserved copy from DB if we saved it earlier.
                        sk = session_str[:16] if session_str else None
                        if sk:
                            with app.app_context():
                                pm = PreservedMedia.query.filter_by(
                                    session_key=sk,
                                    chat_id=peer_id,
                                    message_id=int(message_id),
                                ).first()
                                if pm and pm.file_data:
                                    return pm.file_data, "image/jpeg", None
                        return None, None, "NO_MEDIA"
                    thumbs = getattr(obj, "thumbs", None)
                    if thumbs:
                        # Pick a small/medium-res thumb instead of the full photo
                        # to keep previews fast and avoid timeouts on large images.
                        pick = thumbs[len(thumbs) // 2] if len(thumbs) > 1 else thumbs[0]
                        try:
                            t = await asyncio.wait_for(
                                client.download_media(pick.file_id, in_memory=True), timeout=THUMB_TIMEOUT
                            )
                            return t.getvalue(), "image/jpeg", None
                        except Exception:
                            pass  # fall through to full-res download below
                    data = await asyncio.wait_for(
                        client.download_media(msg, in_memory=True), timeout=THUMB_TIMEOUT
                    )
                    return data.getvalue(), "image/jpeg", None

                if media_type == "sticker":
                    obj = msg.sticker
                    mime = getattr(obj, "mime_type", "image/webp") or "image/webp"
                    is_animated = getattr(obj, "is_animated", False)
                    is_video_sticker = getattr(obj, "is_video", False)
                    if is_animated or is_video_sticker or "video" in mime or "webm" in mime:
                        thumbs = getattr(obj, "thumbs", None)
                        if thumbs:
                            t = await asyncio.wait_for(
                                client.download_media(thumbs[-1].file_id, in_memory=True), timeout=THUMB_TIMEOUT
                            )
                            return t.getvalue(), "image/jpeg", None
                        return None, None, "NO_PREVIEW"
                    data = await asyncio.wait_for(
                        client.download_media(msg, in_memory=True), timeout=THUMB_TIMEOUT
                    )
                    return data.getvalue(), mime, None

                if media_type == "animation":
                    obj = msg.animation
                    thumbs = getattr(obj, "thumbs", None)
                    if thumbs:
                        t = await asyncio.wait_for(
                            client.download_media(thumbs[-1].file_id, in_memory=True), timeout=THUMB_TIMEOUT
                        )
                        return t.getvalue(), "image/jpeg", None
                    return None, None, "NO_PREVIEW"

                if media_type in ("video", "video_note"):
                    obj = getattr(msg, media_type)
                    thumbs = getattr(obj, "thumbs", None)
                    if thumbs:
                        t = await asyncio.wait_for(
                            client.download_media(thumbs[-1].file_id, in_memory=True), timeout=THUMB_TIMEOUT
                        )
                        return t.getvalue(), "image/jpeg", None
                    return None, None, "NO_PREVIEW"

                return None, None, "NO_PREVIEW"

            return await run_with_reconnect(session_str, _do)

    # De-dupe concurrent/retried requests for the same thumbnail: only one
    # actually talks to Telegram, everyone else just awaits its result. This
    # keeps retries (including bulk "load all timed out") from stacking more
    # upload.GetFile calls onto Telegram's flood-wait queue.
    with _thumb_inflight_lock:
        fut = _thumb_inflight.get(cache_key)
        is_owner = fut is None
        if is_owner:
            fut = concurrent.futures.Future()
            _thumb_inflight[cache_key] = fut

    if not is_owner:
        try:
            data, mime, code = fut.result(timeout=THUMB_TIMEOUT + 15)
        except Exception:
            return jsonify({"error": "Timeout", "code": "TIMEOUT", "retryable": True}), 504
        if code:
            return jsonify({"error": code, "code": code, "retryable": False}), 404
        return Response(data, mimetype=mime, headers={"Cache-Control": "private, max-age=7200"})

    try:
        result = run_async(get_thumb())
        fut.set_result(result)
        data, mime, code = result
        if code:
            return jsonify({"error": code, "code": code, "retryable": False}), 404
        _cache_set(cache_key, (data, mime))
        _thumb_disk_write(cache_key, data, mime)  # persist to disk so restarts skip Telegram
        return Response(data, mimetype=mime, headers={"Cache-Control": "private, max-age=7200"})
    except asyncio.TimeoutError:
        fut.set_exception(asyncio.TimeoutError())
        return jsonify({"error": "Timeout", "code": "TIMEOUT", "retryable": True}), 504
    except Exception as e:
        fut.set_exception(e)
        err = str(e)
        # "doesn't contain any downloadable media" = expected for view-once
        # messages where photo=null; not a real error, demote to debug.
        if "doesn't contain any downloadable media" in err:
            logger.debug(f"media-thumb no-media (view-once or expired): {err}")
        else:
            logger.error(f"media-thumb error: {err}")
        if "FLOOD" in err or "Too Many" in err.lower():
            return jsonify({"error": "Rate limited", "code": "RATE_LIMITED", "retryable": True}), 429
        if any(k in err.upper() for k in ("AUTH", "SESSION", "UNAUTHORIZED")):
            return jsonify({"error": "Session expired", "code": "AUTH_EXPIRED", "retryable": False}), 401
        return jsonify({"error": "Server error", "code": "INTERNAL", "retryable": True}), 500
    finally:
        with _thumb_inflight_lock:
            if _thumb_inflight.get(cache_key) is fut:
                del _thumb_inflight[cache_key]


@app.route("/api/media-stream/<chat_id>/<message_id>")
@api_login_required
def media_stream(chat_id, message_id):
    session_str = session.get("session_string")
    if not session_str:
        return jsonify({"error": "No session", "code": "AUTH_REQUIRED"}), 401

    async def get_info():
        async with _stream_sem:
            try:
                peer_id = int(chat_id)
            except Exception:
                peer_id = chat_id

            async def _do(client):
                msg = await asyncio.wait_for(
                    client.get_messages(peer_id, int(message_id)), timeout=8
                )
                if not msg or not msg.media:
                    return None, None, None
                media_type = msg.media.value if msg.media else None
                try:
                    obj = getattr(msg, media_type)
                    mime = getattr(obj, "mime_type", None)
                    size = getattr(obj, "file_size", None)
                except Exception:
                    mime, size = None, None
                mime = _guess_mime(media_type, mime)
                return msg, mime, size

            return await run_with_reconnect(session_str, _do)

    try:
        msg, mime, file_size = run_async(get_info())
        if not msg:
            return jsonify({"error": "Not found", "code": "NOT_FOUND"}), 404

        def stream_wrapper():
            import queue as _q
            q = _q.Queue(maxsize=20)

            async def producer():
                # msg was already fetched by get_info() above — reuse it here
                # instead of calling get_messages() a second time.
                async with _stream_sem:
                    async def _do(client):
                        if not msg or not msg.media:
                            return
                        async for chunk in client.stream_media(msg):
                            while True:
                                try:
                                    q.put(chunk, block=True, timeout=2.0)
                                    break
                                except _q.Full:
                                    continue

                    try:
                        await run_with_reconnect(session_str, _do)
                    except Exception as e:
                        logger.error(f"stream producer error: {e}")
                    finally:
                        q.put(None)

            asyncio.run_coroutine_threadsafe(producer(), _loop)
            while True:
                try:
                    chunk = q.get(timeout=60)
                    if chunk is None:
                        break
                    yield chunk
                except _q.Empty:
                    break
                except Exception:
                    break

        headers = {
            "Content-Disposition": "inline",
            "Accept-Ranges": "none",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        }
        if file_size:
            headers["Content-Length"] = str(file_size)
        return Response(stream_wrapper(), mimetype=mime, headers=headers)
    except asyncio.TimeoutError:
        return jsonify({"error": "Timeout", "code": "TIMEOUT"}), 504
    except Exception as e:
        logger.error(f"media-stream error: {e}")
        return jsonify({"error": str(e), "code": "INTERNAL"}), 500


@app.route("/api/messages/<chat_id>")
@api_login_required
def get_messages_route(chat_id):
    session_str = session.get("session_string")
    if not session_str:
        return jsonify({"error": "No session", "code": "AUTH_REQUIRED"}), 401
    try:
        result = run_async(get_messages_from_chat(
            session_str, chat_id,
            limit=int(request.args.get("limit", 50)),
            offset_id=int(request.args.get("offset_id", 0)),
            query=request.args.get("query"),
            media_only=request.args.get("media_only") == "true"
        ))
        return jsonify(result)
    except Exception as e:
        logger.error(f"get_messages_route error: {e}")
        return jsonify({"error": str(e), "code": "INTERNAL"}), 500

@app.route("/api/dialogs")
@api_login_required
def get_dialogs_route():
    offset = int(request.args.get("offset", 0))
    session_str = session.get("session_string")
    if not session_str:
        return jsonify({"error": "No session", "code": "AUTH_REQUIRED"}), 401

    async def task():
        sk = session_str[:16]
        async def _do(client):
            dialogs = await get_dialogs_list(client, offset=offset, session_key=sk)
            return {"dialogs": dialogs, "has_more_dialogs": len(dialogs) == DIALOGS_PAGE_SIZE}
        return await run_with_reconnect(session_str, _do)

    try:
        return jsonify(run_async(task()))
    except Exception as e:
        logger.error(f"get_dialogs_route error: {e}")
        return jsonify({"error": str(e), "code": "INTERNAL"}), 500

@app.route("/api/sessions")
@api_login_required
def get_sessions():
    from datetime import datetime as _dt
    session_str = session.get("session_string")
    if not session_str:
        return jsonify({"error": "No session", "code": "AUTH_REQUIRED"}), 401

    async def task():
        from pyrogram import raw
        async def _do(client):
            result = await client.invoke(raw.functions.account.GetAuthorizations())
            sessions = []
            for auth in result.authorizations:
                def _ts(v):
                    try:
                        if isinstance(v, int):
                            return _dt.utcfromtimestamp(v).isoformat()
                        return v.isoformat() if hasattr(v, 'isoformat') else str(v)
                    except Exception:
                        return None
                sessions.append({
                    "hash": auth.hash,
                    "current": auth.hash == 0,
                    "device": auth.device_model,
                    "platform": auth.platform,
                    "system": auth.system_version,
                    "app_name": auth.app_name,
                    "app_version": auth.app_version,
                    "ip": auth.ip,
                    "country": auth.country,
                    "region": auth.region,
                    "date_created": _ts(auth.date_created),
                    "date_active": _ts(auth.date_active),
                })
            sessions.sort(key=lambda s: (not s["current"], s["date_active"] or ""))
            return {"sessions": sessions}
        return await run_with_reconnect(session_str, _do)

    try:
        return jsonify(run_async(task()))
    except Exception as e:
        logger.error(f"get_sessions error: {e}")
        return jsonify({"error": str(e), "code": "INTERNAL"}), 500

@app.route("/api/sessions/terminate", methods=["POST"])
@api_login_required
def terminate_session():
    session_str = session.get("session_string")
    data = request.json or {}
    hash_val = data.get("hash")
    if not session_str or hash_val is None:
        return jsonify({"error": "Missing params", "code": "BAD_REQUEST"}), 400
    if hash_val == 0:
        return jsonify({"error": "Cannot terminate current session", "code": "FORBIDDEN"}), 403

    async def task():
        from pyrogram import raw
        async def _do(client):
            await client.invoke(raw.functions.account.ResetAuthorization(hash=hash_val))
            return {"success": True}
        return await run_with_reconnect(session_str, _do)

    try:
        return jsonify(run_async(task()))
    except Exception as e:
        logger.error(f"terminate_session error: {e}")
        return jsonify({"error": str(e), "code": "INTERNAL"}), 500

@app.route("/api/queue-download/<chat_id>/<message_id>")
@api_login_required
def queue_download(chat_id, message_id):
    session_str = session.get("session_string")
    if not session_str:
        return jsonify({"error": "No session"}), 400
    download_id = str(uuid.uuid4())[:8]
    download_queue[download_id] = {
        "status": "queued", "chat_id": chat_id, "msg_id": message_id,
        "filename": None, "safe_name": None, "path": None, "error": None, "size": None
    }
    asyncio.run_coroutine_threadsafe(
        _download_to_server(download_id, session_str, chat_id, message_id), _loop
    )
    return jsonify({"download_id": download_id})

@app.route("/api/downloads")
@api_login_required
def list_downloads():
    # Start with in-memory queue
    items_map = {
        k: {"id": k, "status": v["status"], "filename": v["filename"],
            "safe_name": v["safe_name"], "error": v["error"], "size": v["size"],
            "from_db": False}
        for k, v in list(download_queue.items())
    }
    # Merge in DB records (survive redeploys). If the DB is transiently
    # unavailable (e.g. still recovering after a restart), degrade gracefully
    # and still return the in-memory queue instead of a raw 500 — the frontend
    # polls this endpoint every 2.5s, so a hard failure here becomes a burst
    # of errors instead of a single quiet retry.
    sk = session.get("session_string", "")[:16]
    degraded = False
    try:
        for row in ServerDownload.query.filter_by(session_key=sk).order_by(ServerDownload.created_at.desc()).limit(200).all():
            if row.download_id not in items_map:
                items_map[row.download_id] = {
                    "id":        row.download_id,
                    "status":    row.status or "done",
                    "filename":  row.file_name,
                    "safe_name": row.file_name,
                    "error":     row.error,
                    "size":      format_file_size(row.file_size) if row.file_size else None,
                    "from_db":   True,
                }
    except Exception as e:
        db.session.rollback()
        if not _db_is_transient_error(e):
            raise
        logger.warning(f"list_downloads: DB temporarily unavailable, serving in-memory queue only: {e}")
        degraded = True
    resp = {"downloads": list(items_map.values())}
    if degraded:
        resp["degraded"] = True
    return jsonify(resp)

@app.route("/serve-download/<download_id>")
@login_required
def serve_download(download_id):
    # Check in-memory queue first
    entry = download_queue.get(download_id)
    if entry and entry["status"] == "done" and entry.get("path"):
        path = entry["path"]
        if os.path.exists(path):
            return send_file(path, as_attachment=True, download_name=entry["filename"])
    # Fall back to DB copy (survives redeploys)
    sk = session.get("session_string", "")[:16]
    try:
        row = ServerDownload.query.filter_by(download_id=download_id, session_key=sk).first()
    except Exception as e:
        db.session.rollback()
        if not _db_is_transient_error(e):
            raise
        logger.warning(f"serve_download: DB temporarily unavailable: {e}")
        return "Downloads database temporarily unavailable, please retry shortly", 503
    if row:
        if row.file_path and os.path.exists(row.file_path):
            return send_file(row.file_path, as_attachment=True, download_name=row.file_name or "file")
        # Disk file missing — check by prefix scan (covers path=NULL edge case)
        for fname in os.listdir(DOWNLOADS_DIR):
            if fname.startswith(f"{download_id}_"):
                fpath = os.path.join(DOWNLOADS_DIR, fname)
                return send_file(fpath, as_attachment=True, download_name=row.file_name or fname)
    return "File not found — it may have been cleared or the server was restarted", 404

@app.route("/api/downloads/zip")
@login_required
def download_all_zip():
    """Build a ZIP of all completed downloads and serve it.

    Key constraints:
    - NEVER load file_data blobs from DB into RAM — files can be hundreds of MB each.
    - Write the ZIP to a temp file on disk (not BytesIO) so we don't exhaust RAM.
    - Query only the metadata columns we need (download_id, file_name, file_path, status).
    """
    import zipfile, tempfile, datetime
    sk = session.get("session_string", "")[:16]
    added = 0
    seen_names = {}

    # Write to a temp file so we never hold the full ZIP in RAM.
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False,
                                      dir=DOWNLOADS_DIR, prefix="zip_tmp_")
    tmp_path = tmp.name
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as zf:
            # 1. In-memory queue — these always have a disk path when status=done
            seen_ids = set()
            for did, entry in list(download_queue.items()):
                if entry.get("status") != "done":
                    continue
                path = entry.get("path")
                name = entry.get("filename") or "file"
                if path and os.path.exists(path):
                    zf.write(path, _zip_unique_name(seen_names, name))
                    added += 1
                    seen_ids.add(did)

            # 2. DB records — select ONLY metadata columns, never touch file_data blob.
            try:
                db_rows = db.session.query(
                    ServerDownload.download_id,
                    ServerDownload.file_name,
                    ServerDownload.file_path,
                    ServerDownload.status,
                ).filter_by(session_key=sk).all()
            except Exception as e:
                db.session.rollback()
                if not _db_is_transient_error(e):
                    raise
                logger.warning(f"download_all_zip: DB temporarily unavailable, zipping in-memory items only: {e}")
                db_rows = []

            for row in db_rows:
                if row.download_id in seen_ids:
                    continue  # already added from in-memory queue
                if row.status and row.status != "done":
                    continue
                name = row.file_name or "file"
                if row.file_path and os.path.exists(row.file_path):
                    zf.write(row.file_path, _zip_unique_name(seen_names, name))
                    added += 1
                else:
                    # file_path missing or gone — scan by prefix as last resort
                    for fname in os.listdir(DOWNLOADS_DIR):
                        if fname.startswith(f"{row.download_id}_") and not fname.startswith("zip_tmp_"):
                            zf.write(os.path.join(DOWNLOADS_DIR, fname),
                                     _zip_unique_name(seen_names, name))
                            added += 1
                            break
                    else:
                        logger.warning(f"download_all_zip: disk file missing for {row.download_id} ({name}), skipping")

        tmp.close()
        if added == 0:
            os.unlink(tmp_path)
            return "No completed downloads to zip", 404

        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        logger.info(f"download_all_zip: zipped {added} file(s) → {tmp_path}")
        return send_file(tmp_path, as_attachment=True,
                         download_name=f"downloads_{ts}.zip",
                         mimetype="application/zip")
    except Exception:
        tmp.close()
        try: os.unlink(tmp_path)
        except OSError: pass
        raise

def _zip_unique_name(seen, name):
    if name not in seen:
        seen[name] = 0
        return name
    seen[name] += 1
    base, _, ext = name.rpartition(".")
    return f"{base}_{seen[name]}.{ext}" if base else f"{name}_{seen[name]}"

def _delete_disk_files_for(download_id):
    """Delete any disk file(s) in DOWNLOADS_DIR whose name starts with '{download_id}_'.
    The original code never saved file_path to DB, so existing rows have it as NULL.
    Scanning by prefix is the only reliable way to find and clean up those files."""
    deleted = []
    try:
        for fname in os.listdir(DOWNLOADS_DIR):
            if fname.startswith(f"{download_id}_"):
                fpath = os.path.join(DOWNLOADS_DIR, fname)
                try:
                    os.remove(fpath)
                    deleted.append(fname)
                    logger.info(f"Deleted disk file: {fname}")
                except Exception as e:
                    logger.warning(f"Could not delete disk file {fname}: {e}")
    except Exception as e:
        logger.warning(f"Error scanning {DOWNLOADS_DIR} for {download_id}: {e}")
    return deleted

@app.route("/api/downloads/clear-all", methods=["DELETE"])
@api_login_required
def clear_all_downloads():
    """Delete every server download for this session — in-memory queue + DB rows + disk files.

    Key constraints:
    - NEVER load file_data blobs — query only download_id + file_path metadata.
    - Use a single bulk DELETE SQL statement instead of row-by-row iteration.
    - Wipe all files in DOWNLOADS_DIR that match known download_ids (fast, no RAM cost).
    """
    sk = session.get("session_string", "")[:16]
    disk_deleted = 0

    # 1. Collect all known download_ids from in-memory queue and remove entries
    to_remove = list(download_queue.keys())
    all_ids = set(to_remove)
    for did in to_remove:
        entry = download_queue.pop(did, None)
        if entry and entry.get("path") and os.path.exists(entry["path"]):
            try:
                os.remove(entry["path"])
                disk_deleted += 1
            except Exception as e:
                logger.warning(f"Could not delete queued file {entry['path']}: {e}")

    # 2. Query ONLY metadata (no file_data blob) from DB to get disk paths
    try:
        db_rows = db.session.query(
            ServerDownload.download_id,
            ServerDownload.file_path,
        ).filter_by(session_key=sk).all()

        # Delete disk files using stored path
        for row in db_rows:
            all_ids.add(row.download_id)
            if row.file_path and os.path.exists(row.file_path):
                try:
                    os.remove(row.file_path)
                    disk_deleted += 1
                except Exception as e:
                    logger.warning(f"Could not delete {row.file_path}: {e}")

        # Bulk-delete all DB rows in one SQL statement — no blob loading, no iteration
        deleted_db = ServerDownload.query.filter_by(session_key=sk).delete(synchronize_session=False)
        db.session.commit()
        logger.info(f"clear_all_downloads: removed {deleted_db} DB rows")
    except Exception as e:
        db.session.rollback()
        if not _db_is_transient_error(e):
            raise
        logger.warning(f"clear_all_downloads: DB temporarily unavailable: {e}")
        return jsonify({"error": "Downloads database temporarily unavailable, please retry shortly"}), 503

    # 3. Scan DOWNLOADS_DIR and delete any remaining files for these ids
    #    (catches files where file_path was NULL / stale)
    try:
        for fname in os.listdir(DOWNLOADS_DIR):
            if fname.startswith("zip_tmp_"):
                continue  # skip temp zips
            prefix = fname.split("_")[0]
            if prefix in all_ids:
                try:
                    os.remove(os.path.join(DOWNLOADS_DIR, fname))
                    disk_deleted += 1
                except Exception as e:
                    logger.warning(f"Could not delete disk file {fname}: {e}")
    except Exception as e:
        logger.warning(f"clear_all_downloads: error scanning disk: {e}")

    logger.info(f"clear_all_downloads: {disk_deleted} disk file(s) deleted")
    return jsonify({"success": True, "deleted_db": deleted_db, "deleted_disk": disk_deleted})

@app.route("/api/downloads/delete/<download_id>", methods=["DELETE"])
@api_login_required
def delete_server_download(download_id):
    disk_deleted = 0
    # 1. Remove from in-memory queue
    entry = download_queue.pop(download_id, None)
    if entry and entry.get("path") and os.path.exists(entry["path"]):
        try:
            os.remove(entry["path"])
            disk_deleted += 1
            logger.info(f"Deleted queued file: {entry['path']}")
        except Exception as e:
            logger.warning(f"Could not delete queued file {entry['path']}: {e}")
    # Scan by prefix regardless — catches files where path was None after restart
    disk_deleted += len(_delete_disk_files_for(download_id))

    # 2. Remove from DB — query only metadata (no file_data blob)
    sk = session.get("session_string", "")[:16]
    deleted_db = 0
    try:
        meta = db.session.query(
            ServerDownload.id,
            ServerDownload.file_path,
        ).filter_by(download_id=download_id, session_key=sk).first()
        if meta:
            if meta.file_path and os.path.exists(meta.file_path):
                try:
                    os.remove(meta.file_path)
                    disk_deleted += 1
                    logger.info(f"Deleted DB file: {meta.file_path}")
                except Exception as e:
                    logger.warning(f"Could not delete {meta.file_path}: {e}")
            deleted_db = ServerDownload.query.filter_by(
                download_id=download_id, session_key=sk
            ).delete(synchronize_session=False)
            db.session.commit()
            logger.info(f"Deleted download {download_id} from DB ({disk_deleted} disk file(s) removed)")
    except Exception as e:
        db.session.rollback()
        if not _db_is_transient_error(e):
            raise
        logger.warning(f"delete_server_download: DB temporarily unavailable: {e}")
        if not entry:
            return jsonify({"error": "Downloads database temporarily unavailable, please retry shortly"}), 503
    if not entry and not deleted_db:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"success": True, "deleted_disk": disk_deleted})

LANG_NAMES = {
    "en": "English", "hi": "Hindi", "id": "Indonesian",
    "ar": "Arabic", "zh-CN": "Chinese (Simplified)", "es": "Spanish",
    "fr": "French", "de": "German", "ja": "Japanese",
    "ko": "Korean", "ru": "Russian", "pt": "Portuguese", "tr": "Turkish"
}

@app.route("/translate", methods=["POST"])
@api_login_required
def translate_route():
    data = request.json
    text = data.get("text", "").strip()
    target_lang = data.get("target", data.get("lang", "en"))
    if not text:
        return jsonify({"error": "No text"}), 400
    target_name = LANG_NAMES.get(target_lang, target_lang)

    # Cache identical translation requests — avoids re-hitting AI APIs for the
    # same text in the same session (e.g. "translate all" hitting repeated phrases).
    t_key = (hashlib.md5(text.encode()).hexdigest(), target_lang)
    with _translation_cache_lock:
        hit = _translation_cache.get(t_key)
    if hit:
        return jsonify({"translated": hit[0], "engine": hit[1] + " (cached)"})

    try:
        translated, engine = translate_with_fallback(text, target_lang, target_name)
        with _translation_cache_lock:
            if len(_translation_cache) >= _TRANSLATION_CACHE_MAX:
                # Evict oldest 10 % (simple FIFO approximation)
                for k in list(_translation_cache.keys())[:_TRANSLATION_CACHE_MAX // 10]:
                    del _translation_cache[k]
            _translation_cache[t_key] = (translated, engine)
        return jsonify({"translated": translated, "engine": engine})
    except ValueError as e:
        logger.error(f"Translation failed: {e}")
        return jsonify({"error": str(e)}), 503

@app.route("/delete-messages", methods=["POST"])
@api_login_required
def delete_messages():
    data = request.json or {}
    chat_id = data.get("chat_id")
    message_ids = data.get("message_ids")
    session_str = session.get("session_string")
    if not chat_id or not message_ids or not session_str:
        return jsonify({"error": "Missing params", "code": "BAD_REQUEST"}), 400

    async def task():
        async def _do(client):
            await client.delete_messages(chat_id, message_ids)
            return {"success": True}
        return await run_with_reconnect(session_str, _do)

    try:
        return jsonify(run_async(task()))
    except Exception as e:
        logger.error(f"delete_messages error: {e}")
        return jsonify({"error": str(e), "code": "INTERNAL"}), 500

@app.route("/download/<chat_id>/<message_id>")
@login_required
def download_media_route(chat_id, message_id):
    session_str = session.get("session_string")
    if not session_str:
        return "No session", 400

    async def get_file_info():
        """Fetch the message once; return (file_name, file_size, msg).

        The returned msg object is reused by stream_wrapper so we don't make a
        second get_messages() call for the same download request.
        """
        try:
            client = await get_client(session_str)
            try:
                peer_id = int(chat_id)
            except Exception:
                peer_id = chat_id
            msg = await client.get_messages(peer_id, int(message_id))
            if not msg or not msg.media:
                return None, None, None
            media = getattr(msg, msg.media.value)
            file_name = getattr(media, "file_name", None)
            if not file_name:
                ext = ".file"
                if msg.photo:      ext = ".jpg"
                elif msg.video:    ext = ".mp4"
                elif msg.audio:    ext = ".mp3"
                elif msg.voice:    ext = ".ogg"
                elif msg.document:
                    mime = getattr(media, "mime_type", "")
                    if "video" in mime:   ext = ".mp4"
                    elif "image" in mime: ext = ".jpg"
                    elif "audio" in mime: ext = ".mp3"
                file_name = f"download_{message_id}{ext}"
            return file_name, getattr(media, "file_size", None), msg
        except Exception as e:
            logger.error(f"File info error: {e}")
            raise

    try:
        file_name, file_size, fetched_msg = run_async(get_file_info())
        if not file_name:
            return "File not found", 404

        # Now define the stream generator that closes over fetched_msg — no
        # second get_messages() call needed.
        async def generate_download():
            client = await get_client(session_str)
            try:
                if not fetched_msg or not fetched_msg.media:
                    return
                async for chunk in client.stream_media(fetched_msg):
                    yield chunk
            except Exception as e:
                logger.error(f"Stream error: {e}")
                if "Broken pipe" in str(e) or "Session" in str(e):
                    if session_str in telegram_clients:
                        try:
                            await telegram_clients[session_str].stop()
                        except Exception:
                            pass
                        del telegram_clients[session_str]

        def stream_wrapper():
            import queue
            q = queue.Queue(maxsize=20)

            async def producer():
                try:
                    async for chunk in generate_download():
                        while True:
                            try:
                                q.put(chunk, block=True, timeout=2.0)
                                break
                            except queue.Full:
                                continue
                except Exception as e:
                    logger.error(f"Producer error: {e}")
                finally:
                    q.put(None)

            asyncio.run_coroutine_threadsafe(producer(), _loop)
            while True:
                try:
                    chunk = q.get(timeout=30)
                    if chunk is None:
                        break
                    yield chunk
                except queue.Empty:
                    break
                except Exception as e:
                    logger.error(f"Stream yield error: {e}")
                    break

        headers = {
            'Content-Disposition': f'attachment; filename="{file_name}"',
            'Cache-Control': 'no-cache',
            'X-Content-Type-Options': 'nosniff',
        }
        if file_size:
            headers['Content-Length'] = str(file_size)
        return Response(stream_wrapper(), headers=headers, mimetype='application/octet-stream')
    except Exception as e:
        logger.error(f"Download error: {e}")
        return str(e), 500

@app.route("/api/deleted-messages/<chat_id>")
@api_login_required
def get_deleted_messages(chat_id):
    try:
        chat_id = int(chat_id)
    except Exception:
        pass
    msgs = MessageStore.query.filter_by(chat_id=chat_id).order_by(MessageStore.date.desc()).all()
    return jsonify({"messages": [
        {"text": m.text, "date": m.date.isoformat() if m.date else None}
        for m in msgs
    ]})

@app.route("/api/protected-chats", methods=["GET"])
@api_login_required
def list_protected_chats():
    s = session.get("session_string", "")
    sk = s[:16]
    rows = ProtectedChat.query.filter_by(session_key=sk).all()
    return jsonify({"protected_chats": [r.chat_id for r in rows]})

@app.route("/api/protected-chats/<chat_id>", methods=["POST"])
@api_login_required
def protect_chat(chat_id):
    s = session.get("session_string", "")
    sk = s[:16]
    try:
        cid = int(chat_id)
    except ValueError:
        return jsonify({"error": "Invalid chat_id"}), 400
    if not ProtectedChat.query.filter_by(session_key=sk, chat_id=cid).first():
        db.session.add(ProtectedChat(session_key=sk, chat_id=cid))
        db.session.commit()
    # Update in-memory set so _auto_preserve sees the change immediately
    with _protected_chats_mem_lock:
        if sk in _protected_chats_mem:
            _protected_chats_mem[sk].add(cid)
    return jsonify({"success": True, "protected": True})

@app.route("/api/protected-chats/<chat_id>", methods=["DELETE"])
@api_login_required
def unprotect_chat(chat_id):
    s = session.get("session_string", "")
    sk = s[:16]
    try:
        cid = int(chat_id)
    except ValueError:
        return jsonify({"error": "Invalid chat_id"}), 400
    ProtectedChat.query.filter_by(session_key=sk, chat_id=cid).delete()
    db.session.commit()
    # Remove from in-memory set
    with _protected_chats_mem_lock:
        if sk in _protected_chats_mem:
            _protected_chats_mem[sk].discard(cid)
    return jsonify({"success": True, "protected": False})

@app.route("/api/preserved", methods=["GET"])
@api_login_required
def list_preserved():
    s = session.get("session_string", "")
    sk = s[:16]
    chat_filter = request.args.get("chat_id")
    q = PreservedMedia.query.filter_by(session_key=sk)
    if chat_filter:
        try:
            q = q.filter_by(chat_id=int(chat_filter))
        except ValueError:
            pass
    items = q.order_by(PreservedMedia.saved_at.desc()).limit(500).all()
    return jsonify({"preserved": [
        {
            "id":               pm.id,
            "chat_id":          pm.chat_id,
            "message_id":       pm.message_id,
            "file_name":        pm.file_name,
            "file_size":        format_file_size(pm.file_size) if pm.file_size else "—",
            "media_type":       pm.media_type,
            "reason":           pm.reason,
            "saved_at":         pm.saved_at.isoformat() if pm.saved_at else None,
            "original_deleted": pm.original_deleted,
            "available":        bool(pm.file_data or (pm.file_path and os.path.exists(pm.file_path))),
        }
        for pm in items
    ]})

@app.route("/api/preserved/<int:item_id>/download")
@login_required
def download_preserved(item_id):
    s = session.get("session_string", "")
    sk = s[:16]
    pm = PreservedMedia.query.filter_by(id=item_id, session_key=sk).first()
    if not pm:
        return "Not found", 404
    # Serve from local disk if available, fall back to DB copy
    if pm.file_path and os.path.exists(pm.file_path):
        return send_file(pm.file_path, as_attachment=True, download_name=pm.file_name or "file")
    if pm.file_data:
        import io
        return send_file(
            io.BytesIO(pm.file_data),
            as_attachment=True,
            download_name=pm.file_name or "file"
        )
    return "File no longer available", 404

@app.route("/api/preserved/<int:item_id>", methods=["DELETE"])
@api_login_required
def delete_preserved(item_id):
    s = session.get("session_string", "")
    sk = s[:16]
    pm = PreservedMedia.query.filter_by(id=item_id, session_key=sk).first()
    if not pm:
        return jsonify({"error": "Not found"}), 404
    if pm.file_path and os.path.exists(pm.file_path):
        try:
            os.remove(pm.file_path)
        except Exception:
            pass
    pm.file_data = None  # free DB space before delete
    db.session.delete(pm)
    db.session.commit()
    return jsonify({"success": True})

@app.route("/")
def index():
    if not session.get('app_authenticated'):
        return redirect(url_for('app_login'))
    if session.get("session_string"):
        return redirect(url_for("view_account"))
    return render_template("index.html")

@app.route("/login", methods=["GET", "POST"])
def app_login():
    if request.method == "POST":
        if request.form.get("password") == APP_PASSWORD:
            session.permanent = True  # survive browser close / phone restart for 30 days
            session['app_authenticated'] = True
            return redirect(url_for('index'))
        return render_template("login.html", error="Invalid password")
    return render_template("login.html")

@app.route("/view", methods=["GET", "POST"])
@login_required
def view_account():
    session_string = request.form.get("session_string") or session.get("session_string")
    if not session_string:
        return redirect(url_for("index"))
    session.permanent = True  # survive browser close / phone restart for 30 days
    session["session_string"] = session_string
    result = run_async(get_account_info(session_string))
    if "error" in result:
        logger.error(f"Error getting account info: {result['error']}")
        if _is_auth_key_duplicated(Exception(result["error"])):
            run_async(clear_client(session_string))
            session.pop("session_string", None)
            return render_template("index.html", error=(
                "This session is already active somewhere else. "
                "Stop the other instance first, then enter your session string again."
            ))
        return render_template("index.html", error=result["error"])

    # ── Persist session so it survives browser close & server restart ──────────
    try:
        sk = session_string[:16]
        label = f"{result.get('first_name', '')} {result.get('last_name', '')}".strip() or result.get('username') or sk
        existing = StoredSession.query.filter_by(session_key=sk).first()
        if existing:
            existing.session_string = session_string
            existing.label = label
            existing.last_seen = datetime.utcnow()
        else:
            db.session.add(StoredSession(
                session_key=sk,
                session_string=session_string,
                label=label,
            ))
        db.session.commit()
    except Exception as e:
        logger.error(f"Failed to persist session: {e}")

    # Pass protected chat IDs so the template can initialise the JS Set without
    # an extra API round-trip on every page load.
    try:
        protected_ids = [r.chat_id for r in ProtectedChat.query.filter_by(
            session_key=session_string[:16]).all()]
    except Exception:
        protected_ids = []
    return render_template("account.html", account=result, protected_chats=protected_ids,
                           session_key=session_string[:16])

@app.route("/api/stored-sessions", methods=["GET"])
@api_login_required
def list_stored_sessions():
    rows = StoredSession.query.order_by(StoredSession.last_seen.desc()).all()
    return jsonify({"sessions": [
        {
            "session_key":      r.session_key,
            "label":            r.label,
            "created_at":       r.created_at.isoformat() if r.created_at else None,
            "last_seen":        r.last_seen.isoformat() if r.last_seen else None,
            "connected":        r.session_string in telegram_clients,
            "reconnect_failed": bool(r.reconnect_failed),
        }
        for r in rows
    ]})

@app.route("/api/stored-sessions/<session_key>", methods=["DELETE"])
@api_login_required
def delete_stored_session(session_key):
    row = StoredSession.query.filter_by(session_key=session_key).first()
    if not row:
        return jsonify({"error": "Not found"}), 404
    full_key = row.session_string
    db.session.delete(row)
    db.session.commit()
    run_async(clear_client(full_key))
    return jsonify({"success": True})

@app.route("/resume/<session_key>")
@login_required
def resume_stored_session(session_key):
    """One-click resume: reuse a previously saved session string instead of
    requiring it to be pasted in again after the browser session expired."""
    row = StoredSession.query.filter_by(session_key=session_key).first()
    if not row:
        return redirect(url_for("index"))
    session.permanent = True
    session["session_string"] = row.session_string
    return redirect(url_for("view_account"))

@app.route("/api/stored-sessions/<session_key>/reconnect", methods=["POST"])
@api_login_required
def reconnect_stored_session(session_key):
    row = StoredSession.query.filter_by(session_key=session_key).first()
    if not row:
        return jsonify({"error": "Not found"}), 404
    try:
        run_async(get_client(row.session_string, force_reconnect=True))
        row.last_seen = datetime.utcnow()
        row.reconnect_failed = False
        db.session.commit()
        return jsonify({"success": True, "connected": True})
    except Exception as e:
        err = str(e)
        if any(k in err.upper() for k in (
            "AUTH_KEY_DUPLICATED", "SESSION_REVOKED", "SESSION_EXPIRED",
            "AUTH_KEY_INVALID", "USER_DEACTIVATED"
        )):
            row.reconnect_failed = True
            db.session.commit()
            return jsonify({
                "error": "Session is no longer valid. The session may have been taken over by another login. Please provide a new session string.",
                "code": "SESSION_INVALID"
            }), 401
        return jsonify({"error": err}), 500

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for('app_login'))

@app.route("/_health")
def health_check():
    """Deployment readiness probe — always returns 200 so the VM starts up correctly."""
    return "ok", 200

@app.route("/api/logs")
@login_required
def get_logs():
    """Return recent in-memory log entries for in-app diagnostics.
    Only available to authenticated users — not publicly exposed."""
    level   = request.args.get("level", "").upper()   # INFO / WARNING / ERROR
    limit   = min(int(request.args.get("limit", 200)), 500)
    entries = list(_log_buffer)                        # oldest→newest
    if level:
        entries = [e for e in entries if e["level"] == level]
    return jsonify({"logs": entries[-limit:]})

@app.route("/api/logs/download")
@login_required
def download_log_file():
    """Download the current app.log file for offline analysis."""
    log_path = os.path.join(_LOGS_DIR, "app.log")
    if not os.path.exists(log_path):
        return jsonify({"error": "Log file not found"}), 404
    from flask import send_file
    return send_file(log_path, as_attachment=True, download_name="app.log", mimetype="text/plain")

@app.route("/api/disk-stats")
@login_required
def disk_stats():
    """Return disk usage for all server-managed directories.
    Lets you monitor storage without SSH access — useful on deployed services."""
    def _dir_stats(path):
        total = 0
        count = 0
        try:
            for fname in os.listdir(path):
                try:
                    total += os.path.getsize(os.path.join(path, fname))
                    count += 1
                except Exception:
                    pass
        except Exception:
            pass
        return {"files": count, "bytes": total, "human": format_file_size(total)}

    return jsonify({
        "downloads":       _dir_stats(DOWNLOADS_DIR),
        "preserved_media": _dir_stats(PRESERVED_DIR),
        "thumbnail_cache": _dir_stats(THUMBNAILS_DIR),
    })

@app.route("/api/disk-stats/thumbnails/clear", methods=["DELETE"])
@login_required
def clear_thumbnail_cache():
    """Manually wipe the entire thumbnail disk cache.
    Thumbnails will be re-fetched from Telegram on next view."""
    deleted = 0
    errors = 0
    try:
        for fname in os.listdir(THUMBNAILS_DIR):
            try:
                os.remove(os.path.join(THUMBNAILS_DIR, fname))
                deleted += 1
            except Exception as e:
                logger.warning(f"clear_thumbnail_cache: could not remove {fname}: {e}")
                errors += 1
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    # Also wipe the in-memory thumb cache so stale entries don't linger
    with _thumb_cache_lock:
        _thumb_cache.clear()
    logger.info(f"clear_thumbnail_cache: removed {deleted} file(s) ({errors} error(s))")
    return jsonify({"success": True, "deleted": deleted, "errors": errors})

# ── Gzip compression for JSON / text API responses ────────────────────────────
# Compresses dialogs, messages, account, and other JSON payloads — large dialog
# lists can be 50-80 KB uncompressed; gzip brings them to ~10-15 KB, cutting
# page-load network time substantially on slow connections.

@app.after_request
def compress_response(response):
    if (response.status_code < 200 or response.status_code >= 300
            or response.direct_passthrough
            or 'Content-Encoding' in response.headers):
        return response
    accept_encoding = request.headers.get('Accept-Encoding', '')
    if 'gzip' not in accept_encoding:
        return response
    content_type = response.content_type or ''
    if not any(t in content_type for t in ('json', 'text', 'javascript', 'html')):
        return response
    data = response.get_data()
    if len(data) < 500:  # not worth compressing tiny responses
        return response
    compressed = _gzip.compress(data, compresslevel=6)
    if len(compressed) >= len(data):  # compression made it bigger — skip
        return response
    response.set_data(compressed)
    response.headers['Content-Encoding'] = 'gzip'
    response.headers['Vary'] = 'Accept-Encoding'
    response.headers['Content-Length'] = len(compressed)
    return response


# ── IST timezone helpers (used by export) ────────────────────────────────────
from datetime import timezone as _tz
_IST = _tz(timedelta(hours=5, minutes=30))

def _to_ist_str_from_iso(iso_str):
    """Convert an ISO datetime string to a human-readable IST string."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)
        return dt.astimezone(_IST).strftime("%Y-%m-%d %H:%M IST")
    except Exception:
        return str(iso_str)[:16]

def _sender_color(key):
    colors = ["#e17055","#fdcb6e","#00b894","#0984e3","#6c5ce7","#fd79a8","#55efc4","#74b9ff"]
    try:
        return colors[hash(str(key)) % len(colors)]
    except Exception:
        return colors[0]

def _html_escape(s):
    return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")

def _media_icon(media_type):
    return {"photo":"🖼️","video":"🎬","audio":"🎵","voice":"🎤","document":"📄",
            "animation":"🎞️","sticker":"🎭","video_note":"📹"}.get(media_type or "","📎")

def _ist_date_label(iso_str):
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)
        return dt.astimezone(_IST).strftime("%B %-d, %Y")
    except Exception:
        return str(iso_str)[:10]

def _ist_time_label(iso_str):
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)
        return dt.astimezone(_IST).strftime("%H:%M")
    except Exception:
        return ""

def _ist_date_key(iso_str):
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)
        return dt.astimezone(_IST).strftime("%Y-%m-%d")
    except Exception:
        return str(iso_str)[:10]

# ── Media embedding helpers ───────────────────────────────────────────────────
_DATA_URI_LIMIT = 10 * 1024 * 1024   # embed files ≤ 10 MB as data URIs

_MEDIA_MIME_MAP = {
    "photo": "image/jpeg", "video": "video/mp4", "audio": "audio/mpeg",
    "voice": "audio/ogg", "animation": "video/mp4", "sticker": "image/webp",
    "video_note": "video/mp4", "document": "application/octet-stream",
}
_EXT_MIME_MAP = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska", ".webm": "video/webm",
    ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".wav": "audio/wav",
    ".m4a": "audio/mp4", ".flac": "audio/flac",
    ".pdf": "application/pdf",
}

def _export_guess_mime(mtype, filename=""):
    """Return a MIME type string for an export media file. Extension wins if known.
    Named _export_guess_mime to avoid shadowing the existing _guess_mime helper."""
    _, ext = os.path.splitext((filename or "").lower())
    return _EXT_MIME_MAP.get(ext) or _MEDIA_MIME_MAP.get(mtype, "application/octet-stream")

def _make_data_uri(file_bytes, mtype, filename=""):
    mime = _export_guess_mime(mtype, filename)
    return f"data:{mime};base64,{base64.b64encode(file_bytes).decode()}"

def _build_export_html(messages, chat_name, media_map, generated_at):
    """
    Build a self-contained Telegram-style HTML export.
    media_map: dict of {msg_id: src_string} where src_string is either
               a data URI (fully self-contained) or a relative path like
               "media/filename.ext" (works after ZIP extraction).
               Empty dict → HTML-only (shows icon + metadata placeholder).
    """
    CSS = """
    *{margin:0;padding:0;box-sizing:border-box}
    body{background:#0e1621;color:#e8e8e8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;line-height:1.5}
    .export-header{background:#17212b;padding:16px 20px;display:flex;align-items:center;gap:14px;position:sticky;top:0;z-index:10;border-bottom:1px solid #1e2d3d}
    .export-avatar{width:44px;height:44px;border-radius:50%;background:#5288c1;display:flex;align-items:center;justify-content:center;font-size:1.2rem;font-weight:700;flex-shrink:0}
    .export-title{font-size:1rem;font-weight:600}
    .export-meta{font-size:0.72rem;color:#6c7883;margin-top:2px}
    .messages{max-width:860px;margin:0 auto;padding:16px 12px}
    .date-sep{text-align:center;margin:18px 0 10px;position:relative}
    .date-sep span{background:#17212b;border:1px solid #1e2d3d;border-radius:12px;padding:3px 12px;font-size:0.72rem;color:#6c7883;position:relative;z-index:1}
    .msg-row{display:flex;margin-bottom:3px;gap:8px}
    .msg-row.out{flex-direction:row-reverse}
    .msg-row.in{flex-direction:row}
    .avatar-col{width:36px;flex-shrink:0;display:flex;align-items:flex-end}
    .avatar{width:34px;height:34px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:0.85rem;font-weight:700;flex-shrink:0}
    .bubble-col{max-width:72%;display:flex;flex-direction:column}
    .msg-row.out .bubble-col{align-items:flex-end}
    .bubble{border-radius:12px;padding:8px 12px;position:relative;word-break:break-word}
    .bubble.out{background:#2b5278;border-bottom-right-radius:4px}
    .bubble.in{background:#17212b;border-bottom-left-radius:4px}
    .sender-name{font-size:0.78rem;font-weight:600;margin-bottom:3px}
    .msg-text{white-space:pre-wrap}
    .msg-media{margin-top:6px;background:rgba(0,0,0,0.2);border-radius:8px;overflow:hidden}
    .msg-media img{max-width:100%;max-height:400px;display:block;border-radius:8px}
    .msg-media video{max-width:100%;max-height:300px;display:block;border-radius:8px}
    .msg-media audio{width:100%;margin-top:4px}
    .media-placeholder{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:8px;background:rgba(255,255,255,0.05)}
    .media-placeholder.not-downloaded{border:1px dashed rgba(255,255,255,0.12)}
    .media-not-dl{font-size:0.65rem;color:#e17055;margin-top:2px;font-style:italic}
    .media-icon{font-size:1.8rem;flex-shrink:0}
    .media-info{min-width:0}
    .media-name{font-size:0.82rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .media-size{font-size:0.7rem;color:#6c7883;margin-top:2px}
    .forward-info{font-size:0.72rem;color:#5288c1;border-left:3px solid #5288c1;padding-left:8px;margin-bottom:6px;opacity:0.85}
    .msg-time{font-size:0.65rem;color:#6c7883;margin-top:4px;text-align:right}
    .msg-row.in .msg-time{text-align:left}
    """

    lines = []
    lines.append("<!DOCTYPE html>")
    lines.append('<html lang="en"><head><meta charset="UTF-8">')
    lines.append('<meta name="viewport" content="width=device-width,initial-scale=1">')
    lines.append(f'<title>{_html_escape(chat_name)} — Telegram Export</title>')
    lines.append(f'<style>{CSS}</style></head><body>')

    avatar_letter = (chat_name[0] if chat_name else "?").upper()
    lines.append(f'''<div class="export-header">
  <div class="export-avatar">{avatar_letter}</div>
  <div>
    <div class="export-title">{_html_escape(chat_name)}</div>
    <div class="export-meta">{len(messages)} messages &nbsp;·&nbsp; Exported {_html_escape(generated_at)}</div>
  </div>
</div>''')
    lines.append('<div class="messages">')

    prev_date_key = None
    for msg in messages:
        date_key = _ist_date_key(msg.get("date"))
        if date_key and date_key != prev_date_key:
            lines.append(f'<div class="date-sep"><span>{_html_escape(_ist_date_label(msg.get("date")))}</span></div>')
            prev_date_key = date_key

        is_out = msg.get("is_outgoing")
        row_cls = "out" if is_out else "in"
        bubble_cls = "out" if is_out else "in"
        sender = "You" if is_out else (msg.get("sender_name") or "Unknown")
        sender_un = msg.get("sender_username")
        sender_key = msg.get("sender_key") or sender
        color = _sender_color(sender_key)
        time_str = _ist_time_label(msg.get("date"))
        msg_id = msg.get("id")

        lines.append(f'<div class="msg-row {row_cls}">')
        if not is_out:
            av_letter = (sender[0] if sender else "?").upper()
            lines.append(f'<div class="avatar-col"><div class="avatar" style="background:{color}">{av_letter}</div></div>')
        lines.append(f'<div class="bubble-col"><div class="bubble {bubble_cls}">')

        if not is_out:
            un_part = f' <span style="font-size:0.7rem;opacity:0.6">@{_html_escape(sender_un)}</span>' if sender_un else ""
            lines.append(f'<div class="sender-name" style="color:{color}">{_html_escape(sender)}{un_part}</div>')

        fwd = msg.get("forward_from")
        if fwd:
            lines.append(f'<div class="forward-info">Forwarded from {_html_escape(str(fwd))}</div>')

        text = (msg.get("text") or "").strip()
        if text:
            lines.append(f'<div class="msg-text">{_html_escape(text)}</div>')

        if msg.get("has_media"):
            mtype = msg.get("media_type") or "document"
            fname = msg.get("file_name") or ""
            fsize = msg.get("file_size") or ""
            icon = _media_icon(mtype)
            lines.append('<div class="msg-media">')
            if msg_id in media_map:
                mpath = media_map[msg_id]
                # Browsers block data: URI navigation in target="_blank".
                # Detect embedded URIs and use the download attribute instead.
                is_embedded = mpath.startswith("data:")
                safe_mpath = _html_escape(mpath)
                if mtype == "photo":
                    if is_embedded:
                        dl_name = _html_escape(fname or f"photo_{msg_id}.jpg")
                        lines.append(f'<a href="{safe_mpath}" download="{dl_name}" title="Click to download"><img src="{safe_mpath}" alt="photo" style="cursor:pointer;max-width:100%;max-height:400px;display:block;border-radius:8px"></a>')
                    else:
                        lines.append(f'<a href="{safe_mpath}" target="_blank"><img src="{safe_mpath}" alt="photo" loading="lazy"></a>')
                elif mtype in ("video", "animation", "video_note"):
                    lines.append(f'<video src="{safe_mpath}" controls preload="none" style="max-width:100%;max-height:300px;display:block;border-radius:8px"></video>')
                elif mtype in ("audio", "voice"):
                    lines.append(f'<audio src="{safe_mpath}" controls style="width:100%;margin-top:4px"></audio>')
                else:
                    if is_embedded:
                        display_name = fname or f"file_{msg_id}"
                        dl_name = _html_escape(fname or f"file_{msg_id}")
                        lines.append(f'<a href="{safe_mpath}" download="{dl_name}" style="text-decoration:none;display:block"><div class="media-placeholder" style="cursor:pointer;transition:background 0.15s" onmouseover="this.style.background=\'rgba(82,136,193,0.15)\'" onmouseout="this.style.background=\'\'"><div class="media-icon">{icon}</div><div class="media-info"><div class="media-name">{_html_escape(display_name)}</div><div class="media-size">{_html_escape(fsize)}</div><div style="font-size:0.65rem;color:#5288c1;margin-top:2px">click to download ↓</div></div></div></a>')
                    else:
                        display_name = fname or mpath.split("/")[-1]
                        lines.append(f'<a href="{safe_mpath}" target="_blank" style="text-decoration:none;display:block"><div class="media-placeholder" style="cursor:pointer;transition:background 0.15s" onmouseover="this.style.background=\'rgba(82,136,193,0.15)\'" onmouseout="this.style.background=\'\'"><div class="media-icon">{icon}</div><div class="media-info"><div class="media-name">{_html_escape(display_name)}</div><div class="media-size">{_html_escape(fsize)}</div><div style="font-size:0.65rem;color:#5288c1;margin-top:2px">click to open ↗</div></div></div></a>')
            else:
                display_name = fname or mtype.upper()
                size_part = f'<div class="media-size">{_html_escape(fsize)}</div>' if fsize else ''
                lines.append(f'<div class="media-placeholder not-downloaded"><div class="media-icon">{icon}</div><div class="media-info"><div class="media-name">{_html_escape(display_name)}</div>{size_part}<div class="media-not-dl">file not included in export</div></div></div>')
            lines.append('</div>')

        if not text and not msg.get("has_media"):
            lines.append('<div class="msg-text" style="color:#6c7883;font-style:italic">(no text)</div>')

        lines.append(f'<div class="msg-time">{_html_escape(time_str)}</div>')
        lines.append('</div></div></div>')

    lines.append('</div></body></html>')
    return "\n".join(lines)


@app.route("/api/export/<chat_id>")
@api_login_required
def export_chat(chat_id):
    """Export all messages from a chat. Formats: html, zip (html+media), json, txt."""
    session_str = session.get("session_string")
    if not session_str:
        return jsonify({"error": "No session", "code": "AUTH_REQUIRED"}), 401

    fmt = request.args.get("format", "html").lower()
    if fmt not in ("html", "zip", "json", "txt"):
        fmt = "html"
    _max_raw = request.args.get("max", "5000")
    max_msgs = 0 if _max_raw in ("0", "all") else max(1, int(_max_raw))
    skip_media = request.args.get("skip_media", "0") == "1"

    try:
        chat_id_int = int(chat_id)
    except Exception:
        chat_id_int = chat_id

    _ext_map = {
        "photo": ".jpg", "video": ".mp4", "audio": ".mp3",
        "voice": ".ogg", "animation": ".mp4", "sticker": ".webp",
        "video_note": ".mp4", "document": "",
    }

    async def _fetch_and_download(client):
        raw, offset_id = [], 0
        while True:
            batch_limit = 200 if max_msgs == 0 else min(200, max_msgs - len(raw))
            if batch_limit <= 0:
                break
            batch = []
            async for m in client.get_chat_history(
                chat_id_int, limit=batch_limit, offset_id=offset_id or 0
            ):
                batch.append(m)
            if not batch:
                break
            raw.extend(batch)
            offset_id = batch[-1].id
            if max_msgs != 0 and len(raw) >= max_msgs:
                break
            if len(batch) < batch_limit:
                break
        raw.reverse()

        media_data = {}
        if fmt == "zip" and not skip_media:
            for raw_msg in raw:
                if not raw_msg.media or raw_msg.media.value not in DOWNLOADABLE_MEDIA:
                    continue
                mtype = raw_msg.media.value
                media_obj = getattr(raw_msg, mtype, None)
                fname = getattr(media_obj, "file_name", None)
                if not fname:
                    fname = f"file_{raw_msg.id}{_ext_map.get(mtype, '.bin')}"
                zip_path = f"media/msg_{raw_msg.id}_{fname}"
                try:
                    data = await asyncio.wait_for(
                        client.download_media(raw_msg, in_memory=True), timeout=60
                    )
                    if data:
                        media_data[raw_msg.id] = (zip_path, bytes(data), mtype, fname)
                except Exception as e:
                    logger.warning(f"Export ZIP: skipped media msg {raw_msg.id}: {e}")

        return raw, media_data

    try:
        raw_msgs, media_data = run_async(run_with_reconnect(session_str, _fetch_and_download))
    except Exception as e:
        logger.error(f"export_chat error: {e}")
        return jsonify({"error": str(e), "code": "INTERNAL"}), 500

    messages = [_build_msg_dict(m) for m in raw_msgs]

    sk = session_str[:16]
    with _chat_info_lock:
        ci = _chat_info_cache.get((sk, chat_id_int))
    chat_name = (ci or {}).get("name", f"chat_{chat_id}")
    safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in chat_name).strip() or f"chat_{chat_id}"
    generated_at = datetime.now(_IST).strftime("%Y-%m-%d %H:%M IST")

    import json as _json
    from flask import make_response

    if fmt == "json":
        content = _json.dumps(messages, ensure_ascii=False, indent=2)
        resp = make_response(content.encode("utf-8"))
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
        resp.headers["Content-Disposition"] = f'attachment; filename="{safe_name}_export.json"'
        return resp

    if fmt == "txt":
        lines = [f"Export of: {chat_name}", f"Messages: {len(messages)}",
                 f"Generated: {generated_at}", "-" * 60, ""]
        for msg in messages:
            date_str = _to_ist_str_from_iso(msg.get("date"))
            if msg.get("is_outgoing"):
                sender = "You"
            else:
                sender = msg.get("sender_name") or "Unknown"
                if msg.get("sender_username"):
                    sender += f" (@{msg['sender_username']})"
            text = msg.get("text") or ""
            media_part = ""
            if msg.get("has_media"):
                media_part = f"[{(msg.get('media_type') or 'media').upper()}"
                if msg.get("file_name"):
                    media_part += f": {msg['file_name']}"
                if msg.get("file_size"):
                    media_part += f" ({msg['file_size']})"
                media_part += "]"
            fwd = msg.get("forward_from")
            fwd_part = f"[Fwd: {fwd}] " if fwd else ""
            body = " ".join(p for p in [fwd_part + text, media_part] if p) or "(no text)"
            lines.append(f"[{date_str}] {sender}:")
            lines.append(f"  {body}")
            lines.append("")
        content = "\n".join(lines)
        resp = make_response(content.encode("utf-8"))
        resp.headers["Content-Type"] = "text/plain; charset=utf-8"
        resp.headers["Content-Disposition"] = f'attachment; filename="{safe_name}_export.txt"'
        return resp

    if fmt == "html":
        html_content = _build_export_html(messages, chat_name, {}, generated_at)
        resp = make_response(html_content.encode("utf-8"))
        resp.headers["Content-Type"] = "text/html; charset=utf-8"
        resp.headers["Content-Disposition"] = f'attachment; filename="{safe_name}_export.html"'
        return resp

    import io, zipfile as _zipfile
    # Build media_map: embed small files as data URIs so chat.html is fully
    # self-contained when opened inside the ZIP viewer; large files fall back
    # to relative paths that work after extraction.
    media_map = {}
    for mid, (zip_path, file_bytes, mtype, fname) in media_data.items():
        if len(file_bytes) <= _DATA_URI_LIMIT:
            media_map[mid] = _make_data_uri(file_bytes, mtype, fname)
        else:
            media_map[mid] = zip_path   # relative path — works after extraction
    html_content = _build_export_html(messages, chat_name, media_map, generated_at)
    buf = io.BytesIO()
    with _zipfile.ZipFile(buf, "w", _zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("chat.html", html_content.encode("utf-8"))
        for mid, (zip_path, file_bytes, mtype, fname) in media_data.items():
            zf.writestr(zip_path, file_bytes)
    buf.seek(0)
    resp = make_response(buf.read())
    resp.headers["Content-Type"] = "application/zip"
    resp.headers["Content-Disposition"] = f'attachment; filename="{safe_name}_export.zip"'
    return resp


# ── Background export job system ─────────────────────────────────────────────

async def _run_export_job(job_id, session_str, chat_id_int, fmt, max_msgs, skip_media,
                          chat_name, safe_name, from_date_ts=None, to_date_ts=None):
    """Long-running async export worker — runs in _loop, not in a request thread."""
    job = export_jobs[job_id]
    import zipfile as _zipfile, json as _json, tempfile as _tempfile
    try:
        async def _do(client):
            job['status'] = 'fetching'
            range_label = job.get('range_label') or ''
            step_prefix = f"{range_label} — " if range_label else ""

            raw, offset_id = [], 0
            # Pyrogram's get_chat_history passes offset_date straight to the raw layer,
            # which serialises it as an Int. None is not valid there — use the Unix epoch
            # datetime (timestamp=0) as the "no date filter" sentinel instead.
            from datetime import timezone as _tz3
            _EPOCH_DT = datetime(1970, 1, 1, tzinfo=_tz3.utc)
            if to_date_ts:
                # +86400 so the entire selected day is included (cursor starts at next midnight)
                offset_date_arg = datetime.fromtimestamp(to_date_ts + 86400, tz=_tz3.utc)
            else:
                offset_date_arg = _EPOCH_DT
            done = False
            while not done:
                batch_limit = 200 if max_msgs == 0 else min(200, max_msgs - len(raw))
                if batch_limit <= 0:
                    break
                batch = []
                async for m in client.get_chat_history(
                    chat_id_int,
                    limit=batch_limit,
                    offset_id=offset_id or 0,
                    offset_date=offset_date_arg,
                ):
                    # Stop at from_date boundary (history comes newest→oldest)
                    if from_date_ts is not None:
                        try:
                            msg_ts = m.date.timestamp()
                        except Exception:
                            msg_ts = 0
                        if msg_ts < from_date_ts:
                            done = True
                            break
                    batch.append(m)
                if not batch:
                    break
                raw.extend(batch)
                offset_id = batch[-1].id
                # After first batch reset to epoch (= no date filter); pagination continues via offset_id
                offset_date_arg = _EPOCH_DT
                job['fetched'] = len(raw)
                job['step'] = f"{step_prefix}Fetching… {len(raw):,} messages"
                if done:
                    break
                if max_msgs != 0 and len(raw) >= max_msgs:
                    break
                if len(batch) < batch_limit:
                    break
                # Small pause between batches to reduce Telegram FloodWait pressure
                await asyncio.sleep(0.3)
            raw.reverse()

            messages = [_build_msg_dict(m) for m in raw]
            job['total'] = len(messages)

            tmp_files = {}
            if fmt == 'zip' and not skip_media:
                media_msgs = [m for m in raw if m.media and m.media.value in DOWNLOADABLE_MEDIA]
                job['media_total'] = len(media_msgs)
                job['status'] = 'downloading'
                tmp_dir = _tempfile.mkdtemp(dir=EXPORTS_DIR)
                job['_tmp_dir'] = tmp_dir
                for i, rm in enumerate(media_msgs):
                    mtype = rm.media.value
                    mo = getattr(rm, mtype, None)
                    orig_fname = getattr(mo, 'file_name', None) or f"file_{rm.id}{_MEDIA_EXT_MAP.get(mtype, '.bin')}"
                    # Ask Pyrogram to save to this path; it may append/change extension
                    tmp_path = os.path.join(tmp_dir, f"msg_{rm.id}_{orig_fname}")
                    try:
                        result = await asyncio.wait_for(
                            client.download_media(rm, file_name=tmp_path), timeout=90
                        )
                        # result is the ACTUAL path Pyrogram saved to (may differ from tmp_path)
                        actual_path = str(result) if result else None
                        if not actual_path or not os.path.exists(actual_path):
                            # fallback: check tmp_path itself
                            actual_path = tmp_path if os.path.exists(tmp_path) else None
                        if actual_path:
                            # Build zip member name from actual filename (keeps correct extension)
                            actual_fname = os.path.basename(actual_path)
                            # Ensure it has the msg_id prefix for uniqueness
                            if not actual_fname.startswith(f"msg_{rm.id}_"):
                                actual_fname = f"msg_{rm.id}_{actual_fname}"
                            # Normalise: strip control chars and ZIP path separators
                            actual_fname = "".join(
                                c if c.isprintable() and c not in '\\/:\0' else "_"
                                for c in actual_fname
                            )
                            zip_member = f"media/{actual_fname}"
                            tmp_files[rm.id] = (actual_path, zip_member)
                    except Exception as e:
                        logger.warning(f"Export job {job_id}: skip media {rm.id}: {e}")
                    job['media_done'] = i + 1
                    job['step'] = f"Downloading media… {i+1:,} / {len(media_msgs):,}"
            return messages, tmp_files

        messages, tmp_files = await run_with_reconnect(session_str, _do)

        job['status'] = 'building'
        job['step'] = f"Building {fmt.upper()} file…"
        generated_at = datetime.now(_IST).strftime("%Y-%m-%d %H:%M IST")

        if fmt == 'html':
            out_path = os.path.join(EXPORTS_DIR, f"{job_id}.html")
            with open(out_path, 'w', encoding='utf-8') as f:
                f.write(_build_export_html(messages, chat_name, {}, generated_at))
            job.update(filename=f"{safe_name}_export.html", mimetype="text/html; charset=utf-8")

        elif fmt == 'zip':
            # Embed files ≤ 10 MB as data URIs so chat.html is self-contained
            # when opened inside the ZIP viewer.  Large files use relative paths
            # that resolve correctly after the ZIP is extracted.
            media_map = {}
            for mid, (tmp_path, zip_member) in tmp_files.items():
                try:
                    size = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0
                    if 0 < size <= _DATA_URI_LIMIT:
                        with open(tmp_path, 'rb') as fh:
                            raw_bytes = fh.read()
                        _, ext = os.path.splitext(zip_member.lower())
                        mime = _EXT_MIME_MAP.get(ext, "application/octet-stream")
                        media_map[mid] = f"data:{mime};base64,{base64.b64encode(raw_bytes).decode()}"
                    else:
                        media_map[mid] = zip_member  # relative path for large files
                except Exception:
                    media_map[mid] = zip_member
            html_content = _build_export_html(messages, chat_name, media_map, generated_at)
            out_path = os.path.join(EXPORTS_DIR, f"{job_id}.zip")
            with _zipfile.ZipFile(out_path, 'w', _zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("chat.html", html_content.encode('utf-8'))
                for mid, (tmp_path, zip_member) in tmp_files.items():
                    if os.path.exists(tmp_path):
                        zf.write(tmp_path, zip_member)
                        os.remove(tmp_path)
            tmp_dir = job.get('_tmp_dir')
            if tmp_dir and os.path.isdir(tmp_dir):
                try: os.rmdir(tmp_dir)
                except Exception: pass
            job.update(filename=f"{safe_name}_export.zip", mimetype="application/zip")

        elif fmt == 'json':
            out_path = os.path.join(EXPORTS_DIR, f"{job_id}.json")
            with open(out_path, 'w', encoding='utf-8') as f:
                _json.dump(messages, f, ensure_ascii=False, indent=2)
            job.update(filename=f"{safe_name}_export.json", mimetype="application/json; charset=utf-8")

        else:  # txt
            lines = [f"Export of: {chat_name}", f"Messages: {len(messages)}",
                     f"Generated: {generated_at}", "-" * 60, ""]
            for msg in messages:
                sender = "You" if msg.get("is_outgoing") else (
                    (msg.get("sender_name") or "Unknown") +
                    (f" (@{msg['sender_username']})" if msg.get("sender_username") else "")
                )
                text = msg.get("text") or ""
                media_part = ""
                if msg.get("has_media"):
                    media_part = f"[{(msg.get('media_type') or 'media').upper()}"
                    if msg.get("file_name"): media_part += f": {msg['file_name']}"
                    if msg.get("file_size"): media_part += f" ({msg['file_size']})"
                    media_part += "]"
                fwd = msg.get("forward_from")
                body = " ".join(p for p in [(f"[Fwd:{fwd}] " if fwd else "") + text, media_part] if p) or "(no text)"
                lines += [f"[{_to_ist_str_from_iso(msg.get('date'))}] {sender}:", f"  {body}", ""]
            out_path = os.path.join(EXPORTS_DIR, f"{job_id}.txt")
            with open(out_path, 'w', encoding='utf-8') as f:
                f.write("\n".join(lines))
            job.update(filename=f"{safe_name}_export.txt", mimetype="text/plain; charset=utf-8")

        job['path'] = out_path
        job['status'] = 'done'
        job['step'] = f"Done — {len(messages):,} messages"

    except Exception as e:
        logger.error(f"Export job {job_id} failed: {e}", exc_info=True)
        job['status'] = 'error'
        job['step'] = str(e)
        job['error'] = str(e)
        tmp_dir = job.get('_tmp_dir')
        if tmp_dir and os.path.isdir(tmp_dir):
            try: __import__('shutil').rmtree(tmp_dir, ignore_errors=True)
            except Exception: pass


@app.route("/api/chat-date-range/<chat_id>")
@api_login_required
def chat_date_range(chat_id):
    """Return the newest and oldest message dates for the given chat (fast — fetches 2 msgs)."""
    session_str = session.get("session_string")
    if not session_str:
        return jsonify({"error": "No session"}), 401
    try:
        chat_id_int = int(chat_id)
    except Exception:
        chat_id_int = chat_id

    async def _do(client):
        newest = oldest = None
        # Newest: first message in default history order
        async for m in client.get_chat_history(chat_id_int, limit=1):
            newest = m.date.isoformat()
            break
        # Oldest: use offset_id=1 trick — fetch 1 msg starting from the very beginning
        try:
            async for m in client.get_chat_history(chat_id_int, limit=1, offset_id=1, offset=-1):
                oldest = m.date.isoformat()
                break
        except Exception:
            pass
        return {"newest": newest, "oldest": oldest}

    try:
        result = run_async(run_with_reconnect(session_str, _do))
        return jsonify(result)
    except Exception as e:
        logger.warning(f"chat_date_range error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/export-job/<chat_id>", methods=["POST"])
@api_login_required
def start_export_job(chat_id):
    """Start a background export job, return job_id immediately."""
    session_str = session.get("session_string")
    if not session_str:
        return jsonify({"error": "No session"}), 401
    data = request.get_json(silent=True) or {}
    fmt = data.get("format", "html").lower()
    if fmt not in ("html", "zip", "json", "txt"):
        fmt = "html"
    max_raw = str(data.get("max", 5000))
    max_msgs = 0 if max_raw in ("0", "all") else max(1, int(max_raw))
    skip_media = bool(data.get("skip_media", False))

    from datetime import timezone as _tz2

    # from_date / to_date: "YYYY-MM-DD" → UTC midnight timestamp (float) or None
    def _parse_date_ts(s, end_of_day=False):
        s = (s or "").strip()
        if not s:
            return None
        try:
            dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=_tz2.utc)
            if end_of_day:
                dt = dt.replace(hour=23, minute=59, second=59)
            return dt.timestamp()
        except Exception:
            return None

    from_date_str = data.get("from_date", "").strip()
    to_date_str   = data.get("to_date",   "").strip()
    from_date_ts  = _parse_date_ts(from_date_str)          # UTC midnight of that day (lower bound)
    to_date_ts    = _parse_date_ts(to_date_str)             # UTC midnight of that day; +86400 added in worker → full day included

    try:
        chat_id_int = int(chat_id)
    except Exception:
        chat_id_int = chat_id
    sk = session_str[:16]
    with _chat_info_lock:
        ci = _chat_info_cache.get((sk, chat_id_int))
    chat_name = (ci or {}).get("name", f"chat_{chat_id}")
    safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in chat_name).strip() or f"chat_{chat_id}"

    range_label = ""
    if from_date_str or to_date_str:
        range_label = f"{from_date_str or '…'} → {to_date_str or 'now'}"

    job_id = str(uuid.uuid4())
    export_jobs[job_id] = {
        "status": "pending", "step": "Starting…",
        "fetched": 0, "total": 0, "media_done": 0, "media_total": 0,
        "fmt": fmt, "chat_name": chat_name,
        "from_date": from_date_str, "to_date": to_date_str, "range_label": range_label,
        "path": None, "filename": None, "mimetype": None, "error": None,
        "created": time.time(),
    }
    asyncio.run_coroutine_threadsafe(
        _run_export_job(job_id, session_str, chat_id_int, fmt, max_msgs, skip_media,
                        chat_name, safe_name, from_date_ts, to_date_ts),
        _loop
    )
    return jsonify({"job_id": job_id})


@app.route("/api/export-job-status/<job_id>")
@api_login_required
def export_job_status(job_id):
    job = export_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    return jsonify({
        "status": job["status"], "step": job["step"],
        "fetched": job["fetched"], "total": job["total"],
        "media_done": job["media_done"], "media_total": job["media_total"],
        "fmt": job["fmt"], "filename": job.get("filename"), "error": job.get("error"),
    })


@app.route("/api/export-job-download/<job_id>")
@api_login_required
def export_job_download(job_id):
    job = export_jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Not ready"}), 404
    path = job.get("path")
    if not path or not os.path.exists(path):
        return jsonify({"error": "File missing"}), 404
    return send_file(path, as_attachment=True, download_name=job["filename"], mimetype=job["mimetype"])


@app.route("/api/export-job/<job_id>", methods=["DELETE"])
@api_login_required
def delete_export_job(job_id):
    """Remove a single export job from memory and delete its file from disk.
    Refuses to delete a job that is still running to avoid orphaned files."""
    job = export_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    in_progress = job.get("status") in ("pending", "fetching", "downloading", "building")
    if in_progress:
        return jsonify({"error": "Export is still running — cancel it first"}), 409
    export_jobs.pop(job_id, None)
    path = job.get("path")
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except Exception as e:
            logger.warning(f"delete_export_job: could not remove {path}: {e}")
    tmp_dir = job.get("_tmp_dir")
    if tmp_dir and os.path.isdir(tmp_dir):
        try:
            __import__("shutil").rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
    return jsonify({"success": True})


@app.route("/api/export-jobs/clear-all", methods=["DELETE"])
@api_login_required
def clear_all_export_jobs():
    """Remove all export jobs from memory and delete their files from disk."""
    ids = list(export_jobs.keys())
    deleted_files = 0
    for job_id in ids:
        job = export_jobs.pop(job_id, None)
        if not job:
            continue
        path = job.get("path")
        if path and os.path.exists(path):
            try:
                os.remove(path)
                deleted_files += 1
            except Exception as e:
                logger.warning(f"clear_all_export_jobs: could not remove {path}: {e}")
        tmp_dir = job.get("_tmp_dir")
        if tmp_dir and os.path.isdir(tmp_dir):
            try:
                __import__("shutil").rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass
    # Also sweep EXPORTS_DIR for any orphaned files
    try:
        for fname in os.listdir(EXPORTS_DIR):
            fpath = os.path.join(EXPORTS_DIR, fname)
            if os.path.isfile(fpath):
                try:
                    os.remove(fpath)
                    deleted_files += 1
                except Exception:
                    pass
    except Exception:
        pass
    logger.info(f"clear_all_export_jobs: removed {deleted_files} file(s)")
    return jsonify({"success": True, "deleted": deleted_files})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
