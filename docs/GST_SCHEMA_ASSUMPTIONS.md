# GST data and schema assumptions (checked 29 October 2026)

## Purpose and boundary

UNIWORK prepares **offline simulation payloads** and reconciliation reports. It does not call the GST portal, an Invoice Registration Portal (IRP), the e-way bill portal, or file a GST return. A downloaded JSON file is not an IRN, a valid e-invoice, an e-way bill, proof of ITC eligibility, or evidence of a tax filing.

The application uses the following version assumptions for educational/demo output. Recheck the official materials and full applicable JSON schema immediately before any production integration because current statutory rules, field requirements, validation logic, and schema publications may change.

## Assumed formats

| Capability | Assumption | Official reference |
|---|---|---|
| E-invoice payload | GSTN e-invoice schema / FORM GST INV-1 (INV-01) Version 1.1; field set is schema-shaped for demonstration | https://www.gstn.org.in/e-invoicing and https://einvoice.gst.gov.in/ |
| E-way bill generation request | GSTN E-Way Bill API developer portal v1.03 | https://docs.ewaybillgst.gov.in/apidocs/ |
| ITC statement reconciliation | Normalized CSV mapped from accounting books and permitted GSTR-2B information; not a raw portal export parser | https://tutorial.gst.gov.in/userguide/returns/FAQ_gstr2b.htm |

The official GSTN e-invoicing resource identifies e-invoicing as reporting specified invoice particulars in the notified format and obtaining an IRN. Merely generating JSON locally is not the live e-invoicing process. GSTN's current e-way bill API portal identifies version v1.03; the project still needs full schema validation and any applicable process/authentication integration before production use.

## What the app validates

- Basic GSTIN shape and six-digit PIN code structure for GST simulation exports.
- Product HSN snapshots, quantities, taxable amount, GST rate, tax-head values and invoice totals as applicable to the transaction.
- Transport mode plus selected vehicle or transport-document fields before an e-way-bill-shaped export.
- GSTIN + invoice-number matching, taxable value, total tax and CGST/SGST/IGST head variance in the normalized ITC statement import.
- Duplicate document identifiers within an uploaded CSV and repeated supplier document identifiers across imported statement periods are flagged for review.

These checks are application checks, not a substitute for statutory eligibility, place-of-supply and transaction-classification review or the official portal's validation.

## Current limitations to disclose

1. The e-invoice and e-way bill JSON outputs are **schema-shaped simulations**, not certified full-schema output. The app does not embed the official current JSON schema or run a complete statutory JSON Schema validator. Some required fields, master-code constraints, conditional fields and version-dependent rules may be absent or modeled simplistically.
2. The business's applicable e-invoicing threshold/exemptions, e-way-bill threshold/applicability, place of supply, HSN validity, GST rate, reverse charge and other tax rules must be confirmed for the specific transaction.
3. GSTIN validation checks structure only; it does not query GSTN, verify checksum/registration status or confirm legal entity identity.
4. ITC match results are **review flags**, not legal determinations of entitlement. GSTN guidance recommends reconciling books with GSTR-2B and warns against duplicate credit; additional statutory conditions still require self-assessment.
5. The sample customer/vendor GSTINs and transactions are fictional format examples, not authentic registrations or the problem's ground-truth data.
6. No live GST portal submission or filing occurs. This is consistent with the stated evaluation boundary.

## References

- GSTN e-Invoice overview, schema/format and API resources: https://www.gstn.org.in/e-invoicing
- GSTN e-Invoicing FAQs: https://www.gstn.org.in/einvoice-faqs
- GSTN E-Way Bill API Developer Portal, current version shown as v1.03: https://docs.ewaybillgst.gov.in/apidocs/
- GST Portal FAQ — Form GSTR-2B: https://tutorial.gst.gov.in/userguide/returns/FAQ_gstr2b.htm

Last source review recorded: 29 October 2026. Reconfirm immediately before production use.
