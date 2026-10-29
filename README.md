# UNIWORK v5 — Complete updated procurement, inventory and GST workflow

UNIWORK is a lightweight Flask + SQLite application designed around one connected operational flow for a small electrical-components manufacturer: vendor → purchase order → goods receipt → warehouse ledger → supplier invoice / three-way match → GST and ITC review → accounts payable and receivable → owner reporting.

It is intended as a low-cost demo/evaluation build, not a certified statutory filing product or a full enterprise ERP.

## Release note

This is the v5 development/evaluation package aligned with the supplied problem statement. See `CHANGELOG_v5.md` and `QA_REVIEW_REPORT.md` for what was updated and for the verification boundaries. The Flask runtime test suite and the challenge ground-truth score have not been completed in the build environment.

## Project structure

```text
UNIWORK/
├── backend/
│   ├── app.py                         # Flask routes, business rules and SQLite schema
│   ├── run.py                         # Local development server
│   ├── requirements.txt               # Flask + encryption dependency
│   ├── requirements-production.txt   # Production WSGI dependency set
│   ├── .env.example                   # Environment variable names (not auto-loaded)
│   ├── data/                          # Demo-data notes and example scenario
│   └── tests/test_app.py              # Automated regression / integration tests
├── frontend/
│   ├── templates/                     # Server-rendered Jinja pages
│   └── static/                        # CSS, JS, assets and sample ITC CSV
├── docs/
│   ├── GST_SCHEMA_ASSUMPTIONS.md      # Schema versions, limits and official references
│   └── USER_WORKFLOWS.md              # Business flow and acceptance checks
├── README.md
└── QA_REVIEW_REPORT.md
```

## Run locally on Windows

Open PowerShell in the extracted project directory:

```powershell
cd backend
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python run.py
```

Open `http://127.0.0.1:5000`. On a completely fresh local database, the demo administrator is `admin` / `Forge@2026!`. **Change this password immediately** from the account/password screen. These are demo-only credentials and must never be used for a public or production system. The sample data uses fictional GSTIN-like identifiers and is not ground truth.

The app creates `backend/instance/uniwork.sqlite3` on first access. Existing databases are not overwritten by demo seeding. A project update does not replace or reset your database automatically. Back up the database before any schema/application upgrade.

## Run tests

After installing dependencies, from `backend/` run:

```powershell
python -m unittest discover -s tests -v
```

The tests use a temporary SQLite database and exercise authentication, module page rendering, procurement/receipt workflows, roles/CSRF, inventory, invoice matching and the outward-invoice/GST simulation paths. Test results must be run on a machine with the declared dependencies installed; static validation alone is not a runtime test.

## Modules included

- **Owner dashboard:** procurement spend, open orders, stock alerts, pending supplier invoices, payable and receivable balances, and recent audit activity.
- **Vendor directory:** 80 synthetic starter records for a walkthrough, supplier profiles, edit/activation and historical links.
- **Purchase orders:** multi-line PO creation and status transitions that do not let users bypass the receipt workflow.
- **Goods receipts (GRN):** PO-linked, line-level receipt capture; accepted quantities post into the selected warehouse ledger.
- **Two-godown inventory:** ledger-derived balances by SKU and location, per-location reorder alerts, controlled stock adjustments, stock transfer and material consumption.
- **Supplier invoices / three-way match:** capture vendor invoices by PO and line; compare invoice SKU/price/tax/quantity against PO and received quantities; explain exceptions before payment.
- **Accounts payable:** due-date and outstanding-balance view, partial vendor payments, and payment blocks for invoices that have not passed automated checks.
- **Accounts receivable / outward sales:** customer invoices with line-level GST snapshots; pick a dispatch godown, validate available quantity and atomically post stock-out movements; record customer receipts and prevent overpayments.
- **GST / ITC reconciliation:** normalized CSV import for statement data, book-only and statement-only views, duplicate and value-variance flags, and reconciliation exports.
- **GST payload simulation:** downloadable e-invoice and e-way-bill-shaped JSON from outward invoice lines, with transport detail prerequisites. No live GST portal/IRP filing is implemented.
- **Security and audit:** password hashes, per-tab token sessions, CSRF validation, role-specific access, restricted supplier bank details encrypted using a deployment secret, and a SHA-256 audit hash chain with an integrity page.
- **Reports and exports:** stock by warehouse, mismatch list, payables, receivables and ITC reconciliation CSVs with dataset-specific permission checks.

## Business rules to understand

