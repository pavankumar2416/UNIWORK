import os
import tempfile
import unittest
from pathlib import Path

os.environ["UNIWORK_DB"] = str(Path(tempfile.gettempdir()) / "uniwork-test-suite.sqlite3")

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as forge


class UNIWORKTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            Path(os.environ["UNIWORK_DB"]).unlink()
        except FileNotFoundError:
            pass
        forge.app.config["TESTING"] = True
        cls.client = forge.app.test_client()

    def setUp(self):
        response = self.client.get("/login")
        token_url = response.headers.get("Location", "/login")
        from urllib.parse import urlsplit, parse_qs
        token = parse_qs(urlsplit(token_url).query).get("__tab", [""])[0]
        self.tab_token = token
        self.csrf_token = forge.csrf_token_for(token)
        self.client.get(token_url)
        self.client.post("/login?__tab=" + token,
                         data={"username": "admin", "password": "Forge@2026!", "csrf_token": self.csrf_token},
                         follow_redirects=True)

    def test_health_endpoint_is_public(self):
        response = forge.app.test_client().get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok"})

    def test_login_required(self):
        client = forge.app.test_client()
        response = client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn(b"/login", response.headers["Location"].encode())

    def test_dashboard_loads(self):
        response = self.client.get("/?__tab=" + self.tab_token)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"OPERATIONS SNAPSHOT", response.data)
        # Regression check: dashboard chart data must be JSON embedded in the page,
        # not an unserializable sqlite3.Row object that causes HTTP 500.
        self.assertIn(b"const monthly = [", response.data)

    def test_inventory_page_loads(self):
        response = self.client.get("/inventory?__tab=" + self.tab_token)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"SKU register", response.data)
        # Inventory valuation must be supplied to the template to avoid HTTP 500.
        self.assertIn(b"STOCK VALUATION", response.data)
        self.assertIn(b"current master unit cost", response.data)

    def test_receipts_page_loads_with_order_linked_lines(self):
        response = self.client.get("/receipts?__tab=" + self.tab_token)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"receiptPO", response.data)
        self.assertIn(b"remaining", response.data)

    def test_vendor_directory_and_profile_load(self):
        self.assertEqual(self.client.get("/vendors?__tab=" + self.tab_token).status_code, 200)
        response = self.client.get("/profile?__tab=" + self.tab_token)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"My profile", response.data)

    def test_create_vendor(self):
        response = self.client.post("/vendors/create?__tab=" + self.tab_token, data={
            "code": "VND-TEST-901", "name": "Test Components Pvt Ltd",
            "gstin": "29ABCDE1234F1Z5", "state": "Karnataka",
            "email": "ap@test.example", "phone": "0801234567",
            "payment_terms": "30", "csrf_token": self.csrf_token
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Test Components Pvt Ltd", response.data)

    def test_multi_line_purchase_order(self):
        with forge.app.app_context():
            vendor = forge.query_one("SELECT id FROM vendors WHERE active=1 ORDER BY id LIMIT 1")
            products = forge.query_all("SELECT id FROM products ORDER BY id LIMIT 2")
        response = self.client.post("/purchase-orders/create?__tab=" + self.tab_token, data={
            "vendor_id": str(vendor["id"]),
            "product_id": [str(products[0]["id"]), str(products[1]["id"])],
            "quantity": ["2", "3"], "unit_price": ["10.00", "20.00"],
            "order_date": "2026-10-29", "expected_date": "2026-11-10",
            "notes": "Multi-line regression", "csrf_token": self.csrf_token
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        with forge.app.app_context():
            po = forge.query_one("SELECT id FROM purchase_orders WHERE notes='Multi-line regression' ORDER BY id DESC LIMIT 1")
            self.assertIsNotNone(po)
            lines = forge.query_one("SELECT COUNT(*) AS n FROM purchase_order_lines WHERE po_id=?", (po["id"],))
            self.assertEqual(lines["n"], 2)

    def test_product_master_and_stock_adjustment(self):
        response = self.client.post("/inventory/products/create?__tab=" + self.tab_token, data={
            "sku": "SKU-QA-MAT-051", "name": "QA Test Material", "category": "QA",
            "hsn": "8536", "gst_rate": "5", "unit": "pcs", "reorder_level": "5",
            "unit_cost": "12.50", "csrf_token": self.csrf_token
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        with forge.app.app_context():
            product = forge.query_one("SELECT id FROM products WHERE sku='SKU-QA-MAT-051'")
            warehouse = forge.query_one("SELECT id FROM warehouses ORDER BY id LIMIT 1")
        self.assertIsNotNone(product)
        adjusted = self.client.post("/inventory/adjust?__tab=" + self.tab_token, data={
            "product_id": str(product["id"]), "warehouse_id": str(warehouse["id"]),
            "direction": "ADJUST_IN", "quantity": "7", "movement_date": "2026-10-29",
            "reason": "Verified opening count", "csrf_token": self.csrf_token
        }, follow_redirects=True)
        self.assertEqual(adjusted.status_code, 200)
        with forge.app.app_context():
            row = forge.query_one("SELECT SUM(quantity) AS qty FROM stock_movements WHERE product_id=? AND warehouse_id=? AND movement_type='ADJUST_IN'", (product["id"], warehouse["id"]))
            self.assertEqual(row["qty"], 7)

    def test_po_gst_uses_product_master_rate(self):
        with forge.app.app_context():
            db = forge.get_db()
            vendor = forge.query_one("SELECT id FROM vendors ORDER BY id LIMIT 1")
            product = forge.query_one("SELECT id FROM products WHERE gst_rate=5 ORDER BY id LIMIT 1")
            if not product:
                db.execute("INSERT INTO products(sku,name,category,hsn,gst_rate,unit,reorder_level,unit_cost) VALUES(?,?,?,?,?,?,?,?)",
                           ("SKU-QA-GST-005", "QA 5 Percent Item", "QA", "8536", 5, "pcs", 0, 10))
                db.commit()
                product = forge.query_one("SELECT id FROM products WHERE sku='SKU-QA-GST-005'")
            db.execute("INSERT INTO purchase_orders(po_number,vendor_id,order_date,status,notes,created_at) VALUES(?,?,?,?,?,?)",
                       ("PO-QA-GST-RATE", vendor["id"], "2026-10-29", "Ordered", "GST rate regression", "2026-10-29T00:00:00"))
            po_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            db.execute("INSERT INTO purchase_order_lines(po_id,product_id,quantity,unit_price) VALUES(?,?,?,?)",
                       (po_id, product["id"], 10, 100))
            db.commit()
            taxable, gst = forge.po_totals(po_id)
            self.assertEqual(taxable, 1000.0)
            self.assertEqual(gst, 50.0)

    def test_summary_api(self):
        response = self.client.get("/api/summary?__tab=" + self.tab_token)
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertGreater(data["inventory_skus"], 0)
        self.assertIn("invoice_exceptions", data)

    def test_csv_export(self):
        response = self.client.get("/export/inventory.csv?__tab=" + self.tab_token)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"sku,name,category", response.data)

    def test_procurement_cannot_export_invoice_data(self):
        with forge.app.app_context():
            db = forge.get_db()
            db.execute("INSERT OR IGNORE INTO users(username,password_hash,full_name,role,created_at) VALUES(?,?,?,?,?)",
                       ("exportprocurement", forge.generate_password_hash("ProcurePass123!"), "Export Procurement", "procurement", "2026-01-01T00:00:00"))
            db.commit()
            user = forge.query_one("SELECT id FROM users WHERE username='exportprocurement'")
            token = "test-procurement-export-token"
            now = forge.datetime.now().isoformat(timespec="seconds")
            db.execute("INSERT OR REPLACE INTO tab_sessions(tab_token,user_id,boot_id,created_at,last_seen) VALUES(?,?,?,?,?)",
                       (token, user["id"], forge.BOOT_ID, now, now))
            db.commit()
        response = forge.app.test_client().get("/export/invoices.csv?__tab=" + token)
        self.assertEqual(response.status_code, 403)

    def test_accounts_cannot_export_inventory_data(self):
        with forge.app.app_context():
            db = forge.get_db()
            db.execute("INSERT OR IGNORE INTO users(username,password_hash,full_name,role,created_at) VALUES(?,?,?,?,?)",
                       ("exportaccounts", forge.generate_password_hash("AccountsPass123!"), "Export Accounts", "accounts", "2026-01-01T00:00:00"))
            db.commit()
            user = forge.query_one("SELECT id FROM users WHERE username='exportaccounts'")
            token = "test-accounts-export-token"
            now = forge.datetime.now().isoformat(timespec="seconds")
            db.execute("INSERT OR REPLACE INTO tab_sessions(tab_token,user_id,boot_id,created_at,last_seen) VALUES(?,?,?,?,?)",
                       (token, user["id"], forge.BOOT_ID, now, now))
            db.commit()
        response = forge.app.test_client().get("/export/inventory.csv?__tab=" + token)
        self.assertEqual(response.status_code, 403)

    def test_unknown_export(self):
        response = self.client.get("/export/nope.csv?__tab=" + self.tab_token)
        self.assertEqual(response.status_code, 404)

    def test_all_admin_module_pages_render(self):
        with forge.app.app_context():
            vendor = forge.query_one("SELECT id FROM vendors ORDER BY id LIMIT 1")
            order = forge.query_one("SELECT id FROM purchase_orders ORDER BY id LIMIT 1")
        paths = [
            "/", "/inventory", "/vendors", f"/vendors/{vendor['id']}",
            f"/vendors/{vendor['id']}/edit", "/purchase-orders", f"/purchase-orders/{order['id']}",
            "/receipts", "/invoices", "/reports", "/audit", "/admin/users", "/admin/data",
            "/profile", "/account/password"
        ]
        failures = []
        for path in paths:
            response = self.client.get(path + "?__tab=" + self.tab_token)
            if response.status_code != 200:
                failures.append(f"{path}: HTTP {response.status_code}")
        self.assertEqual(failures, [], "Module page rendering failures: " + "; ".join(failures))

    def test_admin_pages_load(self):
        self.assertEqual(self.client.get("/admin/users?__tab=" + self.tab_token).status_code, 200)
        self.assertEqual(self.client.get("/admin/data?__tab=" + self.tab_token).status_code, 200)

    def test_viewer_cannot_access_admin_page(self):
        with forge.app.app_context():
            db = forge.get_db()
            db.execute("INSERT OR IGNORE INTO users(username,password_hash,full_name,role,created_at) VALUES(?,?,?,?,?)",
                       ("testviewer", forge.generate_password_hash("ViewerPass123!"), "Test Viewer", "viewer", "2026-01-01T00:00:00"))
            db.commit()
            viewer = forge.query_one("SELECT id FROM users WHERE username='testviewer'")
            token = "test-viewer-tab-token"
            now = forge.datetime.now().isoformat(timespec="seconds")
            db.execute(
                "INSERT OR REPLACE INTO tab_sessions(tab_token,user_id,boot_id,created_at,last_seen) VALUES(?,?,?,?,?)",
                (token, viewer["id"], forge.BOOT_ID, now, now))
            db.commit()
        client = forge.app.test_client()
        self.assertEqual(client.get("/admin/data?__tab=" + token).status_code, 403)

    def test_reports_include_stock_alert_count(self):
        response = self.client.get("/reports?__tab=" + self.tab_token)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"STOCK ALERTS", response.data)
        self.assertRegex(response.data.decode(), r'STOCK ALERTS.*?<div class="metric-value">\d+</div>')

    def test_multi_line_po_is_not_marked_received_until_all_lines_arrive(self):
        with forge.app.app_context():
            db = forge.get_db()
            vendor = forge.query_one("SELECT id FROM vendors ORDER BY id LIMIT 1")
            products = forge.query_all("SELECT id FROM products ORDER BY id LIMIT 2")
            warehouse = forge.query_one("SELECT id FROM warehouses ORDER BY id LIMIT 1")
            po_number = "PO-TEST-MULTILINE-001"
            db.execute("DELETE FROM purchase_orders WHERE po_number=?", (po_number,))
            db.execute("INSERT INTO purchase_orders(po_number,vendor_id,order_date,expected_date,status,notes,created_at) VALUES(?,?,?,?,?,?,?)",
                       (po_number, vendor["id"], "2026-10-01", "2026-10-10", "Ordered", "Regression test", "2026-10-01T00:00:00"))
            po_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            for product in products:
                db.execute("INSERT INTO purchase_order_lines(po_id,product_id,quantity,unit_price) VALUES(?,?,1,10)",
                           (po_id, product["id"]))
            db.commit()
            warehouse_id = warehouse["id"]
            product_ids = [row["id"] for row in products]

        first = self.client.post("/receipts/create?__tab=" + self.tab_token, data={
            "po_id": str(po_id), "warehouse_id": str(warehouse_id), "product_id": str(product_ids[0]),
            "quantity": "1", "received_date": "2026-10-02", "reference": "TEST-1", "csrf_token": self.csrf_token
        }, follow_redirects=True)
        self.assertEqual(first.status_code, 200)
        with forge.app.app_context():
            status = forge.query_one("SELECT status FROM purchase_orders WHERE id=?", (po_id,))["status"]
        self.assertEqual(status, "Partially Received")

        second = self.client.post("/receipts/create?__tab=" + self.tab_token, data={
            "po_id": str(po_id), "warehouse_id": str(warehouse_id), "product_id": str(product_ids[1]),
            "quantity": "1", "received_date": "2026-10-03", "reference": "TEST-2", "csrf_token": self.csrf_token
        }, follow_redirects=True)
        self.assertEqual(second.status_code, 200)
        with forge.app.app_context():
            status = forge.query_one("SELECT status FROM purchase_orders WHERE id=?", (po_id,))["status"]
        self.assertEqual(status, "Received")

    def test_manual_po_status_cannot_bypass_receipt_workflow(self):
        with forge.app.app_context():
            row = forge.query_one("SELECT id,status FROM purchase_orders WHERE status IN ('Ordered','Draft','Cancelled') ORDER BY id LIMIT 1")
            po_id = row["id"]
            before_status = row["status"]
        response = self.client.post(
            f"/purchase-orders/{po_id}/status?__tab={self.tab_token}",
            data={"status": "Received", "csrf_token": self.csrf_token}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        with forge.app.app_context():
            after_status = forge.query_one("SELECT status FROM purchase_orders WHERE id=?", (po_id,))["status"]
        self.assertEqual(after_status, before_status)

    def test_post_without_csrf_token_is_rejected(self):
        response = self.client.post(
            "/vendors/create?__tab=" + self.tab_token,
            data={"code": "VND-CSRF-FAIL", "name": "CSRF Check Ltd", "gstin": "29ABCDE1234F1Z5",
                  "state": "Karnataka", "payment_terms": "30"})
        self.assertEqual(response.status_code, 400)

    def test_unknown_path_returns_404_not_server_error(self):
        response = self.client.get("/definitely-not-a-real-page?__tab=" + self.tab_token)
        self.assertEqual(response.status_code, 404)

    def test_viewer_sees_purchase_orders_read_only(self):
        with forge.app.app_context():
            db = forge.get_db()
            db.execute("INSERT OR IGNORE INTO users(username,password_hash,full_name,role,created_at) VALUES(?,?,?,?,?)",
                       ("readonlyviewer", forge.generate_password_hash("ViewerPass123!"), "Read Only", "viewer", "2026-01-01T00:00:00"))
            db.commit()
            viewer = forge.query_one("SELECT id FROM users WHERE username='readonlyviewer'")
            token = "test-readonly-viewer-token"
            now = forge.datetime.now().isoformat(timespec="seconds")
            db.execute("INSERT OR REPLACE INTO tab_sessions(tab_token,user_id,boot_id,created_at,last_seen) VALUES(?,?,?,?,?)",
                       (token, viewer["id"], forge.BOOT_ID, now, now))
            db.commit()
        response = forge.app.test_client().get("/purchase-orders?__tab=" + token)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'id="create-po"', response.data)
        self.assertNotIn(b"update_po_status", response.data)


    def test_problem_statement_demo_master_data_shape(self):
        with forge.app.app_context():
            vendors = forge.query_one("SELECT COUNT(*) n FROM vendors")["n"]
            products = forge.query_one("SELECT COUNT(*) n FROM products")["n"]
            warehouses = forge.query_one("SELECT COUNT(*) n FROM warehouses")["n"]
            orders = forge.query_one("SELECT COUNT(*) n FROM purchase_orders")["n"]
            invoices = forge.query_one("SELECT COUNT(*) n FROM sales_invoices")["n"]
        self.assertGreaterEqual(vendors, 80)
        self.assertGreaterEqual(products, 12)
        self.assertEqual(warehouses, 2)
        self.assertGreaterEqual(orders, 90)
        self.assertGreaterEqual(invoices, 10)

    def test_all_extended_module_pages_render(self):
        paths = ["/consumption", "/inventory/transfers", "/matching", "/gst-center",
                 "/payables", "/receivables", "/admin/audit/integrity"]
        failures = []
        for path in paths:
            response = self.client.get(path + "?__tab=" + self.tab_token)
            if response.status_code != 200:
                failures.append(f"{path}: HTTP {response.status_code}")
        self.assertEqual(failures, [], "Extended module rendering failures: " + "; ".join(failures))

    def test_transfer_and_consumption_keep_warehouse_ledger_consistent(self):
        with forge.app.app_context():
            warehouses = forge.query_all("SELECT id FROM warehouses ORDER BY id")
            candidate = None
            for row in forge.warehouse_stock_rows():
                if int(row["stock"]) >= 1:
                    candidate = dict(row)
                    break
            self.assertIsNotNone(candidate)
            product_id = candidate["id"]
            source_id = candidate["warehouse_id"]
            destination_id = next(w["id"] for w in warehouses if int(w["id"]) != int(source_id))
            source_before = forge.stock_balance(product_id, source_id)
            destination_before = forge.stock_balance(product_id, destination_id)

        transfer = self.client.post("/inventory/transfers/create?__tab=" + self.tab_token, data={
            "product_id": str(product_id), "from_warehouse_id": str(source_id),
            "to_warehouse_id": str(destination_id), "quantity": "1",
            "movement_date": "2026-10-29", "reason": "QA stock rebalance",
            "csrf_token": self.csrf_token
        }, follow_redirects=True)
        self.assertEqual(transfer.status_code, 200)
        with forge.app.app_context():
            self.assertEqual(forge.stock_balance(product_id, source_id), source_before - 1)
            self.assertEqual(forge.stock_balance(product_id, destination_id), destination_before + 1)

        consumption = self.client.post("/consumption/create?__tab=" + self.tab_token, data={
            "product_id": str(product_id), "warehouse_id": str(destination_id),
            "quantity": "1", "movement_date": "2026-10-29",
            "purpose": "QA assembly consumption", "csrf_token": self.csrf_token
        }, follow_redirects=True)
        self.assertEqual(consumption.status_code, 200)
        with forge.app.app_context():
            self.assertEqual(forge.stock_balance(product_id, destination_id), destination_before)
            entry = forge.query_one("SELECT quantity FROM material_consumptions WHERE purpose='QA assembly consumption' ORDER BY id DESC LIMIT 1")
            self.assertIsNotNone(entry)
            self.assertEqual(entry["quantity"], 1)

    def test_sales_invoice_decrements_selected_warehouse_and_exports_gst_simulation(self):
        with forge.app.app_context():
            customer = forge.query_one("SELECT id FROM customers WHERE state='Maharashtra' ORDER BY id LIMIT 1")
            candidate = None
            for row in forge.warehouse_stock_rows():
                if int(row["stock"]) >= 5:
                    candidate = dict(row)
                    break
            self.assertIsNotNone(customer)
            self.assertIsNotNone(candidate)
            before = forge.stock_balance(candidate["id"], candidate["warehouse_id"])
            product_id = candidate["id"]
            warehouse_id = candidate["warehouse_id"]
            sku = candidate["sku"]
        response = self.client.post("/receivables/invoice/create?__tab=" + self.tab_token, data={
            "customer_id": str(customer["id"]), "invoice_number": "QA-SALES-STOCK-001",
            "invoice_date": "2026-10-29", "line_product_id": str(product_id),
            "line_warehouse_id": str(warehouse_id), "line_quantity": "2", "line_unit_price": "100.00",
            "csrf_token": self.csrf_token
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        with forge.app.app_context():
            invoice = forge.query_one("SELECT * FROM sales_invoices WHERE invoice_number='QA-SALES-STOCK-001'")
            line = forge.query_one("SELECT * FROM sales_invoice_lines WHERE invoice_id=?", (invoice["id"],))
            after = forge.stock_balance(product_id, warehouse_id)
        self.assertEqual(after, before - 2)
        self.assertEqual(line["warehouse_id"], warehouse_id)
        self.assertAlmostEqual(float(invoice["igst_amount"]), float(invoice["gst_amount"]), places=2)
        self.assertEqual(line["product_sku"], sku)

        einvoice = self.client.get(f"/gst/export/einvoice/{invoice['id']}.json?__tab={self.tab_token}")
        self.assertEqual(einvoice.status_code, 200)
        self.assertEqual(einvoice.headers.get("X-UNIWORK-Simulation-Only"), "true")
        payload = einvoice.get_json()
        self.assertEqual(payload["Version"], "1.1")
        self.assertEqual(payload["ItemList"][0]["HsnCd"], line["hsn"])

        eway_before_transport = self.client.get(f"/gst/export/ewaybill/{invoice['id']}.json?__tab={self.tab_token}")
        self.assertEqual(eway_before_transport.status_code, 422)
        transport = self.client.post("/receivables/transport?__tab=" + self.tab_token, data={
            "invoice_id": str(invoice["id"]), "trans_mode": "1", "trans_distance": "50",
            "vehicle_no": "KA09AB1234", "vehicle_type": "R", "transporter_name": "QA Transport",
            "csrf_token": self.csrf_token
        }, follow_redirects=True)
        self.assertEqual(transport.status_code, 200)
        eway_after_transport = self.client.get(f"/gst/export/ewaybill/{invoice['id']}.json?__tab={self.tab_token}")
        self.assertEqual(eway_after_transport.status_code, 200)
        self.assertEqual(eway_after_transport.headers.get("X-UNIWORK-Simulation-Only"), "true")
        self.assertIn("itemList", eway_after_transport.get_json())

    def test_sales_invoice_cannot_overdraw_a_godown(self):
        with forge.app.app_context():
            customer = forge.query_one("SELECT id FROM customers ORDER BY id LIMIT 1")
            candidate = dict(forge.warehouse_stock_rows()[0])
            product_id, warehouse_id = candidate["id"], candidate["warehouse_id"]
            before = forge.stock_balance(product_id, warehouse_id)
        attempted = before + 1
        response = self.client.post("/receivables/invoice/create?__tab=" + self.tab_token, data={
            "customer_id": str(customer["id"]), "invoice_number": "QA-SALES-STOCK-OVERDRAW",
            "invoice_date": "2026-10-29", "line_product_id": str(product_id),
            "line_warehouse_id": str(warehouse_id), "line_quantity": str(attempted), "line_unit_price": "1.00",
            "csrf_token": self.csrf_token
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        with forge.app.app_context():
            after = forge.stock_balance(product_id, warehouse_id)
            invoice = forge.query_one("SELECT id FROM sales_invoices WHERE invoice_number='QA-SALES-STOCK-OVERDRAW'")
        self.assertEqual(after, before)
        self.assertIsNone(invoice)


if __name__ == "__main__":
    unittest.main()
