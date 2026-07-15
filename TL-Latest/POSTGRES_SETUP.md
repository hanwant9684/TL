# PostgreSQL Setup on a VPS

The app reads `DATABASE_URL` from the environment.
Set it to a Postgres connection string and SQLAlchemy switches automatically —
no code changes needed.

---

## 1 · Install PostgreSQL

```bash
# Ubuntu / Debian
sudo apt update
sudo apt install -y postgresql postgresql-contrib

# Start and enable
sudo systemctl enable --now postgresql
```

---

## 2 · Create a database and user

```bash
sudo -u postgres psql <<'SQL'
CREATE USER tlapp WITH PASSWORD 'choose_a_strong_password';
CREATE DATABASE tldb OWNER tlapp;
GRANT ALL PRIVILEGES ON DATABASE tldb TO tlapp;
SQL
```

---

## 3 · Allow the app to connect

Edit `/etc/postgresql/<version>/main/pg_hba.conf` and confirm this line exists
(it usually does by default for local connections):

```
local   all   tlapp   md5
```

If the app runs on a **different machine** from Postgres, add:

```
host    tldb   tlapp   <app-server-ip>/32   md5
```

Then open the port in your firewall (only if needed):

```bash
sudo ufw allow from <app-server-ip> to any port 5432
```

And in `/etc/postgresql/<version>/main/postgresql.conf` set:

```
listen_addresses = '*'   # or the specific IP of the DB server
```

Reload after any config change:

```bash
sudo systemctl reload postgresql
```

---

## 4 · Set the environment variable

### If running the app directly (systemd / screen / tmux)

```bash
export DATABASE_URL="postgresql://tlapp:choose_a_strong_password@localhost:5432/tldb"
```

Put that in the `.env` file or your systemd unit's `[Service]` section:

```ini
[Service]
Environment="DATABASE_URL=postgresql://tlapp:choose_a_strong_password@localhost:5432/tldb"
```

### If running on Replit

Go to **Secrets** and add:

| Key | Value |
|-----|-------|
| `DATABASE_URL` | `postgresql://tlapp:choose_a_strong_password@<vps-ip>:5432/tldb` |

---

## 5 · Run migrations / create tables

The app calls `db.create_all()` at startup, so all tables are created
automatically the first time it starts with a valid `DATABASE_URL`.
Just start the app normally — you'll see no errors if the connection works.

To verify from the command line:

```bash
python - <<'PY'
import os, sys
os.environ.setdefault("DATABASE_URL", "postgresql://tlapp:choose_a_strong_password@localhost:5432/tldb")
sys.path.insert(0, ".")
from app import app, db
with app.app_context():
    db.create_all()
    print("Tables OK")
PY
```

---

## 6 · (Optional) Migrate existing SQLite data

If you already have data in `app.db` that you want to keep:

```bash
pip install pgloader
pgloader sqlite:///app.db postgresql://tlapp:choose_a_strong_password@localhost:5432/tldb
```

`pgloader` maps SQLite types to Postgres automatically and copies all rows.

---

## 7 · Connection string formats

| Scenario | DATABASE_URL |
|----------|-------------|
| Same machine (Unix socket) | `postgresql://tlapp:password@localhost/tldb` |
| Same machine (TCP) | `postgresql://tlapp:password@127.0.0.1:5432/tldb` |
| Remote VPS → DB VPS | `postgresql://tlapp:password@<db-ip>:5432/tldb` |
| Render / Railway / Supabase | Paste the URL they give you directly |

> The app already handles the `postgres://` → `postgresql://` rename that
> some hosting providers still use.

---

## 8 · Why Postgres is faster than SQLite here

SQLite uses a single file-level write lock — any concurrent write (preserve
media, save download record, mark deleted) blocks every other writer.
Postgres uses row-level locking and a real connection pool, so the Pyrogram
background handlers, Flask request threads, and the tray-poller can all
write at the same time without queuing behind each other.
