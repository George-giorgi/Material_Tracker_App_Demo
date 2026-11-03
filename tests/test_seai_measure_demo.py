"""Tests for the synthetic SEAI measure data seed."""
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import seed_seai_measure_demo
import server


class SeaiMeasureDemoTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "tracker.sqlite3"
        self.db_path_patch = patch.object(server, "DB_PATH", self.db_path)
        self.db_path_patch.start()
        server.initialize_db()
        with server.connect_db() as db:
            db.executemany(
                """
                INSERT INTO provided_houses(id, source)
                VALUES (?, 'fictional_sample')
                """,
                [(f"DEMO-FIC-{index:03d}",) for index in range(1, 11)],
            )
            db.executescript(
                """
                CREATE TABLE seai_applications (
                    application_id TEXT PRIMARY KEY
                        REFERENCES provided_houses(id) ON DELETE CASCADE,
                    detail_path TEXT NOT NULL,
                    rag_status TEXT NOT NULL,
                    application_status TEXT NOT NULL DEFAULT '',
                    application_programme TEXT NOT NULL DEFAULT '',
                    lot_number TEXT NOT NULL DEFAULT '',
                    allocation_date TEXT NOT NULL DEFAULT '',
                    application_date TEXT NOT NULL DEFAULT '',
                    list_data_json TEXT NOT NULL,
                    detail_data_json TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL
                );
                CREATE TABLE seai_measures (
                    id INTEGER PRIMARY KEY,
                    application_id TEXT NOT NULL
                        REFERENCES seai_applications(application_id) ON DELETE CASCADE,
                    row_number INTEGER NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    survey_quantity TEXT NOT NULL DEFAULT '',
                    survey_quantity_including_variation TEXT NOT NULL DEFAULT '',
                    inspection_quantity TEXT NOT NULL DEFAULT '',
                    invoice_quantity TEXT NOT NULL DEFAULT ''
                );
                """
            )

    def tearDown(self):
        self.db_path_patch.stop()
        self.temp_dir.cleanup()

    def test_seed_creates_repeatable_fake_measure_rows_for_ten_houses(self):
        self.assertEqual(seed_seai_measure_demo.seed_seai_measure_demo(), 10)
        self.assertEqual(seed_seai_measure_demo.seed_seai_measure_demo(), 10)

        with server.connect_db() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM seai_applications").fetchone()[0],
                10,
            )
            self.assertGreater(
                db.execute("SELECT COUNT(*) FROM seai_measures").fetchone()[0],
                60,
            )
            details = json.loads(
                db.execute(
                    """
                    SELECT detail_data_json FROM seai_applications
                    WHERE application_id = 'DEMO-FIC-001'
                    """
                ).fetchone()[0]
            )
            self.assertIs(details["_demo_data"], True)
            measure = details["Measures"][0]
            self.assertEqual(
                set(measure),
                set(seed_seai_measure_demo.MEASURE_FIELDS),
            )
            self.assertEqual(
                measure["Name"], "Gas boiler and controls (Basic & controls pack)"
            )
            variance = details["Variance"][0]
            self.assertEqual(
                set(variance), {"Variance ID", "Measure", "Value", "Comment"}
            )
            self.assertTrue(variance["Variance ID"].startswith("DEMO-VAR-"))
            self.assertIn("Synthetic demo note", variance["Comment"])
            notes = details["Notes"]
            self.assertEqual(len(notes), 4)
            self.assertEqual(
                set(notes[0]), {"Note Type", "Added On", "Added By", "Note"}
            )
            self.assertTrue(all("Synthetic demo note" in n["Note"] for n in notes))

    def test_seed_attaches_pdf_bundle_by_category_repeatably(self):
        seed_seai_measure_demo.seed_seai_measure_demo()
        bundle = Path(self.temp_dir.name) / "bundle.zip"
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.writestr("Survey/x/Report.pdf", b"%PDF-survey")
            archive.writestr("Variation/V1 - y/Plan.pdf", b"%PDF-variation")
            archive.writestr("Variation/V1 - y/notes.txt", b"ignored")
        with server.connect_db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS seai_documents ("
                "id INTEGER PRIMARY KEY, application_id TEXT NOT NULL, "
                "category TEXT NOT NULL DEFAULT '', filename TEXT NOT NULL, "
                "extension TEXT NOT NULL DEFAULT '', "
                "downloaded INTEGER NOT NULL DEFAULT 0, content BLOB)"
            )
        for _ in range(2):
            self.assertEqual(seed_seai_measure_demo.seed_seai_demo_pdfs(bundle), (10, 2))
        with server.connect_db() as db:
            rows = db.execute(
                "SELECT category, filename FROM seai_documents "
                "WHERE application_id = 'DEMO-FIC-001' ORDER BY category"
            ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [("Survey", "Report.pdf"), ("Variation", "Plan.pdf")],
        )

    def test_seed_refuses_to_overwrite_non_demo_application_details(self):
        with server.connect_db() as db:
            db.execute(
                """
                INSERT INTO seai_applications (
                    application_id, detail_path, rag_status, list_data_json,
                    detail_data_json, retrieved_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "DEMO-FIC-001",
                    "/Application/Details/real-record",
                    "Red",
                    "{}",
                    json.dumps({"Measures": []}),
                    "2026-01-01",
                ),
            )

        with self.assertRaisesRegex(RuntimeError, "Refusing to overwrite non-demo"):
            seed_seai_measure_demo.seed_seai_measure_demo()

        with server.connect_db() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM seai_applications").fetchone()[0],
                1,
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM seai_measures").fetchone()[0],
                0,
            )


if __name__ == "__main__":
    unittest.main()
