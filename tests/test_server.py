import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path

from openpyxl import Workbook

import server
import seed_demo


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = server.DB_PATH
        server.DB_PATH = Path(self.temp_dir.name) / "tracker.sqlite3"
        server.initialize_db()

    def tearDown(self):
        server.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def add_material(self, name="Board", opening=10):
        with server.connect_db() as db:
            cursor = db.execute(
                "INSERT INTO materials(code, name, unit, opening_qty) VALUES ('B1', ?, 'boards', ?)",
                (name, opening),
            )
            return cursor.lastrowid

    def add_movement(self, material_id, kind, qty, **values):
        record = {
            "material_id": material_id,
            "kind": kind,
            "qty": qty,
            "date": date.today().isoformat(),
            **values,
        }
        with server.connect_db() as db:
            with db:
                return server._record_movement(db, record)

    def balances(self, material_id):
        with server.connect_db() as db:
            return server.balances_for(db, material_id)

    def start_test_server(self):
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.RequestHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        return httpd, thread

    def test_tommy_photo_is_served_for_sidebar(self):
        httpd, thread = self.start_test_server()
        connection = http.client.HTTPConnection(
            "127.0.0.1", httpd.server_address[1], timeout=5
        )
        try:
            connection.request("GET", "/tommy.jpeg")
            response = connection.getresponse()
            photo = response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Type"), "image/jpeg")
            self.assertTrue(photo)
        finally:
            connection.close()
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def http_request(self, httpd, method, path, payload=None, cookie=""):
        connection = http.client.HTTPConnection(
            "127.0.0.1", httpd.server_address[1], timeout=5
        )
        headers = {}
        body = None
        if payload is not None:
            body = json.dumps(payload)
            headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = json.loads(response.read())
        set_cookie = response.getheader("Set-Cookie", "").split(";", 1)[0]
        status = response.status
        connection.close()
        return status, result, set_cookie

    def test_auth_setup_and_protected_endpoints(self):
        httpd, thread = self.start_test_server()
        try:
            status, result, _ = self.http_request(
                httpd, "GET", "/api/auth/status"
            )
            self.assertEqual(status, 200)
            self.assertTrue(result["setup_required"])
            status, result, _ = self.http_request(
                httpd, "GET", "/api/dashboard"
            )
            self.assertEqual(status, 401)

            status, result, admin_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/setup",
                {
                    "username": "admin1",
                    "display_name": "Tracker Admin",
                    "pin": "12ab56",
                },
            )
            self.assertEqual(status, 400)
            self.assertIn("6 digits", result["error"])

            status, result, admin_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/setup",
                {
                    "username": "admin1",
                    "display_name": "Tracker Admin",
                    "pin": "123456",
                },
            )
            self.assertEqual(status, 201)
            self.assertEqual(result["user"]["role"], "admin")
            status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/auth/setup",
                {
                    "username": "admin2",
                    "display_name": "Second Admin",
                    "pin": "234567",
                },
            )
            self.assertEqual(status, 409)

            status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/users",
                {
                    "username": "employee1",
                    "display_name": "Tracker Employee",
                    "pin": "345678",
                    "pin_confirm": "345678",
                },
                admin_cookie,
            )
            self.assertEqual(status, 201)
            status, result, employee_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/login",
                {
                    "username": "employee1",
                    "pin": "345678",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(result["user"]["role"], "employee")
            status, result, _ = self.http_request(
                httpd, "GET", "/api/dashboard", cookie=employee_cookie
            )
            self.assertEqual(status, 200)
            self.assertIn("materials", result)
            status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/auth/pin",
                {
                    "current_pin": "111111",
                    "new_pin": "456789",
                },
                employee_cookie,
            )
            self.assertEqual(status, 401)
            status, _, renewed_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/pin",
                {
                    "current_pin": "345678",
                    "new_pin": "456789",
                },
                employee_cookie,
            )
            self.assertEqual(status, 200)
            status, _, _ = self.http_request(
                httpd, "GET", "/api/dashboard", cookie=employee_cookie
            )
            self.assertEqual(status, 401)
            status, _, _ = self.http_request(
                httpd, "GET", "/api/dashboard", cookie=renewed_cookie
            )
            self.assertEqual(status, 200)
            status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/auth/logout",
                cookie=renewed_cookie,
            )
            self.assertEqual(status, 200)
            status, _, _ = self.http_request(
                httpd, "GET", "/api/dashboard", cookie=renewed_cookie
            )
            self.assertEqual(status, 401)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_login_locks_after_five_incorrect_pins(self):
        httpd, thread = self.start_test_server()
        try:
            status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/auth/setup",
                {
                    "username": "admin1",
                    "display_name": "Tracker Admin",
                    "pin": "123456",
                },
            )
            self.assertEqual(status, 201)
            for _ in range(4):
                status, _, _ = self.http_request(
                    httpd,
                    "POST",
                    "/api/auth/login",
                    {"username": "admin1", "pin": "999999"},
                )
                self.assertEqual(status, 401)
            status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/auth/login",
                {"username": "admin1", "pin": "999999"},
            )
            self.assertEqual(status, 429)
            status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/auth/login",
                {"username": "admin1", "pin": "123456"},
            )
            self.assertEqual(status, 429)
            with server.connect_db() as db:
                db.execute(
                    "UPDATE app_login_attempts SET locked_until = 0 "
                    "WHERE username_hash = ?",
                    (server._login_attempt_key("admin1"),),
                )
            status, result, _ = self.http_request(
                httpd,
                "POST",
                "/api/auth/login",
                {"username": "admin1", "pin": "123456"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(result["user"]["role"], "admin")
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_house_history_includes_transfers_for_both_house_endpoints(self):
        material_id = self.add_material(name="Board", opening=10)
        with server.connect_db() as db:
            with db:
                server._upsert_house(db, "H-SOURCE", "D01", "Owner One", "Worker One")
                server._upsert_house(db, "H-DEST", "D02", "Owner Two", "Worker Two")
                server._record_movement(
                    db,
                    {
                        "kind": "pickup",
                        "date": date.today().isoformat(),
                        "material_id": material_id,
                        "qty": 4,
                        "house_id": "H-SOURCE",
                        "person": "Worker One",
                        "team": "Site Crew",
                        "notes": "Initial delivery",
                    },
                )
                server._record_movement(
                    db,
                    {
                        "kind": "delivery",
                        "date": date.today().isoformat(),
                        "material_id": material_id,
                        "qty": 1,
                        "house_id": "H-SOURCE",
                        "person": "Worker One",
                        "team": "Site Crew",
                        "notes": "Additional delivery",
                    },
                )
                server._record_movement(
                    db,
                    {
                        "kind": "transfer",
                        "date": date.today().isoformat(),
                        "material_id": material_id,
                        "qty": 2,
                        "source_kind": "house",
                        "source_reference": "H-SOURCE",
                        "destination_kind": "house",
                        "destination_reference": "H-DEST",
                        "person": "Worker One",
                        "team": "Site Crew",
                        "notes": "Moved to second site",
                    },
                )
                server._record_movement(
                    db,
                    {
                        "kind": "return",
                        "date": date.today().isoformat(),
                        "material_id": material_id,
                        "qty": 1,
                        "house_id": "H-SOURCE",
                        "person": "Worker One",
                        "team": "Site Crew",
                        "notes": "Unused board returned",
                    },
                )
                server._record_movement(
                    db,
                    {
                        "kind": "use",
                        "date": date.today().isoformat(),
                        "material_id": material_id,
                        "qty": 1,
                        "house_id": "H-SOURCE",
                        "person": "Worker One",
                        "team": "Site Crew",
                        "notes": "Installed on site",
                    },
                )
                db.execute(
                    "UPDATE houses SET is_finished = 1 WHERE id = ?",
                    ("H-SOURCE",),
                )

        httpd, thread = self.start_test_server()
        try:
            status, _, _ = self.http_request(
                httpd, "GET", "/api/houses/H-DEST"
            )
            self.assertEqual(status, 401)

            status, _, admin_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/setup",
                {
                    "username": "admin1",
                    "display_name": "Tracker Admin",
                    "pin": "123456",
                },
            )
            self.assertEqual(status, 201)
            status, source_history, _ = self.http_request(
                httpd,
                "GET",
                "/api/houses/H-SOURCE",
                cookie=admin_cookie,
            )
            self.assertEqual(status, 200)
            self.assertEqual(source_history["house"]["owner_name"], "Owner One")
            self.assertTrue(source_history["house"]["is_finished"])
            self.assertEqual(len(source_history["movements"]), 5)
            self.assertEqual(
                {movement["kind"] for movement in source_history["movements"]},
                {"pickup", "delivery", "transfer", "return", "use"},
            )
            status, global_history, _ = self.http_request(
                httpd,
                "GET",
                "/api/movements",
                cookie=admin_cookie,
            )
            self.assertEqual(status, 200)
            self.assertNotIn(
                "use",
                {movement["kind"] for movement in global_history["movements"]},
            )
            self.assertNotIn(
                "Installed on site",
                {movement["notes"] for movement in global_history["movements"]},
            )
            status, search_result, _ = self.http_request(
                httpd,
                "GET",
                "/api/movements?q=Installed%20on%20site",
                cookie=admin_cookie,
            )
            self.assertEqual(status, 200)
            self.assertEqual(search_result["movements"], [])
            status, dashboard, _ = self.http_request(
                httpd,
                "GET",
                "/api/dashboard",
                cookie=admin_cookie,
            )
            self.assertEqual(status, 200)
            self.assertNotIn(
                "use",
                {movement["kind"] for movement in dashboard["movements"]},
            )
            self.assertEqual(
                source_history["movements"][0]["notes"],
                "Installed on site",
            )
            used = [
                movement
                for movement in source_history["movements"]
                if movement["kind"] == "use"
            ]
            self.assertEqual(len(used), 1)
            self.assertEqual(used[0]["material"], "Board")
            self.assertEqual(used[0]["qty"], 1)

            status, destination_history, _ = self.http_request(
                httpd,
                "GET",
                "/api/houses/H-DEST",
                cookie=admin_cookie,
            )
            self.assertEqual(status, 200)
            self.assertEqual(destination_history["house"]["employee_name"], "Worker Two")
            self.assertEqual(len(destination_history["movements"]), 1)
            self.assertEqual(
                destination_history["movements"][0]["destination"],
                "house:H-DEST",
            )

            status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/users",
                {
                    "username": "employee1",
                    "display_name": "Tracker Employee",
                    "pin": "345678",
                    "pin_confirm": "345678",
                },
                admin_cookie,
            )
            self.assertEqual(status, 201)
            status, _, employee_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/login",
                {"username": "employee1", "pin": "345678"},
            )
            self.assertEqual(status, 200)
            status, result, _ = self.http_request(
                httpd,
                "GET",
                "/api/houses/H-DEST",
                cookie=employee_cookie,
            )
            self.assertEqual(status, 403)
            self.assertIn("administrator", result["error"].lower())
            status, result, _ = self.http_request(
                httpd,
                "GET",
                "/api/houses/UNKNOWN",
                cookie=employee_cookie,
            )
            self.assertEqual(status, 403)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_fictional_demo_houses_provide_auto_calculator_dimensions(self):
        self.assertEqual(seed_demo.demo_opening_quantity(0), 2040)
        self.assertEqual(seed_demo.demo_opening_quantity(1), 2057)
        with server.connect_db() as db:
            with db:
                self.assertEqual(seed_demo.seed_sample_houses(db), 50)
                self.assertEqual(seed_demo.seed_sample_houses(db), 0)
            rows = db.execute(
                """
                SELECT id, postcode, address, owner_name, area_m2, perimeter_m, source
                FROM provided_houses WHERE source = 'fictional_sample'
                ORDER BY id
                """
            ).fetchall()
        self.assertEqual(len(rows), 50)
        self.assertEqual(rows[0]["id"], "SAMPLE-SEAI-001")
        self.assertEqual(rows[0]["postcode"], "SAMPLE-001")
        self.assertEqual(rows[0]["area_m2"], 78.0)
        self.assertEqual(rows[0]["perimeter_m"], 34.6)

        httpd, thread = self.start_test_server()
        try:
            status, _, admin_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/setup",
                {
                    "username": "admin1",
                    "display_name": "Tracker Admin",
                    "pin": "123456",
                },
            )
            self.assertEqual(status, 201)
            status, result, _ = self.http_request(
                httpd,
                "GET",
                "/api/calculator/houses?q=SAMPLE-SEAI-001",
                cookie=admin_cookie,
            )
            self.assertEqual(status, 200)
            self.assertEqual(len(result["houses"]), 1)
            house = result["houses"][0]
            self.assertEqual(house["area_m2"], 78.0)
            self.assertEqual(house["perimeter_m"], 34.6)
            self.assertEqual(house["source"], "fictional_sample")
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_house_checklist_is_readable_to_all_roles_but_editable_by_admins(self):
        with server.connect_db() as db:
            with db:
                server._upsert_house(db, "H-CHECKLIST")

        httpd, thread = self.start_test_server()
        try:
            status, _, _ = self.http_request(
                httpd, "GET", "/api/house-checklist/H-CHECKLIST"
            )
            self.assertEqual(status, 401)
            status, _, admin_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/setup",
                {
                    "username": "admin1",
                    "display_name": "Tracker Admin",
                    "pin": "123456",
                },
            )
            self.assertEqual(status, 201)
            status, checklist, _ = self.http_request(
                httpd,
                "GET",
                "/api/house-checklist/H-CHECKLIST",
                cookie=admin_cookie,
            )
            self.assertEqual(status, 200)
            self.assertIsNone(checklist["scaffolding_status"])
            self.assertIsNone(checklist["esb_unclipped"])
            self.assertIsNone(checklist["plumber_status"])
            status, dashboard, _ = self.http_request(
                httpd, "GET", "/api/dashboard", cookie=admin_cookie
            )
            self.assertEqual(status, 200)
            house = next(
                house
                for house in dashboard["house_list"]
                if house["id"] == "H-CHECKLIST"
            )
            self.assertIsNone(house["scaffolding_status"])
            self.assertIsNone(house["esb_unclipped"])
            self.assertIsNone(house["plumber_status"])

            status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/users",
                {
                    "username": "employee1",
                    "display_name": "Tracker Employee",
                    "pin": "345678",
                    "pin_confirm": "345678",
                },
                admin_cookie,
            )
            self.assertEqual(status, 201)
            status, _, employee_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/login",
                {"username": "employee1", "pin": "345678"},
            )
            self.assertEqual(status, 200)
            status, result, _ = self.http_request(
                httpd,
                "POST",
                "/api/house-checklist",
                {
                    "house_id": "H-CHECKLIST",
                    "scaffolding_status": "not_necessary",
                    "esb_unclipped": "not_esb",
                    "plumber_status": "not_necessary",
                },
                employee_cookie,
            )
            self.assertEqual(status, 403)
            self.assertIn("administrator", result["error"].lower())

            status, result, _ = self.http_request(
                httpd,
                "POST",
                "/api/house-checklist",
                {
                    "house_id": "H-CHECKLIST",
                    "scaffolding_status": "not_necessary",
                    "esb_unclipped": "not_esb",
                    "plumber_status": "not_necessary",
                },
                admin_cookie,
            )
            self.assertEqual(status, 200)
            self.assertTrue(result["saved"])
            status, checklist, _ = self.http_request(
                httpd,
                "GET",
                "/api/house-checklist/H-CHECKLIST",
                cookie=employee_cookie,
            )
            self.assertEqual(status, 200)
            self.assertEqual(checklist["scaffolding_status"], "not_necessary")
            self.assertEqual(checklist["esb_unclipped"], "not_esb")
            self.assertEqual(checklist["plumber_status"], "not_necessary")
            status, dashboard, _ = self.http_request(
                httpd, "GET", "/api/dashboard", cookie=employee_cookie
            )
            self.assertEqual(status, 200)
            house = next(
                house
                for house in dashboard["house_list"]
                if house["id"] == "H-CHECKLIST"
            )
            self.assertEqual(house["scaffolding_status"], "not_necessary")
            self.assertEqual(house["esb_unclipped"], "not_esb")
            self.assertEqual(house["plumber_status"], "not_necessary")

            status, result, _ = self.http_request(
                httpd,
                "POST",
                "/api/house-checklist",
                {
                    "house_id": "H-CHECKLIST",
                    "scaffolding_status": "sometimes",
                    "esb_unclipped": "yes",
                    "plumber_status": "done",
                },
                admin_cookie,
            )
            self.assertEqual(status, 400)
            self.assertIn("scaffolding", result["error"].lower())
            status, _, _ = self.http_request(
                httpd,
                "GET",
                "/api/houses/H-CHECKLIST",
                cookie=employee_cookie,
            )
            self.assertEqual(status, 403)
            status, _, _ = self.http_request(
                httpd,
                "GET",
                "/api/house-checklist/UNKNOWN",
                cookie=employee_cookie,
            )
            self.assertEqual(status, 404)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_dashboard_counts_each_team_house_once_by_house_status(self):
        material_id = self.add_material(opening=100)
        with server.connect_db() as db:
            with db:
                server._upsert_house(db, "H-OPEN")
                server._upsert_house(db, "H-CLOSED")
                server._upsert_house(db, "H-UNASSIGNED")

        for house_id in ("H-OPEN", "H-OPEN", "H-CLOSED"):
            self.add_movement(
                material_id,
                "pickup",
                1,
                house_id=house_id,
                person="Worker",
                team="Crew A",
                department="Electrical",
            )
        with server.connect_db() as db:
            with db:
                db.execute("UPDATE houses SET is_finished = 1 WHERE id = ?", ("H-CLOSED",))
        self.add_movement(
            material_id,
            "pickup",
            1,
            house_id="H-OPEN",
            person="Worker",
            team="Crew B",
        )
        self.add_movement(
            material_id,
            "transfer",
            1,
            source_kind="house",
            source_reference="H-OPEN",
            destination_kind="house",
            destination_reference="H-UNASSIGNED",
            person="Worker",
            team="Crew B",
        )

        with server.connect_db() as db:
            dashboard = server._dashboard(db)

        self.assertEqual(
            dashboard["team_house_counts"][
                server.team_location("Crew A", "Electrical")
            ],
            {
                "open": 1,
                "closed": 1,
                "open_house_ids": ["H-OPEN"],
                "closed_house_ids": ["H-CLOSED"],
            },
        )
        self.assertEqual(
            dashboard["team_house_counts"][server.team_location("Crew B")],
            {
                "open": 2,
                "closed": 0,
                "open_house_ids": ["H-OPEN", "H-UNASSIGNED"],
                "closed_house_ids": [],
            },
        )

    def test_finishing_house_records_remaining_stock_as_used(self):
        material_id = self.add_material(name="Board", opening=20)
        with server.connect_db() as db:
            with db:
                server._upsert_house(db, "H-FINISH", "D01", "Owner", "Worker")
                server._upsert_house(db, "H-SOURCE", "D02", "Other Owner", "Other Worker")
                movements = [
                    {
                        "kind": "pickup",
                        "house_id": "H-FINISH",
                        "qty": 6,
                        "person": "Worker",
                        "team": "Crew A",
                    },
                    {
                        "kind": "use",
                        "house_id": "H-FINISH",
                        "qty": 1,
                        "person": "Worker",
                        "team": "Crew A",
                    },
                    {
                        "kind": "return",
                        "house_id": "H-FINISH",
                        "qty": 1,
                        "person": "Worker",
                        "team": "Crew A",
                    },
                    {
                        "kind": "pickup",
                        "house_id": "H-SOURCE",
                        "qty": 4,
                        "person": "Other Worker",
                        "team": "Crew B",
                    },
                    {
                        "kind": "transfer",
                        "source_kind": "house",
                        "source_reference": "H-SOURCE",
                        "destination_kind": "house",
                        "destination_reference": "H-FINISH",
                        "person": "Worker",
                        "team": "Crew A",
                        "qty": 2,
                    },
                    {
                        "kind": "transfer",
                        "source_kind": "house",
                        "source_reference": "H-FINISH",
                        "destination_kind": "house",
                        "destination_reference": "H-SOURCE",
                        "person": "Worker",
                        "team": "Crew A",
                        "qty": 1,
                    },
                ]
                for movement in movements:
                    server._record_movement(
                        db,
                        {
                            "date": date.today().isoformat(),
                            "material_id": material_id,
                            **movement,
                        },
                    )
                self.assertEqual(
                    server.balances_for(db, material_id)["house:H-FINISH"],
                    5,
                )

        httpd, thread = self.start_test_server()
        try:
            status, _, admin_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/setup",
                {
                    "username": "admin1",
                    "display_name": "Tracker Admin",
                    "pin": "123456",
                },
            )
            self.assertEqual(status, 201)
            status, result, _ = self.http_request(
                httpd,
                "POST",
                "/api/houses/status",
                {"id": "H-FINISH", "finished": True},
                admin_cookie,
            )
            self.assertEqual(status, 200)
            self.assertEqual(result["materials_closed_out"], 1)

            with server.connect_db() as db:
                self.assertAlmostEqual(
                    server.balances_for(db, material_id)["house:H-FINISH"],
                    0,
                )
                usage = db.execute(
                    """
                    SELECT SUM(qty) AS total, COUNT(*) AS count
                    FROM movements
                    WHERE kind = 'use' AND house_id = ?
                    """,
                    ("H-FINISH",),
                ).fetchone()
                self.assertEqual(usage["count"], 2)
                self.assertAlmostEqual(usage["total"], 6)
                closeout = db.execute(
                    """
                    SELECT person, team, notes FROM movements
                    WHERE kind = 'use' AND house_id = ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    ("H-FINISH",),
                ).fetchone()
                self.assertEqual(closeout["person"], "Tracker Admin")
                self.assertEqual(closeout["team"], "")
                self.assertIn("automatically recorded", closeout["notes"].lower())

            status, result, _ = self.http_request(
                httpd,
                "POST",
                "/api/houses/status",
                {"id": "H-FINISH", "finished": True},
                admin_cookie,
            )
            self.assertEqual(status, 200)
            self.assertEqual(result["materials_closed_out"], 0)
            with server.connect_db() as db:
                self.assertEqual(
                    db.execute(
                        "SELECT COUNT(*) FROM movements "
                        "WHERE kind = 'use' AND house_id = ?",
                        ("H-FINISH",),
                    ).fetchone()[0],
                    2,
                )
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_finishing_house_requires_negative_balances_to_be_reconciled(self):
        material_id = self.add_material(name="Board", opening=10)
        with server.connect_db() as db:
            with db:
                server._upsert_house(db, "H-NEGATIVE")
                server._record_movement(
                    db,
                    {
                        "kind": "transfer",
                        "date": date.today().isoformat(),
                        "material_id": material_id,
                        "qty": 1,
                        "source_kind": "house",
                        "source_reference": "H-NEGATIVE",
                        "destination_kind": "team",
                        "destination_reference": "Crew A",
                        "person": "Worker",
                        "team": "Crew A",
                    },
                    check_balance=False,
                )

        httpd, thread = self.start_test_server()
        try:
            _, _, admin_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/setup",
                {
                    "username": "admin1",
                    "display_name": "Tracker Admin",
                    "pin": "123456",
                },
            )
            status, result, _ = self.http_request(
                httpd,
                "POST",
                "/api/houses/status",
                {"id": "H-NEGATIVE", "finished": True},
                admin_cookie,
            )
            self.assertEqual(status, 409)
            self.assertIn("negative", result["error"].lower())
            with server.connect_db() as db:
                house = db.execute(
                    "SELECT is_finished FROM houses WHERE id = ?",
                    ("H-NEGATIVE",),
                ).fetchone()
                self.assertEqual(house["is_finished"], 0)
                self.assertEqual(
                    db.execute(
                        "SELECT COUNT(*) FROM movements "
                        "WHERE kind = 'use' AND house_id = ?",
                        ("H-NEGATIVE",),
                    ).fetchone()[0],
                    0,
                )
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_employee_cannot_change_stock_or_house_stage(self):
        material_id = self.add_material(opening=10)
        with server.connect_db() as db:
            with db:
                server._upsert_house(
                    db, "H-EXISTING", "A1", "Original Owner", "Original Employee"
                )
                server._upsert_house(
                    db, "H-DEST", "B2", "Destination Owner", "Destination Employee"
                )
        httpd, thread = self.start_test_server()
        try:
            _, _, admin_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/setup",
                {
                    "username": "admin1",
                    "display_name": "Tracker Admin",
                    "pin": "123456",
                },
            )
            self.http_request(
                httpd,
                "POST",
                "/api/users",
                {
                    "username": "employee1",
                    "display_name": "Tracker Employee",
                    "pin": "345678",
                    "pin_confirm": "345678",
                },
                admin_cookie,
            )
            _, _, employee_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/login",
                {
                    "username": "employee1",
                    "pin": "345678",
                },
            )
            for path, payload in (
                ("/api/stocktakes", {}),
                ("/api/materials", {}),
                ("/api/houses", {"id": "H-NEW"}),
                (
                    "/api/houses/status",
                    {"id": "H-EXISTING", "finished": True},
                ),
                ("/api/import", {}),
            ):
                status, _, _ = self.http_request(
                    httpd, "POST", path, payload, employee_cookie
                )
                self.assertEqual(status, 403, path)

            status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/movements/batch",
                {
                    "kind": "pickup",
                    "date": date.today().isoformat(),
                    "house_id": "H-EXISTING",
                    "postcode": "Changed",
                    "owner_name": "Changed Owner",
                    "employee_name": "Changed Employee",
                    "person": "Worker",
                    "team": "Crew A",
                    "items": [{"material_id": material_id, "qty": 1}],
                },
                employee_cookie,
            )
            self.assertEqual(status, 201)
            status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/movements/batch",
                {
                    "kind": "transfer",
                    "date": date.today().isoformat(),
                    "source_kind": "house",
                    "source_reference": "H-EXISTING",
                    "source_postcode": "Changed Source",
                    "destination_kind": "house",
                    "destination_reference": "H-DEST",
                    "destination_postcode": "Changed Destination",
                    "person": "Worker",
                    "team": "Crew A",
                    "items": [{"material_id": material_id, "qty": 1}],
                },
                employee_cookie,
            )
            self.assertEqual(status, 201)
            status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/movements/batch",
                {
                    "kind": "transfer",
                    "date": date.today().isoformat(),
                    "source_kind": "house",
                    "source_reference": "H-EXISTING",
                    "destination_kind": "house",
                    "destination_reference": "H-NEW",
                    "person": "Worker",
                    "team": "Crew A",
                    "items": [{"material_id": material_id, "qty": 1}],
                },
                employee_cookie,
            )
            self.assertEqual(status, 403)
            with server.connect_db() as db:
                house = db.execute(
                    "SELECT postcode, owner_name, employee_name, is_finished "
                    "FROM houses WHERE id = 'H-EXISTING'"
                ).fetchone()
                self.assertEqual(
                    tuple(house),
                    ("A1", "Original Owner", "Original Employee", 0),
                )
                destination = db.execute(
                    "SELECT postcode, owner_name, employee_name FROM houses "
                    "WHERE id = 'H-DEST'"
                ).fetchone()
                self.assertEqual(
                    tuple(destination),
                    ("B2", "Destination Owner", "Destination Employee"),
                )
                self.assertIsNone(
                    db.execute(
                        "SELECT 1 FROM houses WHERE id = 'H-NEW'"
                    ).fetchone()
                )
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_surveyor_can_only_access_ready_house_surveys(self):
        httpd, thread = self.start_test_server()
        try:
            _, _, admin_cookie = self.http_request(
                httpd, "POST", "/api/auth/setup",
                {"username": "admin1", "display_name": "Admin", "pin": "123456"},
            )
            status, _, _ = self.http_request(
                httpd, "POST", "/api/users",
                {"username": "survey1", "display_name": "Surveyor", "pin": "234567", "role": "surveyor"},
                admin_cookie,
            )
            self.assertEqual(status, 201)
            status, _, survey_cookie = self.http_request(
                httpd, "POST", "/api/auth/login", {"username": "survey1", "pin": "234567"}
            )
            self.assertEqual(status, 200)
            with server.connect_db() as db:
                with db:
                    db.executemany(
                        "INSERT INTO provided_houses(id, ready_for_start, source) VALUES (?, ?, 'tracker')",
                        [("H-READY", 1), ("H-PENDING", 0)],
                    )
            status, result, _ = self.http_request(
                httpd, "GET", "/api/calculator/houses?status=not_started", cookie=survey_cookie
            )
            self.assertEqual(status, 200)
            self.assertEqual([h["id"] for h in result["houses"]], ["H-READY"])
            survey = {
                "windows": [{"w": 1.2, "h": 1.0, "qty": 3}],
                "doors": [{"w": 0.9, "h": 2.0, "qty": 1}],
                "house_corners": [2.5], "notes": "Check access",
            }
            for invalid_survey in [
                {**survey, "windows": [{"w": 1.2, "h": 1.0, "qty": 0}]},
                {**survey, "doors": [{"w": float("nan"), "h": 2.0, "qty": 1}]},
                {**survey, "house_corners": [-1]},
                {**survey, "notes": "x" * 2001},
            ]:
                status, _, _ = self.http_request(
                    httpd, "POST", "/api/house-survey",
                    {"house_id": "H-READY", "survey": invalid_survey}, survey_cookie,
                )
                self.assertEqual(status, 400)
            status, result, _ = self.http_request(
                httpd, "POST", "/api/house-survey",
                {"house_id": "H-READY", "survey": survey}, survey_cookie,
            )
            self.assertEqual(status, 200)
            self.assertTrue(result["saved"])
            status, result, _ = self.http_request(
                httpd, "GET", "/api/calculator/houses?q=H-READY", cookie=survey_cookie
            )
            self.assertEqual(status, 200)
            self.assertEqual(result["houses"][0]["survey"], survey)
            status, _, _ = self.http_request(
                httpd, "GET", "/api/dashboard", cookie=survey_cookie
            )
            self.assertEqual(status, 403)
            with server.connect_db() as db:
                with db:
                    db.execute("UPDATE provided_houses SET ready_for_start = 0 WHERE id = 'H-READY'")
            status, _, _ = self.http_request(
                httpd, "POST", "/api/house-survey",
                {"house_id": "H-READY", "survey": survey}, survey_cookie,
            )
            self.assertEqual(status, 409)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_admin_can_view_unstarted_house_notes_snags_and_detached_movement_history(self):
        httpd, thread = self.start_test_server()
        try:
            _, _, admin_cookie = self.http_request(
                httpd, "POST", "/api/auth/setup",
                {"username": "admin1", "display_name": "Admin", "pin": "123456"},
            )
            material_id = self.add_material()
            with server.connect_db() as db:
                with db:
                    db.execute(
                        "INSERT INTO provided_houses(id, postcode, owner_name, notes) "
                        "VALUES ('H-UNSTARTED', 'A1', 'Owner', 'House note retained')"
                    )
                    db.execute(
                        "INSERT INTO house_surveys(house_id, data, notes, updated_by, updated_at) "
                        "VALUES ('H-UNSTARTED', '{}', 'Survey note retained', 'Surveyor', ?)",
                        (date.today().isoformat(),),
                    )
                    db.execute(
                        "INSERT INTO house_snags(house_id, text, done, created_at) "
                        "VALUES ('H-UNSTARTED', 'Repair the sill', 1, 1000)"
                    )
                    server._upsert_house(db, "H-UNSTARTED", "A1", "Owner", "Worker")
                    server._record_movement(db, {
                        "kind": "pickup",
                        "date": date.today().isoformat(),
                        "material_id": material_id,
                        "qty": 2,
                        "house_id": "H-UNSTARTED",
                        "person": "Worker",
                        "team": "Crew",
                    })
                    db.execute(
                        "UPDATE movements SET house_id = NULL WHERE house_id = 'H-UNSTARTED'"
                    )
                    db.execute("DELETE FROM houses WHERE id = 'H-UNSTARTED'")
            status, details, _ = self.http_request(
                httpd, "GET", "/api/houses/H-UNSTARTED", cookie=admin_cookie,
            )
            self.assertEqual(status, 200)
            self.assertEqual(details["house"]["status"], "not_started")
            self.assertEqual(details["house"]["notes"], "House note retained")
            self.assertEqual(details["house"]["survey_notes"], "Survey note retained")
            self.assertEqual(details["house"]["snags"][0]["text"], "Repair the sill")
            self.assertTrue(details["house"]["snags"][0]["done"])
            self.assertEqual(len(details["movements"]), 1)
            self.assertIsNone(details["movements"][0]["house_id"])
            self.assertEqual(details["movements"][0]["destination"], "house:H-UNSTARTED")
            status, dashboard, _ = self.http_request(
                httpd, "GET", "/api/dashboard", cookie=admin_cookie,
            )
            self.assertEqual(status, 200)
            self.assertEqual(dashboard["house_summary"]["provided"], 1)
            self.assertEqual(dashboard["house_summary"]["active"], 0)
            self.assertEqual(dashboard["house_summary"]["closed"], 0)
            self.assertEqual(dashboard["house_summary"]["ready"], 0)
            self.assertEqual(dashboard["house_summary"]["not_started"], 1)
            self.assertEqual(len(dashboard["movements"]), 1)
            status, _, _ = self.http_request(
                httpd, "POST", "/api/houses/archive",
                {"id": "H-UNSTARTED", "archived": False}, admin_cookie,
            )
            self.assertEqual(status, 404)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_surveyor_role_migration_preserves_users_and_sessions(self):
        legacy_path = Path(self.temp_dir.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy_path) as db:
            db.executescript("""
                CREATE TABLE app_users (
                    id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    display_name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('admin', 'employee')),
                    password_salt BLOB NOT NULL, password_hash BLOB NOT NULL,
                    password_iterations INTEGER NOT NULL,
                    credential_type TEXT NOT NULL DEFAULT 'pin', is_active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE app_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
                    expires_at INTEGER NOT NULL
                );
                INSERT INTO app_users VALUES (1, 'admin1', 'Admin', 'admin', X'01', X'02', 1, 'pin', 1);
                INSERT INTO app_sessions VALUES ('session-hash', 1, 9999999999);
            """)
        db.close()
        original_path = server.DB_PATH
        try:
            server.DB_PATH = legacy_path
            server.initialize_db()
            with server.connect_db() as db:
                self.assertEqual(db.execute("SELECT role FROM app_users WHERE id = 1").fetchone()[0], "admin")
                self.assertEqual(db.execute("SELECT user_id FROM app_sessions WHERE token_hash = 'session-hash'").fetchone()[0], 1)
                db.execute("UPDATE app_users SET role = 'surveyor' WHERE id = 1")
        finally:
            server.DB_PATH = original_path

    def test_movement_flow_updates_warehouse_team_and_house_balances(self):
        material_id = self.add_material()
        self.add_movement(material_id, "collection", 6, person="A", team="Crew A")
        self.add_movement(
            material_id,
            "delivery",
            4,
            person="B",
            team="Crew A",
            house_id="H-1",
            source_kind="team",
        )
        self.add_movement(
            material_id, "use", 1, person="B", team="Crew A", house_id="H-1"
        )
        self.add_movement(
            material_id, "return", 1, person="B", team="Crew A", house_id="H-1"
        )

        balances = self.balances(material_id)
        self.assertEqual(balances["warehouse"], 5)
        self.assertEqual(balances["team:Crew A"], 2)
        self.assertEqual(balances["house:H-1"], 2)

    def test_collection_can_be_linked_to_a_house_and_owner(self):
        material_id = self.add_material()
        self.add_movement(
            material_id,
            "collection",
            2,
            person="A",
            team="Crew A",
            house_id="H-1",
            postcode="P1",
            owner_name="House Owner",
        )
        with server.connect_db() as db:
            movement = server._movement_rows(db, 1)[0]
            self.assertEqual(movement["house_id"], "H-1")
            self.assertEqual(movement["house_owner"], "House Owner")
            house = db.execute("SELECT postcode FROM houses WHERE id = 'H-1'").fetchone()
            self.assertEqual(house["postcode"], "P1")

    def test_new_house_details_are_saved_and_returned_for_search(self):
        with server.connect_db() as db:
            with db:
                server._upsert_house(
                    db,
                    "H-SEARCH",
                    postcode="A1 B2C3",
                    owner_name="Owner A",
                    employee_name="Employee A",
                )
            dashboard = server._dashboard(db)
        house = next(item for item in dashboard["house_list"] if item["id"] == "H-SEARCH")
        self.assertEqual(house["postcode"], "A1 B2C3")
        self.assertEqual(house["owner_name"], "Owner A")
        self.assertEqual(house["employee_name"], "Employee A")

    def test_dashboard_reports_demo_mode_setting(self):
        with server.connect_db() as db:
            self.assertFalse(server._dashboard(db)["demo_mode"])
            with db:
                db.execute(
                    "INSERT INTO app_settings(key, value) VALUES ('demo_mode', '1')"
                )
            self.assertTrue(server._dashboard(db)["demo_mode"])

    def test_house_can_be_marked_finished_without_hiding_its_balances(self):
        material_id = self.add_material()
        self.add_movement(
            material_id,
            "pickup",
            2,
            person="A",
            team="Crew A",
            house_id="H-FINISHED",
        )
        with server.connect_db() as db:
            with db:
                db.execute(
                    "UPDATE houses SET is_finished = 1 WHERE id = 'H-FINISHED'"
                )
            house = next(
                item for item in server._dashboard(db)["house_list"]
                if item["id"] == "H-FINISHED"
            )
            self.assertEqual(server.balances_for(db, material_id)["house:H-FINISHED"], 2)
        self.assertEqual(house["is_finished"], 1)

    def test_dashboard_active_house_count_excludes_finished_houses(self):
        with server.connect_db() as db:
            with db:
                server._upsert_house(db, "H-ACTIVE")
                server._upsert_house(db, "H-FINISHED")
                db.execute(
                    "UPDATE houses SET is_finished = 1 WHERE id = 'H-FINISHED'"
                )
            dashboard = server._dashboard(db)
        self.assertEqual(dashboard["houses"], 1)
        self.assertEqual(len(dashboard["house_list"]), 2)

    def test_house_summary_derives_from_provided_list(self):
        with server.connect_db() as db:
            with db:
                db.execute(
                    "INSERT INTO provided_houses(id, postcode, owner_name, source) "
                    "VALUES ('P-1', '', '', 'government'), ('P-2', '', '', 'government'), "
                    "('P-3', '', '', 'government')"
                )
                server._upsert_house(db, "P-1")
                server._upsert_house(db, "P-2")
                db.execute("UPDATE houses SET is_finished = 1 WHERE id = 'P-2'")
            summary = server._dashboard(db)["house_summary"]
        self.assertEqual(
            summary, {"provided": 3, "active": 1, "closed": 1, "ready": 0, "not_started": 1}
        )

    def test_legacy_archive_column_is_removed_from_house_schema(self):
        if sqlite3.sqlite_version_info < (3, 35, 0):
            self.skipTest("SQLite before 3.35 does not support DROP COLUMN.")
        with server.connect_db() as db:
            db.execute(
                "ALTER TABLE houses ADD COLUMN is_archived INTEGER NOT NULL DEFAULT 0"
            )
        server.initialize_db()
        with server.connect_db() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(houses)")}
        self.assertNotIn("is_archived", columns)

    def test_dashboard_orders_open_houses_by_latest_status_change(self):
        with server.connect_db() as db:
            with db:
                server._upsert_house(db, "H-OLDER")
                server._upsert_house(db, "H-NEWER")
                db.execute(
                    "UPDATE houses SET status_changed_at = 100 WHERE id = 'H-OLDER'"
                )
                db.execute(
                    "UPDATE houses SET status_changed_at = 200 WHERE id = 'H-NEWER'"
                )
            house_ids = [
                house["id"] for house in server._dashboard(db)["house_list"]
            ]

        self.assertEqual(house_ids[:2], ["H-NEWER", "H-OLDER"])

    def test_extra_warehouse_pickups_must_be_marked_and_numbered(self):
        material_id = self.add_material(name="Board", opening=20)
        payload = {
            "kind": "pickup",
            "date": date.today().isoformat(),
            "house_id": "H-1",
            "person": "A",
            "team": "Crew A",
            "items": [{"material_id": material_id, "qty": 2}],
        }
        with server.connect_db() as db:
            with db:
                _, first_number = server._record_movement_batch(
                    db, {**payload, "is_extra_delivery": False}
                )
                with self.assertRaisesRegex(server.ApiError, "Mark this pickup"):
                    server._record_movement_batch(
                        db, {**payload, "is_extra_delivery": False}
                    )
                _, extra_number = server._record_movement_batch(
                    db, {**payload, "is_extra_delivery": True}
                )
            self.assertEqual(first_number, 1)
            self.assertEqual(extra_number, 2)
            recorded = [
                row[0]
                for row in db.execute(
                    "SELECT delivery_number FROM movements ORDER BY id"
                )
            ]
        self.assertEqual(recorded, [1, 2])

    def test_search_returns_all_matching_movements_beyond_default_limit(self):
        material_id = self.add_material(opening=100)
        for index in range(3):
            self.add_movement(
                material_id,
                "pickup",
                1,
                person="A",
                team="Crew A",
                house_id=f"H-{index}",
                employee_name="Searchable Employee",
            )
        with server.connect_db() as db:
            result = server._movement_rows(db, limit=1, query="Searchable Employee")
        self.assertEqual(len(result), 3)

    def test_batch_pickup_assigns_multiple_materials_directly_to_house(self):
        board_id = self.add_material(name="Board", opening=15)
        glue_id = self.add_material(name="Glue", opening=8)
        with server.connect_db() as db:
            with db:
                movement_ids, delivery_number = server._record_movement_batch(
                    db,
                    {
                        "kind": "pickup",
                        "date": date.today().isoformat(),
                        "house_id": "H-1",
                        "owner_name": "Owner A",
                        "employee_name": "Employee A",
                        "person": "Employee A",
                        "team": "Crew A",
                        "items": [
                            {"material_id": board_id, "qty": 5},
                            {"material_id": glue_id, "qty": 2},
                        ],
                    },
                )
            house = db.execute(
                "SELECT owner_name, employee_name FROM houses WHERE id = 'H-1'"
            ).fetchone()
            dashboard_house = next(
                item for item in server._dashboard(db)["house_list"] if item["id"] == "H-1"
            )
        self.assertEqual(len(movement_ids), 2)
        self.assertEqual(delivery_number, 1)
        self.assertEqual(house["owner_name"], "Owner A")
        self.assertEqual(house["employee_name"], "Employee A")
        self.assertEqual(dashboard_house["employee_name"], "Employee A")
        self.assertEqual(self.balances(board_id)["house:H-1"], 5)
        self.assertEqual(self.balances(glue_id)["house:H-1"], 2)
        self.assertEqual(self.balances(board_id)["warehouse"], 10)
        self.assertEqual(self.balances(glue_id)["warehouse"], 6)

    def test_house_leftovers_return_to_warehouse(self):
        material_id = self.add_material(name="Board", opening=12)
        self.add_movement(
            material_id,
            "pickup",
            7,
            person="A",
            team="Crew A",
            house_id="H-1",
        )
        self.add_movement(
            material_id,
            "return",
            3,
            person="A",
            team="Crew A",
            house_id="H-1",
        )
        balances = self.balances(material_id)
        self.assertEqual(balances["house:H-1"], 4)
        self.assertEqual(balances["warehouse"], 8)

    def test_batch_transfer_moves_material_between_sites(self):
        material_id = self.add_material(name="Board", opening=12)
        self.add_movement(
            material_id,
            "pickup",
            7,
            person="A",
            team="Crew A",
            house_id="H-1",
        )
        with server.connect_db() as db:
            with db:
                movement_ids, delivery_number = server._record_movement_batch(
                    db,
                    {
                        "kind": "transfer",
                        "date": date.today().isoformat(),
                        "source_kind": "house",
                        "source_reference": "H-1",
                        "destination_kind": "house",
                        "destination_reference": "H-2",
                        "person": "A",
                        "team": "Crew A",
                        "items": [{"material_id": material_id, "qty": 4}],
                    },
                )
        balances = self.balances(material_id)
        self.assertEqual(len(movement_ids), 1)
        self.assertIsNone(delivery_number)
        self.assertEqual(balances["house:H-1"], 3)
        self.assertEqual(balances["house:H-2"], 4)
        self.assertEqual(balances["warehouse"], 5)

    def test_site_transfer_rejects_insufficient_source_and_rolls_back_batch(self):
        material_id = self.add_material(name="Board", opening=12)
        self.add_movement(
            material_id,
            "pickup",
            2,
            person="A",
            team="Crew A",
            house_id="H-1",
        )
        with server.connect_db() as db:
            with self.assertRaisesRegex(server.ApiError, "Insufficient stock"):
                with db:
                    server._record_movement_batch(
                        db,
                        {
                            "kind": "transfer",
                            "date": date.today().isoformat(),
                            "source_kind": "house",
                            "source_reference": "H-1",
                            "destination_kind": "house",
                            "destination_reference": "H-2",
                            "person": "A",
                            "team": "Crew A",
                            "items": [{"material_id": material_id, "qty": 3}],
                        },
                    )
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM movements WHERE kind = 'transfer'"
                ).fetchone()[0],
                0,
            )
            self.assertIsNone(
                db.execute("SELECT 1 FROM houses WHERE id = 'H-2'").fetchone()
            )

    def test_batch_delivery_numbers_increment_once_per_house_delivery(self):
        board_id = self.add_material(name="Board", opening=20)
        glue_id = self.add_material(name="Glue", opening=10)
        with server.connect_db() as db:
            with db:
                first_ids, first_number = server._record_movement_batch(
                    db,
                    {
                        "kind": "delivery",
                        "date": date.today().isoformat(),
                        "house_id": "H-1",
                        "person": "A",
                        "team": "Crew A",
                        "items": [
                            {"material_id": board_id, "qty": 3},
                            {"material_id": glue_id, "qty": 2},
                        ],
                    },
                )
                second_ids, second_number = server._record_movement_batch(
                    db,
                    {
                        "kind": "delivery",
                        "date": date.today().isoformat(),
                        "house_id": "H-1",
                        "person": "A",
                        "team": "Crew A",
                        "items": [{"material_id": board_id, "qty": 1}],
                    },
                )
            numbers = [
                row["delivery_number"]
                for row in db.execute(
                    "SELECT delivery_number FROM movements WHERE id IN (?, ?, ?)",
                    (*first_ids, *second_ids),
                )
            ]
        self.assertEqual(first_number, 1)
        self.assertEqual(second_number, 2)
        self.assertEqual(numbers.count(1), 2)
        self.assertEqual(numbers.count(2), 1)

    def test_warehouse_pickups_for_a_house_are_numbered_as_deliveries(self):
        material_id = self.add_material(name="Board", opening=20)
        with server.connect_db() as db:
            with db:
                _, first_number = server._record_movement_batch(
                    db,
                    {
                        "kind": "pickup",
                        "date": date.today().isoformat(),
                        "house_id": "H-1",
                        "person": "A",
                        "team": "Crew A",
                        "items": [{"material_id": material_id, "qty": 3}],
                    },
                )
                _, second_number = server._record_movement_batch(
                    db,
                    {
                        "kind": "pickup",
                        "date": date.today().isoformat(),
                        "house_id": "H-1",
                        "person": "A",
                        "team": "Crew A",
                        "items": [{"material_id": material_id, "qty": 1}],
                    },
                )
        self.assertEqual(first_number, 1)
        self.assertEqual(second_number, 2)

    def test_failed_batch_rolls_back_every_material_line(self):
        board_id = self.add_material(name="Board", opening=10)
        glue_id = self.add_material(name="Glue", opening=1)
        with server.connect_db() as db:
            with self.assertRaisesRegex(server.ApiError, "Insufficient stock"):
                with db:
                    server._record_movement_batch(
                        db,
                        {
                            "kind": "pickup",
                            "date": date.today().isoformat(),
                            "house_id": "H-1",
                            "person": "A",
                            "team": "Crew A",
                            "items": [
                                {"material_id": board_id, "qty": 2},
                                {"material_id": glue_id, "qty": 2},
                            ],
                        },
                    )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM movements").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM houses").fetchone()[0], 0)

    def test_collection_cannot_exceed_known_warehouse_stock(self):
        material_id = self.add_material(opening=2)
        with self.assertRaisesRegex(server.ApiError, "Insufficient stock"):
            self.add_movement(material_id, "collection", 3, person="A", team="Crew A")
        self.assertEqual(self.balances(material_id)["warehouse"], 2)

    def test_every_new_movement_requires_person_and_team(self):
        material_id = self.add_material()
        with self.assertRaisesRegex(server.ApiError, "person and team"):
            self.add_movement(material_id, "collection", 1, person="A")

    def test_team_names_match_case_insensitively(self):
        material_id = self.add_material()
        self.add_movement(material_id, "collection", 2, person="A", team="Crew A")
        with server.connect_db() as db:
            with db:
                server._record_movement(
                    db,
                    {
                        "material_id": material_id,
                        "kind": "transfer",
                        "qty": 1,
                        "date": date.today().isoformat(),
                        "person": "A",
                        "team": "Crew A",
                        "source_kind": "team",
                        "source_reference": "crew a",
                        "destination_kind": "team",
                        "destination_reference": "Crew B",
                    },
                )
        balances = self.balances(material_id)
        self.assertEqual(balances["team:Crew A"], 1)
        self.assertEqual(balances["team:Crew B"], 1)
        self.assertNotIn("team:crew a", balances)

    def test_same_team_name_is_scoped_by_department(self):
        material_id = self.add_material()
        self.add_movement(
            material_id, "collection", 3, person="A", team="Crew", department="North"
        )
        self.add_movement(
            material_id, "collection", 4, person="B", team="Crew", department="South"
        )
        balances = self.balances(material_id)
        self.assertEqual(balances[server.team_location("Crew", "North")], 3)
        self.assertEqual(balances[server.team_location("Crew", "South")], 4)

        self.add_movement(
            material_id,
            "transfer",
            1,
            person="A",
            team="Crew",
            department="North",
            source_kind="team",
            source_reference="Crew",
            source_department="North",
            destination_kind="team",
            destination_reference="Crew",
            destination_department="South",
        )
        balances = self.balances(material_id)
        self.assertEqual(balances[server.team_location("Crew", "North")], 2)
        self.assertEqual(balances[server.team_location("Crew", "South")], 5)

    def test_stocktake_sets_unknown_warehouse_balance(self):
        material_id = self.add_material(opening=None)
        with self.assertRaisesRegex(server.ApiError, "balance is unknown"):
            self.add_movement(material_id, "collection", 1, person="A", team="Crew A")

        with server.connect_db() as db:
            with db:
                db.execute(
                    """
                    INSERT INTO stocktakes
                        (material_id, location, quantity, movement_id, counted_on, counted_by)
                    VALUES (?, 'warehouse', 5, 0, ?, 'A')
                    """,
                    (material_id, date.today().isoformat()),
                )
        self.add_movement(material_id, "collection", 2, person="A", team="Crew A")
        self.assertEqual(self.balances(material_id)["warehouse"], 3)

    def test_initialize_migrates_existing_house_movements(self):
        with server.connect_db() as db:
            db.executescript(
                """
                DROP TABLE movements;
                INSERT INTO materials(code, name, unit, opening_qty)
                VALUES ('B1', 'Legacy Board', 'boards', 10);
                INSERT INTO houses(id) VALUES ('H-OLD');
                CREATE TABLE movements (
                    id INTEGER PRIMARY KEY,
                    happened_on TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    material_id INTEGER NOT NULL,
                    qty REAL NOT NULL,
                    source TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    person TEXT NOT NULL DEFAULT '',
                    team TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT ''
                );
                INSERT INTO movements
                    (happened_on, kind, material_id, qty, source, destination)
                VALUES ('2026-01-01', 'delivery', 1, 2, 'warehouse', 'house:H-OLD');
                INSERT INTO movements
                    (happened_on, kind, material_id, qty, source, destination)
                VALUES ('2026-01-02', 'delivery', 1, 1, 'warehouse', 'house:H-OLD');
                """
            )
        server.initialize_db()
        with server.connect_db() as db:
            rows = db.execute(
                "SELECT house_id, delivery_number FROM movements ORDER BY id"
            ).fetchall()
            self.assertEqual([row["house_id"] for row in rows], ["H-OLD", "H-OLD"])
            self.assertEqual([row["delivery_number"] for row in rows], [1, 2])

    def test_house_checklists_survive_resetting_active_houses(self):
        with server.connect_db() as db:
            db.executescript(
                """
                INSERT INTO provided_houses(id) VALUES ('H-CHECK');
                INSERT INTO houses(id) VALUES ('H-CHECK');
                DROP TABLE house_checklists;
                CREATE TABLE house_checklists (
                    house_id TEXT PRIMARY KEY REFERENCES houses(id) ON DELETE CASCADE,
                    scaffolding_status TEXT,
                    esb_unclipped TEXT,
                    plumber_status TEXT
                );
                INSERT INTO house_checklists
                    (house_id, scaffolding_status, esb_unclipped, plumber_status)
                VALUES ('H-CHECK', 'done', 'yes', 'not_necessary');
                """
            )

        server.initialize_db()
        with server.connect_db() as db:
            self.assertEqual(
                tuple(db.execute(
                    "SELECT scaffolding_status, esb_unclipped, plumber_status "
                    "FROM house_checklists WHERE house_id = 'H-CHECK'"
                ).fetchone()),
                ("done", "yes", "not_necessary"),
            )
            parent = db.execute(
                "PRAGMA foreign_key_list(house_checklists)"
            ).fetchone()["table"]
            self.assertEqual(parent, "provided_houses")
            db.execute("DELETE FROM houses WHERE id = 'H-CHECK'")
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM house_checklists WHERE house_id = 'H-CHECK'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def create_workbook(self, opening, movements):
        path = Path(self.temp_dir.name) / "input.xlsx"
        workbook = Workbook()
        stock = workbook.active
        stock.title = "STOCK"
        stock.append(["Material ID", "Material", "Starting Stock"])
        stock.append(["B1", "Board", opening])
        sheet = workbook.create_sheet("MOVEMENTS")
        sheet.append(
            [
                "Date", "House ID", "Postcode / Eircode", "Material", "Qty",
                "Movement", "Notes", "Material ID", "House Owner Name",
                "Delivered By", "Worker Team", "Column1",
            ]
        )
        for row in movements:
            sheet.append(row)
        workbook.save(path)
        return path

    def test_import_maps_legacy_deliveries_returns_and_usage(self):
        workbook = self.create_workbook(
            20,
            [
                [date(2026, 1, 1), "H-1", "P1", "Board", 4, "Delivery to House", "", "B1", "Owner", "A", "Crew", ""],
                [date(2026, 1, 2), "H-1", "P1", "Board", 1, "Return to Warehouse", "", "B1", "Owner", "A", "Crew", ""],
                [date(2026, 1, 3), "H-1", "P1", "Board", 1, "Used on House", "", "B1", "Owner", "A", "Crew", ""],
            ],
        )
        with server.connect_db() as db:
            with db:
                result = server._import_workbook(db, workbook)
            material_id = db.execute("SELECT id FROM materials WHERE name = 'Board'").fetchone()[0]

        balances = self.balances(material_id)
        self.assertEqual(result["movement_count"], 3)
        self.assertEqual(balances["warehouse"], 17)
        self.assertEqual(balances["house:H-1"], 2)

    def test_import_marks_unknown_starting_stock_for_stocktake(self):
        workbook = self.create_workbook(
            "?",
            [
                [date(2026, 1, 1), "H-1", "", "Board", 2, "Delivery to House", "", "B1", "", "A", "Crew", ""],
            ],
        )
        with server.connect_db() as db:
            with db:
                result = server._import_workbook(db, workbook)
            material_id = db.execute("SELECT id FROM materials WHERE name = 'Board'").fetchone()[0]
        self.assertTrue(any("unknown" in warning for warning in result["warnings"]))
        self.assertIsNone(self.balances(material_id)["warehouse"])

    def test_import_parses_day_first_text_dates(self):
        workbook = self.create_workbook(
            20,
            [
                ["03/09/2026", "H-1", "", "Board", 2, "Delivery to House", "", "B1", "", "A", "Crew", ""],
            ],
        )
        with server.connect_db() as db:
            with db:
                server._import_workbook(db, workbook)
            saved_date = db.execute("SELECT happened_on FROM movements").fetchone()[0]
        self.assertEqual(saved_date, "2026-09-03")

    def test_import_skips_zero_quantity_template_rows(self):
        workbook = self.create_workbook(
            20,
            [
                [date(2026, 1, 1), "test", "", "Board", 0, "Delivery to House", "", "B1", "", "", "", ""],
            ],
        )
        with server.connect_db() as db:
            with db:
                report = server._import_workbook(db, workbook)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM movements").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM houses").fetchone()[0], 0)
        self.assertTrue(any("zero-quantity" in warning for warning in report["warnings"]))


if __name__ == "__main__":
    unittest.main()