- Stock is ledger-derived: opening + receipt + transfer-in + adjustment-in − transfer-out − consumption − adjustment-out − sales issues. Each movement records a reference. Warehouse quantity is checked inside a database write transaction before material consumption, transfer or sales issue is posted.
- Stock valuation is the on-hand quantity multiplied by current product master unit cost. It is **not** FIFO, LIFO, standard costing or moving-average valuation.
- Supplier three-way match is a review aid. It flags missing invoice line detail, SKU/PO differences, unit-price variance, quantity invoiced above GRN, taxable/GST/header total arithmetic, and certain GST-head inconsistencies. Business tolerance is currently ₹1 for PO unit-price variance and ₹0.05 for arithmetic checks.
- ITC reconciliation compares GSTIN + invoice number, taxable value, total tax and the separate IGST/CGST/SGST heads. A "matched" result is not a legal determination of eligibility. GSTN advises reconciling books with GSTR-2B and not claiming duplicate credit; see the official reference in the schema assumptions document.
- GST simulation uses the configured company state and the master/customer/vendor state as a simplified place-of-supply rule. Confirm legal applicability, HSN, GST rate, reverse-charge, e-invoice/e-way-bill requirements and place-of-supply treatment with a qualified GST professional before any business use.
- The CSV import expects the normalized headers documented on the GST Center screen. It is not yet a parser for every native GST portal export ZIP/Excel/JSON layout.
- The GST JSON formats are schema-shaped teaching/evaluation samples. The app does not bundle the complete current official JSON schemas or a full statutory validator; never represent downloaded JSON as a certified e-invoice or e-way bill.
- The app does not include live portal submission, GST return filing, MRP, payroll or fixed assets, matching the stated evaluation boundary.

## Confidentiality and production deployment

Use only fictional test data for evaluation. Do not commit a `.env` file, database, supplier pricing, bank information, real GSTINs or customer records. Before production or any shared deployment:

1. Set a unique `SECRET_KEY` with at least 32 random characters, a unique `ADMIN_PASSWORD` of at least 12 characters, `ADMIN_USERNAME`, `FLASK_ENV=production`, `COOKIE_SECURE=1` and `SHOW_DEMO_CREDENTIALS=0` in the hosting environment. `.env.example` is documentation only; the app does not auto-load it.
2. Generate a separate Fernet key for supplier bank account encryption:

   ```powershell
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   Save the output as `VENDOR_DATA_KEY` in the host's secret store. Store the key separately from the database and keep a controlled, encrypted backup; losing the key means previously encrypted account numbers cannot be decrypted. Never put it in source control.
3. Store SQLite on persistent storage, plan tested backups/restores and understand single-writer limitations. For higher concurrency/availability, move to a managed database and review the data model/transaction semantics first.
4. Use a production WSGI server, HTTPS, an application-specific domain and secure host settings. Example Render start command from repository root:

   ```sh
   cd backend && gunicorn --workers 2 --bind 0.0.0.0:$PORT app:app
   ```

   Install `backend/requirements-production.txt`. The included startup checks reject missing/placeholder production secrets or the published demo admin password.
5. Run the entire test suite, validate all roles with test accounts, review logs, scan dependencies, verify audit backups, and complete the checklist in `docs/USER_WORKFLOWS.md`.
6. Validate the exact e-invoice/e-way-bill payload against official current schemas and validators before any possible integration. Do not enable live GST filing without separate legal, security and integration review.

## Synthetic data vs. challenge data

A fresh database is seeded with 80 vendors, 12 SKUs, two godowns, 90 purchase orders over about six months, receipts, intentionally varied supplier invoice cases and sample outward invoices/ITC rows. This is only a demonstration fixture. **The actual challenge ground-truth datasets were not supplied with this chat, so no stock accuracy, mismatch catch-rate or ITC accuracy score is claimed.** To evaluate against the problem's rubric, load the actual supplied input files and compare results against their ground truth.

## Publish and deploy

- `render.yaml` contains a Render Blueprint for a demonstration deployment.
- `SUBMISSION_WRITEUP.md` contains the ready-to-paste “How it works & technical architecture” submission field.
- `docs/GITHUB_RENDER_DEPLOYMENT.md` explains GitHub push, Render deployment, release asset upload, environment secrets and final submission checks.
- `RELEASE_NOTES_v5.0.0.md` is suitable for the GitHub Release notes.

**Persistence note:** The sample Render Blueprint uses a free demonstration service. Render free instances have ephemeral filesystems, so SQLite changes may be lost on restart/redeploy/spin-down. Use persistent storage for a demo that must retain user transactions.

## Documentation

- `docs/USER_WORKFLOWS.md` — end-to-end workflows and release acceptance checklist.
- `docs/GST_SCHEMA_ASSUMPTIONS.md` — current schema assumptions, official source links and explicit limits.
- `QA_REVIEW_REPORT.md` — project validation and unresolved test boundaries.
