import sqlite3
import os
from flask import g

_default_dir = os.path.dirname(__file__) or "."
_data_dir = os.environ.get("DATA_DIR", _default_dir)

if not os.path.isdir(_data_dir):
    try:
        os.makedirs(_data_dir, exist_ok=True)
    except OSError:
        _data_dir = "/tmp"

try:
    _test_file = os.path.join(_data_dir, ".write_test")
    with open(_test_file, "w") as f:
        f.write("test")
    os.remove(_test_file)
except OSError:
    _data_dir = "/tmp"

DATABASE = os.path.join(_data_dir, "kintai.db")

INITIAL_CAST_MEMBERS = ["りん", "ももせ", "ゆい", "せり", "らむ", "こと", "はな"]
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "Gift-0723"

_db_initialized = False


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    global _db_initialized
    if _db_initialized:
        return

    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            is_admin INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS shifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            business_date TEXT NOT NULL,
            shift_start TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id),
            UNIQUE(user_id, business_date)
        );

        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            business_date TEXT NOT NULL,
            clock_in TEXT NOT NULL,
            clock_out TEXT,
            punch_type TEXT DEFAULT 'normal',
            status TEXT DEFAULT '',
            late_reason TEXT DEFAULT '',
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
    """)

    user_count = db.execute("SELECT COUNT(*) as cnt FROM users").fetchone()["cnt"]
    if user_count == 0:
        for name in INITIAL_CAST_MEMBERS:
            db.execute("INSERT INTO users (name, is_admin) VALUES (?, 0)", (name,))
        db.execute("INSERT INTO users (name, is_admin) VALUES (?, 1)", (ADMIN_USERNAME,))
    else:
        admin = db.execute("SELECT id FROM users WHERE name = ? AND is_admin = 1", (ADMIN_USERNAME,)).fetchone()
        if not admin:
            db.execute("INSERT INTO users (name, is_admin) VALUES (?, 1)", (ADMIN_USERNAME,))

    db.commit()
    _db_initialized = True


def init_app(app):
    app.teardown_appcontext(close_db)
