import os

import psycopg2
from psycopg2.extras import RealDictCursor
from flask import g

DATABASE_URL = os.environ.get("DATABASE_URL", "")

INITIAL_CAST_MEMBERS = ["りん", "ももせ", "ゆい", "せり", "らむ", "こと", "はな"]
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "Gift-0723"

_db_initialized = False


class _DbWrapper:
    """psycopg2 wrapper that accepts SQLite-style ? placeholders."""

    def __init__(self, conn):
        self.conn = conn

    def execute(self, query, params=None):
        query = query.replace("?", "%s")
        cur = self.conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(query, params)
        return cur

    def commit(self):
        self.conn.commit()

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


def get_db():
    if "db" not in g:
        conn = psycopg2.connect(DATABASE_URL, sslmode="require")
        g.db = _DbWrapper(conn)
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

    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            is_admin INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS shifts (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            business_date TEXT NOT NULL,
            shift_start TEXT NOT NULL,
            UNIQUE(user_id, business_date)
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            business_date TEXT NOT NULL,
            clock_in TEXT NOT NULL,
            clock_out TEXT,
            punch_type TEXT DEFAULT 'normal',
            status TEXT DEFAULT '',
            late_reason TEXT DEFAULT ''
        )
    """)

    # 送迎（ピックアップ）記録。1営業日1回まで。
    db.execute("""
        CREATE TABLE IF NOT EXISTS pickups (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            business_date TEXT NOT NULL,
            clock_time TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, business_date)
        )
    """)

    # 送迎ドライバーのシフト（その営業日に送迎ありかどうか）。
    db.execute("""
        CREATE TABLE IF NOT EXISTS driver_shifts (
            id SERIAL PRIMARY KEY,
            business_date TEXT NOT NULL,
            driver_name TEXT DEFAULT '',
            UNIQUE(business_date, driver_name)
        )
    """)

    # 既存DB向け: キャストの送迎料金（500 or 1000、既定1000）。
    db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS pickup_fee INTEGER DEFAULT 1000")

    db.commit()

    user_count = db.execute("SELECT COUNT(*) as cnt FROM users").fetchone()["cnt"]
    if user_count == 0:
        for name in INITIAL_CAST_MEMBERS:
            db.execute("INSERT INTO users (name, is_admin) VALUES (%s, 0)", (name,))
        db.execute("INSERT INTO users (name, is_admin) VALUES (%s, 1)", (ADMIN_USERNAME,))
    else:
        admin = db.execute(
            "SELECT id FROM users WHERE name = %s AND is_admin = 1", (ADMIN_USERNAME,)
        ).fetchone()
        if not admin:
            db.execute(
                "INSERT INTO users (name, is_admin) VALUES (%s, 1)", (ADMIN_USERNAME,)
            )

    db.commit()
    _db_initialized = True


def init_app(app):
    app.teardown_appcontext(close_db)
