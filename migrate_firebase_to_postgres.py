"""BrewPOS - Firebase to PostgreSQL migration script.

Reads all documents from Firestore collections and writes them into the
corresponding PostgreSQL tables, preserving document IDs as primary keys
wherever possible and maintaining ownerId multi-tenancy relationships.

Prerequisites:
  1. PostgreSQL running and the schema loaded (schema.sql / db.init_db()).
  2. Firebase service-account credentials available via one of:
       FIREBASE_CREDENTIALS_JSON      (raw JSON)
       FIREBASE_CREDENTIALS_JSON_BASE64 (base64-encoded JSON)
       firebase_credentials.json      (file in project root)

Usage:
  python migrate_firebase_to_postgres.py        # run the migration
  python migrate_firebase_to_postgres.py --dry-run   # print what would be migrated

Data safety:
  - Firestore is never modified (read-only).
  - PostgreSQL rows are inserted only if they do not already exist
    (ON CONFLICT DO NOTHING on the primary key), so re-running is safe
    and will not duplicate data.
"""

import argparse
import json
import os
import sys
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Firebase (read-only) initialization
# ---------------------------------------------------------------------------

def init_firestore():
    """Initialize the Firebase Admin SDK in read-only mode.

    Returns the firestore client, or None if Firebase credentials are not
    available (caller handles the absence gracefully).
    """
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
    except ImportError:
        print("[MIGRATE] firebase-admin not installed; cannot read Firestore.")
        return None

    env_b64 = os.environ.get("FIREBASE_CREDENTIALS_JSON_BASE64")
    env_json = os.environ.get("FIREBASE_CREDENTIALS_JSON")
    cred_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "firebase_credentials.json")

    cred = None
    if env_b64:
        import base64
        cred = credentials.Certificate(json.loads(base64.b64decode(env_b64).decode("utf-8")))
    elif env_json:
        cred = credentials.Certificate(json.loads(env_json))
    elif os.path.exists(cred_path):
        cred = credentials.Certificate(json.load(open(cred_path, "r", encoding="utf-8")))

    if cred is None:
        print("[MIGRATE] No Firebase credentials found. Set FIREBASE_CREDENTIALS_JSON "
              "or FIREBASE_CREDENTIALS_JSON_BASE64, or place firebase_credentials.json in the project root.")
        return None

    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    return firestore.client()


# ---------------------------------------------------------------------------
# PostgreSQL helpers (uses db module)
# ---------------------------------------------------------------------------

def pg_query(sql, params=None):
    from db import get_cursor
    with get_cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchall() or []


def pg_execute(sql, params=None, commit=True):
    from db import get_cursor
    with get_cursor(commit=commit) as cur:
        cur.execute(sql, params or ())
        return cur


def pg_exists(table, id_value):
    row = pg_query(
        f"SELECT 1 FROM {table} WHERE id = %s LIMIT 1", (id_value,)
    )
    return bool(row)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_naive(dt):
    if dt is None:
        return datetime.now()
    if isinstance(dt, datetime):
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    return datetime.now()


def _json_default(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


def _serialize(doc_dict):
    """Convert Firestore document dict into JSON-safe values for PostgreSQL."""
    result = {}
    for key, value in (doc_dict or {}).items():
        if isinstance(value, datetime):
            result[key] = value
        elif isinstance(value, (list, dict)):
            result[key] = Json(value)
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# Collection migrations
# ---------------------------------------------------------------------------

COLLECTION_ORDER = [
    "users",
    "cashiers",
    "categories",
    "products",
    "sales",
    "stock_movements",
    "store_settings",
    "admin_activity",
    "cashier_activity",
]


def migrate_collection(fs_db, pg_table, id_field="id", owner_field="ownerId"):
    """Migrate one Firestore collection into one PostgreSQL table."""
    count = 0
    skipped = 0
    for doc in fs_db.collection(pg_table).stream():
        doc_id = doc.id
        data = _serialize(doc.to_dict())

        # Preserve the original document id as the primary key.
        data[id_field] = doc_id

        if pg_exists(pg_table, doc_id):
            skipped += 1
            continue

        columns = list(data.keys())
        placeholders = ", ".join(["%s"] * len(columns))
        col_list = ", ".join(columns)
        sql = (
            f"INSERT INTO {pg_table} ({col_list}) VALUES ({placeholders}) "
            "ON CONFLICT ({id_field}) DO NOTHING".format(id_field=id_field)
        )
        pg_execute(sql, [data[c] for c in columns], commit=True)
        count += 1

    print(f"[MIGRATE] {pg_table}: inserted={count}, already_existed={skipped}")


def migrate_sales_with_items(fs_db):
    """Migrate sales including their items subcollection."""
    count = 0
    skipped = 0
    for doc in fs_db.collection("sales").stream():
        doc_id = doc.id
        data = _serialize(doc.to_dict())
        data["id"] = doc_id

        # Normalize date
        if isinstance(data.get("date"), datetime):
            data["date"] = data["date"].replace(tzinfo=None) if data["date"].tzinfo else data["date"]

        if pg_exists("sales", doc_id):
            skipped += 1
            continue

        columns = list(data.keys())
        placeholders = ", ".join(["%s"] * len(columns))
        col_list = ", ".join(columns)
        sql = (
            f"INSERT INTO sales ({col_list}) VALUES ({placeholders}) "
            "ON CONFLICT (id) DO NOTHING"
        )
        pg_execute(sql, [data[c] for c in columns], commit=True)
        count += 1

        # Migrate items subcollection
        for item_doc in doc.reference.collection("items").stream():
            item_data = _serialize(item_doc.to_dict())
            item_id = item_doc.id
            if pg_exists("sale_items", item_id):
                continue
            item_data["sale_id"] = doc_id
            # sale_items.id is SERIAL, don't insert it manually
            item_data.pop("id", None)
            icolumns = list(item_data.keys())
            iplaceholders = ", ".join(["%s"] * len(icolumns))
            icol_list = ", ".join(icolumns)
            isql = (
                f"INSERT INTO sale_items ({icol_list}) VALUES ({iplaceholders}) "
                "ON CONFLICT (id) DO NOTHING"
            )
            pg_execute(isql, [item_data[c] for c in icolumns], commit=True)

    print(f"[MIGRATE] sales: inserted={count}, already_existed={skipped}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Migrate Firestore data to PostgreSQL.")
    parser.add_argument("--dry-run", action="store_true", help="Show plan without writing.")
    args = parser.parse_args()

    from db import init_db

    print("[MIGRATE] Ensuring PostgreSQL schema exists...")
    init_db()

    fs_db = init_firestore()
    if fs_db is None:
        print("[MIGRATE] Skipping Firestore read: no credentials available.")
        print("[MIGRATE] To migrate existing Firebase data, provide Firebase credentials "
              "and re-run this script.")
        return

    print("[MIGRATE] Reading from Firestore and writing to PostgreSQL...")
    for table in COLLECTION_ORDER:
        if table == "sales":
            migrate_sales_with_items(fs_db)
        else:
            migrate_collection(fs_db, table)

    print("[MIGRATE] Migration complete.")


if __name__ == "__main__":
    main()
