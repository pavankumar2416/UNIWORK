from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from functools import wraps
from pathlib import Path

from flask import Flask, Response, flash, g, jsonify, redirect, render_template, request, url_for, session
from werkzeug.security import generate_password_hash, check_password_hash
try:
    from cryptography.fernet import Fernet
except ImportError:  # bank-detail entry remains disabled until requirements are installed
    Fernet = None

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("UNIWORK_DB", BASE_DIR / "instance" / "uniwork.sqlite3"))
app = Flask(__name__, template_folder="../frontend/templates", static_folder="../frontend/static", static_url_path="/static")
# Stable across workers so tab sessions work when the WSGI server uses multiple workers.
# Sessions are revoked on logout/account disable and expire after 8 hours of inactivity.
BOOT_ID = os.environ.get("UNIWORK_BOOT_ID", "uniwork-session-v1")
IS_PRODUCTION = (
    os.environ.get("RENDER", "").lower() == "true"
    or os.environ.get("FLASK_ENV", "").lower() == "production"
)
_configured_secret = os.environ.get("SECRET_KEY", "").strip()
if IS_PRODUCTION and (
    len(_configured_secret) < 32
    or _configured_secret.lower().startswith(("replace-", "change-me", "your-secret"))
):
    raise RuntimeError("Production startup blocked: set SECRET_KEY to a strong random value of at least 32 characters; do not use the example placeholder.")
_production_admin_password = os.environ.get("ADMIN_PASSWORD", "")
if IS_PRODUCTION and (
    len(_production_admin_password) < 12
    or _production_admin_password.lower().startswith(("replace-", "change-me", "your-password"))
    or _production_admin_password == "Forge@2026!"
):
    raise RuntimeError("Production startup blocked: set ADMIN_PASSWORD to a unique password of at least 12 characters; do not use the example/demo password.")
app.secret_key = _configured_secret or "uniwork-local-demo-secret-change-before-deployment"
_cookie_secure_default = "1" if IS_PRODUCTION else "0"
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", _cookie_secure_default) == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)
app.config["JSON_SORT_KEYS"] = False
_PRODUCTION_DEMO_CREDENTIAL_ROTATION_CHECKED = False

GST_RATES = {"8536": 18, "8544": 18, "8504": 18, "8538": 18, "9403": 18, "3926": 18}
STATUSES = ["Draft", "Ordered", "Partially Received", "Received", "Cancelled"]


