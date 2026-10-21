"""Tests for the SEAI provided houses -> active house -> calculator -> record movement flow."""
import http.client
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path

import seed_houses
import server

ROOT = Path(__file__).resolve().parent.parent
CALC_HTML = (ROOT / "Auto_calc.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
CATALOG = {item["name"] for item in json.loads((ROOT / "demo_catalog.json").read_text())}


def calculate_material_scenarios(scenarios):
    calculator = CALC_HTML[
        CALC_HTML.index("function calculateMaterialQuantities"):
        CALC_HTML.index("function calc(){")
    ]
    corner = next(line for line in CALC_HTML.splitlines() if line.startswith("function corner("))
    script = (
        f"const up=x=>Math.ceil(x-1e-9);\n{corner}\n{calculator}\nconst scenarios="
        + json.dumps(scenarios)
        + ";console.log(JSON.stringify(scenarios.map(calculateMaterialQuantities)))"
    )
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


class FlowBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = server.DB_PATH
        server.DB_PATH = Path(self.temp_dir.name) / "tracker.sqlite3"
        server.initialize_db()

    def tearDown(self):
        server.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def provide(self, house_id, postcode="", address="", owner="", area=None, perimeter=None):
        with server.connect_db() as db:
            db.execute(
                "INSERT INTO provided_houses(id, postcode, address, owner_name, area_m2, perimeter_m, source) "
                "VALUES (?, ?, ?, ?, ?, ?, 'government')",
                (house_id, postcode, address, owner, area, perimeter),
            )

    def summary(self):
        with server.connect_db() as db:
            return server._dashboard(db)["house_summary"]

    def add_material(self, name="Board 100mm", opening=100):
        with server.connect_db() as db:
            return db.execute(
                "INSERT INTO materials(code, name, unit, opening_qty) VALUES ('', ?, 'units', ?)",
                (name, opening),
            ).lastrowid


class HttpBase(FlowBase):
    def setUp(self):
        super().setUp()
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.RequestHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = ""
        _, _, self.cookie = self.call(
            "POST", "/api/auth/setup",
            {"username": "admin1", "display_name": "Admin One", "pin": "123456"},
        )

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def activate(self, payload):
        with server.connect_db() as db:
            db.execute("UPDATE provided_houses SET ready_for_start = 1 WHERE id = ?", (payload["id"],))
        return self.call("POST", "/api/houses", payload)

    def call(self, method, path, payload=None, cookie=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        headers, body = {}, None
        if payload is not None:
            body = json.dumps(payload)
            headers["Content-Type"] = "application/json"
        use_cookie = self.cookie if cookie is None else cookie
        if use_cookie:
            headers["Cookie"] = use_cookie
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = json.loads(response.read() or b"{}")
        set_cookie = response.getheader("Set-Cookie", "").split(";", 1)[0]
        status = response.status
        connection.close()
        return status, result, set_cookie


class ProvidedHouseSummaryTests(FlowBase):
    def test_new_database_has_empty_provided_list(self):
        self.assertEqual(
            self.summary(), {"provided": 0, "active": 0, "closed": 0, "ready": 0, "not_started": 0}
        )

    def test_summary_always_adds_up(self):
        for number in range(5):
            self.provide(f"P-{number}")
        with server.connect_db() as db:
            with db:
                server._upsert_house(db, "P-0")
                server._upsert_house(db, "P-1")
                db.execute("UPDATE houses SET is_finished = 1 WHERE id = 'P-1'")
        s = self.summary()
        self.assertEqual(s, {"provided": 5, "active": 1, "closed": 1, "ready": 0, "not_started": 3})
        self.assertEqual(s["active"] + s["closed"] + s["not_started"], s["provided"])

    def test_fifty_provided_houses_all_count_as_not_started(self):
        for number in range(50):
            self.provide(f"SEAI-{number:03}")
        self.assertEqual(
            self.summary(),
            {"provided": 50, "active": 0, "closed": 0, "ready": 0, "not_started": 50},
        )

    def test_reopening_a_house_moves_it_from_closed_to_active(self):
        self.provide("P-1")
        with server.connect_db() as db:
            with db:
                server._upsert_house(db, "P-1")
                db.execute("UPDATE houses SET is_finished = 1")
        self.assertEqual(self.summary()["closed"], 1)
        with server.connect_db() as db:
            with db:
                db.execute("UPDATE houses SET is_finished = 0")
        s = self.summary()
        self.assertEqual((s["active"], s["closed"]), (1, 0))

    def test_tracker_created_house_joins_provided_list_without_duplicates(self):
        with server.connect_db() as db:
            with db:
                server._upsert_house(db, "NEW-1", "T12", "Owner A")
                server._upsert_house(db, "NEW-1", "T12", "Owner A")
            rows = db.execute("SELECT * FROM provided_houses WHERE id = 'NEW-1'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "tracker")
        self.assertEqual(self.summary()["provided"], 1)

    def test_activating_seai_house_keeps_seai_details(self):
        self.provide("WH1", "T58 V4X9", "3 Hillcrest, Cork", "SEAI Owner", 300.5, 55.0)
        with server.connect_db() as db:
            with db:
                server._upsert_house(db, "WH1", "other", "someone else")
            row = db.execute("SELECT * FROM provided_houses WHERE id = 'WH1'").fetchone()
        self.assertEqual(
            (row["postcode"], row["address"], row["owner_name"], row["area_m2"], row["source"]),
            ("T58 V4X9", "3 Hillcrest, Cork", "SEAI Owner", 300.5, "government"),
        )
        self.assertEqual(self.summary()["provided"], 1)

    def test_legacy_active_count_key_matches_summary(self):
        self.provide("P-1")
        with server.connect_db() as db:
            with db:
                server._upsert_house(db, "P-1")
            dashboard = server._dashboard(db)
        self.assertEqual(dashboard["houses"], dashboard["house_summary"]["active"])


class SeedTests(FlowBase):
    def test_seed_fills_provided_list_only_and_is_idempotent(self):
        seed_houses.seed_provided_houses(40)
        seed_houses.seed_provided_houses(40)
        with server.connect_db() as db:
            provided = db.execute("SELECT COUNT(*) FROM provided_houses").fetchone()[0]
            tracked = db.execute("SELECT COUNT(*) FROM houses").fetchone()[0]
            row = db.execute("SELECT * FROM provided_houses WHERE id = 'WH2131'").fetchone()
        self.assertEqual((provided, tracked), (40, 0))
        self.assertTrue(row["postcode"] and row["address"] and row["owner_name"])
        self.assertGreater(row["area_m2"], 0)
        self.assertGreater(row["perimeter_m"], 0)
        self.assertEqual(row["source"], "fictional_sample")

    def test_seed_does_not_overwrite_edited_details(self):
        seed_houses.seed_provided_houses(3)
        with server.connect_db() as db:
            db.execute("UPDATE provided_houses SET address = 'Custom', area_m2 = 1.5 WHERE id = 'WH2131'")
        seed_houses.seed_provided_houses(3)
        with server.connect_db() as db:
            row = db.execute("SELECT address, area_m2 FROM provided_houses WHERE id = 'WH2131'").fetchone()
        self.assertEqual((row["address"], row["area_m2"]), ("Custom", 1.5))

    def test_seed_removes_orphan_rows_but_keeps_worked_houses(self):
        material_id = self.add_material()
        with server.connect_db() as db:
            with db:
                server._upsert_house(db, "WH2131")
                server._upsert_house(db, "WH2132")
                server._record_movement(
                    db,
                    {"material_id": material_id, "kind": "pickup", "qty": 2,
                     "date": date.today().isoformat(), "house_id": "WH2132",
                     "person": "Tom", "team": "Team A"},
                )
        seed_houses.seed_provided_houses(5)
        with server.connect_db() as db:
            ids = {r["id"] for r in db.execute("SELECT id FROM houses")}
        self.assertEqual(ids, {"WH2132"})


class MigrationTests(unittest.TestCase):
    def test_old_requests_table_is_rebuilt_and_data_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "old.sqlite3"
            original = server.DB_PATH
            server.DB_PATH = path
            try:
                server.initialize_db()
                with server.connect_db() as db:
                    db.execute("DROP TABLE material_requests")
                    db.execute("DROP TABLE provided_houses")
                    db.execute(
                        "CREATE TABLE material_requests (id INTEGER PRIMARY KEY, "
                        "house_id TEXT NOT NULL REFERENCES houses(id), created_at TEXT NOT NULL, "
                        "created_by TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', data TEXT NOT NULL)"
                    )
                    db.execute("INSERT INTO houses(id, postcode, owner_name) VALUES ('WH9', 'X', 'Old Owner')")
                    db.execute(
                        "INSERT INTO material_requests VALUES (1, 'WH9', '2026-01-01T10:00:00', 'Tommy', 'n', '{\"a\": 1}')"
                    )
                server.initialize_db()
                server.initialize_db()
                with server.connect_db() as db:
                    refs = [r["table"] for r in db.execute("PRAGMA foreign_key_list(material_requests)")]
                    request = db.execute("SELECT * FROM material_requests").fetchall()
                    provided = db.execute("SELECT * FROM provided_houses").fetchall()
                    leftover = db.execute(
                        "SELECT name FROM sqlite_master WHERE name = 'material_requests_old'"
                    ).fetchall()
                self.assertEqual(refs, ["provided_houses"])
                self.assertEqual([(r["id"], r["house_id"], r["created_by"], r["data"]) for r in request],
                                 [(1, "WH9", "Tommy", '{"a": 1}')])
                self.assertEqual([(r["id"], r["owner_name"], r["source"]) for r in provided],
                                 [("WH9", "Old Owner", "tracker")])
                self.assertEqual(leftover, [])
            finally:
                server.DB_PATH = original


class CalculatorApiTests(HttpBase):
    def setUp(self):
        super().setUp()
        self.provide("WH2131", "T58 V4X9", "3 Hillcrest, Cork", "Aoife Murphy", 300.5, 55.0)
        self.provide("WH2132", "V92 AA11", "9 Oak Drive, Galway", "Sean Kelly", 210.0, 41.2)
        self.provide("WH2133", "H91 ZZ22", "1 Elm Grove, Limerick", "Niamh Walsh", 180.0, 38.0)

    def search(self, q):
        status, result, _ = self.call("GET", "/api/calculator/houses?q=" + q)
        self.assertEqual(status, 200)
        return result["houses"]

    def test_endpoints_require_login(self):
        for method, path, payload in [
            ("GET", "/api/calculator/houses?q=WH", None),
            ("GET", "/api/calculator/requests?house_id=WH2131", None),
            ("POST", "/api/calculator/requests", {"house_id": "WH2131", "data": {}}),
        ]:
            status, _, _ = self.call(method, path, payload, cookie="")
            self.assertEqual(status, 401, path)

    def test_search_by_id_owner_postcode_and_address(self):
        self.assertEqual([h["id"] for h in self.search("WH2131")], ["WH2131"])
        self.assertEqual([h["id"] for h in self.search("sean")], ["WH2132"])
        self.assertEqual([h["id"] for h in self.search("H91")], ["WH2133"])
        self.assertEqual([h["id"] for h in self.search("Oak%20Drive")], ["WH2132"])
        self.assertEqual(self.search("nothing-matches"), [])

    def test_search_returns_everything_needed_to_prefill(self):
        house = self.search("WH2131")[0]
        self.assertEqual(
            {k: house[k] for k in ("postcode", "address", "owner_name", "area_m2", "perimeter_m")},
            {"postcode": "T58 V4X9", "address": "3 Hillcrest, Cork", "owner_name": "Aoife Murphy",
             "area_m2": 300.5, "perimeter_m": 55.0},
        )

    def test_search_treats_wildcards_literally(self):
        self.assertEqual(self.search("%25"), [])
        self.assertEqual(self.search("_"), [])

    def test_search_is_limited_to_twenty_results(self):
        for n in range(30):
            self.provide(f"BULK-{n:02d}")
        self.assertEqual(len(self.search("BULK")), 20)

    def test_search_reports_status_not_started_active_closed(self):
        self.activate({"id": "WH2132"})
        self.activate({"id": "WH2133"})
        self.call("POST", "/api/houses/status", {"id": "WH2133", "finished": True})
        status = {h["id"]: h["status"] for h in self.search("WH213")}
        self.assertEqual(status, {"WH2131": "not_started", "WH2132": "active", "WH2133": "closed"})

    def test_closed_houses_sort_last(self):
        self.activate({"id": "WH2131"})
        self.call("POST", "/api/houses/status", {"id": "WH2131", "finished": True})
        self.assertEqual(self.search("WH213")[-1]["id"], "WH2131")

    def test_assign_request_to_seai_house_even_if_not_started(self):
        data = {"items": [{"label": "100mm Board", "required": "150 boards", "material": "Board 100mm", "qty": 150}],
                "manual": []}
        status, result, _ = self.call(
            "POST", "/api/calculator/requests", {"house_id": "WH2131", "notes": "n", "data": data}
        )
        self.assertEqual(status, 201)
        status, result, _ = self.call("GET", "/api/calculator/requests?house_id=WH2131")
        self.assertEqual(result["requests"][0]["data"], data)
        self.assertEqual(result["requests"][0]["created_by"], "Admin One")

    def test_assign_rejects_unknown_house_bad_data_and_oversize(self):
        for payload, expected in [
            ({"house_id": "NOPE", "data": {}}, 404),
            ({"house_id": "", "data": {}}, 404),
            ({"house_id": "WH2131", "data": "text"}, 400),
            ({"house_id": "WH2131"}, 400),
            ({"house_id": "WH2131", "data": {"x": "a" * 100_001}}, 400),
        ]:
            status, _, _ = self.call("POST", "/api/calculator/requests", payload)
            self.assertEqual(status, expected, payload.get("house_id"))
        status, result, _ = self.call("GET", "/api/calculator/requests?house_id=WH2131")
        self.assertEqual(result["requests"], [])

    def test_latest_assignment_comes_first_and_other_houses_are_separate(self):
        for label, house in [("first", "WH2131"), ("second", "WH2131"), ("other", "WH2132")]:
            self.call("POST", "/api/calculator/requests", {"house_id": house, "data": {"label": label}})
        _, result, _ = self.call("GET", "/api/calculator/requests?house_id=WH2131")
        self.assertEqual([r["data"]["label"] for r in result["requests"]], ["second", "first"])
        _, result, _ = self.call("GET", "/api/calculator/requests?house_id=WH2132")
        self.assertEqual([r["data"]["label"] for r in result["requests"]], ["other"])

    def test_house_request_does_not_make_a_house_active(self):
        self.call("POST", "/api/calculator/requests", {"house_id": "WH2131", "data": {}})
        self.assertEqual(self.summary()["active"], 0)

    def test_dashboard_exposes_house_summary(self):
        self.activate({"id": "WH2131"})
        _, dashboard, _ = self.call("GET", "/api/dashboard")
        self.assertEqual(dashboard["house_summary"],
                         {"provided": 3, "active": 1, "closed": 0, "ready": 0, "not_started": 2})

    def test_dashboard_summary_includes_maptest_houses_in_combined_totals(self):
        with server.connect_db() as db:
            with db:
                db.execute(
                    "INSERT INTO provided_houses(id, postcode, source, ready_for_start) "
                    "VALUES ('MAPTEST-READY', 'F12 TEST', 'map_test', 1)"
                )
                db.execute(
                    "INSERT INTO provided_houses(id, postcode, source, ready_for_start) "
                    "VALUES ('MAPTEST-ACTIVE', 'F12 TEST', 'map_test', 1)"
                )
                server._upsert_house(db, "MAPTEST-ACTIVE")
        self.activate({"id": "WH2131"})
        _, dashboard, _ = self.call("GET", "/api/dashboard")
        self.assertEqual(
            dashboard["house_summary"],
            {"provided": 5, "active": 2, "closed": 0, "ready": 1, "not_started": 2},
        )


class EndToEndFlowTests(HttpBase):
    def test_ready_house_survey_notes_measurements_activation_and_calculator_request(self):
        self.provide("WH-SURVEY-1", "T1", "1 Survey Lane", "Survey Owner", 100, 40)
        self.assertEqual(
            self.call("POST", "/api/houses/plan", {"id": "WH-SURVEY-1", "ready": True})[0],
            200,
        )
        _, listed, _ = self.call("GET", "/api/calculator/houses?status=ready&q=WH-SURVEY-1")
        self.assertIsNone(listed["houses"][0]["survey"])

        survey = {
            "windows": [{"w": 1.2, "h": 1.5, "qty": 2}],
            "doors": [{"w": 0.9, "h": 2.0, "qty": 1}],
            "house_corners": [2.4, 3.1],
            "notes": "Rear access confirmed; double-check corner by the kitchen.",
        }
        status, saved, _ = self.call(
            "POST", "/api/house-survey",
            {"house_id": "WH-SURVEY-1", "survey": survey},
        )
        self.assertEqual(status, 200)
        self.assertTrue(saved["saved"])
        self.assertEqual(saved["survey"]["notes"], survey["notes"])

        _, listed, _ = self.call("GET", "/api/calculator/houses?q=WH-SURVEY-1")
        self.assertEqual(listed["houses"][0]["survey"], survey)
        status, _, _ = self.call("POST", "/api/houses", {
            "id": "WH-SURVEY-1", "postcode": "T1", "owner_name": "Survey Owner",
        })
        self.assertEqual(status, 201)
        _, active, _ = self.call("GET", "/api/calculator/houses?q=WH-SURVEY-1")
        self.assertEqual(active["houses"][0]["status"], "active")
        self.assertEqual(active["houses"][0]["survey"]["notes"], survey["notes"])

        data = {"items": [{
            "label": "Mortar bags (Glue)", "material": "Mortage bags(Glue)",
            "qty": 51, "required": "51 bags",
        }], "survey_note": active["houses"][0]["survey"]["notes"]}
        status, _, _ = self.call(
            "POST", "/api/calculator/requests",
            {"house_id": "WH-SURVEY-1", "notes": survey["notes"], "data": data},
        )
        self.assertEqual(status, 201)
        _, requests, _ = self.call("GET", "/api/calculator/requests?house_id=WH-SURVEY-1")
        self.assertEqual(requests["requests"][0]["data"], data)
        self.assertEqual(requests["requests"][0]["data"]["items"][0]["qty"], 51)

    def test_seai_house_to_activation_assignment_and_pickup(self):
        self.provide("WH2140", "T1", "5 Elm Grove", "Owner", 250.0, 50.0)
        board = self.add_material("Board 100mm", 100)

        _, found, _ = self.call("GET", "/api/calculator/houses?q=WH2140")
        house = found["houses"][0]
        self.assertEqual(house["status"], "not_started")

        details = {"id": house["id"], "postcode": house["postcode"], "owner_name": house["owner_name"]}
        self.assertEqual(self.call("POST", "/api/houses", details)[0], 409)
        self.assertEqual(self.call("POST", "/api/houses/plan", {"id": house["id"], "ready": True})[0], 200)
        status, _, _ = self.call("POST", "/api/houses", details)
        self.assertEqual(status, 201)
        _, found, _ = self.call("GET", "/api/calculator/houses?q=WH2140")
        self.assertEqual(found["houses"][0]["status"], "active")
        _, found, _ = self.call("GET", "/api/calculator/houses?status=not_started&q=WH2140")
        self.assertEqual(found["houses"], [])
        self.call("POST", "/api/houses/status", {"id": "WH2140", "finished": True})
        _, found, _ = self.call("GET", "/api/calculator/houses?status=not_started&q=WH2140")
        self.assertEqual(found["houses"], [])
        self.call("POST", "/api/houses/status", {"id": "WH2140", "finished": False})

        data = {"items": [{"label": "100mm Board", "material": "Board 100mm", "qty": 40, "required": "40 boards"}],
                "manual": []}
        status, _, _ = self.call("POST", "/api/calculator/requests", {"house_id": "WH2140", "data": data})
        self.assertEqual(status, 201)

        _, listed, _ = self.call("GET", "/api/calculator/requests?house_id=WH2140")
        wanted = listed["requests"][0]["data"]["items"][0]
        self.assertEqual(wanted["qty"], 40)

        status, _, _ = self.call("POST", "/api/movements", {
            "material_id": board, "kind": "pickup", "qty": wanted["qty"],
            "date": date.today().isoformat(), "house_id": "WH2140",
            "person": "Tom", "team": "Team A",
        })
        self.assertEqual(status, 201)
        with server.connect_db() as db:
            balances = server.balances_for(db, board)
        self.assertEqual(balances["warehouse"], 60)
        self.assertEqual(balances["house:WH2140"], 40)
        self.assertEqual(self.summary(), {"provided": 1, "active": 1, "closed": 0, "ready": 0, "not_started": 0})

    def test_house_lifecycle_and_warehouse_balances_stay_synchronized(self):
        house_id = "WH-FULL-FLOW"
        board = self.add_material("Board 100mm", 100)
        mesh = self.add_material("Mesh", 20)
        self.provide(house_id, "T1", "1 Test Lane", "Test Owner", 120, 44)

        _, dashboard, _ = self.call("GET", "/api/dashboard")
        self.assertEqual(
            dashboard["house_summary"],
            {"provided": 1, "active": 0, "closed": 0, "ready": 0, "not_started": 1},
        )
        self.assertEqual(
            self.call("POST", "/api/houses/plan", {"id": house_id, "ready": True})[0],
            200,
        )
        self.assertEqual(self.summary()["ready"], 1)

        survey = {
            "windows": [{"w": 1.2, "h": 1.5, "qty": 2}],
            "doors": [{"w": 0.9, "h": 2.0, "qty": 1}],
            "house_corners": [2.4, 3.1],
            "notes": "Access confirmed.",
        }
        status, saved, _ = self.call(
            "POST", "/api/house-survey", {"house_id": house_id, "survey": survey}
        )
        self.assertEqual(status, 200)
        self.assertEqual(saved["survey"], survey)
        self.assertEqual(
            self.call(
                "POST",
                "/api/houses",
                {"id": house_id, "postcode": "T1", "owner_name": "Test Owner"},
            )[0],
            201,
        )
        _, houses, _ = self.call("GET", f"/api/calculator/houses?q={house_id}")
        self.assertEqual(houses["houses"][0]["status"], "active")
        self.assertEqual(houses["houses"][0]["survey"], survey)

        request_data = {
            "items": [
                {"label": "Board 100mm", "material": "Board 100mm", "qty": 40},
                {"label": "Mesh", "material": "Mesh", "qty": 5},
            ],
            "manual": [],
        }
        status, _, _ = self.call(
            "POST",
            "/api/calculator/requests",
            {"house_id": house_id, "data": request_data},
        )
        self.assertEqual(status, 201)
        _, requests, _ = self.call(
            "GET", f"/api/calculator/requests?house_id={house_id}"
        )
        self.assertEqual(requests["requests"][0]["data"], request_data)

        status, _, _ = self.call(
            "POST",
            "/api/movements/batch",
            {
                "kind": "pickup",
                "date": date.today().isoformat(),
                "house_id": house_id,
                "person": "Admin One",
                "team": "Team A",
                "items": [
                    {"material_id": board, "qty": 40},
                    {"material_id": mesh, "qty": 5},
                ],
            },
        )
        self.assertEqual(status, 201)

        def stock(material_id):
            _, current, _ = self.call("GET", "/api/dashboard")
            material = next(item for item in current["materials"] if item["id"] == material_id)
            balances = {
                location: quantity
                for location, quantity in material["balances"].items()
                if abs(quantity) > 1e-9
            }
            return material["warehouse_qty"], balances

        self.assertEqual(
            stock(board),
            (60, {"warehouse": 60, f"house:{house_id}": 40}),
        )
        self.assertEqual(
            stock(mesh),
            (15, {"warehouse": 15, f"house:{house_id}": 5}),
        )

        status, _, _ = self.call(
            "POST",
            "/api/movements",
            {
                "kind": "use",
                "date": date.today().isoformat(),
                "house_id": house_id,
                "material_id": board,
                "qty": 10,
                "person": "Admin One",
                "team": "Team A",
            },
        )
        self.assertEqual(status, 201)
        status, _, _ = self.call(
            "POST",
            "/api/movements",
            {
                "kind": "return",
                "date": date.today().isoformat(),
                "house_id": house_id,
                "material_id": mesh,
                "qty": 2,
                "person": "Admin One",
                "team": "Team A",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(
            stock(board),
            (60, {"warehouse": 60, f"house:{house_id}": 30, "consumed": 10}),
        )
        self.assertEqual(
            stock(mesh),
            (17, {"warehouse": 17, f"house:{house_id}": 3}),
        )

        status, closed, _ = self.call(
            "POST", "/api/houses/status", {"id": house_id, "finished": True}
        )
        self.assertEqual(status, 200)
        self.assertEqual(closed["materials_closed_out"], 2)
        self.assertEqual(self.summary()["closed"], 1)
        self.assertEqual(
            stock(board), (60, {"warehouse": 60, "consumed": 40})
        )
        self.assertEqual(stock(mesh), (17, {"warehouse": 17, "consumed": 3}))

        status, reopened, _ = self.call(
            "POST", "/api/houses/status", {"id": house_id, "finished": False}
        )
        self.assertEqual(status, 200)
        self.assertEqual(reopened["materials_restored"], 2)
        self.assertEqual(self.summary()["active"], 1)
        self.assertEqual(
            stock(board),
            (60, {"warehouse": 60, f"house:{house_id}": 30, "consumed": 10}),
        )
        self.assertEqual(
            stock(mesh),
            (17, {"warehouse": 17, f"house:{house_id}": 3}),
        )

    def test_closing_a_house_updates_overview_numbers(self):
        self.provide("A")
        self.provide("B")
        for house in ("A", "B"):
            self.activate({"id": house})
        self.call("POST", "/api/houses/status", {"id": "B", "finished": True})
        self.assertEqual(self.summary(), {"provided": 2, "active": 1, "closed": 1, "ready": 0, "not_started": 0})


class HouseOrderStabilityTests(HttpBase):
    def order(self):
        _, dashboard, _ = self.call("GET", "/api/dashboard")
        return [h["id"] for h in dashboard["house_list"]]

    def setUp(self):
        super().setUp()
        for n, stamp in (("A", 300), ("B", 200), ("C", 100)):
            self.activate({"id": n})
        with server.connect_db() as db:
            for n, stamp in (("A", 300), ("B", 200), ("C", 100)):
                db.execute("UPDATE houses SET status_changed_at = ? WHERE id = ?", (stamp, n))

    def test_closing_and_reopening_keeps_position(self):
        before = self.order()
        self.assertEqual(before, ["A", "B", "C"])
        status, _, _ = self.call("POST", "/api/houses/status", {"id": "C", "finished": True})
        self.assertEqual(status, 200)
        status, _, _ = self.call("POST", "/api/houses/status", {"id": "C", "finished": False})
        self.assertEqual(status, 200)
        self.assertEqual(self.order(), before)

    def test_reopened_middle_house_stays_in_the_middle(self):
        self.call("POST", "/api/houses/status", {"id": "B", "finished": True})
        self.call("POST", "/api/houses/status", {"id": "B", "finished": False})
        self.assertEqual(self.order(), ["A", "B", "C"])

    def test_new_house_still_appears_first(self):
        self.activate({"id": "NEW"})
        self.assertEqual(self.order()[0], "NEW")


class ReopenRestoresBalanceTests(HttpBase):
    def setUp(self):
        super().setUp()
        self.board = self.add_material("Board 100mm", 500)
        self.mesh = self.add_material("Mesh", 50)
        self.activate({"id": "WH1"})
        self.pickup(self.board, 40)
        self.pickup(self.mesh, 6)

    def pickup(self, material, qty, house="WH1"):
        status, result, _ = self.call("POST", "/api/movements", {
            "material_id": material, "kind": "pickup", "qty": qty, "house_id": house,
            "date": date.today().isoformat(), "person": "Tom", "team": "A",
        })
        self.assertEqual(status, 201, result)

    def house_balance(self, material, house="WH1"):
        with server.connect_db() as db:
            return server.balances_for(db, material).get(f"house:{house}", 0)

    def finish(self, finished, house="WH1"):
        status, result, _ = self.call("POST", "/api/houses/status", {"id": house, "finished": finished})
        self.assertEqual(status, 200, result)
        return result

    def test_closing_writes_balance_off_and_reopening_restores_it(self):
        self.finish(True)
        self.assertEqual((self.house_balance(self.board), self.house_balance(self.mesh)), (0, 0))
        result = self.finish(False)
        self.assertEqual(result["materials_restored"], 2)
        self.assertEqual((self.house_balance(self.board), self.house_balance(self.mesh)), (40, 6))

    def test_closed_house_rejects_movements_and_assignments_until_reopened(self):
        self.finish(True)
        for kind in ("pickup", "delivery", "use", "return"):
            status, result, _ = self.call("POST", "/api/movements", {
                "material_id": self.board, "kind": kind, "qty": 1, "house_id": "WH1",
                "date": date.today().isoformat(), "person": "Tom", "team": "A",
            })
            self.assertEqual(status, 409, (kind, result))
        status, _, _ = self.call("POST", "/api/movements/batch", {
            "kind": "pickup", "house_id": "WH1", "date": date.today().isoformat(),
            "person": "Tom", "team": "A", "items": [{"material_id": self.mesh, "qty": 1}],
        })
        self.assertEqual(status, 409)
        status, _, _ = self.call("POST", "/api/calculator/requests", {"house_id": "WH1", "data": {"items": []}})
        self.assertEqual(status, 409)
        self.assertEqual(self.house_balance(self.board), 0)
        self.finish(False)
        self.pickup(self.board, 1)
        self.assertEqual(self.house_balance(self.board), 41)

    def test_repeated_close_and_reopen_never_drifts(self):
        for _ in range(4):
            self.finish(True)
            self.finish(False)
            self.assertEqual(self.house_balance(self.board), 40)
        with server.connect_db() as db:
            leftovers = db.execute("SELECT COUNT(*) FROM movements WHERE kind = 'use'").fetchone()[0]
        self.assertEqual(leftovers, 0)

    def test_manual_use_before_closing_is_kept(self):
        self.call("POST", "/api/movements", {
            "material_id": self.board, "kind": "use", "qty": 15, "house_id": "WH1",
            "date": date.today().isoformat(), "person": "Tom", "team": "A",
        })
        self.finish(True)
        self.finish(False)
        self.assertEqual(self.house_balance(self.board), 25)

    def test_stock_added_after_reopen_is_kept(self):
        self.finish(True)
        self.finish(False)
        self.pickup(self.board, 10)
        self.assertEqual(self.house_balance(self.board), 50)

    def test_other_houses_are_untouched(self):
        self.activate({"id": "WH2"})
        self.pickup(self.board, 7, "WH2")
        self.finish(True, "WH2")
        self.finish(True)
        self.finish(False)
        self.assertEqual(self.house_balance(self.board, "WH2"), 0)
        self.assertEqual(self.house_balance(self.board), 40)

    def test_reopening_does_not_change_the_delivered_total_or_warehouse(self):
        self.finish(True)
        self.finish(False)
        _, result, _ = self.call("GET", "/api/calculator/requests?house_id=WH1")
        self.assertEqual(result["delivered"], {str(self.board): 40, str(self.mesh): 6})
        with server.connect_db() as db:
            self.assertEqual(server.balances_for(db, self.board)["warehouse"], 460)

    def test_reopening_a_house_without_closeouts_reports_nothing_restored(self):
        self.assertEqual(self.finish(False)["materials_restored"], 0)

    def test_startup_repairs_open_houses_that_kept_old_closeouts(self):
        self.finish(True)
        with server.connect_db() as db:
            db.execute("UPDATE houses SET is_finished = 0 WHERE id = 'WH1'")
        self.assertEqual(self.house_balance(self.board), 0)
        server.initialize_db()
        self.assertEqual(self.house_balance(self.board), 40)

    def test_startup_leaves_closeouts_of_closed_houses_alone(self):
        self.finish(True)
        server.initialize_db()
        self.assertEqual(self.house_balance(self.board), 0)


class FollowUpDeliveryTests(HttpBase):
    def setUp(self):
        super().setUp()
        self.provide("WH1")
        self.board = self.add_material("Board 100mm", 500)
        self.mesh = self.add_material("Mesh", 50)
        self.activate({"id": "WH1"})
        self.call("POST", "/api/calculator/requests", {"house_id": "WH1", "data": {"items": []}})

    def move(self, material, kind, qty, **extra):
        status, result, _ = self.call("POST", "/api/movements", {
            "material_id": material, "kind": kind, "qty": qty, "house_id": "WH1",
            "date": date.today().isoformat(), "person": "Tom", "team": "Team A", **extra,
        })
        self.assertIn(status, (200, 201), result)

    def delivered(self):
        _, result, _ = self.call("GET", "/api/calculator/requests?house_id=WH1")
        return result["delivered"]

    def test_nothing_delivered_before_first_pickup(self):
        self.assertEqual(self.delivered(), {})

    def test_pickups_add_up_across_deliveries(self):
        self.move(self.board, "pickup", 40)
        self.move(self.board, "pickup", 25, is_extra_delivery=True)
        self.move(self.mesh, "pickup", 3)
        self.assertEqual(self.delivered(), {str(self.board): 65, str(self.mesh): 3})

    def test_returns_reduce_delivered_but_use_does_not(self):
        self.move(self.board, "pickup", 40)
        self.move(self.board, "return", 5)
        self.assertEqual(self.delivered(), {str(self.board): 35})
        self.move(self.board, "use", 10)
        self.assertEqual(self.delivered(), {str(self.board): 35})

    def test_fully_returned_material_is_not_listed(self):
        self.move(self.mesh, "pickup", 4)
        self.move(self.mesh, "return", 4)
        self.assertEqual(self.delivered(), {})

    def test_other_houses_do_not_affect_delivered(self):
        self.activate({"id": "WH2"})
        self.call("POST", "/api/movements", {
            "material_id": self.board, "kind": "pickup", "qty": 9, "house_id": "WH2",
            "date": date.today().isoformat(), "person": "Tom", "team": "Team A",
        })
        self.assertEqual(self.delivered(), {})


@unittest.skipUnless(shutil.which("node"), "node is required for the front-end logic tests")
class RequestPanelTests(unittest.TestCase):
    """Runs renderHouseRequest against a tiny DOM stub."""

    def run_panel(self, delivered, entries, batch=()):
        start = APP_JS.index("const requestCache")
        end = APP_JS.index("async function loadHouseRequest")
        script = """
const escapeHtml = (v) => String(v);
const state = {dashboard:{materials:[{id:1,name:"Board 100mm"},{id:2,name:"Mesh"}]}, batchLines: %s};
const list = {scrollTop: 0};
const dialog = {scrollTop: 0};
let renders = 0;
const box = {dataset:{}, classList:{add(){}, remove(){}}, closest: () => dialog,
  set innerHTML(v) { this._html = v; renders++; }, get innerHTML() { return this._html || ""; }};
const $$ = () => [];
const $ = (sel, root) => sel === "#house-request" ? box : sel === "#movement-house-id" ? {value:"WH1"}
  : sel === "#movement-kind" ? {value:"pickup"} : sel === ".req-list" ? (root && box.innerHTML ? list : null) : null;
%s
list.scrollTop = 0;
requestCache.WH1 = {created_by:"Tom", created_at:"2026-01-01T10:00:00", delivered: %s, data:{items: %s, manual: []}};
renderHouseRequest();
list.scrollTop = 77;
dialog.scrollTop = 310;
state.batchLines.push({material_id: 1, qty: "150"});
renderHouseRequest();
state.batchLines.length = 0;
renderHouseRequest();
console.log(JSON.stringify({html: box.innerHTML, scroll: list.scrollTop, dialogScroll: dialog.scrollTop, renders}));
""" % (json.dumps(list(batch)), APP_JS[start:end], json.dumps(delivered), json.dumps(entries))
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    BOARD = {"label": "100mm Board", "required": "150 boards", "material": "Board 100mm", "qty": 150}

    def test_remaining_quantity_is_prefilled_and_delivery_is_mentioned(self):
        html = self.run_panel({"1": 40}, [self.BOARD])["html"]
        self.assertIn('data-qty="110"', html)
        self.assertIn("already delivered 40", html)
        self.assertIn("remaining 110", html)
        self.assertIn("follow-up delivery", html)

    def test_first_delivery_uses_full_quantity_and_has_no_follow_up_text(self):
        html = self.run_panel({}, [self.BOARD])["html"]
        self.assertIn('data-qty="150"', html)
        self.assertNotIn("follow-up", html)

    def test_fully_delivered_item_is_disabled(self):
        html = self.run_panel({"1": 150}, [self.BOARD])["html"]
        self.assertIn("nothing left to deliver", html)
        self.assertNotIn("data-material-id", html)
        self.assertIn("disabled", html)

    def test_over_delivery_does_not_go_negative(self):
        html = self.run_panel({"1": 200}, [self.BOARD])["html"]
        self.assertNotIn('data-qty="-', html)
        self.assertNotIn("remaining -", html)
        self.assertIn("nothing left to deliver", html)

    def test_scroll_position_survives_a_tick(self):
        entries = [{**self.BOARD, "label": f"Item {n}"} for n in range(15)]
        self.assertEqual(self.run_panel({}, entries)["scroll"], 77)

    def test_ticking_does_not_rebuild_the_panel(self):
        result = self.run_panel({}, [self.BOARD])
        self.assertEqual(result["renders"], 1)
        self.assertEqual(result["dialogScroll"], 310)

    def test_ticked_items_stay_ticked_after_rerender(self):
        html = self.run_panel({}, [self.BOARD], batch=[{"material_id": 1, "qty": "150"}])["html"]
        self.assertRegex(html, r'data-qty="150" checked')


class CalculatorMaterialMappingTests(unittest.TestCase):
    """Every item the calculator can assign must resolve to a tracker material (or be a known exception)."""

    UNTRACKED = {
        "Orange tape",
        "Plastic / card protection",
        "Downpipe", "45° connector", "Downpipe bracket long", "Downpipe bracket short", "Downpipe shoe",
        "Bags",
        "Pins for ext cable",
    }

    @staticmethod
    def key(name):
        return re.sub(r"\s+", " ", name.lower().replace("aluminium", "aluminum")).strip()

    def calculator_auto_names(self):
        rows = re.search(r"let rows=\[(.*?)\];\s*window\.lastRows", CALC_HTML, re.S).group(1)
        names = re.findall(r'"[^"]*"(?:\+[^,\]]*?)?,[^\[\]]*?,"([^"]+)"(?:\+colour\.value\+"\)")?,', rows)
        return rows, names

    def test_automatic_rows_carry_tracker_names_that_exist(self):
        rows, _ = self.calculator_auto_names()
        explicit = set(re.findall(r',"((?:Board|Mortage|Pins|Starter|Window|Mesh|Corner|Primer)[^"]*)",', rows))
        self.assertEqual(
            explicit,
            {"Board 100mm", "Board 20mm white", "Mortage bags(Glue)", "Pins 155mm", "Pins 95mm",
             "Starter track 100mm (2m)", "Window profile (2.4m)", "Mesh", "Corner bit (2.5m)", "Primer"},
        )
        catalog = {self.key(n) for n in CATALOG}
        for name in explicit:
            self.assertIn(self.key(name), catalog, name)
        self.assertIn('"Finish cout ("+colour.value+")"', rows)
        for colour in re.findall(r'<option>((?:White|Light grey \d+|Light cream \d+|Dark cream \d+))</option>',
                                 CALC_HTML.split('id="colour"')[1].split("</select>")[0]):
            self.assertIn(self.key(f"Finish cout ({colour})"), catalog, colour)
        for pink in ("Board 60mm", "Board 60mm Pink"):
            self.assertIn(self.key(pink), catalog)

    def test_manual_materials_resolve_or_are_known_untracked(self):
        block = re.search(r"const stock=\[(.*?)\n\];", CALC_HTML, re.S).group(1)
        full = re.search(r"const full=\[(.*?)\];", CALC_HTML).group(1)
        full = set(re.findall(r'"([^"]+)"', full))
        colours = ["White", "Black", "Brown"]
        catalog = {self.key(n) for n in CATALOG}
        unresolved = set()
        for family, options in re.findall(r'\["([^"]+)"(?:,(\[[^\]]*\]|C))?\]', block):
            if options == "C":
                opts = colours
            elif options:
                opts = re.findall(r'"([^"]+)"', options)
            else:
                opts = [None]
            for opt in opts:
                if opt is None:
                    name = family
                elif family == "Caping 100mm":
                    name = f"Caping {opt} 100mm"
                else:
                    name = f"{family} {opt}" if family in full else opt
                if self.key(name) not in catalog:
                    unresolved.add(family)
        self.assertEqual(unresolved - self.UNTRACKED, set(), "calculator items missing from tracker catalog")


@unittest.skipUnless(shutil.which("node"), "node is required for the calculator logic scenarios")
class CalculatorScenarioTests(unittest.TestCase):
    def calculate(self, scenarios):
        return calculate_material_scenarios(scenarios)

    def test_mortar_adds_area_allowance_to_existing_board_based_amount(self):
        results = self.calculate([{"wall": area} for area in (100, 200, 80)])
        self.assertEqual(
            [(r["areaBasedMortarBags"], r["boardBasedMortarBags"], r["mortarBags"]) for r in results],
            [(18, 35, 53), (36, 70, 106), (15, 28, 43)],
        )

    def test_mortar_area_allowance_uses_net_wall_after_window_and_door_areas(self):
        full_wall, after_openings = self.calculate([{"wall": 100}, {
            "wall": 100,
            "windows": [{"w": 2, "h": 2}],
            "doors": [{"w": 0.9, "h": 2}],
        }])
        self.assertAlmostEqual(after_openings["netWallArea"], 94.2)
        self.assertEqual((full_wall["areaBasedMortarBags"], full_wall["boardBasedMortarBags"]), (18, 35))
        self.assertEqual(
            (
                after_openings["areaBasedMortarBags"],
                after_openings["boardBasedMortarBags"],
                after_openings["mortarBags"],
            ),
            (17, 33, 50),
        )
        self.assertLess(after_openings["areaBasedMortarBags"], full_wall["areaBasedMortarBags"])
        self.assertLess(after_openings["boardBasedMortarBags"], full_wall["boardBasedMortarBags"])

    def test_survey_measurements_produce_mortar_quantity_saved_in_workflow(self):
        result = self.calculate([{
            "wall": 100, "perimeter": 40, "plinth": 0.3, "texture": 2.9,
            "windows": [{"w": 1.2, "h": 1.5}, {"w": 1.2, "h": 1.5}],
            "doors": [{"w": 0.9, "h": 2.0}],
            "houseCorners": [2.4, 3.1],
        }])[0]
        self.assertAlmostEqual(result["netWallArea"], 94.6)
        self.assertEqual(
            (
                result["areaBasedMortarBags"],
                result["boardBasedMortarBags"],
                result["mortarBags"],
            ),
            (18, 33, 51),
        )

    def test_original_material_formulas_match_existing_html_logic(self):
        result = self.calculate([{
            "wall": 250,
            "perimeter": 50,
            "plinth": 0.3,
            "texture": 2.9,
            "windows": [{"w": 1.2, "h": 1.5}, {"w": 1.5, "h": 1.2}],
            "doors": [{"w": 0.9, "h": 2}],
            "houseCorners": [2.5],
        }])[0]
        expected = {
            "wa": 3.6, "da": 1.8, "net": 244.6, "plinthArea": 15,
            "net100": 229.6, "b100": 319, "b60": 21, "b20": 6,
            "boardBasedMortarBags": 85, "pin100": 8, "pin60": 1, "starter": 25,
            "profile": 6, "mesh": 5, "primer": 2, "finish": 29,
        }
        for key, value in expected.items():
            if isinstance(value, float):
                self.assertAlmostEqual(result[key], value, msg=key)
            else:
                self.assertEqual(result[key], value, key)
        self.assertEqual((result["areaBasedMortarBags"], result["mortarBags"]), (45, 130))
        self.assertEqual(result["windowCornerLengths"], [1.2, 1.5, 1.5, 1.5, 1.2, 1.2])

    def test_zero_area_and_incomplete_openings_do_not_add_materials(self):
        zero, incomplete = self.calculate([
            {"wall": 0},
            {"wall": 10, "windows": [{"w": 1, "h": 0}, {"w": 0, "h": 1}]},
        ])
        self.assertEqual(
            (zero["areaBasedMortarBags"], zero["boardBasedMortarBags"], zero["mortarBags"]),
            (0, 0, 0),
        )
        self.assertEqual(
            (incomplete["netWallArea"], incomplete["wa"], incomplete["areaBasedMortarBags"]),
            (10, 0, 2),
        )


@unittest.skipUnless(shutil.which("node"), "node is required for the front-end logic tests")
class ReadyForStartAndNotesTests(HttpBase):
    def setUp(self):
        super().setUp()
        self.provide("WH1", "T1", "1 Elm", "Owner")
        self.provide("WH2", "T2", "2 Elm", "Owner2")

    def plan(self, payload):
        return self.call("POST", "/api/houses/plan", payload)

    def listing(self, status):
        _, found, _ = self.call("GET", "/api/calculator/houses?status=" + status)
        return {h["id"]: h for h in found["houses"]}

    def test_marking_ready_moves_house_between_lists_and_counts(self):
        status, _, _ = self.plan({"id": "WH1", "ready": True})
        self.assertEqual(status, 200)
        self.assertEqual(list(self.listing("ready")), ["WH1"])
        self.assertEqual(list(self.listing("not_started")), ["WH2"])
        self.assertEqual(sorted(self.listing("pending")), ["WH1", "WH2"])
        self.assertEqual(self.listing("ready")["WH1"]["status"], "ready")
        summary = self.summary()
        self.assertEqual((summary["ready"], summary["not_started"]), (1, 1))
        self.plan({"id": "WH1", "ready": False})
        self.assertEqual(self.listing("ready"), {})
        self.assertEqual(self.summary()["not_started"], 2)

    def test_not_started_listing_returns_address_area_and_perimeter(self):
        with server.connect_db() as db:
            db.execute(
                "UPDATE provided_houses SET area_m2 = 142.5, perimeter_m = 51.2 WHERE id = 'WH1'"
            )
        house = self.listing("not_started")["WH1"]
        self.assertEqual((house["address"], house["area_m2"], house["perimeter_m"]),
                         ("1 Elm", 142.5, 51.2))

    def test_location_csv_import_preserves_house_state_and_exposes_map_coordinates(self):
        self.plan({"id": "WH1", "ready": True, "notes": "Keep rear access clear"})
        contents = (
            "house_id,eircode,address,owner_name,latitude,longitude,area_m2,perimeter_m\n"
            "WH1,T12 AB12,1 Elm Road,Ciaran Example,51.8985,-8.4756,142.5,51.2\n"
        ).encode("utf-8")
        with server.connect_db() as db:
            with db:
                result = server._import_house_locations_csv(db, contents)
            house = db.execute(
                "SELECT postcode, address, owner_name, latitude, longitude, area_m2, "
                "perimeter_m, ready_for_start, notes, source FROM provided_houses "
                "WHERE id = 'WH1'"
            ).fetchone()
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["imported"], 0)
        self.assertEqual(
            tuple(house),
            (
                "T12 AB12", "1 Elm Road", "Ciaran Example", 51.8985, -8.4756,
                142.5, 51.2, 1, "Keep rear access clear", "government",
            ),
        )
        _, mapped, _ = self.call("GET", "/api/calculator/houses?status=all")
        map_house = next(row for row in mapped["houses"] if row["id"] == "WH1")
        self.assertEqual(
            (map_house["status"], map_house["latitude"], map_house["longitude"]),
            ("ready", 51.8985, -8.4756),
        )

    def test_location_csv_rejects_invalid_coordinates_without_partial_import(self):
        contents = (
            "house_id,eircode,latitude,longitude\n"
            "WH3,T12 AB12,91,-8.4\n"
            "WH4,T12 CD34,51.9,-8.4\n"
        ).encode("utf-8")
        with server.connect_db() as db:
            with self.assertRaisesRegex(server.ApiError, "invalid latitude"):
                with db:
                    server._import_house_locations_csv(db, contents)
            self.assertIsNone(
                db.execute("SELECT 1 FROM provided_houses WHERE id = 'WH4'").fetchone()
            )

    def test_location_csv_upload_route_accepts_admin_file(self):
        boundary = "house-map-test-boundary"
        csv_data = (
            b"house_id,eircode,latitude,longitude\n"
            b"WH3,T12 AB12,51.8985,-8.4756\n"
        )
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="houses.csv"\r\n'
            "Content-Type: text/csv\r\n\r\n"
        ).encode() + csv_data + f"\r\n--{boundary}--\r\n".encode()
        connection = http.client.HTTPConnection(*self.httpd.server_address, timeout=5)
        connection.request(
            "POST",
            "/api/houses/import",
            body=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
                "Cookie": self.cookie,
            },
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 201)
        self.assertEqual((payload["imported"], payload["updated"]), (1, 0))
        with server.connect_db() as db:
            row = db.execute(
                "SELECT postcode, latitude, longitude FROM provided_houses WHERE id = 'WH3'"
            ).fetchone()
        self.assertEqual(tuple(row), ("T12 AB12", 51.8985, -8.4756))

    def test_map_listing_contains_only_ready_and_active_houses_with_stages(self):
        self.provide("WH3", "T3", "3 Elm", "Owner3")
        self.plan({"id": "WH1", "ready": True})
        self.activate({"id": "WH2", "postcode": "T2", "owner_name": "Owner2"})
        self.call(
            "POST",
            "/api/houses/status",
            {"id": "WH2", "finished": True},
        )
        _, mapped, _ = self.call("GET", "/api/calculator/houses?status=map")
        self.assertEqual(
            {house["id"]: house["status"] for house in mapped["houses"]},
            {"WH1": "ready"},
        )
        self.plan({"id": "WH1", "ready": False})
        self.activate({"id": "WH1", "postcode": "T1", "owner_name": "Owner"})
        with server.connect_db() as db:
            db.execute("UPDATE houses SET stage = 2 WHERE id = 'WH1'")
        _, mapped, _ = self.call("GET", "/api/calculator/houses?status=map")
        self.assertEqual(
            {house["id"]: (house["status"], house["stage"]) for house in mapped["houses"]},
            {"WH1": ("active", 2)},
        )

    def test_map_and_ready_listing_combine_provided_and_maptest_houses(self):
        self.plan({"id": "WH1", "ready": True})
        self.plan({"id": "WH2", "ready": True})
        with server.connect_db() as db:
            with db:
                for index in range(9):
                    db.execute(
                        """INSERT INTO provided_houses
                           (id, postcode, source, ready_for_start, latitude, longitude)
                           VALUES (?, ?, 'map_test', 1, 53.4, -8.1)""",
                        (f"MAPTEST-{index:02}", f"TEST-{index:02}"),
                    )

        _, ready, _ = self.call("GET", "/api/calculator/houses?status=ready")
        _, mapped, _ = self.call("GET", "/api/calculator/houses?status=map")
        _, all_ready, _ = self.call("GET", "/api/calculator/houses?status=ready")
        self.assertEqual(len(ready["houses"]), 11)
        self.assertEqual(len(mapped["houses"]), 11)
        self.assertEqual({house["status"] for house in mapped["houses"]}, {"ready"})
        self.assertEqual(len(all_ready["houses"]), 11)

    def test_activation_clears_ready_and_carries_planned_checklist_and_notes(self):
        with server.connect_db() as db:
            db.execute(
                "UPDATE provided_houses SET area_m2 = 142.5, perimeter_m = 51.2 "
                "WHERE id = 'WH1'"
            )
        self.plan({"id": "WH1", "ready": True, "notes": "Key with neighbour"})
        status, _, _ = self.call("POST", "/api/house-checklist", {
            "house_id": "WH1", "scaffolding_status": "done",
            "esb_unclipped": "no", "plumber_status": "not_necessary"})
        self.assertEqual(status, 200)
        _, planned, _ = self.call("GET", "/api/house-checklist/WH1")
        self.assertEqual(planned["scaffolding_status"], "done")
        self.activate({"id": "WH1", "postcode": "T1", "owner_name": "Owner"})
        self.assertEqual(self.listing("ready"), {})
        self.assertEqual(self.summary()["ready"], 0)
        _, dash, _ = self.call("GET", "/api/dashboard")
        house = next(h for h in dash["house_list"] if h["id"] == "WH1")
        self.assertEqual(house["notes"], "Key with neighbour")
        self.assertEqual(
            (house["postcode"], house["address"], house["area_m2"], house["perimeter_m"]),
            ("T1", "1 Elm", 142.5, 51.2),
        )
        self.assertEqual((house["scaffolding_status"], house["esb_unclipped"], house["plumber_status"]),
                         ("done", "no", "not_necessary"))
        _, details, _ = self.call("GET", "/api/houses/WH1")
        self.assertEqual(
            (details["house"]["postcode"], details["house"]["address"],
             details["house"]["area_m2"], details["house"]["perimeter_m"]),
            ("T1", "1 Elm", 142.5, 51.2),
        )
        self.assertEqual(
            (
                details["house"]["scaffolding_status"],
                details["house"]["esb_unclipped"],
                details["house"]["plumber_status"],
            ),
            ("done", "no", "not_necessary"),
        )

    def test_house_detail_includes_checklist_for_house_without_provided_record(self):
        with server.connect_db() as db:
            db.execute("PRAGMA foreign_keys = OFF")
            db.execute(
                "INSERT INTO houses(id, postcode, owner_name) VALUES (?, ?, ?)",
                ("WH-LEGACY", "T1", "Legacy Owner"),
            )
            db.execute(
                """
                INSERT INTO house_checklists
                    (house_id, scaffolding_status, esb_unclipped, plumber_status)
                VALUES (?, ?, ?, ?)
                """,
                ("WH-LEGACY", "done", "no", "not_necessary"),
            )

        _, details, _ = self.call("GET", "/api/houses/WH-LEGACY")
        self.assertEqual(
            (
                details["house"]["scaffolding_status"],
                details["house"]["esb_unclipped"],
                details["house"]["plumber_status"],
            ),
            ("done", "no", "not_necessary"),
        )

    def test_activation_uses_saved_house_details_and_map_status(self):
        self.provide("WH-ACTIVE", "T12 AB12", "17 Main Street, Cork", "Owner", 142.5, 51.2)
        with server.connect_db() as db:
            db.execute(
                "UPDATE provided_houses SET latitude = 51.8985, longitude = -8.4756 "
                "WHERE id = 'WH-ACTIVE'"
            )
        self.plan({"id": "WH-ACTIVE", "ready": True})

        status, _, _ = self.call(
            "POST",
            "/api/houses",
            {"id": "WH-ACTIVE", "postcode": "WRONG", "owner_name": "Wrong owner"},
        )
        self.assertEqual(status, 201)
        with server.connect_db() as db:
            active = db.execute(
                "SELECT postcode, owner_name FROM houses WHERE id = 'WH-ACTIVE'"
            ).fetchone()
        self.assertEqual(tuple(active), ("T12 AB12", "Owner"))

        _, mapped, _ = self.call("GET", "/api/calculator/houses?status=map")
        house = next(item for item in mapped["houses"] if item["id"] == "WH-ACTIVE")
        self.assertEqual(
            (
                house["status"],
                house["postcode"],
                house["address"],
                house["area_m2"],
                house["perimeter_m"],
                house["latitude"],
                house["longitude"],
            ),
            ("active", "T12 AB12", "17 Main Street, Cork", 142.5, 51.2, 51.8985, -8.4756),
        )

    def test_notes_work_for_any_house_and_are_validated(self):
        self.activate({"id": "WH2"})
        self.assertEqual(self.plan({"id": "WH2", "notes": "Dog in garden"})[0], 200)
        _, dash, _ = self.call("GET", "/api/dashboard")
        self.assertEqual(next(h for h in dash["house_list"] if h["id"] == "WH2")["notes"], "Dog in garden")
        self.assertEqual(self.plan({"id": "WH2", "notes": "x" * 2001})[0], 400)
        self.assertEqual(self.plan({"id": "NOPE", "notes": "a"})[0], 404)

    def test_only_ready_houses_can_be_activated(self):
        status, result, _ = self.call("POST", "/api/houses", {"id": "WH1"})
        self.assertEqual(status, 409)
        self.assertIn("ready for start", result["error"])
        movement = {"kind": "pickup", "qty": 1, "house_id": "WH1", "person": "T", "team": "A",
                    "date": date.today().isoformat()}
        with server.connect_db() as db:
            db.execute("INSERT INTO materials(name, unit, opening_qty) VALUES ('Mesh', 'units', 10)")
            mesh = db.execute("SELECT id FROM materials WHERE name = 'Mesh'").fetchone()[0]
        self.assertEqual(self.call("POST", "/api/movements", {**movement, "material_id": mesh})[0], 409)
        self.assertEqual(self.summary()["active"], 0)
        self.plan({"id": "WH1", "ready": True})
        self.assertEqual(self.call("POST", "/api/houses", {"id": "WH1"})[0], 201)
        self.assertEqual(self.call("POST", "/api/movements", {**movement, "material_id": mesh})[0], 201)

    def test_house_moved_back_to_not_started_cannot_be_activated(self):
        self.plan({"id": "WH1", "ready": True})
        self.plan({"id": "WH1", "ready": False})
        self.assertEqual(self.call("POST", "/api/houses", {"id": "WH1"})[0], 409)

    def test_active_house_cannot_be_marked_ready(self):
        self.activate({"id": "WH2"})
        self.assertEqual(self.plan({"id": "WH2", "ready": True})[0], 409)
        self.assertEqual(self.plan({"id": "WH1", "ready": "yes"})[0], 400)

    def test_unknown_house_has_no_checklist(self):
        self.assertEqual(self.call("GET", "/api/house-checklist/NOPE")[0], 404)


class FrontEndLogicTests(unittest.TestCase):
    def run_js(self, body):
        start = APP_JS.index("const materialKey")
        end = APP_JS.index("function renderHouseRequest")
        script = (
            "const state={dashboard:{materials:["
            '{id:1,name:"Board 100mm"},{id:2,name:"Caping white 100mm"},'
            '{id:3,name:"Aluminum starter track 100mm (2.5m)"},{id:4,name:"Corner outside white"}]}};\n'
            + APP_JS[start:end]
            + "\n"
            + body
        )
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_material_matching_between_calculator_and_tracker(self):
        result = self.run_js(
            'console.log(JSON.stringify(["Board 100mm","board  100MM","Caping 100mm White",'
            '"Aluminium starter track 100mm (2.5m)","Corner Outside white","Orange tape"]'
            ".map(n=>{const m=findTrackerMaterial(n);return m?m.id:null})))"
        )
        self.assertEqual(result, [1, 1, 2, 3, 4, None])

    def test_request_entries_merge_automatic_and_manual_and_skip_empty(self):
        result = self.run_js(
            "console.log(JSON.stringify(requestEntries({data:{items:["
            '{label:"100mm Board",required:"150 boards",material:"Board 100mm",qty:150},'
            '{label:"20mm Board",required:"0 boards",material:"Board 20mm white",qty:0}],'
            'manual:[{material:"Corner Outside white",qty:"6",unit:"pcs"},{material:"Mesh",qty:"",unit:"pcs"}]}})))'
        )
        self.assertEqual(
            [(e["material"], e["qty"]) for e in result],
            [("Board 100mm", 150), ("Corner Outside white", 6)],
        )

    def test_aliases_and_polluted_manual_names_resolve_to_tracker_materials(self):
        script = (
            'state.dashboard.materials.push({id:5,name:"45 deg connector white"},'
            '{id:6,name:"Downpipe brackets long brown"},{id:7,name:"Fisher fixing"});'
            'console.log(JSON.stringify(["45° connector White","Downpipe bracket long Brown",'
            '"Fisher fixing104 in stock"].map(n=>{const m=findTrackerMaterial(cleanMaterialName(n));return m?m.id:null})))'
        )
        self.assertEqual(self.run_js(script), [5, 6, 7])

    def test_calculator_nkey_aliases(self):
        html = (ROOT / "Auto_calc.html").read_text(encoding="utf-8")
        line = next(l for l in html.splitlines() if l.startswith("const nkey="))
        out = subprocess.run(
            ["node", "-e", line + 'console.log(JSON.stringify([nkey("45° connector White"),nkey("Downpipe bracket short Black")]))'],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(json.loads(out.stdout), ["45 deg connector white", "downpipe brackets short black"])

    def test_request_entries_handles_requests_saved_before_automatic_items_existed(self):
        result = self.run_js(
            'console.log(JSON.stringify(requestEntries({data:{manual:[{material:"Mesh",qty:"2",unit:"rolls"}]}})))'
        )
        self.assertEqual([(e["material"], e["qty"]) for e in result], [("Mesh", 2)])


class StageTests(HttpBase):
    def test_active_house_can_be_marked_stage_1_or_2_and_cleared(self):
        self.provide("WH1", "T1", "1 Elm", "Owner")
        self.assertEqual(self.call("POST", "/api/houses/stage", {"id": "WH1", "stage": 1})[0], 404)
        self.assertEqual(self.activate({"id": "WH1"})[0], 201)

        def stage():
            _, data, _ = self.call("GET", "/api/dashboard")
            return {h["id"]: h["stage"] for h in data["house_list"]}["WH1"]

        self.assertEqual(stage(), 1)
        with server.connect_db() as db:
            db.execute("UPDATE houses SET stage = 0 WHERE id = 'WH1'")
        server.initialize_db()
        self.assertEqual(stage(), 1)
        for value in (2, 1):
            self.assertEqual(self.call("POST", "/api/houses/stage", {"id": "WH1", "stage": value})[0], 200)
            self.assertEqual(stage(), value)
        self.assertEqual(self.call("POST", "/api/houses/stage", {"id": "WH1", "stage": 3})[0], 400)
        self.assertEqual(self.call("POST", "/api/houses/stage", {"id": "WH1", "stage": 0})[0], 400)


class SnagTests(HttpBase):
    def setUp(self):
        super().setUp()
        self.provide("WH1", "T1", "1 Elm", "Owner")

    def snag(self, payload):
        return self.call("POST", "/api/houses/snags", payload)

    def test_add_toggle_and_delete_snags_on_not_started_house(self):
        status, result, _ = self.snag({"id": "WH1", "text": "  Cracked sill  "})
        self.assertEqual(status, 200)
        self.assertEqual(result["snags"], [{"id": result["snags"][0]["id"], "text": "Cracked sill", "done": False}])
        snag_id = result["snags"][0]["id"]
        _, result, _ = self.snag({"snag_id": snag_id, "done": True})
        self.assertTrue(result["snags"][0]["done"])
        _, found, _ = self.call("GET", "/api/calculator/houses?status=not_started")
        self.assertTrue(found["houses"][0]["snags"][0]["done"])
        _, result, _ = self.snag({"snag_id": snag_id, "done": False})
        self.assertFalse(result["snags"][0]["done"])
        _, result, _ = self.snag({"snag_id": snag_id, "delete": True})
        self.assertEqual(result["snags"], [])

    def test_snags_survive_activation_and_show_in_dashboard(self):
        self.snag({"id": "WH1", "text": "Paint touch-up"})
        self.activate({"id": "WH1"})
        _, dash, _ = self.call("GET", "/api/dashboard")
        house = next(h for h in dash["house_list"] if h["id"] == "WH1")
        self.assertEqual([s["text"] for s in house["snags"]], ["Paint touch-up"])

    def test_open_snags_listed_before_done_ones(self):
        self.snag({"id": "WH1", "text": "first"})
        _, result, _ = self.snag({"id": "WH1", "text": "second"})
        self.snag({"snag_id": result["snags"][0]["id"], "done": True})
        _, result, _ = self.snag({"id": "WH1", "text": "third"})
        self.assertEqual([(s["text"], s["done"]) for s in result["snags"]],
                         [("second", False), ("third", False), ("first", True)])

    def test_validation(self):
        self.assertEqual(self.snag({"id": "WH1", "text": "   "})[0], 400)
        self.assertEqual(self.snag({"id": "WH1", "text": "x" * 501})[0], 400)
        self.assertEqual(self.snag({"id": "NOPE", "text": "a"})[0], 404)
        self.assertEqual(self.snag({"snag_id": 999, "done": True})[0], 404)
        _, result, _ = self.snag({"id": "WH1", "text": "a"})
        self.assertEqual(self.snag({"snag_id": result["snags"][0]["id"], "done": "yes"})[0], 400)


class UiWiringTests(unittest.TestCase):
    def test_teams_tab_cards_are_compact_and_size_to_content(self):
        css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
        app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("#teams-list.house-grid { grid-auto-rows: auto; align-items: start;", css)
        self.assertIn("#teams-list .team-card { align-self: start;", css)
        self.assertIn("#teams-list .team-house-counts { gap: 8px; margin-top: 10px; }", css)
        self.assertIn('class="team-card"', app_js)
        self.assertIn('class="team-house-counts"', app_js)
    def test_not_started_cards_have_no_activate_button_but_ready_cards_do(self):
        self.assertIn('${!isReady ? `<button class="text-button plan-ready-toggle"', APP_JS)
        self.assertIn('${h.survey ? "Review survey" : "Enter survey measurements"}', APP_JS)

    def test_plan_lists_refresh_without_flashing_loading_state(self):
        body = APP_JS[APP_JS.index("async function renderNotStartedHouses"):]
        body = body[: body.index("function openHouseNote")]
        self.assertIn("planStale", body)
        self.assertIn("list.dataset.planHtml !== html", body)
        refresh = APP_JS[APP_JS.index("async function refresh("):]
        self.assertNotIn("planCache = {}", refresh[: refresh.index("async function searchMovementHistory")])

    def test_snags_are_clickable_and_styled_red_and_green(self):
        css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn("snag-row", APP_JS)
        self.assertIn(".snag-row.done .snag-text", css)
        self.assertIn("line-through", css)
        self.assertIn('class="house-body" data-house-id="${escapeHtml(house.id)}"', APP_JS)
        self.assertIn('const houseBodyScroll = new Map(', APP_JS)
        self.assertIn('body.scrollTop = scrollTop', APP_JS)
    def test_finish_toggle_updates_instantly_and_guards_repeat_clicks(self):
        body = APP_JS[APP_JS.index("async function toggleHouseFinished"):]
        body = body[: body.index("async function submitStocktake")]
        self.assertIn("pendingHouseStatus.has(houseId)", body)
        self.assertLess(body.index("renderHouses()"), body.index('request("/api/houses/status"'))
        self.assertIn("house.is_finished = previous", body)  # rolls back if the save fails

    """Guards for the markup/JS hooks the flow depends on."""

    def test_overview_house_counts_show_combined_tracker_totals(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        for view in ("", "active", "closed", "ready"):
            self.assertIn(f'data-house-view="{view}"', html)
        for element_id in ("stat-provided", "stat-houses", "stat-closed", "stat-not-started"):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn("From SEAI", html)
        self.assertIn("data.house_summary ||", APP_JS)

    def test_active_house_dialog_has_seai_search_and_redirects_to_calculator(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="seai-search"', html)
        self.assertIn("＋ Active house", html)
        self.assertIn("/calculator?house=", APP_JS)

    def test_calculator_prefills_from_query_and_assigns_to_house(self):
        self.assertIn('get("house")', CALC_HTML)
        self.assertIn("Assigned to house</button>", CALC_HTML)
        self.assertNotIn(">Save to house<", CALC_HTML)

    def test_survey_window_quantity_uses_a_dropdown(self):
        survey_row = APP_JS[APP_JS.index("function surveyRow"):APP_JS.index("function openHouseSurvey")]
        self.assertIn("Math.max(20, quantity)", survey_row)
        self.assertIn('Quantity<select>${options}</select>', survey_row)

    def test_window_corner_lengths_are_derived_without_additional_entry_fields(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        calc = CALC_HTML[CALC_HTML.index("function calc()"):CALC_HTML.index("function printDelivery()")]
        quantity_helper = CALC_HTML[
            CALC_HTML.index("function calculateMaterialQuantities"):
            CALC_HTML.index("function calc(){")
        ]
        self.assertNotIn('data-survey-list="opening_corners"', html)
        self.assertNotIn('id="oc"', CALC_HTML)
        self.assertIn("W.flatMap(x=>[x.w,x.h,x.h])", quantity_helper)
        self.assertIn("houseCornerLengths.concat(windowCornerLengths)", quantity_helper)
        loader = CALC_HTML[CALC_HTML.index("function loadSurveyMeasurements"):CALC_HTML.index("function chooseHouse")]
        self.assertIn("item.w,item.h,item.h", loader)
        self.assertNotIn("survey.opening_corners", loader)

    def test_calculator_measurement_card_starts_clean_and_explains_corner_estimate(self):
        self.assertIn('id="windows" class="measurement-list"', CALC_HTML)
        self.assertIn('id="doors" class="measurement-list"', CALC_HTML)
        self.assertNotIn('addOpen("windows","Window");addOpen("doors","Door")', CALC_HTML)
        self.assertIn("filter(r=>r.w>0&&r.h>0)", CALC_HTML)
        self.assertIn('id="cornerSummary" aria-live="polite"', CALC_HTML)
        self.assertIn("Window corners include each window width and both sides.", CALC_HTML)

    def test_survey_loaded_summary_groups_measurements_and_corner_formula(self):
        loader = CALC_HTML[CALC_HTML.index("function loadSurveyMeasurements"):CALC_HTML.index("function chooseHouse")]
        self.assertIn('className="survey-summary-grid"', loader)
        self.assertIn("item.qty*(item.w+2*item.h)", loader)
        self.assertIn("Each length is considered separately when reusing 2.5 m pieces.", loader)
        self.assertIn('className="survey-note"', loader)

    def test_saved_survey_is_marked_and_has_a_review_action(self):
        self.assertIn("function surveyStatusBadge(survey)", APP_JS)
        self.assertIn('class="status-pill survey-complete">Survey recorded', APP_JS)
        self.assertIn('${h.survey ? "Review survey" : "Enter survey measurements"}', APP_JS)
        self.assertIn('${h.survey ? "survey-reviewed" : "survey-needed"}', APP_JS)
        css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn(".house-card .house-card-actions .house-survey-open.survey-reviewed::before", css)
        self.assertIn(".status-pill.survey-complete", css)

    def test_ready_house_actions_have_distinct_accessible_colors(self):
        for action in ("seai-activate", "house-survey-open", "house-note-open"):
            self.assertIn(f'class="text-button {action}', APP_JS)
        css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
        for color in ("seai-activate", "house-survey-open.survey-needed", "house-survey-open.survey-reviewed", "house-note-open", "house-snags-open", "house-record-movement", "house-finish-toggle", "house-open"):
            self.assertIn(f".house-card .house-card-actions .{color}", css)
        self.assertIn(".house-card .house-card-actions .text-button:focus-visible", css)

    def test_ready_queue_cards_use_flexible_layout_and_prominent_activation(self):
        css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
        ready_renderer = APP_JS[
            APP_JS.index("async function renderNotStartedHouses"):
            APP_JS.index("function openHouseNote")
        ]
        self.assertIn('const isMapTestHouse = h.source === "map_test"', ready_renderer)
        self.assertIn("const cardNote = houseCardNote(h)", ready_renderer)
        card_note_helper = APP_JS[
            APP_JS.index("function houseCardNote"):
            APP_JS.index("function findPlanHouse")
        ]
        self.assertIn('if (house.source !== "map_test") return notes', card_note_helper)
        self.assertIn("synthetic coordinates do not identify the Eircode locatio", card_note_helper)
        self.assertIn("return notes", card_note_helper)
        result = subprocess.run(
            [
                "node",
                "-e",
                card_note_helper
                + '\nconsole.log(JSON.stringify(['
                + 'houseCardNote({source:"map_test",notes:"TEST ONLY: synthetic coordinates do not identify the Eircode location."}),'
                + 'houseCardNote({source:"map_test",notes:"TEST ONLY: synthetic coordinates do not identify the Eircode location.Site access note"}),'
                + 'houseCardNote({source:"map_test",notes:"Site access note"}),'
                + 'houseCardNote({source:"government",notes:"Normal house note"})'
                + "]));",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            ["", "Site access note", "Site access note", "Normal house note"],
        )
        self.assertIn("isMapTestHouse ? \"\" : h.owner_name", ready_renderer)
        self.assertIn("isMapTestHouse ? \"\" : h.address", ready_renderer)
        self.assertIn(
            'class="card house-card ${isReady ? "house-ready house-plan-ready" : "house-pending house-plan-pending"}"',
            APP_JS,
        )
        self.assertIn(".house-grid.house-grid-ready { grid-auto-rows: auto;", css)
        self.assertIn(".house-grid.house-grid-ready .house-plan-ready { height: auto;", css)
        self.assertIn(".house-grid.house-grid-ready .house-plan-ready .house-body { flex: 1 1 auto;", css)
        self.assertIn("max-height: 280px; overflow-x: hidden; overflow-y: auto;", css)
        self.assertIn(".house-grid.house-grid-ready .house-plan-ready .house-card-actions { height: auto;", css)
        self.assertIn(".house-grid.house-grid-ready .house-plan-ready .house-card-actions .seai-activate { grid-column: 1 / -1;", css)
        self.assertIn(".house-grid.house-grid-ready > .house-plan-ready .house-card-actions { flex: 0 0 auto; height: auto; min-height: 0; max-height: none; overflow: visible; }", css)

    def test_not_started_queue_cards_match_design_and_scroll_overflow_content(self):
        css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn(
            'class="card house-card ${isReady ? "house-ready house-plan-ready" : "house-pending house-plan-pending"}"',
            APP_JS,
        )
        self.assertIn(
            '<div class="house-body"${!isReady ? \' tabindex="0" role="region" aria-label="House details and pre-start checklist"\' : ""}>',
            APP_JS,
        )
        self.assertIn('${isReady ? `<div class="house-checklist-card">', APP_JS)
        self.assertIn(".house-grid.house-grid-compact .house-plan-pending { height: 340px;", css)
        self.assertIn(".house-grid.house-grid-compact .house-plan-pending .house-body { flex: 1 1 0;", css)
        self.assertIn("overflow-y: auto; padding: 0 7px 10px 0;", css)
        self.assertIn(".house-grid.house-grid-compact .house-plan-pending .house-card-actions { height: auto;", css)
        self.assertIn(".house-grid.house-grid-compact .house-plan-pending .house-card-actions .plan-ready-toggle[data-ready=\"true\"] { grid-column: 1 / -1;", css)
        self.assertIn(".house-grid.house-grid-compact { grid-auto-rows: auto; align-items: start; }", css)
        self.assertIn(".house-grid.house-grid-compact > .house-plan-pending { height: auto; min-height: 0;", css)
        self.assertIn(".house-grid.house-grid-compact > .house-plan-pending .house-body { flex: 0 1 auto; max-height: 180px;", css)
        self.assertIn(".house-grid.house-grid-compact > .house-plan-pending .house-card-actions { flex: 0 0 auto;", css)
        self.assertIn(".house-card .house-top,\n.house-card .house-body,\n.house-card .house-card-actions { scrollbar-color:", css)
        self.assertIn(".house-card .house-body::-webkit-scrollbar-thumb:hover", css)

    def test_house_cards_keep_one_shape_across_status_tabs(self):
        css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn(
            ".house-grid.house-grid-ready,\n.house-grid.house-grid-compact { grid-auto-rows: 460px;",
            css,
        )
        self.assertIn(
            ".house-grid > .house-card { height: 460px; min-height: 460px;",
            css,
        )
        self.assertIn(
            ".house-grid.house-grid-ready > .house-plan-ready,\n.house-grid.house-grid-compact > .house-plan-pending { height: 460px; min-height: 460px; }",
            css,
        )
        self.assertIn(
            ".house-grid > .house-card .house-body { flex: 1 1 0; min-height: 0; max-height: none; overflow-x: hidden; overflow-y: auto;",
            css,
        )
        self.assertIn(
            ".house-grid > .house-card .house-card-actions { display: grid; flex: 0 0 174px; height: 174px;",
            css,
        )

    def test_active_house_card_shows_content_without_nested_scroll_area(self):
        css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn(".house-card:has(.house-body) { min-height: 290px; height: auto;", css)
        self.assertIn(".house-body { flex: 0 1 auto; min-height: 0; overflow: visible;", css)
        self.assertIn(".house-grid { grid-auto-rows: 500px; align-items: stretch; }", css)
        self.assertIn(".house-card:has(.house-body) { height: 500px; min-height: 500px; }", css)
        self.assertIn(".house-card .house-body { flex: 1 1 auto; min-height: 0; overflow-x: hidden; overflow-y: auto;", css)
        self.assertIn(".house-card .house-card-actions { display: grid; height: 198px; min-height: 198px; max-height: 198px;", css)
        self.assertIn("list.classList.toggle(\"house-grid-compact\", !isReady)", APP_JS)
        self.assertIn(".house-grid.house-grid-compact { grid-auto-rows: 340px; }", css)
        self.assertIn(".house-grid.house-grid-compact .house-pending { height: 380px; min-height: 380px; }", css)
        self.assertIn(".house-grid.house-grid-compact .house-pending .house-card-actions { height: auto; min-height: 0; max-height: none; grid-template-rows: auto; }", css)
        self.assertIn("grid-template-columns:repeat(2,minmax(0,1fr))", css)
        self.assertIn('class="house-card-actions"', APP_JS)
        self.assertIn('class="stage-switch" role="group" aria-label="Stage"', APP_JS)
        self.assertIn('.house-card .house-card-actions .stage-btn[data-stage="1"].on', css)
        self.assertIn('.house-card .house-card-actions .stage-btn[data-stage="2"].on', css)
        self.assertIn(".house-card .house-card-actions .stage-btn:focus-visible", css)

    def test_archive_feature_is_removed_and_not_started_cards_show_workflow(self):
        css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="house-detail-checklist"', html)
        self.assertIn('$("#house-detail-checklist").innerHTML', APP_JS)
        self.assertIn('aria-label="House record sections"', html)
        self.assertIn('id="house-detail-status"', html)
        self.assertIn('id="house-detail-checklist-summary"', html)
        self.assertIn("recordedChecklistCount", APP_JS)
        self.assertIn("scrollIntoView({", APP_JS)
        self.assertNotIn("archived", html.lower())
        self.assertNotIn("is_archived", APP_JS)
        self.assertNotIn("house-archive-toggle", APP_JS)
        self.assertNotIn("archived", css.lower())
        self.assertIn('class="house-workflow"', APP_JS)
        self.assertIn("house-workflow-steps", css)
        self.assertIn('const stages = ["Not started", "Ready for start", "Active", "Closed"]', APP_JS)
        self.assertIn('grid-template-columns: repeat(4, minmax(0, 1fr))', css)
        self.assertIn('${houseWorkflowHtml(house.is_finished ? 4 : 3)}', APP_JS)
        self.assertIn("Stage 1/2 work stages appear after activation.", APP_JS)
        self.assertIn('class="text-button house-open"', APP_JS)
        self.assertIn('id="house-detail-notes"', html)
        self.assertIn('id="house-detail-snags"', html)
        self.assertIn("house.survey_notes", APP_JS)
        self.assertIn("house.snags || []", APP_JS)
        self.assertIn('$("#go-overview").classList.toggle("hidden", tab === "overview")', APP_JS)

    def test_not_started_and_closed_cards_show_only_allowed_actions(self):
        not_started_html = APP_JS[
            APP_JS.index("async function renderNotStartedHouses"):
            APP_JS.index("function openHouseNote")
        ]
        self.assertIn('${isReady ? noteHtml(h.notes) : ""}', not_started_html)
        not_started_actions = not_started_html[
            not_started_html.index('<div class="house-card-actions">'):
        ]
        not_started = re.search(
            r"\$\{!isReady \? `([\s\S]*?)` :", not_started_actions
        )
        closed = re.search(
            r"\$\{house\.is_finished \? `([\s\S]*?)` : `", APP_JS
        )
        self.assertIsNotNone(not_started)
        self.assertIsNotNone(closed)
        self.assertEqual(
            re.findall(r'<button class="text-button ([^"]+)', not_started.group(1)),
            ["plan-ready-toggle"],
        )
        self.assertEqual(
            re.findall(r'<button class="text-button ([^"]+)', closed.group(1)),
            ["house-finish-toggle", "house-open"],
        )
        ready_actions = re.search(r": `([\s\S]*?)`\}", not_started_actions)
        self.assertIsNotNone(ready_actions)
        self.assertNotIn("house-snags-open", ready_actions.group(1))
        self.assertIn("h.area_m2", APP_JS)
        self.assertIn("h.perimeter_m", APP_JS)
        self.assertIn('id="go-overview"', (ROOT / "static" / "index.html").read_text(encoding="utf-8"))
        self.assertIn('$("#go-overview").addEventListener("click", goToHomeView)', APP_JS)

    def test_logo_returns_to_overview_and_hides_overview_shortcut(self):
        self.assertIn('$$(".brand, .mobile-brand").forEach((link) =>', APP_JS)
        self.assertIn('link.addEventListener("click", goToHomeView)', APP_JS)
        self.assertIn('setTab("overview");\n  showHousesOverview();', APP_JS)
        self.assertIn('$("#go-overview").classList.toggle("hidden", tab === "overview")', APP_JS)
        self.assertIn('$("#go-overview").classList.add("hidden")', APP_JS)
        self.assertIn('button.setAttribute("aria-current", "page")', APP_JS)

    def test_seai_map_shows_house_stages_and_supports_map_actions(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
        for element in (
            'id="house-map-view"',
            'id="house-map-canvas"',
            'id="house-map-markers"',
            'id="house-map-selection"',
            'id="house-map-summary"',
            'id="house-map-list-count"',
            'id="house-locations-import"',
        ):
            self.assertIn(element, html)
        legend = html[html.index('class="house-map-legend"'):html.index("</div>", html.index('class="house-map-legend"'))]
        self.assertNotIn("Not started", legend)
        self.assertNotIn("Closed", legend)
        self.assertIn("Ready for start", legend)
        self.assertIn("Active · Stage 1", legend)
        self.assertIn("Active · Stage 2", legend)
        self.assertIn('if (tile.dataset.houseView === "")', APP_JS)
        self.assertIn('request("/api/calculator/houses?status=map")', APP_JS)
        self.assertIn('request(`/api/calculator/houses?status=${mode}`)', APP_JS)
        self.assertIn(".house-map-layout { grid-template-columns: minmax(0, 1.8fr) minmax(310px, .82fr);", css)
        self.assertIn("scroll-snap-type: y proximity", css)
        self.assertIn('marker.dataset.stage = String(house.stage || 1)', APP_JS)
        map_code = APP_JS[
            APP_JS.index("const HOUSE_MAP_TILE_SIZE"):
            APP_JS.index("function mapHouseActions")
        ]
        self.assertIn('function hasUsableHouseCoordinates(house)', map_code)
        self.assertIn(
            "const HOUSE_MAP_DEFAULT_CENTER = { latitude: 53.4, longitude: -8, zoom: 6 }",
            map_code,
        )
        self.assertIn('filter(\n    hasUsableHouseCoordinates,', map_code)
        self.assertIn("if (!mappedHouses.length) {", map_code)
        self.assertIn('className = "house-map-empty"', map_code)
        self.assertIn("state.houseMapCamera = { ...center, zoom: HOUSE_MAP_DEFAULT_CENTER.zoom }", map_code)
        self.assertIn(
            'image.src = `https://tile.openstreetmap.org/${zoom}/${wrappedX}/${tileY}.png`',
            map_code,
        )
        self.assertLess(
            map_code.index("tiles.append(empty);"),
            map_code.index("image.src = `https://tile.openstreetmap.org/"),
        )
        coordinate_helper = map_code[
            map_code.index("function hasUsableHouseCoordinates"):
            map_code.index("function projectMapPoint")
        ]
        coordinate_result = subprocess.run(
            [
                "node",
                "-e",
                coordinate_helper
                + '\nconsole.log(JSON.stringify(['
                + 'hasUsableHouseCoordinates({source:"map_test",latitude:53.4,longitude:-8.1}),'
                + 'hasUsableHouseCoordinates({source:"government",latitude:53.4,longitude:-8.1}),'
                + 'hasUsableHouseCoordinates({source:"government",latitude:null,longitude:null})'
                + "]));",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(coordinate_result.returncode, 0, coordinate_result.stderr)
        self.assertEqual(json.loads(coordinate_result.stdout), [True, True, False])
        self.assertIn('house.source === "map_test"', map_code)
        self.assertIn('notice.className = "house-map-empty house-map-approximate"', map_code)
        self.assertIn("Approximate demo pins", map_code)
        self.assertIn("not exact Eircode location", map_code)
        self.assertIn('marker.dataset.locationAccuracy = "approximate"', map_code)
        self.assertIn('house-map-marker-approximate', map_code)
        self.assertNotIn("TEST ONLY", map_code)
        self.assertNotIn("synthetic coordinates", map_code)
        self.assertNotIn("do not identify the Eircode location", map_code)
        self.assertIn(
            'marker.title = `${house.id} · ${mapHouseStatusLabel(house)}${activeStage}${locationLabel}`;',
            APP_JS,
        )
        self.assertIn('house.status === "active" ? String(house.stage || 1) : "R"', APP_JS)
        self.assertIn("mapHouseActions(displayHouse)", APP_JS)
        self.assertIn("https://tile.openstreetmap.org/", APP_JS)
        self.assertIn("map-status-active[data-stage=\"2\"]", css)
        self.assertIn(".house-map-selection .house-card-actions .seai-activate", css)
        self.assertIn(".house-map-selection .house-card-actions .map-stage-caption", css)
        self.assertIn('.house-map-selection .house-card-actions .stage-btn[data-stage="2"].on', css)
        self.assertIn('class="map-stage-caption">WORK STAGE</span>', APP_JS)
        self.assertIn(".house-map-list-item:focus-visible", css)
        self.assertIn(".house-map-empty strong", css)
        self.assertIn("The map shows Ireland.", map_code)
        self.assertIn(".house-map-marker-approximate", css)
        self.assertIn(".house-map-approximate", css)
        self.assertIn("house-map-sidebar-heading", html)
        self.assertIn("mappedCount", APP_JS)
        self.assertIn(".house-map-summary", css)
        self.assertIn("We will not invent a residential postcode", html)

    def test_presentation_guide_covers_survey_stages_and_house_history(self):
        guide = (ROOT / "DEMO-PRESENTATION.md").read_text(encoding="utf-8")
        self.assertIn("Ready for start", guide)
        self.assertIn("survey", guide.lower())
        self.assertIn("Stage 1", guide)
        self.assertIn("Stage 2", guide)
        self.assertNotIn("archive", guide.lower())
        self.assertIn("snag history", guide.lower())
        self.assertIn("50 provided houses", guide)

    def test_record_movement_hooks(self):
        self.assertIn('id="house-request"', (ROOT / "static" / "index.html").read_text(encoding="utf-8"))
        self.assertIn("house-record-movement", APP_JS)
        self.assertIn("toggleRequestItem", APP_JS)


if __name__ == "__main__":
    unittest.main()
