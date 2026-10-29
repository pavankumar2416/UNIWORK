# UNIWORK demo data

The application seeds a **synthetic demonstration scenario** into a fresh SQLite database when the `vendors` table is empty and demo seeding has not been disabled. It currently creates a vendor base of 80 rows, 12 SKUs, two warehouses, 90 purchase orders dated across roughly six months, receipts, supplier invoices with deliberately introduced exceptions, distributor sales invoices, and an example normalized ITC statement slice.

This is reproducible synthetic data for application walkthroughs and regression tests. It is **not** the challenge's ground-truth dataset and must not be used to claim stock, mismatch-detection, or ITC-accuracy scores against that dataset.

## CSV input for ITC reconciliation

The Accounts & GST role can import a normalized CSV from the GST Center. Required columns:

- `supplier_gstin`
- `invoice_number`
- `taxable_value`
- `igst_amount`
- `cgst_amount`
- `sgst_amount`

Optional columns: `invoice_date` (YYYY-MM-DD) and `eligibility` (`Yes`, `No`, `Eligible`, `Ineligible`, `Available`, `Not available`, or `Unknown`). The import is a reconciliation-oriented normalization format; it is not a direct parser for every native GST portal ZIP/JSON/Excel export layout.

A sample file is available at `frontend/static/sample_gstr2b.csv`. Replace its example rows with the organization's permitted export values before use. Never import real data into a shared demo deployment.

## Database behavior

- Seed records are created only if no vendors exist. Re-running the server does not overwrite an existing database.
- The Administrator's demo-data reset tool is destructive and must only be used on a test environment with a backup.
- Keep production database files on persistent storage and back them up regularly.
