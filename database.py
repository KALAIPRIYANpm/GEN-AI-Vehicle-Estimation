"""SQLite storage: schema, migrations from the legacy `reports` table, and small helpers."""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

import bcrypt

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DB_PATH", os.path.join(BASE_DIR, "car_damage_detection.db"))

PARTS = ["Bonnet", "Bumper", "Dickey", "Door", "Fender", "Light", "Windshield"]

# Used when a brand/model has no row in the price catalogue.
FALLBACK_PRICES = {
    "Bonnet": 15000, "Bumper": 10000, "Dickey": 8000, "Door": 18000,
    "Fender": 5000, "Light": 3500, "Windshield": 8000,
}

DEFAULT_SETTINGS = {
    "coverage_pct": "85",        # % of the claim total the insurer pays
    "deductible": "1000",        # compulsory deductible (₹) the customer always pays
    "gst_pct": "18",             # GST on parts + labour
    "labour_pct": "20",          # labour as % of part price
    "repair_pct": "40",          # cost of repairing instead of replacing, % of part price
    "min_confidence": "0.25",    # default detection threshold
    "vehicle_check": "1",        # reject photos that contain no vehicle
}

CLAIM_STATUSES = ["draft", "pending", "under_review", "approved", "rejected"]


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _columns(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


SCHEMA = """
CREATE TABLE IF NOT EXISTS user_info (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    password TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    vehicle_id TEXT NOT NULL UNIQUE,
    contact_number TEXT NOT NULL,
    address TEXT NOT NULL,
    car_brand TEXT NOT NULL,
    model TEXT NOT NULL,
    registration_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS car_models (
    brand TEXT NOT NULL,
    model TEXT NOT NULL,
    part TEXT NOT NULL,
    price INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_car_models ON car_models(brand, model, part);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
    report_id TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    car_brand TEXT,
    car_model TEXT,
    vehicle_id TEXT,
    title TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    parts_cost INTEGER DEFAULT 0,
    labour_cost INTEGER DEFAULT 0,
    gst INTEGER DEFAULT 0,
    total_estimate INTEGER DEFAULT 0,
    insurance_coverage INTEGER DEFAULT 0,
    customer_pays INTEGER DEFAULT 0,
    coverage_pct REAL,
    deductible INTEGER,
    policy_number TEXT,
    policy_source TEXT,
    rc_book_path TEXT,
    insurance_copy_path TEXT,
    incident_date TEXT,
    incident_note TEXT,
    admin_note TEXT,
    legacy INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    submitted_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_claims_email ON claims(email);
CREATE INDEX IF NOT EXISTS ix_claims_status ON claims(status);

CREATE TABLE IF NOT EXISTS claim_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id TEXT NOT NULL REFERENCES claims(report_id) ON DELETE CASCADE,
    position INTEGER DEFAULT 0,
    orig_path TEXT,
    det_path TEXT,
    width INTEGER,
    height INTEGER,
    detections TEXT
);

CREATE TABLE IF NOT EXISTS claim_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id TEXT NOT NULL REFERENCES claims(report_id) ON DELETE CASCADE,
    part TEXT NOT NULL,
    instances INTEGER DEFAULT 1,
    confidence REAL,
    severity TEXT,
    action TEXT DEFAULT 'replace',
    base_price INTEGER DEFAULT 0,
    unit_price INTEGER DEFAULT 0,
    labour INTEGER DEFAULT 0,
    total INTEGER DEFAULT 0,
    included INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS claim_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id TEXT NOT NULL REFERENCES claims(report_id) ON DELETE CASCADE,
    actor TEXT,
    event TEXT NOT NULL,
    note TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def init_db():
    """Create tables, seed data, and migrate older databases in place. Safe to run repeatedly."""
    conn = connect()
    try:
        # Older databases created car_models without the unique index and may hold duplicates.
        if _table_exists(conn, "car_models"):
            conn.execute(
                "DELETE FROM car_models WHERE rowid NOT IN "
                "(SELECT MAX(rowid) FROM car_models GROUP BY brand, model, part)"
            )
        conn.executescript(SCHEMA)

        if "status" not in _columns(conn, "user_info"):
            conn.execute("ALTER TABLE user_info ADD COLUMN status TEXT DEFAULT 'active'")

        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))

        if conn.execute("SELECT COUNT(*) FROM car_models").fetchone()[0] == 0:
            seed_prices(conn)

        admin_email = os.getenv("ADMIN_EMAIL", "admin@gmail.com")
        admin_password = os.getenv("ADMIN_PASSWORD", "admin123")
        if conn.execute("SELECT COUNT(*) FROM admins").fetchone()[0] == 0:
            conn.execute(
                "INSERT INTO admins(email, password) VALUES (?, ?)",
                (admin_email, hash_password(admin_password)),
            )

        migrate_legacy_reports(conn)
        conn.commit()
    finally:
        conn.close()


def seed_prices(conn):
    path = os.path.join(BASE_DIR, "car_parts_prices.json")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    for brand, models in data.items():
        for model, parts in models.items():
            for part, price in parts.items():
                conn.execute(
                    "INSERT OR REPLACE INTO car_models(brand, model, part, price) VALUES (?,?,?,?)",
                    (brand.upper(), model, part, int(price)),
                )


def migrate_legacy_reports(conn):
    """The first version stored one row per detected part in `reports`. Fold those into claims."""
    if not _table_exists(conn, "reports"):
        return
    rows = conn.execute("SELECT * FROM reports ORDER BY id").fetchall()
    groups = {}
    for r in rows:
        groups.setdefault(r["report_id"], []).append(r)

    for report_id, items in groups.items():
        if conn.execute("SELECT 1 FROM claims WHERE report_id=?", (report_id,)).fetchone():
            continue
        first = items[0]
        user = conn.execute(
            "SELECT car_brand, model, vehicle_id FROM user_info WHERE email=?", (first["email"],)
        ).fetchone()
        status = (first["status"] or "pending").lower()
        if status not in CLAIM_STATUSES:
            status = "pending"
        total = int(first["total_estimate"] or 0)
        cov = int(first["insurance_coverage"] or 0)
        conn.execute(
            """INSERT INTO claims (report_id, email, car_brand, car_model, vehicle_id, status,
                   parts_cost, labour_cost, gst, total_estimate, insurance_coverage, customer_pays,
                   coverage_pct, deductible, policy_number, policy_source, rc_book_path,
                   insurance_copy_path, legacy, created_at, updated_at, submitted_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)""",
            (
                report_id, first["email"],
                user["car_brand"] if user else None, user["model"] if user else None,
                user["vehicle_id"] if user else None, status,
                total, 0, 0, total, cov, int(first["customer_pays"] or 0),
                85.0, 0, first["policy_number"], "ocr" if first["policy_number"] else None,
                _legacy_path(first["rc_book_path"]), _legacy_path(first["insurance_copy_path"]),
                first["created_at"], first["created_at"], first["created_at"],
            ),
        )
        det = first["image_path"]
        if det:
            orig = det.replace("det_report_", "orig_report_")
            conn.execute(
                "INSERT INTO claim_images(report_id, position, orig_path, det_path, detections) VALUES (?,?,?,?,?)",
                (report_id, 0, _legacy_path(orig), _legacy_path(det), None),
            )
        for it in items:
            if not it["Identified_Component"]:
                continue
            conn.execute(
                """INSERT INTO claim_items(report_id, part, instances, severity, action, base_price,
                   unit_price, labour, total) VALUES (?,?,?,?,?,?,?,?,?)""",
                (report_id, it["Identified_Component"], it["Instances"] or 1, None, "replace",
                 it["price"] or 0, it["price"] or 0, 0, it["total"] or 0),
            )
        conn.execute(
            "INSERT INTO claim_events(report_id, actor, event, note, created_at) VALUES (?,?,?,?,?)",
            (report_id, first["email"], "created", "Imported from the previous version", first["created_at"]),
        )
        if status in ("approved", "rejected"):
            conn.execute(
                "INSERT INTO claim_events(report_id, actor, event, created_at) VALUES (?,?,?,?)",
                (report_id, "admin", status, first["created_at"]),
            )
    conn.execute("ALTER TABLE reports RENAME TO reports_legacy")


def _legacy_path(p):
    """Legacy files lived under static/; record them with a 'static:' prefix."""
    if not p:
        return None
    return "static:" + p.lstrip("/")


# ---------------- helpers ----------------
def hash_password(pw):
    return bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def check_password(pw, hashed):
    try:
        return bcrypt.checkpw(pw.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, AttributeError):
        return False


def get_settings(conn=None):
    own = conn is None
    conn = conn or connect()
    try:
        s = dict(DEFAULT_SETTINGS)
        s.update({r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")})
        return {
            "coverage_pct": float(s["coverage_pct"]),
            "deductible": int(float(s["deductible"])),
            "gst_pct": float(s["gst_pct"]),
            "labour_pct": float(s["labour_pct"]),
            "repair_pct": float(s["repair_pct"]),
            "min_confidence": float(s["min_confidence"]),
            "vehicle_check": s["vehicle_check"] in ("1", "true", "on"),
        }
    finally:
        if own:
            conn.close()


def get_catalogue(conn):
    """{BRAND: {model: {part: price}}}"""
    out = {}
    for r in conn.execute("SELECT brand, model, part, price FROM car_models ORDER BY brand, model"):
        out.setdefault(r["brand"], {}).setdefault(r["model"], {})[r["part"]] = r["price"]
    return out


def part_price(conn, brand, model, part):
    row = conn.execute(
        "SELECT price FROM car_models WHERE UPPER(brand)=UPPER(?) AND LOWER(model)=LOWER(?) AND part=?",
        (brand or "", model or "", part),
    ).fetchone()
    if row:
        return int(row["price"]), True
    return int(FALLBACK_PRICES.get(part, 10000)), False


def add_event(conn, report_id, actor, event, note=None):
    conn.execute(
        "INSERT INTO claim_events(report_id, actor, event, note, created_at) VALUES (?,?,?,?,?)",
        (report_id, actor, event, note, now()),
    )
