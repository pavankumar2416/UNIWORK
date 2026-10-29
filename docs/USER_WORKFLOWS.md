# UNIWORK workflows and acceptance checks

## 1. Procurement → goods receipt → inventory

1. Procurement creates a PO for an active vendor with one or more distinct SKU lines.
2. A storekeeper selects the PO, warehouse and PO line(s), records only the outstanding accepted quantity and saves a GRN.
3. The GRN and its stock-in movements must commit together. PO status is derived from all lines; manually changing status cannot bypass receipts.
4. Transfers post matched `TRANSFER_OUT` and `TRANSFER_IN` movements. Consumption posts a warehouse-specific `CONSUMPTION` movement. Adjustments require a reason and may not make a warehouse balance negative.
5. Warehouse balances are ledger-derived by SKU and godown. Reorder flags are assessed per SKU-location against the configured reorder level. Aggregate stock value uses the current product master unit cost, not moving-average or standard-cost accounting.

## 2. Supplier invoice → three-way match → payment

1. Accounts capture the supplier invoice against the PO with line-level quantities, unit prices and GST rates.
2. The matching page compares invoice SKU against PO lines; ordered price/quantity; received quantity across GRNs linked to the PO; line arithmetic and GST; header taxable/GST/total values; and GST head split.
3. Missing line detail, invoice quantity above receipt quantity, off-PO SKU, price/tax variance, mismatched totals and invoice/state tax-head inconsistencies remain explicit exceptions.
4. The user reviews the exception text. An approval override requires a reason and does not silently mark unresolved ITC as eligible.
5. Payment entry is blocked unless the invoice is approved and automated matching has no outstanding exceptions. Partial payments are supported and overpayment is blocked.

## 3. Distributor sale → inventory issue → receivable

1. Accounts creates a line-level outward invoice and chooses a dispatch godown for each SKU line.
2. Available stock is rechecked inside a write transaction. If stock is insufficient, invoice creation is rejected without posting the invoice or changing stock.
3. The sales invoice line and `OUT` stock movement are saved atomically. GST uses the configured company/customer state comparison and the product master tax rate as a demo rule.
4. Customer receipts update the open balance and reject overpayment. The receivables view shows totals, due dates, and overdue balances.
5. Store real production values only after a qualified business/tax owner reviews the rules, address/place-of-supply handling and sales document requirements.

## 4. GSTR-2B / ITC reconciliation

1. Accounts uploads a normalized CSV with supplier GSTIN, invoice number, taxable value and tax heads.
2. The app matches supplier GSTIN + invoice number and compares taxable value and total tax plus individual tax heads.
3. Results distinguish books-only records in periods present in the imported statement, statement-only invoices, duplicate books/statement identifiers, amount mismatches, statement-indicated unavailability and matched-but-review-eligibility records.
4. This importer does not yet parse every raw GSTR-2B download packaging layout. The source period and mapping from a real exported file should be reviewed before using it for operational month-end work.
5. No ITC is legally declared or claimed by this application.

## 5. Owner's daily answer

- **What do we owe?** Open supplier invoice balances less recorded payments, due dates and overdue items.
- **What are we owed?** Open customer invoice balances less receipts, due dates and overdue items.
- **Where is stock?** Per-SKU stock by each godown, combined on-hand stock and reorder flags.
- **What needs review?** PO–GRN–invoice exceptions and normalized ITC reconciliation exceptions.

## Release acceptance checklist

- [ ] Start from a fresh database and confirm 80 synthetic vendors, 12 SKUs, two godowns, 90 POs and line-level demo outward invoices are seeded.
- [ ] Create a PO; receive one line partially; confirm PO remains partially received; receive the remainder and confirm full receipt status.
- [ ] Confirm receipt raises exactly the chosen warehouse stock; transfer lowers the source and raises the destination by the same quantity.
- [ ] Confirm material consumption and sales issue lower the selected godown and cannot overdraw it.
- [ ] Create a supplier invoice with a line-level quantity/price/GST exception and confirm reasoned explanations appear before payment.
- [ ] Confirm an approved, passing supplier invoice permits partial payment and cannot be paid beyond outstanding.
- [ ] Create same-state and inter-state sales invoices and independently recalculate IGST vs CGST/SGST and invoice totals.
- [ ] Confirm customer payment lowers AR outstanding and cannot overpay.
- [ ] Import a normalized ITC CSV with a matching row, a tax mismatch, a statement-only invoice and duplicate IDs; check each result.
- [ ] Confirm unauthorised roles cannot access supplier bank data, ITC import, payment actions or restricted exports.
- [ ] Tamper with an audit row in a disposable test database and confirm the audit chain integrity page detects the change.
- [ ] Validate any exported GST JSON against the current official schema/validator and test the exact required fields before describing schema compliance.
- [ ] Run the full Flask test suite on a machine with dependencies installed and document all failures/resolutions.

This project ships synthetic data and regression tests; ground-truth accuracy must be measured separately using the challenge dataset. The actual challenge data files are not bundled unless provided by the project owner.
