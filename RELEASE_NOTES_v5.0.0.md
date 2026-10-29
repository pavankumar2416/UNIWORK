# UNIWORK v5.0.0 — Source Release

This release packages the UNIWORK procurement, inventory, vendor invoicing, GST/ITC review and reporting application for evaluation.

## Included
- Flask application with Jinja frontend, CSS and JavaScript
- Purchase order, goods receipt and inventory ledger workflow
- Two-godown stock, transfers and material consumption
- Supplier invoice matching review and payment/receivable tracking
- GST statement comparison and simulated e-invoice/e-way-bill-shaped JSON output
- Role controls, CSRF validation, encrypted supplier bank data support and audit-chain checks
- Regression test suite, workflow docs, GST schema assumptions, deployment Blueprint and project write-up

## Verification notice
This release includes automated regression tests, but a complete runtime run and challenge-ground-truth score must be produced in the evaluator's dependency-enabled environment. Included data is synthetic demonstration data. No production-readiness or statutory-compliance certification is claimed.

## Running
See `README.md` and `docs/GITHUB_RENDER_DEPLOYMENT.md`.
