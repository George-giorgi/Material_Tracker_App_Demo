import tempfile
import unittest
from datetime import date
from pathlib import Path

from openpyxl import Workbook

import server


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