def money(value) -> float:
    """Round a finite monetary amount to two decimal places using GST-style rounding."""
    try:
        number = Decimal(str(value or 0))
        if not number.is_finite():
            raise ValueError("Enter a valid finite monetary amount.")
        return float(number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except (ArithmeticError, ValueError) as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError("Monetary amount is outside the supported range.") from None


def parse_nonnegative_money(raw, field_name: str) -> float:
    """Parse and validate a browser form's non-negative monetary value."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a valid number.") from None
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{field_name} must be finite and cannot be negative.")
    if value > 1_000_000_000_000:
        raise ValueError(f"{field_name} exceeds the supported maximum of ₹1,000,000,000,000.")
    return money(value)


def validate_iso_date(raw, field_name: str, *, optional: bool = False):
    value = (raw or "").strip()
    if not value and optional:
        return None
    if not value:
        raise ValueError(f"{field_name} is required.")
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a valid YYYY-MM-DD date.") from None


def _vendor_data_fernet():
    """Return a Fernet cipher derived from a deployment secret, never a database-stored key."""
    key = os.environ.get("VENDOR_DATA_KEY", "").strip()
    if Fernet is None:
        raise ValueError("Install the declared cryptography dependency before maintaining supplier bank details.")
    if not key:
        raise ValueError("Supplier bank details are disabled until VENDOR_DATA_KEY is configured.")
    try:
        return Fernet(key.encode("utf-8"))
    except (TypeError, ValueError):
        raise ValueError("VENDOR_DATA_KEY must be a valid Fernet key. Generate one with the documented command.") from None


def csrf_token_for(tab_token: str | None) -> str:
    """Create a request token bound to a random tab token and the app secret."""
    if not tab_token:
        return ""
    key = app.secret_key if isinstance(app.secret_key, bytes) else str(app.secret_key).encode("utf-8")
    return hmac.new(key, ("uniwork-csrf:" + tab_token).encode("utf-8"), hashlib.sha256).hexdigest()


def get_db():
    if "db" not in g:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    global _PRODUCTION_DEMO_CREDENTIAL_ROTATION_CHECKED
    db = get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS tab_sessions (
        tab_token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        boot_id TEXT NOT NULL, created_at TEXT NOT NULL, last_seen TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL, full_name TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('admin','procurement','storekeeper','accounts','viewer')),
        active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS vendors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, gstin TEXT NOT NULL,
        state TEXT NOT NULL, email TEXT, phone TEXT, payment_terms INTEGER DEFAULT 30,
        active INTEGER DEFAULT 1, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sku TEXT UNIQUE NOT NULL, name TEXT NOT NULL, category TEXT NOT NULL,
        hsn TEXT NOT NULL, gst_rate REAL NOT NULL, unit TEXT NOT NULL DEFAULT 'pcs',
        reorder_level INTEGER NOT NULL DEFAULT 10, unit_cost REAL NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS warehouses (
        id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL, city TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS purchase_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, po_number TEXT UNIQUE NOT NULL,
        vendor_id INTEGER NOT NULL REFERENCES vendors(id), order_date TEXT NOT NULL,
        expected_date TEXT, status TEXT NOT NULL DEFAULT 'Draft',
        notes TEXT, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS purchase_order_lines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        po_id INTEGER NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
        product_id INTEGER NOT NULL REFERENCES products(id),
        quantity INTEGER NOT NULL CHECK(quantity > 0), unit_price REAL NOT NULL CHECK(unit_price >= 0)
    );
    CREATE TABLE IF NOT EXISTS goods_receipts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, grn_number TEXT UNIQUE NOT NULL,
        po_id INTEGER NOT NULL REFERENCES purchase_orders(id),
        warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
        received_date TEXT NOT NULL, reference TEXT, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS goods_receipt_lines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        grn_id INTEGER NOT NULL REFERENCES goods_receipts(id) ON DELETE CASCADE,
        product_id INTEGER NOT NULL REFERENCES products(id),
        quantity INTEGER NOT NULL CHECK(quantity > 0)
    );
    CREATE TABLE IF NOT EXISTS stock_movements (
        id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL REFERENCES products(id),
        warehouse_id INTEGER NOT NULL REFERENCES warehouses(id), movement_type TEXT NOT NULL,
        quantity INTEGER NOT NULL, reference TEXT NOT NULL, movement_date TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS vendor_invoices (
        id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_number TEXT NOT NULL,
        vendor_id INTEGER NOT NULL REFERENCES vendors(id), po_id INTEGER REFERENCES purchase_orders(id),
        invoice_date TEXT NOT NULL, taxable_value REAL NOT NULL, gst_amount REAL NOT NULL,
        total_amount REAL NOT NULL, status TEXT NOT NULL DEFAULT 'Pending',
        itc_status TEXT NOT NULL DEFAULT 'Review', notes TEXT, created_at TEXT NOT NULL,
        UNIQUE(vendor_id, invoice_number)
    );
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL,
        entity_type TEXT NOT NULL, entity_id INTEGER, details TEXT, created_at TEXT NOT NULL
    );
    """)
    # Additional operational modules and safe, backward-compatible migrations.
    db.executescript("""
    CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL, gstin TEXT NOT NULL, state TEXT NOT NULL,
        address TEXT NOT NULL DEFAULT '', pincode TEXT NOT NULL DEFAULT '',
        email TEXT, phone TEXT, payment_terms INTEGER NOT NULL DEFAULT 30,
        active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS vendor_invoice_lines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_id INTEGER NOT NULL REFERENCES vendor_invoices(id) ON DELETE CASCADE,
        po_line_id INTEGER REFERENCES purchase_order_lines(id),
        product_id INTEGER NOT NULL REFERENCES products(id),
        quantity INTEGER NOT NULL CHECK(quantity > 0),
        unit_price REAL NOT NULL CHECK(unit_price >= 0),
        taxable_value REAL NOT NULL CHECK(taxable_value >= 0),
        gst_rate REAL NOT NULL, gst_amount REAL NOT NULL CHECK(gst_amount >= 0),
        UNIQUE(invoice_id, product_id)
    );
    CREATE TABLE IF NOT EXISTS material_consumptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, reference TEXT UNIQUE NOT NULL,
        product_id INTEGER NOT NULL REFERENCES products(id),
        warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
        quantity INTEGER NOT NULL CHECK(quantity > 0),
        movement_date TEXT NOT NULL, purpose TEXT NOT NULL,
        created_by TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS stock_transfers (
        id INTEGER PRIMARY KEY AUTOINCREMENT, reference TEXT UNIQUE NOT NULL,
        product_id INTEGER NOT NULL REFERENCES products(id),
        quantity INTEGER NOT NULL CHECK(quantity > 0),
        from_warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
        to_warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
        movement_date TEXT NOT NULL, reason TEXT NOT NULL,
        created_by TEXT NOT NULL, created_at TEXT NOT NULL,
        CHECK(from_warehouse_id <> to_warehouse_id)
    );
    CREATE TABLE IF NOT EXISTS vendor_payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_id INTEGER NOT NULL REFERENCES vendor_invoices(id),
        payment_date TEXT NOT NULL, amount REAL NOT NULL CHECK(amount > 0),
        reference TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
        created_by TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS sales_invoices (
        id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_number TEXT UNIQUE NOT NULL,
        customer_id INTEGER NOT NULL REFERENCES customers(id), invoice_date TEXT NOT NULL,
        due_date TEXT NOT NULL, taxable_value REAL NOT NULL CHECK(taxable_value >= 0),
        igst_amount REAL NOT NULL DEFAULT 0 CHECK(igst_amount >= 0),
        cgst_amount REAL NOT NULL DEFAULT 0 CHECK(cgst_amount >= 0),
        sgst_amount REAL NOT NULL DEFAULT 0 CHECK(sgst_amount >= 0),
        gst_amount REAL NOT NULL CHECK(gst_amount >= 0),
        total_amount REAL NOT NULL CHECK(total_amount >= 0),
        paid_amount REAL NOT NULL DEFAULT 0 CHECK(paid_amount >= 0),
        status TEXT NOT NULL DEFAULT 'Open', notes TEXT NOT NULL DEFAULT '',
        created_by TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS sales_invoice_lines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_id INTEGER NOT NULL REFERENCES sales_invoices(id) ON DELETE CASCADE,
        product_id INTEGER NOT NULL REFERENCES products(id),
        product_sku TEXT NOT NULL, product_name TEXT NOT NULL, hsn TEXT NOT NULL, unit TEXT NOT NULL,
        quantity INTEGER NOT NULL CHECK(quantity > 0), unit_price REAL NOT NULL CHECK(unit_price >= 0),
        taxable_value REAL NOT NULL CHECK(taxable_value >= 0), gst_rate REAL NOT NULL,
        igst_amount REAL NOT NULL DEFAULT 0, cgst_amount REAL NOT NULL DEFAULT 0,
        sgst_amount REAL NOT NULL DEFAULT 0, gst_amount REAL NOT NULL DEFAULT 0,
        total_amount REAL NOT NULL CHECK(total_amount >= 0),
        UNIQUE(invoice_id, product_id)
    );
    CREATE TABLE IF NOT EXISTS sales_invoice_transport (
        invoice_id INTEGER PRIMARY KEY REFERENCES sales_invoices(id) ON DELETE CASCADE,
        trans_mode TEXT NOT NULL DEFAULT '1', trans_distance TEXT NOT NULL DEFAULT '0',
        vehicle_no TEXT NOT NULL DEFAULT '', vehicle_type TEXT NOT NULL DEFAULT 'R',
        transporter_id TEXT NOT NULL DEFAULT '', transporter_name TEXT NOT NULL DEFAULT '',
        transport_doc_no TEXT NOT NULL DEFAULT '', transport_doc_date TEXT,
        updated_by TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS vendor_bank_details (
        vendor_id INTEGER PRIMARY KEY REFERENCES vendors(id) ON DELETE CASCADE,
        bank_name TEXT NOT NULL, account_ciphertext TEXT NOT NULL, account_last4 TEXT NOT NULL,
        ifsc TEXT NOT NULL, updated_by TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS customer_payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_id INTEGER NOT NULL REFERENCES sales_invoices(id),
        payment_date TEXT NOT NULL, amount REAL NOT NULL CHECK(amount > 0),
        reference TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
        created_by TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS itc_statement_rows (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_period TEXT NOT NULL, supplier_gstin TEXT NOT NULL,
        invoice_number TEXT NOT NULL, invoice_date TEXT,
        taxable_value REAL NOT NULL DEFAULT 0,
        igst_amount REAL NOT NULL DEFAULT 0, cgst_amount REAL NOT NULL DEFAULT 0,
        sgst_amount REAL NOT NULL DEFAULT 0, total_tax REAL NOT NULL DEFAULT 0,
        eligibility TEXT NOT NULL DEFAULT 'Unknown', reconciliation_status TEXT NOT NULL DEFAULT 'Unmatched',
        notes TEXT NOT NULL DEFAULT '', imported_by TEXT NOT NULL, imported_at TEXT NOT NULL,
        UNIQUE(source_period, supplier_gstin, invoice_number)
    );
    CREATE TABLE IF NOT EXISTS company_profile (
        key TEXT PRIMARY KEY, value TEXT NOT NULL
    );
    """)

    def _ensure_column(table, column, declaration):
        existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    _ensure_column("vendor_invoices", "due_date", "TEXT")
    _ensure_column("sales_invoice_lines", "warehouse_id", "INTEGER REFERENCES warehouses(id)")
    _ensure_column("vendor_invoices", "paid_amount", "REAL NOT NULL DEFAULT 0")
    _ensure_column("vendor_invoices", "igst_amount", "REAL NOT NULL DEFAULT 0")
    _ensure_column("vendor_invoices", "cgst_amount", "REAL NOT NULL DEFAULT 0")
    _ensure_column("vendor_invoices", "sgst_amount", "REAL NOT NULL DEFAULT 0")
    _ensure_column("audit_log", "previous_hash", "TEXT")
    _ensure_column("audit_log", "event_hash", "TEXT")
    # Populate configuration and sample distributors once; use synthetic test records only.
    db.executemany("INSERT OR IGNORE INTO company_profile(key,value) VALUES(?,?)", [
        ("company_name", os.environ.get("COMPANY_NAME", "Northstar Components Pvt Ltd")),
        ("company_gstin", os.environ.get("COMPANY_GSTIN", "29AABCN0000A1Z5")),
        ("company_state", os.environ.get("COMPANY_STATE", "Karnataka")),
        ("company_address", os.environ.get("COMPANY_ADDRESS", "Industrial Area, Mysuru")),
        ("company_pincode", os.environ.get("COMPANY_PINCODE", "570016")),
        ("company_phone", os.environ.get("COMPANY_PHONE", "")),
        ("company_email", os.environ.get("COMPANY_EMAIL", "")),
    ])
    # Backfill tax-head splits on legacy invoices, whose older schema stored GST as one total only.
    company_state_row = db.execute("SELECT value FROM company_profile WHERE key='company_state'").fetchone()
    company_state_for_backfill = os.environ.get("COMPANY_STATE", company_state_row["value"] if company_state_row else "Karnataka").strip().lower()
    db.execute("""UPDATE vendor_invoices
                  SET igst_amount=CASE WHEN lower(COALESCE((SELECT state FROM vendors WHERE id=vendor_invoices.vendor_id),''))=lower(?) THEN 0 ELSE gst_amount END,
                      cgst_amount=CASE WHEN lower(COALESCE((SELECT state FROM vendors WHERE id=vendor_invoices.vendor_id),''))=lower(?) THEN ROUND(gst_amount/2.0,2) ELSE 0 END,
                      sgst_amount=CASE WHEN lower(COALESCE((SELECT state FROM vendors WHERE id=vendor_invoices.vendor_id),''))=lower(?) THEN ROUND(gst_amount-ROUND(gst_amount/2.0,2),2) ELSE 0 END
                  WHERE COALESCE(igst_amount,0)=0 AND COALESCE(cgst_amount,0)=0 AND COALESCE(sgst_amount,0)=0 AND COALESCE(gst_amount,0)>0""",
               (company_state_for_backfill, company_state_for_backfill, company_state_for_backfill))
    if db.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0:
        customer_seed = [
            ("CUS-001", "Karnataka Distribution House", "29AABCK1001A1Z5", "Karnataka", "Bengaluru", "560001", "ap@kdh.example", "080-4001001", 30),
            ("CUS-002", "Western Electrical Distributors", "27AABCW2002B1Z6", "Maharashtra", "Pune", "411001", "finance@wed.example", "020-4002002", 30),
            ("CUS-003", "Tamil Nadu Power Supplies", "33AABCT3003C1Z7", "Tamil Nadu", "Chennai", "600001", "accounts@tnps.example", "044-4003003", 45),
            ("CUS-004", "Telangana Industrial Traders", "36AABCT4004D1Z8", "Telangana", "Hyderabad", "500001", "ap@tit.example", "040-4004004", 30),
            ("CUS-005", "Gujarat Components Network", "24AABCG5005E1Z9", "Gujarat", "Ahmedabad", "380001", "accounts@gcn.example", "079-4005005", 30),
        ]
        db.executemany("INSERT INTO customers(code,name,gstin,state,address,pincode,email,phone,payment_terms,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       [(*c, datetime.now().isoformat(timespec="seconds")) for c in customer_seed])
    # One-time migration for genuinely legacy audit logs that predate hash columns.
    # Never silently rebuild a partially populated chain: that condition must remain detectable.
    audit_rows = db.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
    hash_migration = db.execute("SELECT value FROM app_state WHERE key='audit_hash_migration_v1'").fetchone()
    if audit_rows and not hash_migration and not any(r["event_hash"] or r["previous_hash"] for r in audit_rows):
        previous = "GENESIS"
        for row in audit_rows:
            digest = _audit_digest(previous, row["id"], row["actor"], row["action"], row["entity_type"], row["entity_id"], row["details"] or "", row["created_at"])
            db.execute("UPDATE audit_log SET previous_hash=?,event_hash=? WHERE id=?", (previous, digest, row["id"]))
            previous = digest
    db.execute("INSERT OR IGNORE INTO app_state(key,value) VALUES('audit_hash_migration_v1','1')")
    db.commit()
    # A predictable local-demo admin makes first launch straightforward. Change it immediately.
    if not db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        username = os.environ.get("ADMIN_USERNAME", "admin")
        password = os.environ.get("ADMIN_PASSWORD", "Forge@2026!")
        db.execute("INSERT INTO users(username,password_hash,full_name,role,created_at) VALUES(?,?,?,?,?)",
                   (username, generate_password_hash(password, method="pbkdf2:sha256"), "UNIWORK Administrator", "admin", datetime.now().isoformat(timespec="seconds")))
        db.commit()

    # If this database was first initialized as a local demo and later deployed,
    # rotate any administrator still using the known demo password exactly once
    # per worker. A custom password is never overwritten by this migration.
    if IS_PRODUCTION and not _PRODUCTION_DEMO_CREDENTIAL_ROTATION_CHECKED:
        demo_admins = db.execute("SELECT id,username,password_hash FROM users WHERE role='admin'").fetchall()
        rotated_at = datetime.now().isoformat(timespec="seconds")
        for admin_row in demo_admins:
            try:
                still_using_demo_password = check_password_hash(admin_row["password_hash"], "Forge@2026!")
            except (ValueError, TypeError):
                still_using_demo_password = False
            if still_using_demo_password:
                db.execute("UPDATE users SET password_hash=? WHERE id=?",
                           (generate_password_hash(_production_admin_password, method="pbkdf2:sha256"), admin_row["id"]))
                audit("system", "ROTATE_DEMO_ADMIN_PASSWORD", "user", admin_row["id"],
                      f"Rotated the known demo password for administrator {admin_row['username']} from production environment configuration")
        db.commit()
        _PRODUCTION_DEMO_CREDENTIAL_ROTATION_CHECKED = True


def _audit_digest(previous_hash, event_id, actor, action, entity_type, entity_id, details, created_at):
    payload = [previous_hash, event_id, actor, action, entity_type, entity_id, details or "", created_at]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def audit(actor, action, entity_type, entity_id=None, details=""):
    db = get_db()
    created_at = datetime.now().isoformat(timespec="seconds")
    previous = query_one("SELECT id,event_hash FROM audit_log ORDER BY id DESC LIMIT 1")
    previous_hash = previous["event_hash"] if previous and previous["event_hash"] else "GENESIS"
    cur = db.execute(
        "INSERT INTO audit_log(actor,action,entity_type,entity_id,details,created_at,previous_hash,event_hash) VALUES(?,?,?,?,?,?,?,?)",
        (actor, action, entity_type, entity_id, details, created_at, previous_hash, "PENDING")
    )
    event_hash = _audit_digest(previous_hash, cur.lastrowid, actor, action, entity_type, entity_id, details, created_at)
    db.execute("UPDATE audit_log SET event_hash=? WHERE id=?", (event_hash, cur.lastrowid))
    db.commit()


def seed_db():
    """Seed a fresh database with explicitly synthetic, six-month workflow data.

    Real challenge datasets can be loaded separately; this demo seed is not ground truth.
    Existing databases are never overwritten by startup seeding.
    """
    db = get_db()
    disabled = db.execute("SELECT value FROM app_state WHERE key='demo_seed_disabled'").fetchone()
    if disabled and disabled["value"] == "1":
        return
    if db.execute("SELECT COUNT(*) FROM vendors").fetchone()[0]:
        return

    now = datetime.now().isoformat(timespec="seconds")
    vendors = [
        ("VND-001", "Mysuru Copper & Conductors", "29AABCM1001A1Z5", "Karnataka", "orders@mysurucopper.example", "0821-4001001", 30),
        ("VND-002", "Bharat Switchgear Works", "27AABCB2002B1Z6", "Maharashtra", "sales@bharatswitch.example", "022-4002002", 45),
        ("VND-003", "Southline Polymers", "33AABCS3003C1Z7", "Tamil Nadu", "billing@southline.example", "044-4003003", 30),
        ("VND-004", "Deccan Electrical Metals", "36AABCD4004D1Z8", "Telangana", "ap@deccanmetals.example", "040-4004004", 15),
        ("VND-005", "Precision Enclosures India", "29AABCP5005E1Z9", "Karnataka", "accounts@precision.example", "080-4005005", 30),
        ("VND-006", "Western Relay & Controls", "24AABCW6006F1Z0", "Gujarat", "finance@westernrelay.example", "079-4006006", 60),
    ]
    state_master = [("29", "Karnataka"), ("27", "Maharashtra"), ("33", "Tamil Nadu"), ("36", "Telangana"), ("24", "Gujarat")]
    for i in range(7, 81):
        state_code, state_name = state_master[(i - 7) % len(state_master)]
        # Format-valid synthetic GSTIN-like identifiers; these are not real registrations.
        gstin = f"{state_code}AABCS{i:04d}A1Z{i % 10}"
        vendors.append((f"VND-{i:03d}", f"Synthetic Electrical Supplier {i:03d}", gstin, state_name,
                        f"ap{i:03d}@supplier.example", f"080-40{i:04d}", [15, 30, 45, 60][i % 4]))
    db.executemany("INSERT INTO vendors(code,name,gstin,state,email,phone,payment_terms,created_at) VALUES(?,?,?,?,?,?,?,?)",
                   [(*v, now) for v in vendors])

    products = [
        ("SKU-EL-001", "Copper winding wire 1.2mm", "Conductors", "8544", 18, "kg", 45, 620),
        ("SKU-EL-002", "Miniature circuit breaker 16A", "Switchgear", "8536", 18, "pcs", 80, 145),
        ("SKU-EL-003", "Terminal block 12-way", "Connectors", "8536", 18, "pcs", 55, 78),
        ("SKU-EL-004", "ABS junction enclosure", "Enclosures", "3926", 18, "pcs", 35, 92),
        ("SKU-EL-005", "Control transformer 230/24V", "Transformers", "8504", 18, "pcs", 12, 890),
        ("SKU-EL-006", "DIN rail 1 metre", "Mounting", "8538", 18, "pcs", 40, 64),
        ("SKU-EL-007", "PVC insulated cable 2.5sqmm", "Conductors", "8544", 18, "m", 120, 28),
        ("SKU-EL-008", "Weatherproof enclosure IP65", "Enclosures", "3926", 18, "pcs", 18, 410),
        ("SKU-EL-009", "Industrial relay 24V DC", "Switchgear", "8536", 18, "pcs", 24, 235),
        ("SKU-EL-010", "Panel mounting plate", "Mounting", "8538", 18, "pcs", 20, 185),
        ("SKU-EL-011", "Copper busbar 20x3mm", "Conductors", "7407", 18, "kg", 15, 760),
        ("SKU-EL-012", "Cable gland M20", "Connectors", "8536", 18, "pcs", 60, 24),
    ]
    db.executemany("INSERT INTO products(sku,name,category,hsn,gst_rate,unit,reorder_level,unit_cost) VALUES(?,?,?,?,?,?,?,?)", products)
    db.executemany("INSERT INTO warehouses(code,name,city) VALUES(?,?,?)",
                   [("WH-MYS", "Mysuru Main Store", "Mysuru"), ("WH-BLR", "Bengaluru Dispatch Store", "Bengaluru")])

    rng = __import__("random").Random(31)
    product_rows = db.execute("SELECT * FROM products ORDER BY id").fetchall()
    product_ids = [row["id"] for row in product_rows]
    warehouse_rows = db.execute("SELECT * FROM warehouses ORDER BY id").fetchall()
    for product in product_rows:
        for warehouse in warehouse_rows:
            opening = rng.randint(max(2, product["reorder_level"] // 2), max(3, product["reorder_level"] * 3))
            db.execute("INSERT INTO stock_movements(product_id,warehouse_id,movement_type,quantity,reference,movement_date,created_at) VALUES(?,?,?, ?,?,?,?)",
                       (product["id"], warehouse["id"], "OPENING", opening, "OPENING-BALANCE", (date.today()-timedelta(days=190)).isoformat(), now))

    vendor_rows = db.execute("SELECT id,code,state,payment_terms FROM vendors ORDER BY id").fetchall()
    seeded_invoice_ids = []
    for i in range(1, 91):
        vendor = vendor_rows[(i - 1) % len(vendor_rows)]
        day_offset = 180 - round((i - 1) * 178 / 89)
        order_date = date.today() - timedelta(days=day_offset)
        expected_date = order_date + timedelta(days=7 + (i % 6))
        status = ["Received", "Received", "Partially Received", "Ordered"][i % 4]
        po_number = f"PO-2026-{i:04d}"
        db.execute("INSERT INTO purchase_orders(po_number,vendor_id,order_date,expected_date,status,notes,created_at) VALUES(?,?,?,?,?,?,?)",
                   (po_number, vendor["id"], order_date.isoformat(), expected_date.isoformat(), status,
                    "Synthetic six-month evaluation scenario" if i % 10 == 0 else "", now))
        po_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        lines = []
        # Most orders have two SKUs; line data is deliberately deterministic and reproducible.
        for line_index in range(2):
            product = product_rows[(i * 3 + line_index * 5) % len(product_rows)]
            quantity = 18 + ((i * 7 + line_index * 11) % 56)
            price = money(float(product["unit_cost"]) * (1.04 if (i + line_index) % 19 == 0 else 1.0))
            db.execute("INSERT INTO purchase_order_lines(po_id,product_id,quantity,unit_price) VALUES(?,?,?,?)",
                       (po_id, product["id"], quantity, price))
            po_line_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            rec_qty = quantity if status == "Received" else (max(1, quantity * 2 // 3) if status == "Partially Received" else 0)
            lines.append({"po_line_id": po_line_id, "product_id": product["id"], "sku": product["sku"],
                          "quantity": quantity, "price": price, "gst_rate": float(product["gst_rate"]),
                          "rec_qty": rec_qty, "product": product})
        if status in {"Received", "Partially Received"}:
            warehouse = warehouse_rows[i % len(warehouse_rows)]
            grn_number = f"GRN-2026-{i:04d}"
            received_date = order_date + timedelta(days=min(5, max(1, (expected_date-order_date).days-1)))
            db.execute("INSERT INTO goods_receipts(grn_number,po_id,warehouse_id,received_date,reference,created_at) VALUES(?,?,?,?,?,?)",
                       (grn_number, po_id, warehouse["id"], received_date.isoformat(), f"Challan-{i:04d}", now))
            grn_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            for line in lines:
                db.execute("INSERT INTO goods_receipt_lines(grn_id,product_id,quantity) VALUES(?,?,?)",
                           (grn_id, line["product_id"], line["rec_qty"]))
                db.execute("INSERT INTO stock_movements(product_id,warehouse_id,movement_type,quantity,reference,movement_date,created_at) VALUES(?,?, 'IN',?,?,?,?)",
                           (line["product_id"], warehouse["id"], line["rec_qty"], grn_number, received_date.isoformat(), now))

        if i % 3 == 0:
            invoice_date = order_date + timedelta(days=5)
            invoice_lines = []
            for line_index, line in enumerate(lines):
                received = line["rec_qty"]
                qty = received if received > 0 else max(1, line["quantity"])
                if i % 23 == 0 and line_index == 0 and received < line["quantity"]:
                    qty = min(line["quantity"], received + max(1, line["quantity"] // 5))
                unit_price = money(line["price"] * (1.05 if i % 19 == 0 and line_index == 0 else 1.0))
                rate = 12.0 if i % 17 == 0 and line_index == 0 and line["gst_rate"] != 12 else line["gst_rate"]
                taxable_value = money(qty * unit_price)
                tax_amount = money(taxable_value * rate / 100.0)
                invoice_lines.append((line["po_line_id"], line["product_id"], qty, unit_price, taxable_value, rate, tax_amount))
            header_taxable = money(sum(v[4] for v in invoice_lines))
            header_gst = money(sum(v[6] for v in invoice_lines))
            due_date = (invoice_date + timedelta(days=int(vendor["payment_terms"] or 30))).isoformat()
            invoice_number = f"INV-2026-{i:04d}"
            intrastate = vendor["state"].lower() == os.environ.get("COMPANY_STATE", "Karnataka").lower()
            igst = 0.0 if intrastate else header_gst
            cgst = money(header_gst/2) if intrastate else 0.0
            sgst = money(header_gst-cgst) if intrastate else 0.0
            db.execute("""INSERT INTO vendor_invoices(invoice_number,vendor_id,po_id,invoice_date,due_date,taxable_value,gst_amount,total_amount,paid_amount,status,itc_status,notes,created_at,igst_amount,cgst_amount,sgst_amount)
                        VALUES(?,?,?,?,?,?,?,?,0,'Pending','Review',?,?, ?,?,?)""",
                       (invoice_number, vendor["id"], po_id, invoice_date.isoformat(), due_date, header_taxable, header_gst,
                        money(header_taxable+header_gst), "Synthetic invoice; verify matching result", now, igst, cgst, sgst))
            invoice_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            db.executemany("INSERT INTO vendor_invoice_lines(invoice_id,po_line_id,product_id,quantity,unit_price,taxable_value,gst_rate,gst_amount) VALUES(?,?,?,?,?,?,?,?)",
                           [(invoice_id, *line) for line in invoice_lines])
            invoice = query_one("SELECT * FROM vendor_invoices WHERE id=?", (invoice_id,))
            checks = invoice_checks(invoice)
            status_value = "Approved" if checks["match"] else "Pending"
            itc_status = "Eligible" if checks["match"] else "Review"
            db.execute("UPDATE vendor_invoices SET status=?,itc_status=?,notes=? WHERE id=?",
                       (status_value, itc_status, "Synthetic test invoice" if checks["match"] else "Synthetic exception: " + "; ".join(checks["issues"][:3]), invoice_id))
            seeded_invoice_ids.append(invoice_id)

    # Outward invoices across five distributor states support AR and GST simulation demonstrations.
    customers = db.execute("SELECT * FROM customers ORDER BY id").fetchall()
    for i in range(1, 16):
        customer = customers[(i - 1) % len(customers)]
        product = product_rows[(i * 2) % len(product_rows)]
        invoice_date = date.today() - timedelta(days=150 - (i - 1) * 9)
        quantity = 2 + (i % 9)
        unit_price = money(float(product["unit_cost"]) * 1.35)
        taxable = money(quantity * unit_price)
        rate = float(product["gst_rate"])
        tax = money(taxable * rate / 100.0)
        intrastate = customer["state"].strip().lower() == os.environ.get("COMPANY_STATE", "Karnataka").strip().lower()
        cgst = money(tax / 2) if intrastate else 0.0
        sgst = money(tax-cgst) if intrastate else 0.0
        igst = 0.0 if intrastate else tax
        total = money(taxable + tax)
        due_date = (invoice_date + timedelta(days=int(customer["payment_terms"] or 30))).isoformat()
        invoice_number = f"UNI-SI-2026-{i:04d}"
        paid = total if i % 5 == 0 else money(total * 0.25) if i % 4 == 0 else 0.0
        status_value = "Paid" if paid + 0.005 >= total else "Partially paid" if paid > 0 else "Open"
        db.execute("""INSERT INTO sales_invoices(invoice_number,customer_id,invoice_date,due_date,taxable_value,igst_amount,cgst_amount,sgst_amount,gst_amount,total_amount,paid_amount,status,notes,created_by,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (invoice_number, customer["id"], invoice_date.isoformat(), due_date, taxable, igst, cgst, sgst, tax, total, paid,
                    status_value, "Synthetic distributor invoice", "system", now))
        sales_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        # Outward sales reduce the physical warehouse ledger as well as creating the AR invoice.
        candidate_balances = []
        for wh in warehouse_rows:
            wh_balance = db.execute("""SELECT COALESCE(SUM(CASE WHEN movement_type IN ('OPENING','IN','ADJUST_IN','TRANSFER_IN') THEN quantity
                                       WHEN movement_type IN ('OUT','ADJUST_OUT','TRANSFER_OUT','CONSUMPTION') THEN -quantity ELSE 0 END),0)
                                      FROM stock_movements WHERE product_id=? AND warehouse_id=?""",
                                    (product["id"], wh["id"])).fetchone()[0]
            candidate_balances.append((int(wh_balance), wh))
        candidate_balances.sort(key=lambda item: item[0], reverse=True)
        available, selected_warehouse = candidate_balances[0]
        if available < 1:
            # Keep the demo data honest rather than creating a negative stock balance.
            db.execute("DELETE FROM sales_invoices WHERE id=?", (sales_id,))
            continue
        if available < quantity:
            # Keep synthetic demo ledgers internally consistent if a seed quantity is customized.
            quantity = available
            taxable = money(quantity * unit_price)
            tax = money(taxable * rate / 100.0)
            cgst = money(tax / 2) if intrastate else 0.0
            sgst = money(tax-cgst) if intrastate else 0.0
            igst = 0.0 if intrastate else tax
            total = money(taxable + tax)
            paid = min(paid, total)
            status_value = "Paid" if paid + 0.005 >= total else "Partially paid" if paid > 0 else "Open"
            db.execute("UPDATE sales_invoices SET taxable_value=?,igst_amount=?,cgst_amount=?,sgst_amount=?,gst_amount=?,total_amount=?,paid_amount=?,status=? WHERE id=?",
                       (taxable, igst, cgst, sgst, tax, total, paid, status_value, sales_id))
        db.execute("""INSERT INTO sales_invoice_lines(invoice_id,product_id,warehouse_id,product_sku,product_name,hsn,unit,quantity,unit_price,taxable_value,gst_rate,igst_amount,cgst_amount,sgst_amount,gst_amount,total_amount)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (sales_id, product["id"], selected_warehouse["id"], product["sku"], product["name"], product["hsn"], product["unit"], quantity,
                    unit_price, taxable, rate, igst, cgst, sgst, tax, total))
        db.execute("INSERT INTO stock_movements(product_id,warehouse_id,movement_type,quantity,reference,movement_date,created_at) VALUES(?,?,'OUT',?,?,?,?)",
                   (product["id"], selected_warehouse["id"], quantity, f"SHIP-{invoice_number}", invoice_date.isoformat(), now))
        # Synthetic transport detail allows an EWB-shaped sample payload without a live portal call.
        db.execute("""INSERT INTO sales_invoice_transport(invoice_id,trans_mode,trans_distance,vehicle_no,vehicle_type,transporter_id,transporter_name,transport_doc_no,transport_doc_date,updated_by,updated_at)
                    VALUES(?,'1',?,?, 'R','','Synthetic demo transporter','','',?,?)""",
                   (sales_id, str(40 + (i * 23) % 600), f"KA09AB{i:04d}", "system", now))
        if paid > 0:
            db.execute("INSERT INTO customer_payments(invoice_id,payment_date,amount,reference,notes,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                       (sales_id, (invoice_date + timedelta(days=10)).isoformat(), paid, f"DEMO-UTR-{i:04d}", "Synthetic opening settlement", "system", now))

    # Seed a small synthetic statement slice with matches, value differences and one not-available flag.
    booked = query_all("""SELECT vi.*,v.gstin supplier_gstin,v.state supplier_state FROM vendor_invoices vi JOIN vendors v ON v.id=vi.vendor_id ORDER BY vi.id LIMIT 12""")
    for idx, invoice in enumerate(booked, start=1):
        source_period = invoice["invoice_date"][:7]
        taxable = float(invoice["taxable_value"])
        igst, cgst, sgst = float(invoice["igst_amount"] or 0), float(invoice["cgst_amount"] or 0), float(invoice["sgst_amount"] or 0)
        eligibility = "No" if idx % 7 == 0 else "Yes"
        if idx % 5 == 0:
            taxable = money(taxable + 25)
        db.execute("""INSERT OR IGNORE INTO itc_statement_rows(source_period,supplier_gstin,invoice_number,invoice_date,taxable_value,igst_amount,cgst_amount,sgst_amount,total_tax,eligibility,reconciliation_status,notes,imported_by,imported_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,'Unmatched','',?,?)""",
                   (source_period, invoice["supplier_gstin"], invoice["invoice_number"], invoice["invoice_date"], taxable,
                    igst, cgst, sgst, money(igst+cgst+sgst), eligibility, "system", now))
        row = query_one("SELECT id FROM itc_statement_rows WHERE source_period=? AND supplier_gstin=? AND invoice_number=?",
                        (source_period, invoice["supplier_gstin"], invoice["invoice_number"]))
        if row:
            _reconcile_itc_row(row["id"])

    db.commit()
    audit("system", "DEMO_SEED", "database", None,
          "Seeded 80 synthetic vendors, 12 SKUs, two godowns, 90 purchase orders across six months, receipts, invoice exceptions, outward invoices and ITC statement sample rows")

def query_all(sql, params=()):
    return get_db().execute(sql, params).fetchall()


def query_one(sql, params=()):
    return get_db().execute(sql, params).fetchone()


def inventory_rows():
    return query_all("""
      SELECT p.id,p.sku,p.name,p.category,p.hsn,p.gst_rate,p.unit,p.reorder_level,p.unit_cost,
        COALESCE(SUM(CASE WHEN sm.movement_type IN ('OPENING','IN','ADJUST_IN','TRANSFER_IN') THEN sm.quantity
                          WHEN sm.movement_type IN ('OUT','ADJUST_OUT','TRANSFER_OUT','CONSUMPTION') THEN -sm.quantity ELSE 0 END),0) AS stock
      FROM products p LEFT JOIN stock_movements sm ON sm.product_id=p.id
      GROUP BY p.id ORDER BY p.name
    """)


def warehouse_stock_rows():
    """Return a ledger-derived SKU balance per physical warehouse."""
    return query_all("""SELECT p.id,p.sku,p.name,p.name AS product_name,p.unit,p.reorder_level,p.unit_cost,
        w.id AS warehouse_id,w.code AS warehouse_code,w.name AS warehouse_name,
        COALESCE(SUM(CASE WHEN sm.movement_type IN ('OPENING','IN','ADJUST_IN','TRANSFER_IN') THEN sm.quantity
                          WHEN sm.movement_type IN ('OUT','ADJUST_OUT','TRANSFER_OUT','CONSUMPTION') THEN -sm.quantity ELSE 0 END),0) AS stock
        FROM products p CROSS JOIN warehouses w
        LEFT JOIN stock_movements sm ON sm.product_id=p.id AND sm.warehouse_id=w.id
        GROUP BY p.id,w.id ORDER BY p.name,w.name""")


def po_totals(po_id):
    """Return taxable value and GST calculated from each product's saved GST rate."""
    row = query_one("""SELECT COALESCE(SUM(pol.quantity*pol.unit_price),0) AS taxable,
                              COALESCE(SUM(pol.quantity*pol.unit_price*p.gst_rate/100.0),0) AS gst
                       FROM purchase_order_lines pol
                       JOIN products p ON p.id=pol.product_id
                       WHERE pol.po_id=?""", (po_id,))
    return money(row["taxable"]), money(row["gst"])


def invoice_checks(inv):
    """Explain purchase-order, receipt, value and GST exceptions for one supplier invoice."""
    po = query_one(
        "SELECT po.id,po.po_number,po.status,po.vendor_id,v.gstin,v.state "
        "FROM purchase_orders po JOIN vendors v ON v.id=po.vendor_id WHERE po.id=?",
        (inv["po_id"],),
    ) if inv["po_id"] else None
    issues = []
    expected_gst = 0.0
    expected_taxable = 0.0

    if not po:
        issues.append("No linked purchase order")
    else:
        if "vendor_id" in inv.keys() and int(inv["vendor_id"]) != int(po["vendor_id"]):
            issues.append("Invoice vendor does not match the linked purchase order vendor")
        po_taxable, po_gst = po_totals(po["id"])
        expected_taxable, expected_gst = po_taxable, po_gst
        line_rows = query_all("""SELECT vil.*,p.name product_name,p.sku,p.gst_rate AS current_gst_rate,
                      pol.quantity AS po_quantity,pol.unit_price AS po_unit_price,
                      COALESCE((SELECT SUM(grl.quantity) FROM goods_receipts gr JOIN goods_receipt_lines grl ON grl.grn_id=gr.id
                                WHERE gr.po_id=? AND grl.product_id=vil.product_id),0) AS received_quantity
                      FROM vendor_invoice_lines vil JOIN products p ON p.id=vil.product_id
                      LEFT JOIN purchase_order_lines pol ON pol.po_id=? AND pol.product_id=vil.product_id
                      WHERE vil.invoice_id=? ORDER BY vil.id""", (po["id"], po["id"], inv["id"]))
        if line_rows:
            line_taxable = money(sum(float(x["taxable_value"]) for x in line_rows))
            line_gst = money(sum(float(x["gst_amount"]) for x in line_rows))
            expected_taxable, expected_gst = line_taxable, line_gst
            for line in line_rows:
                if line["po_quantity"] is None:
                    issues.append(f"{line['sku']} is not on the linked PO")
                else:
                    if abs(float(line["unit_price"]) - float(line["po_unit_price"])) > 1:
                        issues.append(f"{line['sku']} price variance: PO ₹{float(line['po_unit_price']):,.2f}, invoice ₹{float(line['unit_price']):,.2f}")
                    if int(line["quantity"]) > int(line["received_quantity"]):
                        issues.append(f"{line['sku']} quantity mismatch: invoiced {line['quantity']}, received {line['received_quantity']}")
                calculated_taxable = money(float(line["quantity"]) * float(line["unit_price"]))
                if abs(float(line["taxable_value"]) - calculated_taxable) > 0.05:
                    issues.append(f"{line['sku']} taxable amount does not equal quantity × unit price")
                if abs(float(line["gst_rate"]) - float(line["current_gst_rate"])) > 0.001:
                    issues.append(f"{line['sku']} GST rate differs from current product master")
                calculated_tax = money(float(line["taxable_value"]) * float(line["gst_rate"]) / 100.0)
                if abs(float(line["gst_amount"]) - calculated_tax) > 0.05:
                    issues.append(f"{line['sku']} GST variance: expected ₹{calculated_tax:,.2f}")
            if abs(float(inv["taxable_value"]) - line_taxable) > 0.05:
                issues.append(f"Invoice header taxable value differs from line total by ₹{abs(float(inv['taxable_value'])-line_taxable):,.2f}")
            if abs(float(inv["gst_amount"]) - line_gst) > 0.05:
                issues.append(f"Invoice header GST differs from line total by ₹{abs(float(inv['gst_amount'])-line_gst):,.2f}")
        else:
            if abs(float(inv["taxable_value"]) - po_taxable) > 1:
                issues.append(f"Taxable value variance ₹{abs(float(inv['taxable_value'])-po_taxable):,.2f}")
            if abs(float(inv["gst_amount"]) - po_gst) > 1:
                issues.append(f"GST mismatch: expected ₹{po_gst:,.2f} based on linked PO line rates")
            issues.append("Invoice line details not recorded; three-way match is incomplete")

    if abs(float(inv["total_amount"]) - (float(inv["taxable_value"]) + float(inv["gst_amount"]))) > 0.05:
        issues.append("Invoice total does not equal taxable value + GST")
    split_total = money(float(inv["igst_amount"] or 0) + float(inv["cgst_amount"] or 0) + float(inv["sgst_amount"] or 0))
    if abs(float(inv["gst_amount"]) - split_total) > 0.05:
        issues.append("IGST + CGST + SGST does not equal header GST")
    if po:
        same_state = (po["state"] or "").strip().lower() == os.environ.get("COMPANY_STATE", "Karnataka").strip().lower()
        if same_state and float(inv["igst_amount"] or 0) > 0.05:
            issues.append("Same-state supplier invoice has IGST rather than CGST + SGST")
        if not same_state and (float(inv["cgst_amount"] or 0) > 0.05 or float(inv["sgst_amount"] or 0) > 0.05):
            issues.append("Inter-state supplier invoice has CGST/SGST rather than IGST")
    return {"issues": list(dict.fromkeys(issues)), "match": not issues,
            "expected_gst": money(expected_gst), "expected_taxable": money(expected_taxable)}


@app.context_processor
def inject_shared_context():
    return {"now_date": date.today().isoformat()}


ROLE_LABELS = {"admin":"Administrator", "procurement":"Procurement", "storekeeper":"Storekeeper", "accounts":"Accounts & GST", "viewer":"Read-only viewer"}
ROLE_ACCESS = {
    "admin": {"*"},
    "procurement": {"dashboard","inventory","vendors","vendor_detail","create_vendor","edit_vendor","purchase_orders","purchase_order_detail","create_po","update_po_status","receipts","reports","export_csv","matching","profile"},
    "storekeeper": {"dashboard","inventory","receipts","create_receipt","adjust_inventory","consumption","create_consumption","transfers","create_transfer","profile"},
    "accounts": {"dashboard","vendors","vendor_detail","vendor_bank","invoices","create_invoice","review_invoice","approve_invoice_override","reports","export_csv","matching","gst_center","import_itc_statement","payables","record_vendor_payment","receivables","create_sales_invoice","record_customer_payment","save_transport_details","gst_export","profile","view_financials"},
    "viewer": {"dashboard","inventory","receipts","change_password","profile"},
}

@app.url_defaults
def preserve_tab_token(_endpoint, values):
    """Keep the tab-specific authentication token on same-tab navigation."""
    token = getattr(g, "tab_token", None)
    if token and _endpoint != "static" and "__tab" not in values:
        values["__tab"] = token


def session_expired(last_seen: str) -> bool:
    try:
        timestamp = datetime.fromisoformat(last_seen)
    except (TypeError, ValueError):
        return True
    return datetime.now() - timestamp > timedelta(hours=8)


@app.before_request
def bootstrap():
    # Static assets, liveness checks, and unknown URLs do not need DB initialization.
    if request.endpoint in {"static", "setup_status", "health"} or request.endpoint is None:
        return None

    init_db()
    seed_db()

    token = request.args.get("__tab") or request.form.get("__tab") or request.headers.get("X-UNIWORK-Tab")
    g.tab_token = token

    if request.endpoint == "login":
        if not token:
            fresh_token = secrets.token_urlsafe(32)
            return redirect(url_for("login", __tab=fresh_token, next=request.args.get("next", "")))
        if request.method == "GET":
            row = query_one("SELECT user_id,last_seen FROM tab_sessions WHERE tab_token=?", (token,))
            if row and not session_expired(row["last_seen"]):
                user = query_one("SELECT id,username,full_name,role,active FROM users WHERE id=?", (row["user_id"],))
                if user and user["active"]:
                    return redirect(url_for("dashboard", __tab=token))
        return None

    if not token:
        return redirect(url_for("login", next=request.path))

    row = query_one("SELECT user_id,last_seen FROM tab_sessions WHERE tab_token=?", (token,))
    if not row:
        return redirect(url_for("login", __tab=token, next=request.path))
    if session_expired(row["last_seen"]):
        get_db().execute("DELETE FROM tab_sessions WHERE tab_token=?", (token,))
        get_db().commit()
        return redirect(url_for("login", __tab=secrets.token_urlsafe(32), next=request.path))
    user = query_one("SELECT id,username,full_name,role,active FROM users WHERE id=?", (row["user_id"],))
    if not user or not user["active"]:
        get_db().execute("DELETE FROM tab_sessions WHERE tab_token=?", (token,))
        get_db().commit()
        return redirect(url_for("login", __tab=token))
    get_db().execute("UPDATE tab_sessions SET last_seen=? WHERE tab_token=?", (datetime.now().isoformat(timespec="seconds"), token))
    get_db().commit()
    g.user = user
    if request.method == "POST":
        submitted_csrf = request.form.get("csrf_token", "")
        if not hmac.compare_digest(submitted_csrf, csrf_token_for(token)):
            return render_template("error.html", title="Invalid request", code=400,
                                   message="Your form expired or the security token was invalid. Refresh and try again."), 400
    allowed = ROLE_ACCESS.get(user["role"], set())
    if "*" not in allowed and request.endpoint not in allowed and request.endpoint not in {"logout", "forbidden", "change_password"}:
        if request.method == "POST":
            flash("Your role does not have permission to perform that action.", "error")
        return render_template("error.html", title="Access denied", code=403,
                               message="Access denied — your role does not have permission for this page."), 403


@app.after_request
def prevent_private_page_caching(response):
    # Authentication pages and private business data should not be kept in the
    # browser's HTTP cache; authorization is still checked on every request.
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response

@app.context_processor
def inject_user_context():
    user = getattr(g, "user", None)

    def can_access(endpoint):
        if not user:
            return False
        allowed = ROLE_ACCESS.get(user["role"], set())
        return "*" in allowed or endpoint in allowed

    return {
        "current_user": user,
        "role_labels": ROLE_LABELS,
        "is_admin": bool(user and user["role"] == "admin"),
        "can_access": can_access,
        "csrf_token_value": csrf_token_for(getattr(g, "tab_token", None) or request.args.get("__tab") or request.form.get("__tab")),
        "show_demo_credentials": (not IS_PRODUCTION
                                  and os.environ.get("SHOW_DEMO_CREDENTIALS", "1") == "1"),
    }

@app.route("/login", methods=["GET", "POST"])
def login():
    token = request.args.get("__tab") or request.form.get("__tab")
    if not token:
        token = secrets.token_urlsafe(32)
        return redirect(url_for("login", __tab=token, next=request.args.get("next", "")))
    if request.method == "POST":
        submitted_csrf = request.form.get("csrf_token", "")
        if not hmac.compare_digest(submitted_csrf, csrf_token_for(token)):
            flash("Your sign-in form expired or was invalid. Refresh the page and try again.", "error")
            return render_template("login.html", title="Sign in · UNIWORK", tab_token=token), 400
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = query_one("SELECT * FROM users WHERE username=? AND active=1", (username,))
        if user and check_password_hash(user["password_hash"], password):
            now = datetime.now().isoformat(timespec="seconds")
            get_db().execute(
                "INSERT INTO tab_sessions(tab_token,user_id,boot_id,created_at,last_seen) VALUES(?,?,?,?,?) "
                "ON CONFLICT(tab_token) DO UPDATE SET user_id=excluded.user_id,boot_id=excluded.boot_id,last_seen=excluded.last_seen",
                (token, user["id"], BOOT_ID, now, now)
            )
            get_db().commit()
            audit(user["username"], "LOGIN", "user", user["id"], "Successful login (tab-specific session)")
            target = request.args.get("next", "")
            if not target.startswith("/") or target.startswith("//"):
                target = url_for("dashboard")
            from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
            parts = urlsplit(target)
            query = dict(parse_qsl(parts.query, keep_blank_values=True))
            query["__tab"] = token
            target = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
            return redirect(target)
        flash("Invalid username or password, or the account is disabled.", "error")
    return render_template("login.html", title="Sign in · UNIWORK", tab_token=token)

@app.route("/logout", methods=["POST"])
def logout():
    user = getattr(g, "user", None)
    token = request.args.get("__tab") or request.form.get("__tab") or request.headers.get("X-UNIWORK-Tab")
    if user:
        audit(user["username"], "LOGOUT", "user", user["id"], "Signed out of this tab")
    if token:
        get_db().execute("DELETE FROM tab_sessions WHERE tab_token=?", (token,))
        get_db().commit()
    flash("You have been signed out of this tab.", "success")
    return redirect(url_for("login", __tab=secrets.token_urlsafe(32)))

@app.route("/account/password", methods=["GET", "POST"])
def change_password():
    if request.method == "POST":
        current = query_one("SELECT password_hash FROM users WHERE id=?", (g.user["id"],))
        new_password = request.form.get("new_password", "")
        if not current or not check_password_hash(current["password_hash"], request.form.get("current_password", "")):
            flash("Current password is incorrect.", "error")
        elif len(new_password) < 10:
            flash("New password must be at least 10 characters.", "error")
        elif new_password != request.form.get("confirm_password", ""):
            flash("New password and confirmation do not match.", "error")
        else:
            get_db().execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new_password, method="pbkdf2:sha256"), g.user["id"]))
            get_db().commit()
            audit(g.user["username"], "CHANGE_PASSWORD", "user", g.user["id"], "Password changed")
            flash("Password changed successfully.", "success")
            return redirect(url_for("dashboard"))
    return render_template("change_password.html", title="Change password")

@app.route("/admin/users", methods=["GET", "POST"])
def user_management():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        full_name = request.form.get("full_name", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "")
        if len(username) < 3 or len(full_name) < 2 or len(password) < 10 or role not in ROLE_LABELS:
            flash("Use a username of 3+ characters, full name, password of 10+ characters, and valid role.", "error")
        else:
            try:
                get_db().execute("INSERT INTO users(username,password_hash,full_name,role,created_at) VALUES(?,?,?,?,?)",
                    (username, generate_password_hash(password, method="pbkdf2:sha256"), full_name, role, datetime.now().isoformat(timespec="seconds")))
                get_db().commit()
                audit(g.user["username"], "CREATE_USER", "user", None, f"Created {username} with role {role}")
                flash(f"Account {username} created.", "success")
            except sqlite3.IntegrityError:
                flash("That username already exists.", "error")
        return redirect(url_for("user_management"))
    return render_template("users.html", title="User & role management", users=query_all("SELECT id,username,full_name,role,active,created_at FROM users ORDER BY id"), roles=ROLE_LABELS)

@app.post("/admin/users/<int:user_id>/toggle")
def toggle_user(user_id):
    if user_id == g.user["id"]:
        flash("You cannot disable your own account.", "error")
    else:
        user = query_one("SELECT username,active FROM users WHERE id=?", (user_id,))
        if user:
            get_db().execute("UPDATE users SET active=? WHERE id=?", (0 if user["active"] else 1, user_id))
            get_db().commit()
            audit(g.user["username"], "TOGGLE_USER", "user", user_id, f"{user['username']} active={not user['active']}")
            flash("User access updated.", "success")
    return redirect(url_for("user_management"))

@app.route("/admin/data", methods=["GET", "POST"])
def data_management():
    if request.method == "POST":
        action = request.form.get("action")
        confirm = request.form.get("confirm", "").strip()
        db = get_db()
        transaction_tables = ("sales_invoice_transport", "sales_invoice_lines", "customer_payments", "sales_invoices",
                              "vendor_payments", "vendor_invoice_lines", "vendor_invoices", "itc_statement_rows",
                              "goods_receipt_lines", "goods_receipts", "material_consumptions", "stock_transfers",
                              "stock_movements", "purchase_order_lines", "purchase_orders")
        if action == "clear_transactions" and confirm == "CLEAR TRANSACTIONS":
            for table in transaction_tables:
                db.execute(f"DELETE FROM {table}")
            db.execute("DELETE FROM audit_log")
            db.commit()
            audit(g.user["username"], "CLEAR_TRANSACTIONS", "database", None, "Cleared transaction records; master data and accounts preserved")
            flash("Transaction data cleared. Vendors, customers, products, warehouses and user accounts were preserved.", "success")
        elif action == "reset_demo" and confirm == "RESET DEMO DATA":
            db.execute("INSERT INTO app_state(key,value) VALUES('demo_seed_disabled','0') ON CONFLICT(key) DO UPDATE SET value='0'")
            for table in transaction_tables:
                db.execute(f"DELETE FROM {table}")
            db.execute("DELETE FROM vendor_bank_details")
            db.execute("DELETE FROM audit_log")
            for table in ("vendors", "products", "warehouses"):
                db.execute(f"DELETE FROM {table}")
            db.commit()
            seed_db()
            audit(g.user["username"], "RESET_DEMO_DATA", "database", None, "Re-seeded synthetic demonstration data")
            flash("Synthetic demonstration data was reset. User accounts and customer master data were preserved.", "success")
        elif action == "delete_all_business_data" and confirm == "DELETE ALL BUSINESS DATA":
            db.execute("INSERT INTO app_state(key,value) VALUES('demo_seed_disabled','1') ON CONFLICT(key) DO UPDATE SET value='0'")
            for table in transaction_tables:
                db.execute(f"DELETE FROM {table}")
            db.execute("DELETE FROM vendor_bank_details")
            db.execute("DELETE FROM audit_log")
            for table in ("vendors", "products", "warehouses", "customers"):
                db.execute(f"DELETE FROM {table}")
            db.commit()
            audit(g.user["username"], "DELETE_ALL_BUSINESS_DATA", "database", None, "Deleted business/master/transaction data; accounts preserved")
            flash("Business/master data was deleted. User accounts and company configuration remain.", "success")
        else:
            flash("Confirmation text did not match. No data was deleted.", "error")
        return redirect(url_for("data_management"))
    counts = {table: query_one(f"SELECT COUNT(*) n FROM {table}")["n"] for table in ("users","vendors","customers","products","warehouses","purchase_orders","goods_receipts","vendor_invoices","sales_invoices","itc_statement_rows","stock_movements","audit_log")}
    return render_template("data_management.html", title="Data management", counts=counts)


@app.route("/")
def dashboard():
    inv = inventory_rows()
    warehouse_balances = warehouse_stock_rows()
    # Reorder alerts are evaluated separately for each godown, not only on combined stock.
    low = [dict(p) for p in warehouse_balances if int(p["stock"]) <= int(p["reorder_level"])]
    invoice_rows = query_all("""SELECT vi.*,v.name AS vendor_name,po.po_number FROM vendor_invoices vi
                                JOIN vendors v ON v.id=vi.vendor_id LEFT JOIN purchase_orders po ON po.id=vi.po_id
                                ORDER BY vi.invoice_date DESC,vi.id DESC""")
    exception_count = sum(1 for i in invoice_rows if not invoice_checks(i)["match"])
    outstanding = query_one("SELECT COALESCE(SUM(total_amount),0) amount FROM vendor_invoices WHERE status='Pending'")["amount"]
    po_open = query_one("SELECT COUNT(*) n FROM purchase_orders WHERE status IN ('Draft','Ordered','Partially Received')")["n"]
    stock_value = sum(float(p["stock"]) * float(p["unit_cost"]) for p in inv)
    warehouse_count = query_one("SELECT COUNT(*) AS n FROM warehouses")["n"]
    payable_outstanding = money(query_one("SELECT COALESCE(SUM(total_amount-COALESCE(paid_amount,0)),0) amount FROM vendor_invoices")["amount"])
    receivable_outstanding = money(query_one("SELECT COALESCE(SUM(total_amount-paid_amount),0) amount FROM sales_invoices")["amount"])
    monthly_rows = query_all("""SELECT substr(order_date,1,7) month, COUNT(*) orders,
                                 COALESCE(SUM(pol.quantity*pol.unit_price),0) value
                                FROM purchase_orders po JOIN purchase_order_lines pol ON pol.po_id=po.id
                                WHERE order_date >= date('now','-5 months') GROUP BY substr(order_date,1,7) ORDER BY month""")
    # sqlite3.Row supports key access but is not JSON serializable by Jinja's tojson filter.
    # Convert each row to a plain dict before embedding the chart data in dashboard.html.
    monthly = [dict(row) for row in monthly_rows]
    activity = query_all("SELECT * FROM audit_log ORDER BY id DESC LIMIT 6")
    return render_template("dashboard.html", title="Operations overview", products=inv, low=low,
                           invoices=invoice_rows[:6], exception_count=exception_count, outstanding=outstanding,
                           po_open=po_open, stock_value=stock_value, warehouse_count=warehouse_count,
                           payable_outstanding=payable_outstanding, receivable_outstanding=receivable_outstanding,
                           monthly=monthly, activity=activity)


@app.route("/inventory")
def inventory():
    rows = inventory_rows()
    warehouse_stock = warehouse_stock_rows()
    low_count = sum(1 for p in warehouse_stock if int(p["stock"]) <= int(p["reorder_level"]))
    stock_value = sum(float(p["stock"] or 0) * float(p["unit_cost"] or 0) for p in rows)
    warehouses = query_all("SELECT * FROM warehouses ORDER BY name")
    movements = query_all("""SELECT sm.*,p.sku,p.name AS product_name,p.unit,w.name AS warehouse_name
                            FROM stock_movements sm JOIN products p ON p.id=sm.product_id
                            JOIN warehouses w ON w.id=sm.warehouse_id
                            ORDER BY sm.movement_date DESC,sm.id DESC LIMIT 100""")
    warehouse_stock = query_all("""SELECT p.sku,p.name product_name,p.unit,p.reorder_level,p.unit_cost,
        w.code warehouse_code,w.name warehouse_name,
        COALESCE(SUM(CASE WHEN sm.movement_type IN ('OPENING','IN','ADJUST_IN','TRANSFER_IN') THEN sm.quantity
                          WHEN sm.movement_type IN ('OUT','ADJUST_OUT','TRANSFER_OUT','CONSUMPTION') THEN -sm.quantity ELSE 0 END),0) AS stock
        FROM products p CROSS JOIN warehouses w
        LEFT JOIN stock_movements sm ON sm.product_id=p.id AND sm.warehouse_id=w.id
        GROUP BY p.id,w.id ORDER BY p.name,w.name""")
    return render_template("inventory.html", title="Inventory control", products=rows,
                           low_count=low_count, stock_value=stock_value, warehouses=warehouses,
                           movements=movements, warehouse_stock=warehouse_stock)


@app.post("/inventory/products/create")
def create_product():
    if g.user["role"] != "admin":
        return render_template("error.html", title="Access denied", code=403,
                               message="Only administrators can add products to the product master."), 403
    db = get_db()
    try:
        sku = request.form.get("sku", "").strip().upper()
        name = request.form.get("name", "").strip()
        category = request.form.get("category", "").strip()
        hsn = request.form.get("hsn", "").strip()
        unit = request.form.get("unit", "pcs").strip()
        gst_rate = float(request.form.get("gst_rate", "18"))
        reorder_level = int(request.form.get("reorder_level", "0"))
        unit_cost = parse_nonnegative_money(request.form.get("unit_cost", "0"), "Unit cost")
        import re
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9._/-]{1,29}", sku):
            raise ValueError("SKU must be 2–30 characters and contain only letters, numbers, dots, slashes, underscores or hyphens.")
        if not name or len(name) > 120 or not category or len(category) > 80:
            raise ValueError("Product name (max 120 characters) and category (max 80 characters) are required.")
        if not re.fullmatch(r"[0-9]{4,8}", hsn):
            raise ValueError("HSN must contain 4 to 8 digits.")
        if unit not in {"pcs", "kg", "g", "m", "l", "box", "set"}:
            raise ValueError("Choose a supported unit of measure.")
        if gst_rate not in {0, 5, 12, 18, 28, 40}:
            raise ValueError("Choose a configured GST rate: 0%, 5%, 12%, 18%, 28% or 40%.")
        if reorder_level < 0 or reorder_level > 1_000_000:
            raise ValueError("Reorder level must be between 0 and 1,000,000.")
        db.execute("INSERT INTO products(sku,name,category,hsn,gst_rate,unit,reorder_level,unit_cost) VALUES(?,?,?,?,?,?,?,?)",
                    (sku, name, category, hsn, gst_rate, unit, reorder_level, unit_cost))
        db.commit()
        product = query_one("SELECT id FROM products WHERE sku=?", (sku,))
        audit(g.user["username"], "CREATE", "product", product["id"], f"{sku}; {name}; GST={gst_rate}%")
        flash(f"Product {sku} added to the product master. Opening stock remains zero until a ledger movement is recorded.", "success")
    except (ValueError, KeyError, sqlite3.IntegrityError) as exc:
        db.rollback()
        flash(f"Could not add product: {exc if str(exc) else 'SKU already exists.'}", "error")
    return redirect(url_for("inventory"))


@app.post("/inventory/adjust")
def adjust_inventory():
    if g.user["role"] not in {"admin", "storekeeper"}:
        return render_template("error.html", title="Access denied", code=403,
                               message="Only administrators and storekeepers can post stock adjustments."), 403
    db = get_db()
    try:
        product_id = int(request.form.get("product_id", ""))
        warehouse_id = int(request.form.get("warehouse_id", ""))
        quantity = int(request.form.get("quantity", ""))
        direction = request.form.get("direction", "")
        reason = request.form.get("reason", "").strip()
        movement_date = validate_iso_date(request.form.get("movement_date") or date.today().isoformat(), "Adjustment date")
        if quantity < 1 or quantity > 1_000_000:
            raise ValueError("Adjustment quantity must be between 1 and 1,000,000 units.")
        if direction not in {"ADJUST_IN", "ADJUST_OUT"}:
            raise ValueError("Choose whether stock is increasing or decreasing.")
        if len(reason) < 5 or len(reason) > 240:
            raise ValueError("Enter an adjustment reason between 5 and 240 characters.")
        product = query_one("SELECT id,sku,name FROM products WHERE id=?", (product_id,))
        warehouse = query_one("SELECT id,code,name FROM warehouses WHERE id=?", (warehouse_id,))
        if not product or not warehouse:
            raise ValueError("Choose a valid product and warehouse.")
        db.execute("BEGIN IMMEDIATE")
        balance = query_one("""SELECT COALESCE(SUM(CASE WHEN movement_type IN ('OPENING','IN','ADJUST_IN','TRANSFER_IN') THEN quantity
                                WHEN movement_type IN ('OUT','ADJUST_OUT','TRANSFER_OUT','CONSUMPTION') THEN -quantity ELSE 0 END),0) stock
                                FROM stock_movements WHERE product_id=? AND warehouse_id=?""", (product_id, warehouse_id))["stock"]
        if direction == "ADJUST_OUT" and quantity > int(balance):
            raise ValueError(f"Cannot remove {quantity} units: {warehouse['name']} currently has {balance} units.")
        ref = "ADJ-" + date.today().strftime("%Y%m%d") + "-" + secrets.token_hex(3).upper()
        db.execute("""INSERT INTO stock_movements(product_id,warehouse_id,movement_type,quantity,reference,movement_date,created_at)
                      VALUES(?,?,?,?,?,?,?)""", (product_id, warehouse_id, direction, quantity, ref, movement_date, datetime.now().isoformat(timespec="seconds")))
        db.commit()
        audit(g.user["username"], "STOCK_ADJUSTMENT", "stock_movement", ref,
              f"{product['sku']}; {warehouse['code']}; {direction}; qty={quantity}; reason={reason}")
        flash(f"Stock adjustment {ref} posted. The ledger balance has been updated.", "success")
    except (ValueError, KeyError, sqlite3.IntegrityError) as exc:
        db.rollback()
        flash(f"Stock adjustment not posted: {exc}", "error")
    return redirect(url_for("inventory"))


@app.route("/vendors")
def vendors():
    rows = query_all("SELECT v.*,COUNT(DISTINCT po.id) AS po_count FROM vendors v LEFT JOIN purchase_orders po ON po.vendor_id=v.id GROUP BY v.id ORDER BY v.name")
    return render_template("vendors.html", title="Vendor directory", vendors=rows)


@app.route("/vendors/<int:vendor_id>")
def vendor_detail(vendor_id):
    vendor = query_one("SELECT * FROM vendors WHERE id=?", (vendor_id,))
    if not vendor:
        return render_template("error.html", title="Vendor not found", code=404,
                               message="The requested vendor record could not be found."), 404
    orders = query_all("""SELECT po.*, COALESCE(SUM(pol.quantity*pol.unit_price),0) AS taxable,
                         COALESCE(SUM(pol.quantity*pol.unit_price*p.gst_rate/100.0),0) AS gst,
                         COUNT(pol.id) AS line_count
                         FROM purchase_orders po LEFT JOIN purchase_order_lines pol ON pol.po_id=po.id
                         LEFT JOIN products p ON p.id=pol.product_id
                         WHERE po.vendor_id=? GROUP BY po.id ORDER BY po.order_date DESC,po.id DESC""", (vendor_id,))
    invoices_rows = query_all("""SELECT vi.*,po.po_number FROM vendor_invoices vi
                                 LEFT JOIN purchase_orders po ON po.id=vi.po_id
                                 WHERE vi.vendor_id=? ORDER BY vi.invoice_date DESC,vi.id DESC""", (vendor_id,))
    po_lines = {}
    for order in orders:
        po_lines[order["id"]] = query_all("""SELECT pol.quantity,pol.unit_price,p.sku,p.name,p.unit
            FROM purchase_order_lines pol JOIN products p ON p.id=pol.product_id
            WHERE pol.po_id=? ORDER BY p.name""", (order["id"],))
    return render_template("vendor_detail.html", title="Vendor details", vendor=vendor,
                           orders=orders, po_lines=po_lines, invoices=invoices_rows)


@app.post("/vendors/create")
def create_vendor():
    if g.user["role"] not in {"admin", "procurement"}:
        return render_template("error.html", title="Access denied", code=403,
                               message="Only administrators and procurement users can add vendors."), 403
    db = get_db()
    code = request.form.get("code", "").strip().upper()
    name = request.form.get("name", "").strip()
    gstin = request.form.get("gstin", "").strip().upper()
    state = request.form.get("state", "").strip()
    email = request.form.get("email", "").strip()
    phone = request.form.get("phone", "").strip()
    try:
        terms = int(request.form.get("payment_terms", "30"))
        if not all([code, name, gstin, state]):
            raise ValueError("Vendor code, name, GSTIN and state are required.")
        if len(code) > 30 or len(name) > 120:
            raise ValueError("Vendor code must be 30 characters or fewer and name 120 characters or fewer.")
        import re
        if not re.fullmatch(r"[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]", gstin):
            raise ValueError("GSTIN must use the standard 15-character format.")
        if email and ("@" not in email or email.startswith("@") or email.endswith("@")):
            raise ValueError("Enter a valid vendor email address.")
        if terms < 0 or terms > 365:
            raise ValueError("Payment terms must be between 0 and 365 days.")
        now = datetime.now().isoformat(timespec="seconds")
        cur = db.execute("""INSERT INTO vendors(code,name,gstin,state,email,phone,payment_terms,active,created_at)
                            VALUES(?,?,?,?,?,?,?,1,?)""",
                         (code, name, gstin, state, email, phone, terms, now))
        db.commit()
        audit(g.user["username"], "CREATE", "vendor", cur.lastrowid, f"{code}; {name}")
        flash(f"Vendor {name} added successfully.", "success")
    except (ValueError, sqlite3.IntegrityError) as exc:
        db.rollback()
        flash(f"Could not add vendor: {exc if str(exc) else 'Vendor code already exists.'}", "error")
    return redirect(url_for("vendors"))


@app.route("/vendors/<int:vendor_id>/edit", methods=["GET", "POST"])
def edit_vendor(vendor_id):
    if g.user["role"] not in {"admin", "procurement"}:
        return render_template("error.html", title="Access denied", code=403,
                               message="Only administrators and procurement users can edit vendor master data."), 403
    vendor = query_one("SELECT * FROM vendors WHERE id=?", (vendor_id,))
    if not vendor:
        return render_template("error.html", title="Vendor not found", code=404,
                               message="The requested vendor record could not be found."), 404
    if request.method == "POST":
        db = get_db()
        code = request.form.get("code", "").strip().upper()
        name = request.form.get("name", "").strip()
        gstin = request.form.get("gstin", "").strip().upper()
        state = request.form.get("state", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        try:
            terms = int(request.form.get("payment_terms", "30"))
            active = int(request.form.get("active", "1"))
            import re
            if not all([code, name, gstin, state]):
                raise ValueError("Vendor code, name, GSTIN and state are required.")
            if len(code) > 30 or len(name) > 120:
                raise ValueError("Vendor code must be 30 characters or fewer and name 120 characters or fewer.")
            if not re.fullmatch(r"[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]", gstin):
                raise ValueError("GSTIN must use the standard 15-character format.")
            if email and ("@" not in email or email.startswith("@") or email.endswith("@")):
                raise ValueError("Enter a valid vendor email address.")
            if terms < 0 or terms > 365 or active not in {0, 1}:
                raise ValueError("Payment terms must be between 0 and 365 days.")
            db.execute("""UPDATE vendors SET code=?,name=?,gstin=?,state=?,email=?,phone=?,payment_terms=?,active=?
                          WHERE id=?""", (code, name, gstin, state, email, phone, terms, active, vendor_id))
            db.commit()
            audit(g.user["username"], "UPDATE", "vendor", vendor_id, f"{code}; {name}; active={active}")
            flash(f"Vendor {code} updated successfully.", "success")
            return redirect(url_for("vendor_detail", vendor_id=vendor_id))
        except (ValueError, KeyError, sqlite3.IntegrityError) as exc:
            db.rollback()
            flash(f"Could not update vendor: {exc if str(exc) else 'Vendor code already exists.'}", "error")
            vendor = {"id": vendor_id, "code": code, "name": name, "gstin": gstin, "state": state,
                      "email": email, "phone": phone, "payment_terms": request.form.get("payment_terms", "30"),
                      "active": request.form.get("active", "1"), "created_at": vendor["created_at"]}
    states = ["Andhra Pradesh","Arunachal Pradesh","Assam","Bihar","Chhattisgarh","Goa","Gujarat","Haryana","Himachal Pradesh","Jharkhand","Karnataka","Kerala","Madhya Pradesh","Maharashtra","Manipur","Meghalaya","Mizoram","Nagaland","Odisha","Punjab","Rajasthan","Sikkim","Tamil Nadu","Telangana","Tripura","Uttar Pradesh","Uttarakhand","West Bengal","Andaman and Nicobar Islands","Chandigarh","Dadra and Nagar Haveli and Daman and Diu","Delhi","Jammu and Kashmir","Ladakh","Lakshadweep","Puducherry"]
    return render_template("vendor_edit.html", title="Edit vendor", vendor=vendor, states=states)


@app.route("/profile")
def profile():
    user = query_one("SELECT id,username,full_name,role,active,created_at FROM users WHERE id=?", (g.user["id"],))
    activity = query_all("SELECT action,entity_type,details,created_at FROM audit_log WHERE actor=? ORDER BY id DESC LIMIT 10", (g.user["username"],))
    return render_template("profile.html", title="My profile", profile=user, activity=activity)


@app.route("/purchase-orders")
def purchase_orders():
    raw_orders = query_all("""SELECT po.*,v.name vendor_name,
                      COALESCE(SUM(pol.quantity*pol.unit_price),0) taxable,
                      COALESCE(SUM(pol.quantity*pol.unit_price*p.gst_rate/100.0),0) gst,
                      COUNT(pol.id) line_count
                      FROM purchase_orders po JOIN vendors v ON v.id=po.vendor_id
                      LEFT JOIN purchase_order_lines pol ON pol.po_id=po.id
                      LEFT JOIN products p ON p.id=pol.product_id
                      GROUP BY po.id ORDER BY po.order_date DESC,po.id DESC""")
    products = query_all("SELECT * FROM products ORDER BY name")
    vendors_list = query_all("SELECT * FROM vendors WHERE active=1 ORDER BY name")
    return render_template("purchase_orders.html", title="Procurement", orders=raw_orders, products=products, vendors=vendors_list, statuses=STATUSES)


@app.post("/purchase-orders/create")
def create_po():
    db = get_db()
    try:
        vendor_id = int(request.form["vendor_id"])
        product_ids = request.form.getlist("product_id") or [request.form.get("product_id", "")]
        quantities = request.form.getlist("quantity") or [request.form.get("quantity", "")]
        prices = request.form.getlist("unit_price") or [request.form.get("unit_price", "")]
        lines = []
        for index in range(max(len(product_ids), len(quantities), len(prices))):
            pid = (product_ids[index] if index < len(product_ids) else "").strip()
            qty_raw = (quantities[index] if index < len(quantities) else "").strip()
            price_raw = (prices[index] if index < len(prices) else "").strip()
            if not pid and not qty_raw and not price_raw:
                continue
            if not pid or not qty_raw or not price_raw:
                raise ValueError(f"Line {index + 1} needs a product, quantity and unit price.")
            product_id = int(pid)
            quantity = int(qty_raw)
            unit_price = parse_nonnegative_money(price_raw, f"Line {index + 1} unit price")
            if quantity < 1 or quantity > 1_000_000:
                raise ValueError(f"Line {index + 1} quantity must be between 1 and 1,000,000.")
            lines.append((product_id, quantity, unit_price))
        if not lines:
            raise ValueError("Add at least one product line to the purchase order.")
        if len(lines) > 20:
            raise ValueError("A purchase order can contain at most 20 line items.")
        if len({line[0] for line in lines}) != len(lines):
            raise ValueError("Each product can appear once per purchase order. Combine duplicate product quantities into one line.")
        order_date = validate_iso_date(request.form.get("order_date") or date.today().isoformat(), "Order date")
        expected_date = validate_iso_date(request.form.get("expected_date"), "Expected delivery date", optional=True)
        if expected_date and expected_date < order_date:
            raise ValueError("Expected delivery date cannot be before the order date.")
        vendor = query_one("SELECT id FROM vendors WHERE id=? AND active=1", (vendor_id,))
        if not vendor:
            raise ValueError("Choose an active vendor.")
        for product_id, _quantity, _unit_price in lines:
            if not query_one("SELECT id FROM products WHERE id=?", (product_id,)):
                raise ValueError("One of the selected products no longer exists. Refresh the form and try again.")
        po_num = f"PO-{date.today().strftime('%Y%m%d')}-{secrets.token_hex(4).upper()}"
        db.execute("BEGIN IMMEDIATE")
        db.execute("""INSERT INTO purchase_orders(po_number,vendor_id,order_date,expected_date,status,notes,created_at)
                      VALUES(?,?,?,?,'Ordered',?,?)""",
                   (po_num, vendor_id, order_date, expected_date, request.form.get("notes", "").strip()[:500],
                    datetime.now().isoformat(timespec="seconds")))
        po_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.executemany("INSERT INTO purchase_order_lines(po_id,product_id,quantity,unit_price) VALUES(?,?,?,?)",
                       [(po_id, product_id, quantity, unit_price) for product_id, quantity, unit_price in lines])
        db.commit()
        total_qty = sum(line[1] for line in lines)
        total_value = money(sum(line[1] * line[2] for line in lines))
        audit(g.user["username"], "CREATE", "purchase_order", po_id,
              f"{po_num}; lines={len(lines)}; units={total_qty}; taxable={total_value:.2f}")
        flash(f"{po_num} created with {len(lines)} line item(s). It is ready for goods receipt.", "success")
    except (ValueError, KeyError, sqlite3.IntegrityError) as e:
        db.rollback()
        flash(f"Could not create purchase order: {e}", "error")
    return redirect(url_for("purchase_orders"))


@app.route("/purchase-orders/<int:po_id>")
def purchase_order_detail(po_id):
    order = query_one("""SELECT po.*,v.name AS vendor_name,v.code AS vendor_code,v.gstin,v.state,v.email,v.phone
                         FROM purchase_orders po JOIN vendors v ON v.id=po.vendor_id WHERE po.id=?""", (po_id,))
    if not order:
        return render_template("error.html", title="Purchase order not found", code=404,
                               message="The requested purchase order could not be found."), 404
    lines = query_all("""SELECT pol.id,pol.product_id,pol.quantity AS ordered_quantity,pol.unit_price,
                        p.sku,p.name,p.unit,p.hsn,p.gst_rate,
                        pol.quantity*pol.unit_price AS taxable,
                        pol.quantity*pol.unit_price*p.gst_rate/100.0 AS gst,
                        COALESCE((SELECT SUM(grl.quantity) FROM goods_receipts gr
                                  JOIN goods_receipt_lines grl ON grl.grn_id=gr.id
                                  WHERE gr.po_id=? AND grl.product_id=pol.product_id),0) AS received_quantity
                        FROM purchase_order_lines pol JOIN products p ON p.id=pol.product_id
                        WHERE pol.po_id=? ORDER BY pol.id""", (po_id, po_id))
    receipts = query_all("""SELECT gr.*,w.name AS warehouse_name,
                           GROUP_CONCAT(p.name || ' × ' || grl.quantity, ', ') AS items
                           FROM goods_receipts gr JOIN warehouses w ON w.id=gr.warehouse_id
                           LEFT JOIN goods_receipt_lines grl ON grl.grn_id=gr.id
                           LEFT JOIN products p ON p.id=grl.product_id
                           WHERE gr.po_id=? GROUP BY gr.id ORDER BY gr.received_date DESC,gr.id DESC""", (po_id,))
    taxable = money(sum(float(line["taxable"]) for line in lines))
    gst = money(sum(float(line["gst"]) for line in lines))
    return render_template("purchase_order_detail.html", title="Purchase order details", order=order,
                           lines=lines, receipts=receipts, taxable=taxable, gst=gst)


@app.post("/purchase-orders/<int:po_id>/status")
def update_po_status(po_id):
    status = request.form.get("status")
    if status not in STATUSES:
        flash("Invalid purchase order status.", "error")
    else:
        db = get_db()
        row = query_one("SELECT po_number,status FROM purchase_orders WHERE id=?", (po_id,))
        if not row:
            flash("Purchase order not found.", "error")
        elif status in {"Partially Received", "Received"} and status != row["status"]:
            flash("Receipt statuses are calculated automatically from posted goods receipts.", "error")
        elif row["status"] in {"Partially Received", "Received"} and status != row["status"]:
            flash("A purchase order with posted receipts cannot be manually reset or cancelled.", "error")
        else:
            db.execute("UPDATE purchase_orders SET status=? WHERE id=?", (status, po_id))
            db.commit()
            audit(g.user["username"], "STATUS_CHANGE", "purchase_order", po_id, f"{row['po_number']} → {status}")
            flash(f"{row['po_number']} status updated.", "success")
    return redirect(url_for("purchase_orders"))


@app.route("/receipts")
def receipts():
    rows = query_all("""SELECT gr.*,po.po_number,v.name vendor_name,w.name warehouse_name,
                       GROUP_CONCAT(p.name || ' × ' || grl.quantity, ', ') items
                       FROM goods_receipts gr JOIN purchase_orders po ON po.id=gr.po_id
                       JOIN vendors v ON v.id=po.vendor_id JOIN warehouses w ON w.id=gr.warehouse_id
                       LEFT JOIN goods_receipt_lines grl ON grl.grn_id=gr.id
                       LEFT JOIN products p ON p.id=grl.product_id
                       GROUP BY gr.id ORDER BY gr.received_date DESC""")
    orders = query_all("""SELECT po.id,po.po_number,v.name vendor_name FROM purchase_orders po
                          JOIN vendors v ON v.id=po.vendor_id
                          WHERE po.status IN ('Ordered','Partially Received') ORDER BY po.order_date DESC""")
    warehouses = query_all("SELECT * FROM warehouses ORDER BY name")
    products = query_all("SELECT * FROM products ORDER BY name")
    po_line_rows = query_all("""SELECT po.id AS po_id,pol.product_id,p.name,p.sku,p.unit,
                              pol.quantity AS ordered_quantity,
                              COALESCE(SUM(grl.quantity),0) AS received_quantity
                              FROM purchase_orders po
                              JOIN purchase_order_lines pol ON pol.po_id=po.id
                              JOIN products p ON p.id=pol.product_id
                              LEFT JOIN goods_receipts gr ON gr.po_id=po.id
                              LEFT JOIN goods_receipt_lines grl ON grl.grn_id=gr.id AND grl.product_id=pol.product_id
                              WHERE po.status IN ('Ordered','Partially Received')
                              GROUP BY po.id,pol.id,p.id ORDER BY po.order_date DESC,p.name""")
    po_lines_by_order = {}
    for row in po_line_rows:
        po_lines_by_order.setdefault(str(row["po_id"]), []).append({
            "product_id": row["product_id"], "name": row["name"], "sku": row["sku"], "unit": row["unit"],
            "ordered": int(row["ordered_quantity"]), "received": int(row["received_quantity"]),
            "remaining": max(0, int(row["ordered_quantity"]) - int(row["received_quantity"]))
        })
    return render_template("receipts.html", title="Goods receipts", receipts=rows, orders=orders,
                           warehouses=warehouses, products=products, po_lines_by_order=po_lines_by_order)


@app.post("/receipts/create")
def create_receipt():
    db = get_db()
    try:
        # Acquire SQLite's write lock before validating outstanding quantities so
        # two concurrent requests cannot both approve the same remaining balance.
        db.execute("BEGIN IMMEDIATE")
        po_id = int(request.form["po_id"])
        warehouse_id = int(request.form["warehouse_id"])
        product_id = int(request.form["product_id"])
        qty = int(request.form["quantity"])
        if qty <= 0:
            raise ValueError("Receipt quantity must be greater than zero.")
        po = query_one("SELECT * FROM purchase_orders WHERE id=?", (po_id,))
        line = query_one("SELECT * FROM purchase_order_lines WHERE po_id=? AND product_id=?", (po_id, product_id))
        if not po or not line:
            raise ValueError("The selected product must exist on the selected purchase order.")
        if po["status"] not in {"Ordered", "Partially Received"}:
            raise ValueError("Only ordered or partially received purchase orders can accept receipts.")
        already = query_one("""SELECT COALESCE(SUM(grl.quantity),0) qty FROM goods_receipts gr
                               JOIN goods_receipt_lines grl ON grl.grn_id=gr.id
                               WHERE gr.po_id=? AND grl.product_id=?""", (po_id, product_id))["qty"]
        if qty + already > line["quantity"]:
            raise ValueError(f"Receipt exceeds ordered quantity. Ordered {line['quantity']}; already received {already}.")
        wh = query_one("SELECT id FROM warehouses WHERE id=?", (warehouse_id,))
        if not wh:
            raise ValueError("Choose a valid warehouse.")
        grn = f"GRN-{date.today().strftime('%Y%m%d')}-{secrets.token_hex(4).upper()}"
        received_date = validate_iso_date(request.form.get("received_date") or date.today().isoformat(), "Receipt date")
        db.execute("""INSERT INTO goods_receipts(grn_number,po_id,warehouse_id,received_date,reference,created_at)
                      VALUES(?,?,?,?,?,?)""", (grn, po_id, warehouse_id, received_date,
                      request.form.get("reference","").strip(), datetime.now().isoformat(timespec="seconds")))
        grn_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute("INSERT INTO goods_receipt_lines(grn_id,product_id,quantity) VALUES(?,?,?)", (grn_id, product_id, qty))
        db.execute("""INSERT INTO stock_movements(product_id,warehouse_id,movement_type,quantity,reference,movement_date,created_at)
                      VALUES(?,?, 'IN',?,?,?,?)""",
                   (product_id, warehouse_id, qty, grn, received_date, datetime.now().isoformat(timespec="seconds")))
        # Compute the PO status across every line, not only the selected product.
        # This also keeps multi-line orders receivable if this schema is extended later.
        receipt_state = query_all("""SELECT pol.id, pol.quantity AS ordered_quantity,
                                   COALESCE(SUM(grl.quantity),0) AS received_quantity
                                   FROM purchase_order_lines pol
                                   LEFT JOIN goods_receipts gr ON gr.po_id=pol.po_id
                                   LEFT JOIN goods_receipt_lines grl
                                     ON grl.grn_id=gr.id AND grl.product_id=pol.product_id
                                   WHERE pol.po_id=? GROUP BY pol.id,pol.quantity""", (po_id,))
        all_lines_received = bool(receipt_state) and all(
            int(state["received_quantity"]) >= int(state["ordered_quantity"])
            for state in receipt_state
        )
        new_status = "Received" if all_lines_received else "Partially Received"
        db.execute("UPDATE purchase_orders SET status=? WHERE id=?", (new_status, po_id))
        db.commit()
        audit(g.user["username"], "CREATE", "goods_receipt", grn_id, f"{grn}; qty={qty}; warehouse={warehouse_id}")
        flash(f"{grn} posted. Stock ledger updated atomically.", "success")
    except (ValueError, KeyError, sqlite3.IntegrityError) as e:
        db.rollback()
        flash(f"Receipt not posted: {e}", "error")
    return redirect(url_for("receipts"))


@app.route("/invoices")
def invoices():
    rows = query_all("""SELECT vi.*,v.name vendor_name,v.gstin,po.po_number FROM vendor_invoices vi
                        JOIN vendors v ON v.id=vi.vendor_id LEFT JOIN purchase_orders po ON po.id=vi.po_id
                        ORDER BY vi.invoice_date DESC,vi.id DESC""")
    checked = []
    for row in rows:
        checked.append({"invoice": row, **invoice_checks(row)})
    vendors_list = query_all("SELECT * FROM vendors ORDER BY name")
    orders = query_all("""SELECT po.*, COALESCE(SUM(pol.quantity*pol.unit_price),0) AS taxable,
                          COALESCE(SUM(pol.quantity*pol.unit_price*p.gst_rate/100.0),0) AS estimated_gst
                          FROM purchase_orders po LEFT JOIN purchase_order_lines pol ON pol.po_id=po.id
                          LEFT JOIN products p ON p.id=pol.product_id
                          GROUP BY po.id ORDER BY po.order_date DESC,po.id DESC""")
    po_line_catalog = {}
    for order in orders:
        line_rows = query_all("""SELECT pol.product_id,pol.quantity,pol.unit_price,p.sku,p.name,p.unit,p.gst_rate,
                COALESCE((SELECT SUM(grl.quantity) FROM goods_receipts gr JOIN goods_receipt_lines grl ON grl.grn_id=gr.id
                          WHERE gr.po_id=? AND grl.product_id=pol.product_id),0) received_quantity
                FROM purchase_order_lines pol JOIN products p ON p.id=pol.product_id WHERE pol.po_id=? ORDER BY pol.id""", (order["id"], order["id"]))
        po_line_catalog[str(order["id"])] = [dict(line) for line in line_rows]
    return render_template("invoices.html", title="Invoice & GST desk", invoices=checked, vendors=vendors_list,
                           orders=orders, po_line_catalog=po_line_catalog)


@app.post("/invoices/create")
def create_invoice():
    db = get_db()
    try:
        vendor_id = int(request.form["vendor_id"])
        po_id = int(request.form["po_id"])
        invoice_number = request.form.get("invoice_number", "").strip()
        invoice_date = validate_iso_date(request.form.get("invoice_date") or date.today().isoformat(), "Invoice date")
        due_date = validate_iso_date(request.form.get("due_date") or "", "Payment due date", optional=True)
        taxable = parse_nonnegative_money(request.form.get("taxable_value"), "Taxable value")
        gst = parse_nonnegative_money(request.form.get("gst_amount"), "GST amount")
        if not invoice_number:
            raise ValueError("Enter an invoice number.")
        if len(invoice_number) > 120:
            raise ValueError("Invoice number must be 120 characters or fewer.")
        po = query_one("SELECT vendor_id,status FROM purchase_orders WHERE id=?", (po_id,))
        if not po or po["vendor_id"] != vendor_id:
            raise ValueError("The purchase order must belong to the selected vendor.")
        vendor = query_one("SELECT payment_terms,state FROM vendors WHERE id=?", (vendor_id,))
        if not due_date:
            from datetime import date as _date, timedelta as _timedelta
            due_date = ( _date.fromisoformat(invoice_date) + _timedelta(days=int(vendor["payment_terms"] or 30)) ).isoformat()
        product_ids = request.form.getlist("line_product_id")
        quantities = request.form.getlist("line_quantity")
        prices = request.form.getlist("line_unit_price")
        detail_lines = []
        if any((product_ids, quantities, prices)):
            max_len = max(len(product_ids), len(quantities), len(prices))
            seen = set()
            for idx in range(max_len):
                pid_raw = product_ids[idx].strip() if idx < len(product_ids) else ""
                qty_raw = quantities[idx].strip() if idx < len(quantities) else ""
                price_raw = prices[idx].strip() if idx < len(prices) else ""
                if not pid_raw and not qty_raw and not price_raw:
                    continue
                if not pid_raw or not qty_raw or not price_raw:
                    raise ValueError(f"Invoice line {idx+1} requires product, quantity and unit price.")
                product_id = int(pid_raw)
                qty = int(qty_raw)
                price = parse_nonnegative_money(price_raw, f"Invoice line {idx+1} unit price")
                if qty <= 0 or qty > 1_000_000:
                    raise ValueError(f"Invoice line {idx+1} quantity must be between 1 and 1,000,000.")
                if product_id in seen:
                    raise ValueError("Each product can appear only once in an invoice. Combine duplicate product lines.")
                seen.add(product_id)
                po_line = query_one("SELECT id,quantity,unit_price FROM purchase_order_lines WHERE po_id=? AND product_id=?", (po_id, product_id))
                product = query_one("SELECT id,gst_rate FROM products WHERE id=?", (product_id,))
                if not po_line or not product:
                    raise ValueError("Every invoice product must be present on the linked purchase order.")
                line_taxable = money(qty * price)
                line_gst = money(line_taxable * float(product["gst_rate"]) / 100.0)
                detail_lines.append((po_line["id"], product_id, qty, price, line_taxable, float(product["gst_rate"]), line_gst))
            if not detail_lines:
                raise ValueError("Add at least one invoice detail line.")
        else:
            # Existing saved forms and older integrations can still submit summaries;
            # the matching report clearly marks such invoices as missing line detail.
            po_lines = query_all("SELECT pol.id,pol.product_id,pol.quantity,pol.unit_price,p.gst_rate FROM purchase_order_lines pol JOIN products p ON p.id=pol.product_id WHERE pol.po_id=?", (po_id,))
            detail_lines = [(r["id"], r["product_id"], int(r["quantity"]), float(r["unit_price"]),
                             money(int(r["quantity"]) * float(r["unit_price"])), float(r["gst_rate"]),
                             money(int(r["quantity"]) * float(r["unit_price"]) * float(r["gst_rate"]) / 100)) for r in po_lines]
        # Match invoice lines against the receipt ledger before the invoice can be approved.
        issues = []
        line_taxable = money(sum(x[4] for x in detail_lines))
        line_gst = money(sum(x[6] for x in detail_lines))
        if abs(taxable - line_taxable) > 1:
            issues.append("Header taxable value differs from invoice line total")
        if abs(gst - line_gst) > 1:
            issues.append("Header GST differs from invoice line total")
        for po_line_id, product_id, qty, price, line_value, rate, line_gst_value in detail_lines:
            po_line = query_one("SELECT quantity,unit_price FROM purchase_order_lines WHERE id=?", (po_line_id,))
            received_qty = query_one("""SELECT COALESCE(SUM(grl.quantity),0) qty FROM goods_receipts gr
                                       JOIN goods_receipt_lines grl ON grl.grn_id=gr.id
                                       WHERE gr.po_id=? AND grl.product_id=?""", (po_id, product_id))["qty"]
            if qty > int(received_qty):
                issues.append(f"Invoice quantity exceeds received quantity for product ID {product_id}")
            if abs(price - float(po_line["unit_price"])) > 1:
                issues.append(f"Unit price variance for product ID {product_id}")
        total = money(taxable + gst)
        company_state = os.environ.get("COMPANY_STATE", "Karnataka").strip().lower()
        intrastate = (vendor["state"] or "").strip().lower() == company_state
        igst = 0.0 if intrastate else gst
        cgst = money(gst / 2) if intrastate else 0.0
        sgst = money(gst - cgst) if intrastate else 0.0
        status = "Pending" if issues else "Approved"
        itc = "Review" if issues else "Eligible"
        db.execute("BEGIN IMMEDIATE")
        cur = db.execute("""INSERT INTO vendor_invoices(invoice_number,vendor_id,po_id,invoice_date,due_date,taxable_value,gst_amount,total_amount,paid_amount,status,itc_status,notes,created_at,igst_amount,cgst_amount,sgst_amount)
                            VALUES(?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?)""",
                       (invoice_number, vendor_id, po_id, invoice_date, due_date, taxable, gst, total, status, itc,
                        request.form.get("notes", "").strip()[:1000], datetime.now().isoformat(timespec="seconds"), igst, cgst, sgst))
        inv_id = cur.lastrowid
        db.executemany("""INSERT INTO vendor_invoice_lines(invoice_id,po_line_id,product_id,quantity,unit_price,taxable_value,gst_rate,gst_amount)
                            VALUES(?,?,?,?,?,?,?,?)""", [(inv_id, *line) for line in detail_lines])
        db.commit()
        audit(g.user["username"], "CREATE", "vendor_invoice", inv_id,
              f"{invoice_number}; total={total:.2f}; line_count={len(detail_lines)}; status={status}")
        flash(f"Invoice {invoice_number} captured. {'Review the three-way exceptions before payment.' if issues else 'Line values and receipt quantities passed current checks.'}", "success" if not issues else "error")
    except (ValueError, KeyError, sqlite3.IntegrityError) as e:
        db.rollback()
        flash(f"Invoice not saved: {e}", "error")
    return redirect(url_for("invoices"))


@app.post("/invoices/<int:invoice_id>/review")
def review_invoice(invoice_id):
    row = query_one("SELECT * FROM vendor_invoices WHERE id=?", (invoice_id,))
    if not row:
        flash("Invoice not found.", "error")
    else:
        checks = invoice_checks(row)
        db = get_db()
        if checks["match"]:
            db.execute("UPDATE vendor_invoices SET status='Approved',itc_status='Eligible' WHERE id=?", (invoice_id,))
            flash("Invoice passed the current rule checks and is marked approved.", "success")
            audit(g.user["username"], "APPROVE", "vendor_invoice", invoice_id, row["invoice_number"])
        else:
            db.execute("UPDATE vendor_invoices SET status='Pending',itc_status='Review' WHERE id=?", (invoice_id,))
            flash("Invoice still has exceptions. Review the PO value and GST rate before approval.", "error")
            audit(g.user["username"], "FLAG", "vendor_invoice", invoice_id, "; ".join(checks["issues"]))
        db.commit()
    return redirect(url_for("invoices"))


@app.post("/invoices/<int:invoice_id>/approve-override")
def approve_invoice_override(invoice_id):
    # Manual override is deliberately separate from automated matching. It requires
    # a reason and never marks ITC as eligible while an exception remains.
    reason = request.form.get("override_reason", "").strip()
    if not reason:
        flash("A reason is required to approve an invoice with exceptions.", "error")
        return redirect(url_for("invoices"))
    row = query_one("SELECT * FROM vendor_invoices WHERE id=?", (invoice_id,))
    if not row:
        flash("Invoice not found.", "error")
        return redirect(url_for("invoices"))
    checks = invoice_checks(row)
    db = get_db()
    db.execute("UPDATE vendor_invoices SET status='Approved',itc_status=? WHERE id=?",
               ("Eligible" if checks["match"] else "Review", invoice_id))
    db.commit()
    detail = f"{row['invoice_number']}; manual override reason: {reason}"
    if checks["issues"]:
        detail += "; unresolved checks: " + "; ".join(checks["issues"])
    audit(g.user["username"], "APPROVE_OVERRIDE", "vendor_invoice", invoice_id, detail)
    flash("Invoice manually approved. ITC remains under review because exceptions are unresolved." if checks["issues"] else "Invoice approved; automated checks passed.", "success")
    return redirect(url_for("invoices"))


@app.route("/vendors/<int:vendor_id>/bank", methods=["GET", "POST"])
def vendor_bank(vendor_id):
    vendor = query_one("SELECT id,code,name,gstin,state FROM vendors WHERE id=?", (vendor_id,))
    if not vendor:
        return render_template("error.html", title="Vendor not found", code=404,
                               message="The requested vendor could not be found."), 404
    existing = query_one("SELECT bank_name,account_last4,ifsc,updated_by,updated_at FROM vendor_bank_details WHERE vendor_id=?", (vendor_id,))
    if request.method == "POST":
        try:
            bank_name = request.form.get("bank_name", "").strip()
            account_number = re.sub(r"[\s-]", "", request.form.get("account_number", ""))
            ifsc = request.form.get("ifsc", "").strip().upper()
            if not (2 <= len(bank_name) <= 120):
                raise ValueError("Bank name is required and must be 2–120 characters.")
            if not re.fullmatch(r"[0-9]{9,18}", account_number):
                raise ValueError("Bank account number must contain 9–18 digits.")
            if not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", ifsc):
                raise ValueError("IFSC must use the expected 11-character format.")
            cipher = _vendor_data_fernet()
            ciphertext = cipher.encrypt(account_number.encode("utf-8")).decode("ascii")
            now = datetime.now().isoformat(timespec="seconds")
            get_db().execute("""INSERT INTO vendor_bank_details(vendor_id,bank_name,account_ciphertext,account_last4,ifsc,updated_by,updated_at)
                             VALUES(?,?,?,?,?,?,?) ON CONFLICT(vendor_id) DO UPDATE SET bank_name=excluded.bank_name,
                             account_ciphertext=excluded.account_ciphertext,account_last4=excluded.account_last4,
                             ifsc=excluded.ifsc,updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
                             (vendor_id, bank_name, ciphertext, account_number[-4:], ifsc, g.user["username"], now))
            get_db().commit()
            audit(g.user["username"], "UPDATE_VENDOR_BANK_DETAILS", "vendor", vendor_id,
                  f"{vendor['code']}; bank={bank_name}; account_last4={account_number[-4:]}; IFSC={ifsc}; encrypted_at_rest=true")
            flash("Supplier payment details saved encrypted. The full account number is not displayed back to users.", "success")
            return redirect(url_for("vendor_bank", vendor_id=vendor_id))
        except (ValueError, sqlite3.IntegrityError) as exc:
            get_db().rollback()
            flash(f"Supplier payment details not saved: {exc}", "error")
    existing = query_one("SELECT bank_name,account_last4,ifsc,updated_by,updated_at FROM vendor_bank_details WHERE vendor_id=?", (vendor_id,))
    return render_template("vendor_bank.html", title="Restricted supplier payment details", vendor=vendor, details=existing,
                           encryption_ready=bool(os.environ.get("VENDOR_DATA_KEY", "").strip() and Fernet is not None))


@app.route("/reports")
def reports():
    inv = inventory_rows()
    invoices_rows = query_all("""SELECT vi.*,v.name vendor_name,v.state FROM vendor_invoices vi
                                 JOIN vendors v ON v.id=vi.vendor_id ORDER BY vi.invoice_date DESC""")
    gst_total = sum(float(i["gst_amount"]) for i in invoices_rows)
    eligible = sum(float(i["gst_amount"]) for i in invoices_rows if i["itc_status"] == "Eligible")
    pending = sum(float(i["total_amount"]) for i in invoices_rows if i["status"] == "Pending")
    interstate = query_all("""SELECT v.state,COUNT(*) invoices,SUM(vi.taxable_value) taxable,SUM(vi.gst_amount) gst
                               FROM vendor_invoices vi JOIN vendors v ON v.id=vi.vendor_id
                               GROUP BY v.state ORDER BY taxable DESC""")
    low_stock_count = sum(1 for product in inv if product["stock"] <= product["reorder_level"])
    return render_template("reports.html", title="GST & management reports", products=inv, invoices=invoices_rows,
                           gst_total=gst_total, eligible=eligible, pending=pending, interstate=interstate,
                           low_stock_count=low_stock_count)


@app.route("/audit")
def audit_page():
    rows = query_all("SELECT * FROM audit_log ORDER BY id DESC LIMIT 100")
    integrity = audit_chain_status()
    return render_template("audit.html", title="Audit trail", events=rows, integrity=integrity)


@app.route("/api/summary")
def api_summary():
    inv = inventory_rows()
    invoice_rows = query_all("SELECT * FROM vendor_invoices")
    return jsonify({
        "as_of": datetime.now().isoformat(timespec="seconds"),
        "inventory_skus": len(inv),
        "stock_units": sum(int(p["stock"]) for p in inv),
        "low_stock_skus": sum(1 for p in inv if p["stock"] <= p["reorder_level"]),
        "stock_value": money(sum(float(p["stock"]) * float(p["unit_cost"]) for p in inv)),
        "open_purchase_orders": query_one("SELECT COUNT(*) n FROM purchase_orders WHERE status IN ('Draft','Ordered','Partially Received')")["n"],
        "invoice_exceptions": sum(1 for i in invoice_rows if not invoice_checks(i)["match"]),
        "pending_payables": money(sum(float(i["total_amount"]) for i in invoice_rows if i["status"] == "Pending")),
        "accounts_payable_outstanding": money(query_one("SELECT COALESCE(SUM(total_amount-COALESCE(paid_amount,0)),0) amount FROM vendor_invoices")["amount"]),
        "accounts_receivable_outstanding": money(query_one("SELECT COALESCE(SUM(total_amount-paid_amount),0) amount FROM sales_invoices")["amount"]),
        "audit_chain_valid": audit_chain_status()["valid"],
    })


@app.route("/export/<kind>.csv")
def export_csv(kind):
    # The shared export endpoint must still enforce per-dataset permissions.
    if kind in {"warehouse-stock", "itc-reconciliation", "payables", "receivables", "mismatches"}:
        # Permissions for these exports are enforced separately from view permissions.
        role_permissions = ROLE_ACCESS.get(g.user["role"], set())
        required = {"warehouse-stock": "inventory", "itc-reconciliation": "gst_center",
                    "payables": "payables", "receivables": "receivables", "mismatches": "matching"}[kind]
        if "*" not in role_permissions and required not in role_permissions:
            return render_template("error.html", title="Access denied", code=403, message="Your role cannot export this financial dataset."), 403
        return export_csv_extended(kind)
    # A general export permission must not expose datasets hidden in the UI.
    dataset_permission = {
        "inventory": "inventory",
        "invoices": "invoices",
        "purchase-orders": "purchase_orders",
    }.get(kind)
    role = g.user["role"]
    role_permissions = ROLE_ACCESS.get(role, set())
    if dataset_permission and "*" not in role_permissions and dataset_permission not in role_permissions:
        return render_template("error.html", title="Access denied", code=403,
                               message="Your role does not have permission to export this dataset."), 403

    if kind == "inventory":
        rows = inventory_rows()
        headers = ["sku", "name", "category", "hsn", "gst_rate", "unit", "stock", "reorder_level", "unit_cost"]
        data = [[r[h] for h in headers] for r in rows]
    elif kind == "invoices":
        rows = query_all("""SELECT vi.invoice_number,v.name vendor, v.gstin,vi.invoice_date,vi.taxable_value,vi.igst_amount,vi.cgst_amount,vi.sgst_amount,vi.gst_amount,vi.total_amount,vi.status,vi.itc_status
                            FROM vendor_invoices vi JOIN vendors v ON v.id=vi.vendor_id ORDER BY vi.invoice_date DESC""")
        headers = list(rows[0].keys()) if rows else ["invoice_number","vendor","gstin","invoice_date","taxable_value","igst_amount","cgst_amount","sgst_amount","gst_amount","total_amount","status","itc_status"]
        data = [list(r) for r in rows]
    elif kind == "purchase-orders":
        rows = query_all("""SELECT po.po_number,v.name vendor,po.order_date,po.expected_date,po.status,
                            SUM(pol.quantity*pol.unit_price) taxable,
                            SUM(pol.quantity*pol.unit_price*p.gst_rate/100.0) estimated_gst,
                            COUNT(pol.id) line_count
                            FROM purchase_orders po JOIN vendors v ON v.id=po.vendor_id
                            JOIN purchase_order_lines pol ON pol.po_id=po.id
                            JOIN products p ON p.id=pol.product_id
                            GROUP BY po.id ORDER BY po.order_date DESC""")
        headers = list(rows[0].keys()) if rows else ["po_number","vendor","order_date","expected_date","status","taxable","estimated_gst","line_count"]
        data = [list(r) for r in rows]
    else:
        return jsonify({"error": "Unknown export type"}), 404
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(headers)
    writer.writerows(data)
    return Response("\ufeff" + out.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=uniwork-{kind}.csv"})


@app.route("/health", methods=["GET"])
def health():
    """Minimal public liveness endpoint for deployment health checks."""
    return jsonify({"status": "ok"}), 200


# --------------------------- Additional operational modules ---------------------------

def stock_balance(product_id, warehouse_id):
    row = query_one("""SELECT COALESCE(SUM(CASE WHEN movement_type IN ('OPENING','IN','ADJUST_IN','TRANSFER_IN') THEN quantity
                         WHEN movement_type IN ('OUT','ADJUST_OUT','TRANSFER_OUT','CONSUMPTION') THEN -quantity ELSE 0 END),0) stock
                       FROM stock_movements WHERE product_id=? AND warehouse_id=?""", (product_id, warehouse_id))
    return int(row["stock"] or 0)


def audit_chain_status():
    rows = query_all("SELECT * FROM audit_log ORDER BY id")
    previous = "GENESIS"
    for row in rows:
        expected = _audit_digest(previous, row["id"], row["actor"], row["action"], row["entity_type"], row["entity_id"], row["details"] or "", row["created_at"])
        if row["previous_hash"] != previous or row["event_hash"] != expected:
            return {"valid": False, "checked": row["id"], "message": f"Integrity mismatch detected at audit event #{row['id']}."}
        previous = expected
    return {"valid": True, "checked": len(rows), "message": f"Verified {len(rows)} audit events."}


@app.route("/consumption", methods=["GET"])
def consumption():
    rows = query_all("""SELECT mc.*,p.sku,p.name product_name,p.unit,w.name warehouse_name
                        FROM material_consumptions mc JOIN products p ON p.id=mc.product_id
                        JOIN warehouses w ON w.id=mc.warehouse_id ORDER BY mc.movement_date DESC,mc.id DESC LIMIT 100""")
    products = query_all("SELECT * FROM products ORDER BY name")
    warehouses = query_all("SELECT * FROM warehouses ORDER BY name")
    balances = [{"product_id": p["id"], "warehouse_id": w["id"], "product_name": p["name"], "sku": p["sku"],
                 "warehouse_name": w["name"], "unit": p["unit"], "stock": stock_balance(p["id"], w["id"])}
                for p in products for w in warehouses]
    return render_template("consumption.html", title="Material consumption", consumptions=rows,
                           products=products, warehouses=warehouses, balances=balances)


@app.post("/consumption/create")
def create_consumption():
    db = get_db()
    try:
        product_id = int(request.form.get("product_id", ""))
        warehouse_id = int(request.form.get("warehouse_id", ""))
        quantity = int(request.form.get("quantity", ""))
        movement_date = validate_iso_date(request.form.get("movement_date") or date.today().isoformat(), "Consumption date")
        purpose = request.form.get("purpose", "").strip()
        if quantity <= 0 or quantity > 1_000_000:
            raise ValueError("Consumption quantity must be between 1 and 1,000,000.")
        if len(purpose) < 5 or len(purpose) > 240:
            raise ValueError("Enter a purpose or work-order reference between 5 and 240 characters.")
        product = query_one("SELECT id,sku,name FROM products WHERE id=?", (product_id,))
        warehouse = query_one("SELECT id,code,name FROM warehouses WHERE id=?", (warehouse_id,))
        if not product or not warehouse:
            raise ValueError("Choose a valid product and warehouse.")
        db.execute("BEGIN IMMEDIATE")
        balance = stock_balance(product_id, warehouse_id)
        if quantity > balance:
            raise ValueError(f"Insufficient stock in {warehouse['name']}. Available: {balance}.")
        ref = f"CON-{date.today():%Y%m%d}-{secrets.token_hex(3).upper()}"
        now = datetime.now().isoformat(timespec="seconds")
        db.execute("INSERT INTO material_consumptions(reference,product_id,warehouse_id,quantity,movement_date,purpose,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                   (ref, product_id, warehouse_id, quantity, movement_date, purpose, g.user["username"], now))
        db.execute("INSERT INTO stock_movements(product_id,warehouse_id,movement_type,quantity,reference,movement_date,created_at) VALUES(?,?,'CONSUMPTION',?,?,?,?)",
                   (product_id, warehouse_id, quantity, ref, movement_date, now))
        db.commit()
        audit(g.user["username"], "MATERIAL_CONSUMPTION", "stock_movement", ref, f"{product['sku']}; {warehouse['code']}; qty={quantity}; purpose={purpose}")
        flash(f"Material issue {ref} posted; warehouse stock updated.", "success")
    except (ValueError, KeyError, sqlite3.IntegrityError) as exc:
        db.rollback()
        flash(f"Consumption not posted: {exc}", "error")
    return redirect(url_for("consumption"))


@app.route("/inventory/transfers", methods=["GET"])
def transfers():
    rows = query_all("""SELECT st.*,p.sku,p.name product_name,p.unit,fw.name from_warehouse,tw.name to_warehouse
                        FROM stock_transfers st JOIN products p ON p.id=st.product_id
                        JOIN warehouses fw ON fw.id=st.from_warehouse_id JOIN warehouses tw ON tw.id=st.to_warehouse_id
                        ORDER BY st.movement_date DESC,st.id DESC LIMIT 100""")
    return render_template("transfers.html", title="Warehouse transfers", transfers=rows,
                           products=query_all("SELECT * FROM products ORDER BY name"),
                           warehouses=query_all("SELECT * FROM warehouses ORDER BY name"))


@app.post("/inventory/transfers/create")
def create_transfer():
    db = get_db()
    try:
        product_id = int(request.form.get("product_id", ""))
        from_id = int(request.form.get("from_warehouse_id", ""))
        to_id = int(request.form.get("to_warehouse_id", ""))
        quantity = int(request.form.get("quantity", ""))
        movement_date = validate_iso_date(request.form.get("movement_date") or date.today().isoformat(), "Transfer date")
        reason = request.form.get("reason", "").strip()
        if from_id == to_id:
            raise ValueError("Source and destination warehouses must be different.")
        if quantity <= 0 or quantity > 1_000_000:
            raise ValueError("Transfer quantity must be between 1 and 1,000,000.")
        if len(reason) < 5 or len(reason) > 240:
            raise ValueError("Enter a transfer reason between 5 and 240 characters.")
        product = query_one("SELECT id,sku,name FROM products WHERE id=?", (product_id,))
        source = query_one("SELECT id,code,name FROM warehouses WHERE id=?", (from_id,))
        dest = query_one("SELECT id,code,name FROM warehouses WHERE id=?", (to_id,))
        if not product or not source or not dest:
            raise ValueError("Choose a valid product and both warehouses.")
        db.execute("BEGIN IMMEDIATE")
        balance = stock_balance(product_id, from_id)
        if quantity > balance:
            raise ValueError(f"Transfer exceeds source stock. {source['name']} has {balance} units.")
        ref = f"TRF-{date.today():%Y%m%d}-{secrets.token_hex(3).upper()}"
        now = datetime.now().isoformat(timespec="seconds")
        db.execute("INSERT INTO stock_transfers(reference,product_id,quantity,from_warehouse_id,to_warehouse_id,movement_date,reason,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                   (ref, product_id, quantity, from_id, to_id, movement_date, reason, g.user["username"], now))
        db.execute("INSERT INTO stock_movements(product_id,warehouse_id,movement_type,quantity,reference,movement_date,created_at) VALUES(?,?,'TRANSFER_OUT',?,?,?,?)",
                   (product_id, from_id, quantity, ref, movement_date, now))
        db.execute("INSERT INTO stock_movements(product_id,warehouse_id,movement_type,quantity,reference,movement_date,created_at) VALUES(?,?,'TRANSFER_IN',?,?,?,?)",
                   (product_id, to_id, quantity, ref, movement_date, now))
        db.commit()
        audit(g.user["username"], "WAREHOUSE_TRANSFER", "stock_transfer", ref,
              f"{product['sku']}; {source['code']} -> {dest['code']}; qty={quantity}; reason={reason}")
        flash(f"Transfer {ref} posted atomically to both warehouse ledgers.", "success")
    except (ValueError, KeyError, sqlite3.IntegrityError) as exc:
        db.rollback()
        flash(f"Transfer not posted: {exc}", "error")
    return redirect(url_for("transfers"))


@app.route("/matching")
def matching():
    rows = query_all("""SELECT vi.*,v.name vendor_name,v.gstin,po.po_number
                        FROM vendor_invoices vi JOIN vendors v ON v.id=vi.vendor_id
                        LEFT JOIN purchase_orders po ON po.id=vi.po_id ORDER BY vi.invoice_date DESC,vi.id DESC""")
    checked = []
    for invoice in rows:
        result = invoice_checks(invoice)
        details = query_all("""SELECT vil.quantity invoice_qty,vil.unit_price invoice_price,vil.taxable_value,vil.gst_amount,
                              vil.gst_rate,p.name product_name,p.sku,
                              pol.quantity ordered_qty,pol.unit_price po_price,
                              COALESCE((SELECT SUM(grl.quantity) FROM goods_receipts gr JOIN goods_receipt_lines grl ON grl.grn_id=gr.id
                                        WHERE gr.po_id=? AND grl.product_id=vil.product_id),0) received_qty
                              FROM vendor_invoice_lines vil JOIN products p ON p.id=vil.product_id
                              LEFT JOIN purchase_order_lines pol ON pol.po_id=? AND pol.product_id=vil.product_id
                              WHERE vil.invoice_id=? ORDER BY vil.id""", (invoice["po_id"], invoice["po_id"], invoice["id"])) if invoice["po_id"] else []
        checked.append({"invoice": invoice, "checks": result, "lines": details})
    stats = {"total": len(checked), "passed": sum(1 for x in checked if x["checks"]["match"]),
             "exceptions": sum(1 for x in checked if not x["checks"]["match"])}
    return render_template("matching.html", title="Three-way matching", checked=checked, stats=stats)


@app.route("/gst-center", methods=["GET"])
def gst_center():
    statement = query_all("SELECT * FROM itc_statement_rows ORDER BY source_period DESC,supplier_gstin,invoice_number")
    counts = query_one("SELECT COUNT(*) n FROM itc_statement_rows")
    statuses = query_all("SELECT reconciliation_status,COUNT(*) n,COALESCE(SUM(total_tax),0) tax FROM itc_statement_rows GROUP BY reconciliation_status")
    book_only = query_all("""SELECT vi.invoice_number,vi.invoice_date,vi.taxable_value,vi.gst_amount,v.name vendor_name,v.gstin
                           FROM vendor_invoices vi JOIN vendors v ON v.id=vi.vendor_id
                           WHERE EXISTS (SELECT 1 FROM itc_statement_rows s WHERE s.source_period=substr(vi.invoice_date,1,7))
                             AND NOT EXISTS (SELECT 1 FROM itc_statement_rows s WHERE s.source_period=substr(vi.invoice_date,1,7)
                                             AND UPPER(REPLACE(s.invoice_number,' ',''))=UPPER(REPLACE(vi.invoice_number,' ',''))
                                             AND UPPER(s.supplier_gstin)=UPPER(v.gstin))
                           ORDER BY vi.invoice_date DESC,vi.invoice_number LIMIT 200""")
    return render_template("gst_center.html", title="GST & ITC reconciliation", statement=statement,
                           statement_count=counts["n"], statuses=statuses, book_only=book_only,
                           recent_invoices=query_all("""SELECT si.*,c.name customer_name,
                                                       (SELECT COUNT(*) FROM sales_invoice_lines sl WHERE sl.invoice_id=si.id) line_count
                                                      FROM sales_invoices si JOIN customers c ON c.id=si.customer_id ORDER BY si.invoice_date DESC LIMIT 20"""))


def _reconcile_itc_row(row_id):
    db = get_db()
    statement = query_one("SELECT * FROM itc_statement_rows WHERE id=?", (row_id,))
    if not statement:
        return
    statement_duplicates = query_one("""SELECT COUNT(*) n FROM itc_statement_rows
                                      WHERE UPPER(REPLACE(invoice_number,' ',''))=UPPER(REPLACE(?, ' ',''))
                                        AND UPPER(supplier_gstin)=UPPER(?)""",
                                     (statement["invoice_number"], statement["supplier_gstin"]))["n"]
    matches = query_all("""SELECT vi.* FROM vendor_invoices vi JOIN vendors v ON v.id=vi.vendor_id
                           WHERE UPPER(REPLACE(vi.invoice_number,' ',''))=UPPER(REPLACE(?, ' ',''))
                             AND UPPER(v.gstin)=UPPER(?)""", (statement["invoice_number"], statement["supplier_gstin"]))
    duplicate_count = len(matches)
    if int(statement_duplicates or 0) > 1:
        status = "Duplicate statement document"
        notes = "The same supplier GSTIN + invoice number appears in more than one imported period. Check duplicates/amendments before considering ITC."
        # Update every duplicate period row so the earlier import is flagged too.
        db.execute("""UPDATE itc_statement_rows SET reconciliation_status=?,notes=?
                      WHERE UPPER(REPLACE(invoice_number,' ',''))=UPPER(REPLACE(?, ' ',''))
                        AND UPPER(supplier_gstin)=UPPER(?)""",
                   (status, notes, statement["invoice_number"], statement["supplier_gstin"]))
        return
    elif duplicate_count == 0:
        status, notes = "Missing in books", "Statement invoice has no matching vendor GSTIN + invoice number in books."
    elif duplicate_count > 1:
        status, notes = "Duplicate book record", "Multiple book invoices match this supplier document. Review duplicates before considering ITC."
    else:
        invoice = matches[0]
        book_tax = float(invoice["gst_amount"])
        tax_variance = abs(book_tax - float(statement["total_tax"]))
        taxable_variance = abs(float(invoice["taxable_value"]) - float(statement["taxable_value"]))
        igst_variance = abs(float(invoice["igst_amount"] or 0) - float(statement["igst_amount"]))
        cgst_variance = abs(float(invoice["cgst_amount"] or 0) - float(statement["cgst_amount"]))
        sgst_variance = abs(float(invoice["sgst_amount"] or 0) - float(statement["sgst_amount"]))
        if tax_variance > 1 or taxable_variance > 1 or max(igst_variance, cgst_variance, sgst_variance) > 1:
            status = "Value mismatch"
            notes = (f"Taxable variance ₹{taxable_variance:.2f}; total-tax variance ₹{tax_variance:.2f}; "
                     f"IGST variance ₹{igst_variance:.2f}; CGST variance ₹{cgst_variance:.2f}; SGST variance ₹{sgst_variance:.2f}.")
        elif statement["eligibility"].lower() in {"no", "ineligible", "not available"}:
            status = "Not available in statement"
            notes = "Statement indicates ITC is not available; accountant review required."
        else:
            status = "Matched - review eligibility"
            notes = "GSTIN, invoice number, taxable value and tax total matched; this is not a legal ITC eligibility determination."
    db.execute("UPDATE itc_statement_rows SET reconciliation_status=?,notes=? WHERE id=?", (status, notes, row_id))


@app.post("/gst-center/import")
def import_itc_statement():
    uploaded = request.files.get("statement_file")
    period = request.form.get("source_period", "").strip()
    try:
        if not uploaded or not uploaded.filename.lower().endswith(".csv"):
            raise ValueError("Upload a CSV export of GSTR-2B/accounting statement data.")
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", period):
            raise ValueError("Source period must use YYYY-MM format.")
        raw = uploaded.stream.read(5 * 1024 * 1024 + 1)
        if len(raw) > 5 * 1024 * 1024:
            raise ValueError("CSV file must be 5 MB or smaller.")
        text = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        required = {"supplier_gstin", "invoice_number", "taxable_value", "igst_amount", "cgst_amount", "sgst_amount"}
        headers = {h.strip().lower() for h in (reader.fieldnames or [])}
        if not required.issubset(headers):
            raise ValueError("CSV must include supplier_gstin, invoice_number, taxable_value, igst_amount, cgst_amount and sgst_amount columns. Optional: invoice_date, eligibility.")
        norm_rows = []
        seen_documents = set()
        for index, src in enumerate(reader, start=2):
            src = {(k or "").strip().lower(): (v or "").strip() for k, v in src.items()}
            gstin = src.get("supplier_gstin", "").upper()
            invno = src.get("invoice_number", "")
            if not gstin or not re.fullmatch(r"[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]", gstin):
                raise ValueError(f"Row {index}: supplier_gstin is missing or invalid.")
            if not invno or len(invno) > 120:
                raise ValueError(f"Row {index}: invoice_number is required (max 120 characters).")
            doc_key = (gstin, invno.strip().upper().replace(" ", ""))
            if doc_key in seen_documents:
                raise ValueError(f"Row {index}: duplicate supplier GSTIN + invoice number in this CSV; resolve the duplicate before importing.")
            seen_documents.add(doc_key)
            inv_date = validate_iso_date(src.get("invoice_date", ""), f"Row {index} invoice_date", optional=True)
            taxable_val = parse_nonnegative_money(src.get("taxable_value", "0"), f"Row {index} taxable_value")
            igst = parse_nonnegative_money(src.get("igst_amount", "0"), f"Row {index} igst_amount")
            cgst = parse_nonnegative_money(src.get("cgst_amount", "0"), f"Row {index} cgst_amount")
            sgst = parse_nonnegative_money(src.get("sgst_amount", "0"), f"Row {index} sgst_amount")
            eligibility = (src.get("eligibility", "Unknown") or "Unknown").strip()
            if eligibility.lower() not in {"yes", "no", "eligible", "ineligible", "available", "not available", "unknown"}:
                raise ValueError(f"Row {index}: eligibility must be Yes, No, Eligible, Ineligible or Unknown.")
            norm_rows.append((period, gstin, invno, inv_date, taxable_val, igst, cgst, sgst, money(igst+cgst+sgst), eligibility))
        if not norm_rows:
            raise ValueError("The uploaded CSV contains no data rows.")
        db = get_db()
        db.execute("BEGIN IMMEDIATE")
        inserted = 0
        for row in norm_rows:
            db.execute("""INSERT INTO itc_statement_rows(source_period,supplier_gstin,invoice_number,invoice_date,taxable_value,igst_amount,cgst_amount,sgst_amount,total_tax,eligibility,reconciliation_status,notes,imported_by,imported_at)
                         VALUES(?,?,?,?,?,?,?,?,?,?,'Unmatched','',?,?)
                         ON CONFLICT(source_period,supplier_gstin,invoice_number) DO UPDATE SET invoice_date=excluded.invoice_date,taxable_value=excluded.taxable_value,igst_amount=excluded.igst_amount,cgst_amount=excluded.cgst_amount,sgst_amount=excluded.sgst_amount,total_tax=excluded.total_tax,eligibility=excluded.eligibility,imported_by=excluded.imported_by,imported_at=excluded.imported_at,reconciliation_status='Unmatched',notes=''""",
                       (*row, g.user["username"], datetime.now().isoformat(timespec="seconds")))
            rec = query_one("SELECT id FROM itc_statement_rows WHERE source_period=? AND supplier_gstin=? AND invoice_number=?", (period, row[1], row[2]))
            _reconcile_itc_row(rec["id"])
            inserted += 1
        db.commit()
        audit(g.user["username"], "IMPORT_ITC_STATEMENT", "itc_statement", None, f"period={period}; rows={inserted}; filename={uploaded.filename[:120]}")
        flash(f"Imported and reconciled {inserted} statement rows for {period}.", "success")
    except (UnicodeDecodeError, csv.Error, ValueError, KeyError, sqlite3.IntegrityError) as exc:
        get_db().rollback()
        flash(f"ITC statement not imported: {exc}", "error")
    return redirect(url_for("gst_center"))


@app.route("/payables", methods=["GET"])
def payables():
    rows = query_all("""SELECT vi.*,v.name vendor_name,v.gstin,po.po_number,
                       ROUND(vi.total_amount-COALESCE(vi.paid_amount,0),2) outstanding,
                       CASE WHEN vi.due_date IS NOT NULL AND date(vi.due_date)<date('now') AND vi.total_amount>COALESCE(vi.paid_amount,0) THEN 1 ELSE 0 END overdue
                       FROM vendor_invoices vi JOIN vendors v ON v.id=vi.vendor_id LEFT JOIN purchase_orders po ON po.id=vi.po_id
                       ORDER BY CASE WHEN vi.due_date IS NULL THEN 1 ELSE 0 END,vi.due_date,vi.invoice_date""")
    totals = query_one("SELECT COALESCE(SUM(total_amount-COALESCE(paid_amount,0)),0) outstanding FROM vendor_invoices")
    payments = query_all("""SELECT vp.*,vi.invoice_number,v.name vendor_name FROM vendor_payments vp
                           JOIN vendor_invoices vi ON vi.id=vp.invoice_id JOIN vendors v ON v.id=vi.vendor_id
                           ORDER BY vp.payment_date DESC,vp.id DESC LIMIT 50""")
    return render_template("payables.html", title="Accounts payable", invoices=rows, totals=totals["outstanding"],
                           payments=payments, payment_invoices=query_all("SELECT vi.id,vi.invoice_number,v.name vendor_name,vi.total_amount-vi.paid_amount outstanding FROM vendor_invoices vi JOIN vendors v ON v.id=vi.vendor_id WHERE vi.total_amount>vi.paid_amount ORDER BY vi.due_date"))


@app.post("/payables/payment")
def record_vendor_payment():
    db = get_db()
    try:
        invoice_id = int(request.form.get("invoice_id", ""))
        amount = parse_nonnegative_money(request.form.get("amount", ""), "Payment amount")
        if amount <= 0:
            raise ValueError("Payment amount must be greater than zero.")
        payment_date = validate_iso_date(request.form.get("payment_date") or date.today().isoformat(), "Payment date")
        reference = request.form.get("reference", "").strip()
        notes = request.form.get("notes", "").strip()[:500]
        if not reference or len(reference) > 120:
            raise ValueError("Enter a payment reference up to 120 characters.")
        db.execute("BEGIN IMMEDIATE")
        invoice = query_one("SELECT * FROM vendor_invoices WHERE id=?", (invoice_id,))
        if not invoice:
            raise ValueError("Choose a valid vendor invoice.")
        if invoice["status"] != "Approved":
            raise ValueError("Payment is blocked until the invoice is approved after exception review.")
        current_checks = invoice_checks(invoice)
        if not current_checks["match"]:
            raise ValueError("Payment is blocked because three-way matching still has exceptions: " + "; ".join(current_checks["issues"][:3]))
        outstanding = money(float(invoice["total_amount"]) - float(invoice["paid_amount"] or 0))
        if amount > outstanding + 0.01:
            raise ValueError(f"Payment exceeds outstanding amount ₹{outstanding:,.2f}.")
        now = datetime.now().isoformat(timespec="seconds")
        db.execute("INSERT INTO vendor_payments(invoice_id,payment_date,amount,reference,notes,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                   (invoice_id, payment_date, amount, reference, notes, g.user["username"], now))
        paid = money(float(invoice["paid_amount"] or 0) + amount)
        db.execute("UPDATE vendor_invoices SET paid_amount=? WHERE id=?", (paid, invoice_id))
        db.commit()
        audit(g.user["username"], "VENDOR_PAYMENT", "vendor_invoice", invoice_id, f"{invoice['invoice_number']}; amount={amount:.2f}; ref={reference}")
        flash(f"Payment recorded. Remaining balance: ₹{money(float(invoice['total_amount'])-paid):,.2f}.", "success")
    except (ValueError, KeyError, sqlite3.IntegrityError) as exc:
        db.rollback()
        flash(f"Payment not recorded: {exc}", "error")
    return redirect(url_for("payables"))


@app.route("/receivables", methods=["GET"])
def receivables():
    customers = query_all("SELECT * FROM customers WHERE active=1 ORDER BY name")
    invoices = query_all("""SELECT si.*,c.name customer_name,c.gstin,c.state,
                           ROUND(si.total_amount-si.paid_amount,2) outstanding,
                           CASE WHEN date(si.due_date)<date('now') AND si.total_amount>si.paid_amount THEN 1 ELSE 0 END overdue,
                           CASE WHEN tr.invoice_id IS NULL THEN 0 ELSE 1 END has_transport,
                           (SELECT COUNT(*) FROM sales_invoice_lines sl WHERE sl.invoice_id=si.id) line_count
                           FROM sales_invoices si JOIN customers c ON c.id=si.customer_id
                           LEFT JOIN sales_invoice_transport tr ON tr.invoice_id=si.id
                           ORDER BY si.due_date,si.invoice_date DESC""")
    totals = query_one("SELECT COALESCE(SUM(total_amount-paid_amount),0) outstanding FROM sales_invoices")
    payments = query_all("""SELECT cp.*,si.invoice_number,c.name customer_name FROM customer_payments cp
                           JOIN sales_invoices si ON si.id=cp.invoice_id JOIN customers c ON c.id=si.customer_id
                           ORDER BY cp.payment_date DESC,cp.id DESC LIMIT 50""")
    products = query_all("SELECT id,sku,name,hsn,gst_rate,unit,unit_cost FROM products ORDER BY name")
    warehouses = query_all("SELECT id,code,name,city FROM warehouses ORDER BY name")
    transport_invoices = query_all("""SELECT si.id,si.invoice_number,c.name customer_name FROM sales_invoices si
                                    JOIN customers c ON c.id=si.customer_id
                                    WHERE EXISTS (SELECT 1 FROM sales_invoice_lines sl WHERE sl.invoice_id=si.id)
                                    ORDER BY si.invoice_date DESC""")
    company_row = query_one("SELECT value FROM company_profile WHERE key='company_state'")
    company_state = os.environ.get("COMPANY_STATE", company_row["value"] if company_row else "Karnataka")
    return render_template("receivables.html", title="Accounts receivable", customers=customers, invoices=invoices,
                           totals=totals["outstanding"], payments=payments, products=products, warehouses=warehouses,
                           company_state=company_state,
                           transport_invoices=transport_invoices,
                           payment_invoices=query_all("SELECT si.id,si.invoice_number,c.name customer_name,si.total_amount-si.paid_amount outstanding FROM sales_invoices si JOIN customers c ON c.id=si.customer_id WHERE si.total_amount>si.paid_amount ORDER BY si.due_date"))


@app.post("/receivables/invoice/create")
def create_sales_invoice():
    db = get_db()
    try:
        customer_id = int(request.form.get("customer_id", ""))
        invoice_number = request.form.get("invoice_number", "").strip()
        invoice_date = validate_iso_date(request.form.get("invoice_date") or date.today().isoformat(), "Invoice date")
        customer = query_one("SELECT * FROM customers WHERE id=? AND active=1", (customer_id,))
        if not customer:
            raise ValueError("Choose an active customer.")
        if not invoice_number or len(invoice_number) > 120:
            raise ValueError("Enter a sales invoice number up to 120 characters.")
        if query_one("SELECT 1 FROM sales_invoices WHERE invoice_number=?", (invoice_number,)):
            raise ValueError("That sales invoice number already exists.")

        product_ids = request.form.getlist("line_product_id")
        warehouse_ids = request.form.getlist("line_warehouse_id")
        quantities = request.form.getlist("line_quantity")
        prices = request.form.getlist("line_unit_price")
        raw_len = max([len(product_ids), len(warehouse_ids), len(quantities), len(prices)], default=0)
        if raw_len == 0 or raw_len > 20:
            raise ValueError("Add between 1 and 20 sales invoice lines.")
        selected_lines = []
        seen = set()
        company_row = query_one("SELECT value FROM company_profile WHERE key='company_state'")
        company_state = os.environ.get("COMPANY_STATE", company_row["value"] if company_row else "Karnataka").strip().lower()
        intrastate = customer["state"].strip().lower() == company_state
        for idx in range(raw_len):
            pid_raw = product_ids[idx].strip() if idx < len(product_ids) else ""
            warehouse_raw = warehouse_ids[idx].strip() if idx < len(warehouse_ids) else ""
            qty_raw = quantities[idx].strip() if idx < len(quantities) else ""
            price_raw = prices[idx].strip() if idx < len(prices) else ""
            if not pid_raw and not warehouse_raw and not qty_raw and not price_raw:
                continue
            if not pid_raw or not warehouse_raw or not qty_raw or not price_raw:
                raise ValueError(f"Sales line {idx+1} needs a product, dispatch warehouse, quantity and unit price.")
            product_id = int(pid_raw)
            warehouse_id = int(warehouse_raw)
            warehouse = query_one("SELECT id,name,code FROM warehouses WHERE id=?", (warehouse_id,))
            if not warehouse:
                raise ValueError(f"Sales line {idx+1} references an unknown dispatch warehouse.")
            quantity = int(qty_raw)
            unit_price = parse_nonnegative_money(price_raw, f"Sales line {idx+1} unit price")
            if quantity < 1 or quantity > 1_000_000:
                raise ValueError(f"Sales line {idx+1} quantity must be between 1 and 1,000,000.")
            if product_id in seen:
                raise ValueError("Each product may appear once per sales invoice. Combine duplicate products into one line.")
            seen.add(product_id)
            product = query_one("SELECT id,sku,name,hsn,unit,gst_rate FROM products WHERE id=?", (product_id,))
            if not product:
                raise ValueError(f"Sales line {idx+1} references a product that no longer exists.")
            gst_rate = float(product["gst_rate"])
            if gst_rate not in {0, 5, 12, 18, 28, 40}:
                raise ValueError(f"Product {product['sku']} has an unsupported GST rate; correct the product master first.")
            taxable_value = money(quantity * unit_price)
            gst_amount = money(taxable_value * gst_rate / 100.0)
            cgst_amount = money(gst_amount / 2) if intrastate else 0.0
            sgst_amount = money(gst_amount - cgst_amount) if intrastate else 0.0
            igst_amount = 0.0 if intrastate else gst_amount
            line_total = money(taxable_value + gst_amount)
            selected_lines.append({"product": product, "warehouse": warehouse, "quantity": quantity, "unit_price": unit_price,
                                   "taxable_value": taxable_value, "gst_rate": gst_rate,
                                   "igst_amount": igst_amount, "cgst_amount": cgst_amount,
                                   "sgst_amount": sgst_amount, "gst_amount": gst_amount,
                                   "total_amount": line_total})
        if not selected_lines:
            raise ValueError("Add at least one non-empty sales invoice line.")

        taxable = money(sum(x["taxable_value"] for x in selected_lines))
        igst = money(sum(x["igst_amount"] for x in selected_lines))
        cgst = money(sum(x["cgst_amount"] for x in selected_lines))
        sgst = money(sum(x["sgst_amount"] for x in selected_lines))
        tax = money(igst + cgst + sgst)
        total = money(taxable + tax)
        due_date = validate_iso_date(request.form.get("due_date") or "", "Due date", optional=True)
        if not due_date:
            due_date = (date.fromisoformat(invoice_date) + timedelta(days=int(customer["payment_terms"] or 30))).isoformat()
        if due_date < invoice_date:
            raise ValueError("Due date cannot be before the invoice date.")
        notes = request.form.get("notes", "").strip()[:1000]
        now = datetime.now().isoformat(timespec="seconds")
        db.execute("BEGIN IMMEDIATE")
        if query_one("SELECT 1 FROM sales_invoices WHERE invoice_number=?", (invoice_number,)):
            raise ValueError("That sales invoice number was just created by another request; refresh and choose another number.")
        for line in selected_lines:
            available_stock = stock_balance(line["product"]["id"], line["warehouse"]["id"])
            if line["quantity"] > available_stock:
                raise ValueError(f"Insufficient stock for {line['product']['sku']} at {line['warehouse']['name']}: available {available_stock}, requested {line['quantity']}.")
        db.execute("""INSERT INTO sales_invoices(invoice_number,customer_id,invoice_date,due_date,taxable_value,igst_amount,cgst_amount,sgst_amount,gst_amount,total_amount,paid_amount,status,notes,created_by,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,0,'Open',?,?,?)""",
                   (invoice_number, customer_id, invoice_date, due_date, taxable, igst, cgst, sgst, tax, total,
                    notes, g.user["username"], now))
        invoice_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        for line in selected_lines:
            product = line["product"]
            db.execute("""INSERT INTO sales_invoice_lines(invoice_id,product_id,warehouse_id,product_sku,product_name,hsn,unit,quantity,unit_price,taxable_value,gst_rate,igst_amount,cgst_amount,sgst_amount,gst_amount,total_amount)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (invoice_id, product["id"], line["warehouse"]["id"], product["sku"], product["name"], product["hsn"], product["unit"],
                        line["quantity"], line["unit_price"], line["taxable_value"], line["gst_rate"], line["igst_amount"],
                        line["cgst_amount"], line["sgst_amount"], line["gst_amount"], line["total_amount"]))
            db.execute("INSERT INTO stock_movements(product_id,warehouse_id,movement_type,quantity,reference,movement_date,created_at) VALUES(?,?,'OUT',?,?,?,?)",
                       (product["id"], line["warehouse"]["id"], line["quantity"], f"SHIP-{invoice_number}", invoice_date, now))
        db.commit()
        audit(g.user["username"], "CREATE", "sales_invoice", invoice_id,
              f"{invoice_number}; lines={len(selected_lines)}; taxable={taxable:.2f}; IGST={igst:.2f}; CGST={cgst:.2f}; SGST={sgst:.2f}; total={total:.2f}")
        flash(f"Sales invoice {invoice_number} created with {len(selected_lines)} line(s). GST split: IGST ₹{igst:.2f}, CGST ₹{cgst:.2f}, SGST ₹{sgst:.2f}.", "success")
    except (ValueError, KeyError, sqlite3.IntegrityError) as exc:
        db.rollback()
        flash(f"Sales invoice not created: {exc}", "error")
    return redirect(url_for("receivables"))


@app.post("/receivables/transport")
def save_transport_details():
    db = get_db()
    try:
        invoice_id = int(request.form.get("invoice_id", ""))
        invoice = query_one("SELECT id,invoice_number FROM sales_invoices WHERE id=?", (invoice_id,))
        if not invoice:
            raise ValueError("Choose a valid sales invoice.")
        if not query_one("SELECT id FROM sales_invoice_lines WHERE invoice_id=? LIMIT 1", (invoice_id,)):
            raise ValueError("Transport details can only be saved for a line-level invoice.")
        mode = request.form.get("trans_mode", "1")
        if mode not in {"1", "2", "3", "4"}:
            raise ValueError("Choose a supported transport mode: road, rail, air or ship.")
        distance_raw = request.form.get("trans_distance", "0").strip()
        try:
            distance_value = int(distance_raw)
        except ValueError:
            raise ValueError("Transport distance must be a whole number of kilometres.") from None
        if not 0 <= distance_value <= 9999:
            raise ValueError("Transport distance must be from 0 to 9,999 kilometres.")
        vehicle_no = request.form.get("vehicle_no", "").strip().upper()
        vehicle_type = request.form.get("vehicle_type", "R").strip().upper()
        transporter_id = request.form.get("transporter_id", "").strip().upper()
        transporter_name = request.form.get("transporter_name", "").strip()
        doc_no = request.form.get("transport_doc_no", "").strip().upper()
        doc_date = validate_iso_date(request.form.get("transport_doc_date", ""), "Transport document date", optional=True)
        if mode == "1" and not re.fullmatch(r"[A-Z]{2}[0-9]{1,2}[A-Z]{0,3}[0-9]{1,4}", vehicle_no):
            raise ValueError("Road transport requires a valid-looking vehicle registration, e.g. KA09AB1234.")
        if vehicle_type not in {"R", "O"}:
            raise ValueError("Vehicle type must be R (regular) or O (over-dimensional cargo).")
        if mode in {"2", "3", "4"} and (not doc_no or not doc_date):
            raise ValueError("Rail, air or ship transport requires a transport document number and date.")
        if transporter_id and not re.fullmatch(r"[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]", transporter_id):
            raise ValueError("Transporter ID must have the expected GSTIN structure, or be blank.")
        if len(transporter_name) > 100 or len(doc_no) > 50:
            raise ValueError("Transporter name or document number is too long.")
        now = datetime.now().isoformat(timespec="seconds")
        db.execute("""INSERT INTO sales_invoice_transport(invoice_id,trans_mode,trans_distance,vehicle_no,vehicle_type,transporter_id,transporter_name,transport_doc_no,transport_doc_date,updated_by,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(invoice_id) DO UPDATE SET trans_mode=excluded.trans_mode,trans_distance=excluded.trans_distance,vehicle_no=excluded.vehicle_no,vehicle_type=excluded.vehicle_type,transporter_id=excluded.transporter_id,transporter_name=excluded.transporter_name,transport_doc_no=excluded.transport_doc_no,transport_doc_date=excluded.transport_doc_date,updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
                   (invoice_id, mode, str(distance_value), vehicle_no, vehicle_type, transporter_id, transporter_name, doc_no, doc_date, g.user["username"], now))
        db.commit()
        audit(g.user["username"], "UPDATE_TRANSPORT_DETAILS", "sales_invoice", invoice_id,
              f"{invoice['invoice_number']}; mode={mode}; distance_km={distance_value}; vehicle={vehicle_no or 'not applicable'}; transport_doc={doc_no or 'not applicable'}")
        flash(f"Transport details saved for {invoice['invoice_number']}. E-way-bill simulation can now be prepared.", "success")
    except (ValueError, KeyError, sqlite3.IntegrityError) as exc:
        db.rollback()
        flash(f"Transport details not saved: {exc}", "error")
    return redirect(url_for("receivables"))


@app.post("/receivables/payment")
def record_customer_payment():
    db = get_db()
    try:
        invoice_id = int(request.form.get("invoice_id", ""))
        amount = parse_nonnegative_money(request.form.get("amount", ""), "Receipt amount")
        if amount <= 0:
            raise ValueError("Receipt amount must be greater than zero.")
        payment_date = validate_iso_date(request.form.get("payment_date") or date.today().isoformat(), "Receipt date")
        reference = request.form.get("reference", "").strip()
        if not reference or len(reference) > 120:
            raise ValueError("Enter a payment reference up to 120 characters.")
        db.execute("BEGIN IMMEDIATE")
        invoice = query_one("SELECT * FROM sales_invoices WHERE id=?", (invoice_id,))
        if not invoice:
            raise ValueError("Choose a valid customer invoice.")
        outstanding = money(float(invoice["total_amount"]) - float(invoice["paid_amount"] or 0))
        if amount > outstanding + 0.001:
            raise ValueError(f"Receipt exceeds outstanding amount ₹{outstanding:,.2f}.")
        now = datetime.now().isoformat(timespec="seconds")
        db.execute("INSERT INTO customer_payments(invoice_id,payment_date,amount,reference,notes,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                   (invoice_id, payment_date, amount, reference, request.form.get("notes", "").strip()[:500], g.user["username"], now))
        paid = money(float(invoice["paid_amount"] or 0) + amount)
        db.execute("UPDATE sales_invoices SET paid_amount=?,status=? WHERE id=?",
                   (paid, "Paid" if paid + 0.005 >= float(invoice["total_amount"]) else "Partially paid", invoice_id))
        db.commit()
        audit(g.user["username"], "CUSTOMER_PAYMENT", "sales_invoice", invoice_id, f"{invoice['invoice_number']}; amount={amount:.2f}; ref={reference}")
        flash(f"Customer receipt recorded. Remaining balance: ₹{money(float(invoice['total_amount'])-paid):,.2f}.", "success")
    except (ValueError, KeyError, sqlite3.IntegrityError) as exc:
        db.rollback()
        flash(f"Receipt not recorded: {exc}", "error")
    return redirect(url_for("receivables"))


STATE_CODE_BY_NAME = {
    "andhra pradesh": "37", "arunachal pradesh": "12", "assam": "18", "bihar": "10",
    "chhattisgarh": "22", "goa": "30", "gujarat": "24", "haryana": "06", "himachal pradesh": "02",
    "jharkhand": "20", "karnataka": "29", "kerala": "32", "madhya pradesh": "23", "maharashtra": "27",
    "manipur": "14", "meghalaya": "17", "mizoram": "15", "nagaland": "13", "odisha": "21",
    "punjab": "03", "rajasthan": "08", "sikkim": "11", "tamil nadu": "33", "telangana": "36",
    "tripura": "16", "uttar pradesh": "09", "uttarakhand": "05", "west bengal": "19", "delhi": "07",
    "jammu and kashmir": "01", "ladakh": "38", "puducherry": "34", "chandigarh": "04",
}


def _state_code(state_name, gstin=""):
    gstin = (gstin or "").strip().upper()
    if len(gstin) >= 2 and gstin[:2].isdigit():
        return gstin[:2]
    return STATE_CODE_BY_NAME.get((state_name or "").strip().lower(), "29")


def _gst_unit_code(unit):
    return {"pcs": "PCS", "kg": "KGS", "g": "GMS", "m": "MTR", "l": "LTR", "box": "BOX", "set": "SET"}.get((unit or "pcs").lower(), "OTH")


@app.route("/gst/export/<kind>/<int:invoice_id>.json")
def gst_export(kind, invoice_id):
    if kind not in {"einvoice", "ewaybill"}:
        return jsonify({"error": "Unknown GST simulation export type"}), 404
    invoice = query_one("""SELECT si.*,c.name customer_name,c.gstin customer_gstin,c.state customer_state,c.address customer_address,c.pincode customer_pincode
                           FROM sales_invoices si JOIN customers c ON c.id=si.customer_id WHERE si.id=?""", (invoice_id,))
    if not invoice:
        return jsonify({"error": "Sales invoice not found"}), 404
    lines = query_all("SELECT * FROM sales_invoice_lines WHERE invoice_id=? ORDER BY id", (invoice_id,))
    if not lines:
        return jsonify({"error": "This legacy summary-only invoice has no saved product/HSN line details. Create a new line-level invoice before exporting statutory-format simulation data."}), 422
    profile = {row["key"]: row["value"] for row in query_all("SELECT key,value FROM company_profile")}
    company_gstin = os.environ.get("COMPANY_GSTIN", profile.get("company_gstin", "")).strip().upper()
    company_name = os.environ.get("COMPANY_NAME", profile.get("company_name", "")).strip()
    company_state = os.environ.get("COMPANY_STATE", profile.get("company_state", "Karnataka")).strip()
    company_address = os.environ.get("COMPANY_ADDRESS", profile.get("company_address", "")).strip()
    company_pincode = os.environ.get("COMPANY_PINCODE", profile.get("company_pincode", "")).strip()
    if not re.fullmatch(r"[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]", company_gstin):
        return jsonify({"error": "Configure a structurally valid company GSTIN before preparing the GST payload."}), 422
    if not re.fullmatch(r"[0-9]{6}", company_pincode):
        return jsonify({"error": "Configure a six-digit company dispatch PIN code before preparing the GST payload."}), 422
    buyer_gstin = (invoice["customer_gstin"] or "").strip().upper()
    buyer_pin = (invoice["customer_pincode"] or "").strip()
    if not re.fullmatch(r"[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]", buyer_gstin):
        return jsonify({"error": "The selected customer GSTIN does not match the expected structure; correct customer master data."}), 422
    if not re.fullmatch(r"[0-9]{6}", buyer_pin):
        return jsonify({"error": "The selected customer must have a six-digit PIN code before export."}), 422

    seller_state_code = _state_code(company_state, company_gstin)
    buyer_state_code = _state_code(invoice["customer_state"], buyer_gstin)
    item_rows = []
    for number, line in enumerate(lines, start=1):
        taxable_amount = float(line["taxable_value"])
        gross_amount = money(float(line["quantity"]) * float(line["unit_price"]))
        item_rows.append({
            "SlNo": str(number), "PrdDesc": line["product_name"], "IsServc": "N", "HsnCd": line["hsn"],
            "Qty": float(line["quantity"]), "FreeQty": 0, "Unit": _gst_unit_code(line["unit"]),
            "UnitPrice": float(line["unit_price"]), "TotAmt": float(gross_amount), "Discount": 0,
            "PreTaxVal": 0, "AssAmt": float(taxable_amount), "GstRt": float(line["gst_rate"]),
            "IgstAmt": float(line["igst_amount"]), "CgstAmt": float(line["cgst_amount"]),
            "SgstAmt": float(line["sgst_amount"]), "CesRt": 0, "CesAmt": 0, "CesNonAdvlAmt": 0,
            "StateCesRt": 0, "StateCesAmt": 0, "StateCesNonAdvlAmt": 0, "OthChrg": 0,
            "TotItemVal": float(line["total_amount"])
        })
    if kind == "einvoice":
        payload = {
            "Version": "1.1",
            "TranDtls": {"TaxSch": "GST", "SupTyp": "B2B", "RegRev": "N", "EcmGstin": None, "IgstOnIntra": "N"},
            "DocDtls": {"Typ": "INV", "No": invoice["invoice_number"], "Dt": datetime.strptime(invoice["invoice_date"], "%Y-%m-%d").strftime("%d/%m/%Y")},
            "SellerDtls": {"Gstin": company_gstin, "LglNm": company_name, "TrdNm": company_name,
                           "Addr1": company_address[:100] or "Not configured", "Addr2": "", "Loc": "Mysuru",
                           "Pin": int(company_pincode), "Stcd": seller_state_code,
                           **({"Ph": profile["company_phone"]} if profile.get("company_phone") else {}),
                           **({"Em": profile["company_email"]} if profile.get("company_email") else {})},
            "BuyerDtls": {"Gstin": buyer_gstin, "LglNm": invoice["customer_name"], "TrdNm": invoice["customer_name"],
                           "Pos": buyer_state_code, "Addr1": (invoice["customer_address"] or invoice["customer_name"])[:100],
                           "Addr2": "", "Loc": invoice["customer_state"], "Pin": int(buyer_pin), "Stcd": buyer_state_code},
            "ItemList": item_rows,
            "ValDtls": {"AssVal": float(invoice["taxable_value"]), "CgstVal": float(invoice["cgst_amount"]),
                        "SgstVal": float(invoice["sgst_amount"]), "IgstVal": float(invoice["igst_amount"]),
                        "CesVal": 0, "StCesVal": 0, "Discount": 0, "OthChrg": 0, "RndOffAmt": 0,
                        "TotInvVal": float(invoice["total_amount"])}
        }
        schema_assumption = "GSTN e-invoice INV-01 JSON schema Version 1.1; offline simulation only. Current IRP schema/conditional fields must be revalidated before any production use."
    else:
        transport = query_one("SELECT * FROM sales_invoice_transport WHERE invoice_id=?", (invoice_id,))
        if not transport:
            return jsonify({"error": "Transport details are missing. Save transport mode, distance and vehicle/transport-document details on Accounts receivable before preparing an e-way-bill simulation."}), 422
        mode = transport["trans_mode"]
        if mode == "1" and not transport["vehicle_no"]:
            return jsonify({"error": "Road e-way-bill simulation requires a vehicle registration number."}), 422
        if mode in {"2", "3", "4"} and (not transport["transport_doc_no"] or not transport["transport_doc_date"]):
            return jsonify({"error": "Rail/air/ship e-way-bill simulation requires a transport document number and date."}), 422
        payload = {
            "supplyType": "O", "subSupplyType": "1", "subSupplyDesc": "", "docType": "INV",
            "docNo": invoice["invoice_number"], "docDate": datetime.strptime(invoice["invoice_date"], "%Y-%m-%d").strftime("%d/%m/%Y"),
            "fromGstin": company_gstin, "fromTrdName": company_name, "fromAddr1": company_address[:100] or "Not configured",
            "fromAddr2": "", "fromPlace": "Mysuru", "fromPincode": int(company_pincode), "actFromStateCode": int(seller_state_code), "fromStateCode": int(seller_state_code),
            "toGstin": buyer_gstin, "toTrdName": invoice["customer_name"], "toAddr1": (invoice["customer_address"] or invoice["customer_name"])[:100],
            "toAddr2": "", "toPlace": invoice["customer_state"], "toPincode": int(buyer_pin), "actToStateCode": int(buyer_state_code), "toStateCode": int(buyer_state_code),
            "totalValue": float(invoice["taxable_value"]), "cgstValue": float(invoice["cgst_amount"]), "sgstValue": float(invoice["sgst_amount"]),
            "igstValue": float(invoice["igst_amount"]), "cessValue": 0, "otherValue": 0, "totInvValue": float(invoice["total_amount"]),
            "transMode": mode, "transDistance": str(transport["trans_distance"]), "transporterId": transport["transporter_id"],
            "transporterName": transport["transporter_name"], "transDocNo": transport["transport_doc_no"],
            "transDocDate": datetime.strptime(transport["transport_doc_date"], "%Y-%m-%d").strftime("%d/%m/%Y") if transport["transport_doc_date"] else "",
            "vehicleNo": transport["vehicle_no"], "vehicleType": transport["vehicle_type"], "transactionType": 1,
            "itemList": [{"productName": line["product_name"], "productDesc": line["product_name"], "hsnCode": line["hsn"],
                          "quantity": float(line["quantity"]), "qtyUnit": _gst_unit_code(line["unit"]), "taxableAmount": float(line["taxable_value"]),
                          "cgstRate": float(line["gst_rate"]) / 2 if float(line["cgst_amount"]) else 0,
                          "sgstRate": float(line["gst_rate"]) / 2 if float(line["sgst_amount"]) else 0,
                          "igstRate": float(line["gst_rate"]) if float(line["igst_amount"]) else 0, "cessRate": 0}
                         for line in lines]
        }
        schema_assumption = "GSTN e-Way Bill Generate API payload version 1.03; offline simulation only. Actual applicability, transporter/vehicle validations and current schema must be confirmed before production use."
    response = app.response_class(json.dumps(payload, ensure_ascii=False, indent=2), mimetype="application/json")
    response.headers["Content-Disposition"] = f'attachment; filename="uniwork-{kind}-{invoice["invoice_number"].replace("/", "-")}.json"'
    response.headers["X-UNIWORK-Simulation-Only"] = "true"
    response.headers["X-UNIWORK-Schema-Assumption"] = schema_assumption[:240]
    return response

def export_csv_extended(kind):
    # This endpoint is intentionally registered after the original endpoint name for backward compatibility;
    # only newly introduced data kinds are handled here.
    if kind == "warehouse-stock":
        rows = query_all("""SELECT p.sku,p.name product,w.code warehouse_code,w.name warehouse_name,
            COALESCE(SUM(CASE WHEN sm.movement_type IN ('OPENING','IN','ADJUST_IN','TRANSFER_IN') THEN sm.quantity WHEN sm.movement_type IN ('OUT','ADJUST_OUT','TRANSFER_OUT','CONSUMPTION') THEN -sm.quantity ELSE 0 END),0) stock,p.unit,p.reorder_level
            FROM products p CROSS JOIN warehouses w LEFT JOIN stock_movements sm ON sm.product_id=p.id AND sm.warehouse_id=w.id
            GROUP BY p.id,w.id ORDER BY p.name,w.name""")
    elif kind == "itc-reconciliation":
        rows = query_all("SELECT source_period,supplier_gstin,invoice_number,invoice_date,taxable_value,igst_amount,cgst_amount,sgst_amount,total_tax,eligibility,reconciliation_status,notes FROM itc_statement_rows ORDER BY source_period,invoice_number")
    elif kind == "payables":
        rows = query_all("SELECT vi.invoice_number,v.name vendor,vi.invoice_date,vi.due_date,vi.total_amount,vi.paid_amount,ROUND(vi.total_amount-vi.paid_amount,2) outstanding,vi.status,vi.itc_status FROM vendor_invoices vi JOIN vendors v ON v.id=vi.vendor_id ORDER BY vi.due_date")
    elif kind == "receivables":
        rows = query_all("SELECT si.invoice_number,c.name customer,si.invoice_date,si.due_date,si.total_amount,si.paid_amount,ROUND(si.total_amount-si.paid_amount,2) outstanding,si.status FROM sales_invoices si JOIN customers c ON c.id=si.customer_id ORDER BY si.due_date")
    elif kind == "mismatches":
        out = []
        for row in query_all("SELECT vi.* FROM vendor_invoices vi ORDER BY vi.id"):
            c = invoice_checks(row)
            out.append({"invoice_number": row["invoice_number"], "status": row["status"], "issues": "; ".join(c["issues"]), "itc_status": row["itc_status"]})
        rows = out
    else:
        return None
    if rows and isinstance(rows[0], dict):
        headers = list(rows[0].keys())
        data = [list(r.values()) for r in rows]
    else:
        headers = list(rows[0].keys()) if rows else []
        data = [list(r) for r in rows]
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(headers or ["no_data"])
    writer.writerows(data)
    return Response("\ufeff" + out.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=uniwork-{kind}.csv"})


@app.route("/admin/audit/integrity")
def audit_integrity():
    result = audit_chain_status()
    return render_template("audit_integrity.html", title="Audit integrity check", result=result)


@app.errorhandler(404)
def not_found(_error):
    return render_template("error.html", title="Not found", code=404, message="This page does not exist."), 404


@app.errorhandler(500)
def server_error(_error):
    return render_template("error.html", title="Something went wrong", code=500,
                           message="The request could not be completed. Check the terminal logs."), 500


if __name__ == "__main__":
    with app.app_context():
        init_db()
        seed_db()
    print("\\nUNIWORK ERP is starting. Open http://127.0.0.1:5000\\n")
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "5000")), debug=os.environ.get("FLASK_DEBUG") == "1")
