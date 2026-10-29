# UNIWORK v5 — Senior QA and problem-statement alignment report

**Review date:** 29 October 2026  
**Project:** UNIWORK procurement, inventory, vendor invoicing and GST workflow  
**Release status:** Updated development/evaluation build; runtime integration verification remains outstanding.

## 1. Summary

The project has been expanded toward the stated objective: join vendor procurement, PO/GRN receipt, two-godown inventory, supplier invoice matching, GST/ITC review, AP/AR and owner-level reporting in one small-manufacturer workflow. The package includes the source, templates, sample normalized ITC CSV, database seeding, documented environment variables, regression tests and workflow/schema notes.

It is not yet appropriate to claim that every module works end to end. Python/Jinja/JavaScript and SQLite schema checks passed, but the Flask test suite could not be executed in this environment because Flask and Werkzeug were not installed and package download was blocked. The original challenge ground-truth transaction files were not provided; the demonstration dataset is synthetic and cannot support a claimed rubric score.

## 2. Alignment matrix

| Problem-statement criterion | v5 implementation | Validation boundary |
|---|---|---|
| 80 vendors and six-month demo scenario | Fresh-database seed creates 80 synthetic vendors, 12 SKUs and 90 POs over roughly six months | A fixture for walkthroughs, not the original ground truth |
| Two godowns | Warehouse-specific ledger balances and reorder flags; cross-godown transfers | Reconciliation against ground-truth balances still required |
| Stock through receipts and consumption | PO-linked GRN stock-in, consumption issues, documented adjustments and transfer ledger movements | Runtime transaction tests must be run locally |
| Sales/distributor transactions | Line-level sales invoices choose a dispatch godown; stock issue is written atomically with invoice creation | Basic GST split uses configured company/customer state comparison |
| PO–GRN–invoice matching | Compares supplier, PO SKU/price/quantity, received amounts, line arithmetic/tax and header totals/tax heads | Matching rules are a review aid; measure precision/recall against real challenge cases |
| GST calculations | Decimal-based money rounding, product GST rates and CGST/SGST/IGST values | Full statutory treatment, exemptions, reverse charge and place-of-supply need expert review |
| ITC comparison | Normalized CSV input matches supplier GSTIN + invoice number and flags unmatched rows, variances and duplicates | Not a parser for every raw native GSTR-2B export; flags are not legal entitlement decisions |
| GST-ready output | E-invoice-shaped JSON (assumed INV-01 v1.1) and e-way-bill-shaped JSON (assumed API v1.03), visibly simulation-only | No full official JSON schema validator and no live portal submission |
| What do we owe today? | Supplier payables, due dates, overdue amounts and partial-payment ledger | Test with scenario-specific balances |
| What are we owed today? | Customer receivables, due dates, overdue amounts and payment recording | Test with scenario-specific balances |
| Tamper-evident audit trail | SHA-256 chained audit entries and chain-integrity page | Application-level tamper evidence; a database administrator can still alter database and chain together |
| Confidential vendor bank/pricing data | Encrypted account number storage via a separate deployment key and role-specific access | Configure/secure the key and review role paths before real data |
| Affordable/lightweight deployment | Flask + SQLite, local start instructions and Gunicorn production dependency set | SQLite is single-writer oriented; production backups/persistence required |
| Evaluation boundary | GST portal filing is simulated; MRP, payroll and fixed assets are excluded | Matches the stated scope boundary |

## 3. Main workflow map

1. Procurement maintains supplier data and creates a multi-line purchase order.
2. Storekeeper receives the PO lines into one of the two godowns. The GRN, receipt line, stock movement and PO status update are linked.
3. Stock balances are derived from ledger movements. Adjustments require a reason; transfer and consumption routes validate the selected godown balance.
4. Accounts records a supplier invoice against a PO. The matching view explains PO, GRN, quantity, price, tax and total inconsistencies before payment.
5. Approved invoices that pass match controls can receive partial or final payments. Invoices with outstanding match issues are blocked from standard payment entry; any override requires a reason and is auditable.
6. Outward sales invoices capture line-level item, HSN, quantity, price and dispatch godown. Quantity is checked within a write transaction and the stock issue is committed with the invoice.
7. ITC statement rows can be imported from a normalized CSV. Differences are shown for review rather than automatically declaring legal credit entitlement.
8. The owner can review stock by godown, supplier payables, customer receivables, mismatch queues and audit events.

