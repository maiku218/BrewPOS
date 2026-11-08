"""BrewPOS - PostgreSQL database layer.

Replaces firebase_config.py. Provides PostgreSQL connection management,
schema initialization, default-data seeding, and query helpers using
psycopg2 with parameterized queries (%s placeholders) to prevent SQL injection.

All credentials are read from environment variables (never hardcoded).
"""

import os
from contextlib import contextmanager
from datetime import datetime

import psycopg2
from psycopg2.extras import RealDictCursor, Json

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Connection settings (environment variables, never hardcoded)
# ---------------------------------------------------------------------------
DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_NAME = os.environ.get("DB_NAME", "brewpos")
DB_USER = os.environ.get("DB_USER", "postgres")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")

# ---------------------------------------------------------------------------
# Application constants (mirrored from the original Firebase config)
# ---------------------------------------------------------------------------
LOW_STOCK_THRESHOLD = int(os.environ.get("LOW_STOCK_THRESHOLD", "10"))
EXPIRING_DAYS = int(os.environ.get("EXPIRING_DAYS", "7"))

DEFAULT_CATEGORIES = [
    "Coffee", "Non-Coffee", "Milk Tea", "Tea",
    "Pastry", "Cakes", "Sandwich", "Snacks",
    "Add-ons", "Others",
]

DEFAULT_ADDONS = [
    "Extra Espresso Shot", "Whipped Cream", "Cheese Foam",
    "Caramel Syrup", "Chocolate Syrup",
]

DEFAULT_STORE_SETTINGS = {
    "storeName": "BrewPOS",
    "subtitle": "Coffee Shop POS",
    "address": "",
    "contact": "",
    "footer": "Thank you for your purchase!\nPlease come again.",
}


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

def get_connection():
    """Open a new PostgreSQL connection."""
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )


@contextmanager
def get_cursor(commit=False):
    """Yield a RealDictCursor; commit and close the connection on exit."""
    conn = get_connection()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        yield cur
        if commit:
            conn.commit()
    finally:
        cur.close()
        conn.close()


@contextmanager
def transaction():
    """Yield (conn, cur) within a single transaction; commit on success,
    rollback on exception, always close the connection."""
    conn = get_connection()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        yield conn, cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def init_db():
    """Create tables, indexes, and seed default data if missing.

    Safe to run on every startup (idempotent via CREATE TABLE IF NOT EXISTS
    and existence checks before seeding).

    Raises psycopg2.OperationalError if PostgreSQL is not reachable.
    """
    schema_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
    with open(schema_path, "r", encoding="utf-8") as fh:
        schema_sql = fh.read()

    statements = [s.strip() for s in schema_sql.split(";") if s.strip()]

    # Use autocommit so a failed statement (e.g. a comment fragment split by
    # a ';' inside a comment block) doesn't abort the whole transaction.
    conn = get_connection()
    conn.autocommit = True
    try:
        cur = conn.cursor()
        for statement in statements:
            try:
                cur.execute(statement)
            except Exception:
                pass
        cur.close()

        # Seed default categories
        cur = conn.cursor()
        for cat in DEFAULT_CATEGORIES:
            cur.execute(
                "INSERT INTO categories (name) SELECT %s WHERE NOT EXISTS "
                "(SELECT 1 FROM categories WHERE name = %s)",
                (cat, cat),
            )

        # Seed default store settings (owner-scoped config rows are created
        # lazily when an admin first visits receipt customization).
        cur.execute(
            "INSERT INTO store_settings (id, store_name, subtitle, address, contact, footer, owner_id) "
            "SELECT 'config', %s, %s, %s, %s, %s, NULL "
            "WHERE NOT EXISTS (SELECT 1 FROM store_settings WHERE id = 'config')",
            (
                DEFAULT_STORE_SETTINGS["storeName"],
                DEFAULT_STORE_SETTINGS["subtitle"],
                DEFAULT_STORE_SETTINGS["address"],
                DEFAULT_STORE_SETTINGS["contact"],
                DEFAULT_STORE_SETTINGS["footer"],
            ),
        )
        cur.close()
    finally:
        conn.autocommit = False
        conn.close()


# ---------------------------------------------------------------------------
# Generic query helpers
# ---------------------------------------------------------------------------

def query_all(sql, params=None):
    with get_cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchall() or []


def query_one(sql, params=None):
    with get_cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchone()


def execute(sql, params=None, commit=True):
    with get_cursor(commit=commit) as cur:
        cur.execute(sql, params or ())
        return cur


def execute_returning(sql, params=None):
    with get_cursor(commit=True) as cur:
        cur.execute(sql, params or ())
        return cur.fetchone()


def now():
    return datetime.now()


# Backwards-compatible aliases used by the rest of the application.
def get_db():
    """Return a connection object kept for interface compatibility.

    The rest of the app now uses the query helpers above, but this function
    is retained so that any code path still referencing `get_db()` does not
    break at import time.
    """
    return get_connection()


def init_postgres():
    """Alias for init_db kept for readability in app.py."""
    init_db()
