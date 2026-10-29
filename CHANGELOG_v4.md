# UNIWORK v4 Development Changelog

## Purpose

This release focuses on making the business flow easier to follow and filling workflow gaps after the dashboard and inventory HTTP 500 issues. It is a development/demo release, not a production-certified accounting package.

## End-to-end flow

1. **Vendor master** — create, review, edit and deactivate vendor records.
2. **Purchase order** — create a PO containing up to 20 unique product lines; review taxable and estimated GST totals.
3. **PO detail** — inspect line quantities, quantities received, quantities remaining and GRN history.
4. **Goods receipt (GRN)** — record the quantity still outstanding against a selected PO line; the stock ledger and PO status update as part of the receipt transaction.
5. **Inventory** — inspect current balances and reorder warnings; administrators add SKUs; administrators/storekeepers record documented stock adjustments.
6. **Invoice desk** — capture a supplier invoice against its vendor PO, compare taxable/GST values against configured product rates, review exceptions, and record reasoned overrides.
7. **Reports and audit** — review summary metrics and use exports permitted for the current role.

## Main development updates

- Fixed dashboard chart serialization by converting SQLite rows into ordinary dictionaries before passing chart data to Jinja.
- Fixed the Inventory template context for stock valuation and expanded the inventory route with warehouse and recent movement data.
- Added multi-line purchase order creation and a detail page with line-level receipt progress.
- Added an open-PO-aware goods receipt form.
- Added product creation, controlled stock adjustments, and recent movement history.
- Added vendor edit and deactivation workflow.
- Replaced fixed 18% PO GST assumptions with configured product-line GST rates for demo matching.
- Improved global/page table search composition, workflow explanations, and mobile layout rules.
- Added regression tests for dashboard JSON, inventory valuation, receipt form rendering, multi-line POs, stock adjustments, GST rate calculation and module routes.

## Validation status

Static checks have passed in the review container: Python compilation, parsing of all 18 Jinja templates, JavaScript syntax, route-reference checks and duplicate template-ID checks. The complete Flask suite could not run in that container because Flask was not installed and package-network access was unavailable. Run `python -m unittest discover -s tests -v` from `backend/` on the target machine before using this build.

## Known boundaries

- GST is a simplified internal matching estimate. There is no GSTN filing, e-invoicing, e-waybill integration, or full CGST/SGST/IGST statutory determination.
- Historical PO tax rates are not snapshotted; changing product master GST rates can affect historical estimates.
- Inventory value uses current master unit cost, not FIFO or moving-average costing.
- Tab-specific auth tokens are in the URL query; treat authenticated links as sensitive. Use secure cookie-based sessions in a production redesign.
- Use persistent storage and backups if deploying SQLite on a hosted provider.
- This package is not automatically deployed to Render.