## 4. Verification performed in the package-build environment

| Check | Result | Details |
|---|---|---|
| Python syntax | PASS | `backend/app.py`, `backend/run.py`, `backend/tests/test_app.py` compile |
| Jinja parsing | PASS | All 26 templates parse |
| JavaScript syntax | PASS | `node --check frontend/static/app.js` |
| Template route names | PASS | Static scan found no missing `url_for` endpoint names |
| CSRF fields | PASS | No POST form missing a `csrf_token` field |
| SQLite schema/migrations | PASS | Extracted schema scripts and column migrations execute in in-memory SQLite; expected core tables present |
| Flask unit/integration suite | BLOCKED | Flask/Werkzeug unavailable here; installing dependencies failed due unavailable package-network access |
| Browser E2E | NOT RUN | Needs a local running app and browser walkthrough |
| Render deployment | NOT PERFORMED | ZIP does not update the hosted application automatically |
| Challenge ground-truth scoring | NOT POSSIBLE YET | Source dataset/expected-results file was not included in the materials received |

## 5. Running locally and executing tests

In PowerShell, from the extracted project root:

```powershell
cd backend
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python run.py
```

Open `http://127.0.0.1:5000`. On a fresh local demo database the credentials are `admin` / `Forge@2026!`; change them immediately and never use the demo credentials in a shared or production environment. Existing databases are not reset by re-extracting the ZIP. Take a backup before application/schema changes. The full test run is required before treating the release as verified.

## 6. Important limitations

- **Synthetic data:** Fresh seed data is fictional and merely resembles the problem's scale. The real six-month source transactions and ground truth were not supplied. Do not report stock accuracy or mismatch/ITC detection rates until those inputs are loaded and evaluated.
- **GST legal rules:** CGST/SGST versus IGST handling currently uses a simplified state comparison. The application does not verify registration status, statutory thresholds, exemptions, reverse charge, place of supply or all transaction-specific rules.
- **GST formats:** Schema assumptions are documented in `docs/GST_SCHEMA_ASSUMPTIONS.md`. JSON is shaped for evaluation/demo and includes an `X-UNIWORK-Simulation-Only` response header. It is not a certified payload or a filing. Recheck official current schemas and validate complete payloads before any possible integration.
- **ITC:** Imported rows are reconciliation signals only. A match does not establish legal eligibility.
- **Inventory valuation:** On-hand quantity × current master unit cost; no FIFO, moving average or historical cost snapshots.
- **Audit integrity:** Hash chain makes edits detectable under ordinary access, but is not a protected external immutable ledger.
- **Session token transport:** The app uses a tab token in the URL query string. Do not share authenticated URLs. A production-hardening pass should migrate to secure cookie-based server-side sessions.
- **Database/deployment:** SQLite needs persistent storage, controlled backup/restore and concurrency review for hosting. The updated project has not been deployed to Render.
- **Secrets:** Production requires a strong `SECRET_KEY`, unique admin credentials, `VENDOR_DATA_KEY` protected separately from the database, HTTPS, `COOKIE_SECURE=1`, and disabled demo credentials.

## 7. Recommended final acceptance steps

1. Run the full automated tests on a computer with dependencies installed and fix every failure.
2. Browser-test each role and the complete PO → GRN → stock → supplier invoice → match → payment sequence.
3. Verify transfer and consumption quantities, sales-stock issues, and balances for both godowns.
4. Test ITC files with match, tax mismatch, statement-only, book-only and duplicate cases.
5. Test payables/receivables and invalid/overpayment attempts.
6. Tamper with a disposable audit row and verify that the integrity page detects the chain break.
7. Load the actual supplied transaction dataset and compare each expected stock and mismatch result to compute the rubric metrics.
8. Confirm statutory schema/version requirements from the official references and validate outputs against the complete applicable validators.
9. Back up the current database and deploy only after local tests pass and production secrets/storage are configured.

**Conclusion:** This package is the most complete updated development build prepared from the code and problem statement available in this conversation. It closes significant workflow gaps, but full runtime verification, challenge-dataset scoring, statutory-schema validation and deployment remain pending.
