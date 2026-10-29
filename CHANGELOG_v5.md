# UNIWORK v5 — Problem-statement alignment pass

**Date:** 29 October 2026

This release moves UNIWORK toward the stated small-manufacturer workflow, going beyond the earlier page-level dashboard/inventory fixes. It remains an evaluation/development build until the full runtime suite and ground-truth evaluation are completed.

## Functional updates

- Seeds a deterministic, explicitly synthetic demo scenario with 80 vendor records, 12 product SKUs, two godowns, 90 purchase orders over approximately six months, GRNs, supplier invoice cases, outward invoices and a small normalized ITC statement slice.
- Provides PO-linked receiving with remaining-quantity checks and stock-ledger postings.
- Adds per-godown stock balances, reorder signals, documented adjustments, cross-godown transfers and warehouse-specific material consumption.
- Captures outward sales invoice line details with HSN/unit/GST snapshots and selected dispatch godown. Stock is validated inside a write transaction and issued in the same transaction as the invoice.
- Provides supplier invoice line comparisons against PO and GRN quantities, pricing, tax rates, calculations, totals and CGST/SGST/IGST split, with exceptions shown before payment.
- Provides accounts payable and receivable views, due dates, outstanding/overdue amounts, partial payments and overpayment checks.
- Provides normalized CSV import and export for ITC reconciliation, including statement-only/book-only rows, tax differences and duplicate identifiers for review.
- Provides offline GST JSON-shaped e-invoice and e-way-bill simulations, with assumptions recorded in `docs/GST_SCHEMA_ASSUMPTIONS.md`. No portal calls or filings are performed.
- Adds encrypted supplier bank account storage (requires separate `VENDOR_DATA_KEY`), role-specific route/export access, CSRF validation, non-cache headers and a SHA-256 chained audit-integrity view.
- Fixes the SQLite Row-to-JSON dashboard failure and the missing stock valuation template context that previously produced HTTP 500 pages.
- Expands automated regression tests across key routes, demo data shape, sales-stock issue, GST simulation and stock overdraw prevention.

## Static validation performed for v5 package

- Python syntax compilation: PASS for `app.py`, `run.py`, and `tests/test_app.py`.
- Jinja template parsing: PASS for all 26 templates.
- JavaScript syntax check (`node --check frontend/static/app.js`): PASS.
- Static template route-reference scan: no missing endpoint references found.
- POST-form CSRF scan: no missing token fields found in POST forms.
- SQLite schema scripts and listed backward-compatible column migrations: passed an in-memory SQLite smoke check; all expected core tables were present.

## Not certified / needs verification

- The Flask runtime/integration suite could not be executed in the package-build environment: Flask and Werkzeug were unavailable and dependency installation was blocked by network resolution. The tests are included but must be run locally after installing `backend/requirements.txt`.
- Browser-based end-to-end testing and deployment to Render were not performed.
- The problem statement's original six-month ground-truth dataset was not provided in this chat. The seeded scenario is fictional test data. No claimed stock accuracy, mismatch detection rate or ITC accuracy score is based on the challenge ground truth.
- GST/e-invoice/e-way-bill payloads are explicitly simulations based on documented schema assumptions, not certified full-schema validation or live portal integration.
