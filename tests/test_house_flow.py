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
            self.summary(), {"provided": 0, "active": 0, "closed": 0, "not_started": 0}
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
        self.assertEqual(s, {"provided": 5, "active": 1, "closed": 1, "not_started": 3})
        self.assertEqual(s["active"] + s["closed"] + s["not_started"], s["provided"])

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
        self.assertEqual(row["source"], "government")

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
        self.call("POST", "/api/houses", {"id": "WH2132"})
        self.call("POST", "/api/houses", {"id": "WH2133"})
        self.call("POST", "/api/houses/status", {"id": "WH2133", "finished": True})
        status = {h["id"]: h["status"] for h in self.search("WH213")}
        self.assertEqual(status, {"WH2131": "not_started", "WH2132": "active", "WH2133": "closed"})

    def test_closed_houses_sort_last(self):
        self.call("POST", "/api/houses", {"id": "WH2131"})
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
        self.call("POST", "/api/houses", {"id": "WH2131"})
        _, dashboard, _ = self.call("GET", "/api/dashboard")
        self.assertEqual(dashboard["house_summary"],
                         {"provided": 3, "active": 1, "closed": 0, "not_started": 2})


class EndToEndFlowTests(HttpBase):
    def test_seai_house_to_activation_assignment_and_pickup(self):
        self.provide("WH2140", "T1", "5 Elm Grove", "Owner", 250.0, 50.0)
        board = self.add_material("Board 100mm", 100)

        _, found, _ = self.call("GET", "/api/calculator/houses?q=WH2140")
        house = found["houses"][0]
        self.assertEqual(house["status"], "not_started")

        status, _, _ = self.call("POST", "/api/houses", {
            "id": house["id"], "postcode": house["postcode"], "owner_name": house["owner_name"],
        })
        self.assertEqual(status, 201)
        _, found, _ = self.call("GET", "/api/calculator/houses?q=WH2140")
        self.assertEqual(found["houses"][0]["status"], "active")

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
        self.assertEqual(self.summary(), {"provided": 1, "active": 1, "closed": 0, "not_started": 0})

    def test_closing_a_house_updates_overview_numbers(self):
        self.provide("A")
        self.provide("B")
        for house in ("A", "B"):
            self.call("POST", "/api/houses", {"id": house})
        self.call("POST", "/api/houses/status", {"id": "B", "finished": True})
        self.assertEqual(self.summary(), {"provided": 2, "active": 1, "closed": 1, "not_started": 0})


class HouseOrderStabilityTests(HttpBase):
    def order(self):
        _, dashboard, _ = self.call("GET", "/api/dashboard")
        return [h["id"] for h in dashboard["house_list"]]

    def setUp(self):
        super().setUp()
        for n, stamp in (("A", 300), ("B", 200), ("C", 100)):
            self.call("POST", "/api/houses", {"id": n})
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
        self.call("POST", "/api/houses", {"id": "NEW"})
        self.assertEqual(self.order()[0], "NEW")


class ReopenRestoresBalanceTests(HttpBase):
    def setUp(self):
        super().setUp()
        self.board = self.add_material("Board 100mm", 500)
        self.mesh = self.add_material("Mesh", 50)
        self.call("POST", "/api/houses", {"id": "WH1"})
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

    def test_stock_added_while_closed_is_kept_on_reopen(self):
        self.finish(True)
        self.pickup(self.board, 10)
        self.finish(False)
        self.assertEqual(self.house_balance(self.board), 50)

    def test_other_houses_are_untouched(self):
        self.call("POST", "/api/houses", {"id": "WH2"})
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
        self.call("POST", "/api/houses", {"id": "WH1"})
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
        self.call("POST", "/api/houses", {"id": "WH2"})
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


@unittest.skipUnless(shutil.which("node"), "node is required for the front-end logic tests")
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

    def test_request_entries_handles_requests_saved_before_automatic_items_existed(self):
        result = self.run_js(
            'console.log(JSON.stringify(requestEntries({data:{manual:[{material:"Mesh",qty:"2",unit:"rolls"}]}})))'
        )
        self.assertEqual([(e["material"], e["qty"]) for e in result], [("Mesh", 2)])


class UiWiringTests(unittest.TestCase):
    def test_finish_toggle_updates_instantly_and_guards_repeat_clicks(self):
        body = APP_JS[APP_JS.index("async function toggleHouseFinished"):]
        body = body[: body.index("async function submitStocktake")]
        self.assertIn("pendingHouseStatus.has(houseId)", body)
        self.assertLess(body.index("renderHouses()"), body.index('request("/api/houses/status"'))
        self.assertIn("house.is_finished = previous", body)  # rolls back if the save fails

    """Guards for the markup/JS hooks the flow depends on."""

    def test_overview_card_has_three_clickable_status_tiles(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        for view in ("", "active", "closed"):
            self.assertIn(f'data-house-view="{view}"', html)
        for element_id in ("stat-provided", "stat-houses", "stat-closed", "stat-not-started"):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn("From SEAI", html)

    def test_active_house_dialog_has_seai_search_and_redirects_to_calculator(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="seai-search"', html)
        self.assertIn("＋ Active house", html)
        self.assertIn("/calculator?house=", APP_JS)

    def test_calculator_prefills_from_query_and_assigns_to_house(self):
        self.assertIn('get("house")', CALC_HTML)
        self.assertIn("Assigned to house</button>", CALC_HTML)
        self.assertNotIn(">Save to house<", CALC_HTML)

    def test_record_movement_hooks(self):
        self.assertIn('id="house-request"', (ROOT / "static" / "index.html").read_text(encoding="utf-8"))
        self.assertIn("house-record-movement", APP_JS)
        self.assertIn("toggleRequestItem", APP_JS)


if __name__ == "__main__":
    unittest.main()
