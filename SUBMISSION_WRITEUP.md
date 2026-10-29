# UNIWORK — Project Submission Write-up

## How it works and technical architecture

UNIWORK is a lightweight web application for a small electrical-components manufacturer that needs to connect procurement, goods receipt, stock control, vendor invoices and GST review without adopting a heavyweight enterprise ERP. The application is implemented in Python using Flask, server-rendered Jinja templates, HTML/CSS/JavaScript and SQLite. Gunicorn is used as the production WSGI server in the Render deployment configuration.

### End-to-end workflow

1. **Vendor master:** Procurement users maintain vendor records and link purchase history. Confidential supplier bank details are restricted and encrypted with a key supplied through deployment configuration.
2. **Purchase order:** Users create purchase orders with multiple product lines, quantities, prices and estimated GST.
3. **Goods receipt (GRN):** Storekeepers receive PO-linked quantities into a selected godown. Accepted receipts create stock-ledger movements.
4. **Two-godown inventory:** Stock balances are derived from the movement ledger, including receipts, consumption, adjustments, transfers and sales issues. The interface supports location-wise stock, reorder indicators and stock valuation based on current product-master cost.
5. **Vendor invoice matching:** Invoice lines are compared against purchase order lines and recorded receipt quantities. Exceptions such as quantity over-receipt/invoice, price or tax differences, missing line detail and arithmetic variances are surfaced for review before payment.
6. **GST and ITC review:** The GST center imports normalized statement data and compares records with books using supplier GSTIN and invoice identifiers, taxable value and tax heads. It flags book-only, statement-only, duplicate and value-variance cases.
7. **Owner reporting:** Dashboard and CSV reports summarize stock, reorder alerts, mismatches, accounts payable and accounts receivable. GST e-invoice/e-way-bill-shaped JSON outputs are simulations only; the application does not submit to live GST systems or file returns.

### Architecture and controls

- **Presentation layer:** Jinja templates plus static CSS and JavaScript.
- **Application layer:** Flask routes and business logic in `backend/app.py`, including form validation and role-based access checks.
- **Data layer:** SQLite tables for masters, purchase orders, receipts, stock movements, invoices, reconciliation rows, users and audit events. Foreign-key constraints and database transactions are used for critical stock operations.
- **Security controls:** Password hashing, tab-bound sessions, CSRF validation for state-changing forms, secure/HTTP-only session cookie settings for production, role checks, encryption of supplier bank details when `VENDOR_DATA_KEY` is configured, and a SHA-256 chained audit log with an integrity check page.
- **Deployment:** GitHub stores the source. Render builds dependencies from `backend/requirements-production.txt`, starts Gunicorn, and checks `/health`. Production secret values are provided through Render's environment settings and are not committed to the repository.

### Scope and assumptions

The project covers procurement, goods receipts, inventory and transfers, material consumption, supplier invoice matching, GST/ITC review, simulated statutory payloads and owner-level reporting. Live GST portal submission, return filing, MRP, payroll and fixed assets are outside scope. GST output schemas are documented as assumptions for an evaluation/demo and are not a certification of statutory compliance. Confirm official schema versions and business-specific tax treatment before commercial use.

The included dataset is synthetic demo data. The supplied six-month challenge ground-truth dataset was not included in this project package, so no ground-truth stock-accuracy or mismatch-catch-rate score is claimed. The automated regression tests are included and must be run in an environment with dependencies installed before treating the build as fully verified.

### Deployment and data persistence note

The included `render.yaml` enables a low-friction free demo deployment. Render free services use an ephemeral filesystem; SQLite changes may be lost on spin-down, restart or redeploy. For durable evaluation records, use a paid Render service with a persistent disk mounted at `/var/data` and set `UNIWORK_DB=/var/data/uniwork.sqlite3`, or migrate the app data layer to a managed persistent database. Never use real vendor bank details, live customer information or statutory filing in this demonstration deployment.
