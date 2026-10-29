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
from io import BytesIO
from datetime import date
from pathlib import Path
from unittest.mock import patch

import seed_houses
import server
import pymupdf as fitz
from pypdf import PdfReader
from reportlab.pdfgen import canvas

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
    result = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
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
        payload = {
            **payload,
            "employee_name": payload.get("employee_name") or "Test Employee",
            "perimeter_m": payload.get("perimeter_m") or 20,
        }
        survey_perimeter = payload["perimeter_m"]
        with server.connect_db() as db:
            db.execute("UPDATE provided_houses SET ready_for_start = 1 WHERE id = ?", (payload["id"],))
            db.execute(
                """
                INSERT OR IGNORE INTO house_surveys(house_id, data, notes, updated_by, updated_at)
                SELECT id, ?, '', 'Test Surveyor', '2026-10-21T10:00:00'
                FROM provided_houses WHERE id = ?
                """,
                (json.dumps({
                    "windows": [],
                    "doors": [],
                    "house_corners": [],
                    "notes": "",
                    "perimeter_m": survey_perimeter,
                }), payload["id"]),
            )
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

    def call_multipart(self, path, house_id, filename, content, cookie=None):
        boundary = "----MaterialTrackerTestBoundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="house_id"\r\n\r\n'
            f"{house_id}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
        ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("ascii")
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        use_cookie = self.cookie if cookie is None else cookie
        if use_cookie:
            headers["Cookie"] = use_cookie
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.httpd.server_address[1], timeout=5
        )
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        result = json.loads(response.read() or b"{}")
        status = response.status
        connection.close()
        return status, result

    def call_raw(self, method, path, cookie=None):
        headers = {}
        use_cookie = self.cookie if cookie is None else cookie
        if use_cookie:
            headers["Cookie"] = use_cookie
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.httpd.server_address[1], timeout=5
        )
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        status = response.status
        content_type = response.getheader("Content-Type")
        disposition = response.getheader("Content-Disposition")
        body = response.read()
        connection.close()
        return status, content_type, disposition, body

    def call_raw_json(self, method, path, payload, cookie=None):
        headers = {"Content-Type": "application/json"}
        use_cookie = self.cookie if cookie is None else cookie
        if use_cookie:
            headers["Cookie"] = use_cookie
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.httpd.server_address[1], timeout=5
        )
        connection.request(method, path, body=json.dumps(payload), headers=headers)
        response = connection.getresponse()
        result = (
            response.status,
            response.getheader("Content-Type"),
            response.getheader("Content-Disposition"),
            response.read(),
        )
        connection.close()
        return result

    def call_raw_with_headers(self, method, path, cookie=None):
        headers = {}
        use_cookie = self.cookie if cookie is None else cookie
        if use_cookie:
            headers["Cookie"] = use_cookie
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.httpd.server_address[1], timeout=5
        )
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        result = (
            response.status,
            dict(response.getheaders()),
            response.read(),
        )
        connection.close()
        return result


class HouseDocumentHttpTests(HttpBase):
    def make_ready_house(self, house_id="DOC-TEST"):
        self.provide(house_id, postcode="D01 TEST")
        with server.connect_db() as db:
            db.execute(
                "UPDATE provided_houses SET ready_for_start = 1 WHERE id = ?",
                (house_id,),
            )

    def make_test_pdf(self, text):
        buffer = BytesIO()
        pdf = canvas.Canvas(buffer)
        pdf.drawString(72, 720, text)
        pdf.save()
        return buffer.getvalue()

    def make_two_page_pdf(self):
        buffer = BytesIO()
        pdf = canvas.Canvas(buffer)
        pdf.drawString(72, 720, "First page content")
        pdf.showPage()
        pdf.drawString(72, 720, "Second page content")
        pdf.save()
        return buffer.getvalue()

    def test_pdf_preview_renders_pages_and_rejects_missing_page(self):
        self.make_ready_house()
        status, uploaded = self.call_multipart(
            "/api/house-documents",
            "DOC-TEST",
            "survey-plan.pdf",
            self.make_two_page_pdf(),
        )
        self.assertEqual(status, 201, uploaded)

        status, headers, preview = self.call_raw_with_headers(
            "GET", f"/api/house-documents/{uploaded['id']}/preview?page=2"
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(headers["X-PDF-Page-Count"], "2")
        self.assertTrue(preview.startswith(b"\x89PNG\r\n\x1a\n"))

        status, headers, body = self.call_raw_with_headers(
            "GET", f"/api/house-documents/{uploaded['id']}/preview?page=3"
        )
        self.assertEqual(status, 404)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertIn("page does not exist", json.loads(body)["error"])

    def test_surveyor_saves_annotated_copy_without_changing_original(self):
        self.make_ready_house()
        original_pdf = self.make_test_pdf("Original plan content")
        status, uploaded = self.call_multipart(
            "/api/house-documents", "DOC-TEST", "survey-plan.pdf", original_pdf
        )
        self.assertEqual(status, 201, uploaded)
        status, _, _ = self.call(
            "POST",
            "/api/users",
            {
                "username": "survey-annotator",
                "display_name": "Survey Annotator",
                "pin": "234567",
                "role": "surveyor",
            },
        )
        self.assertEqual(status, 201)
        status, _, surveyor_cookie = self.call(
            "POST",
            "/api/auth/login",
            {"username": "survey-annotator", "pin": "234567"},
        )
        self.assertEqual(status, 200)

        annotations = [
            {
                "page": 1,
                "tool": "pen",
                "color": "#d93025",
                "width": 0.004,
                "points": [[0.2, 0.3], [0.5, 0.4]],
            },
            {
                "page": 1,
                "tool": "text",
                "color": "#245fbd",
                "font_size": 0.02,
                "point": [0.2, 0.5],
                "text": "Survey check",
            },
        ]
        status, saved, _ = self.call(
            "POST",
            f"/api/house-documents/{uploaded['id']}/annotations",
            {"annotations": annotations},
            cookie=surveyor_cookie,
        )
        self.assertEqual(status, 201, saved)
        self.assertEqual(saved["filename"], "survey-plan-annotated.pdf")

        with server.connect_db() as db:
            rows = db.execute(
                """
                SELECT filename, content FROM house_documents
                WHERE house_id = 'DOC-TEST' ORDER BY id
                """
            ).fetchall()
        self.assertEqual([row["filename"] for row in rows], [
            "survey-plan.pdf",
            "survey-plan-annotated.pdf",
        ])
        self.assertEqual(rows[0]["content"], original_pdf)
        annotated = rows[1]["content"]
        self.assertNotEqual(annotated, original_pdf)
        self.assertIn("Survey check", PdfReader(BytesIO(annotated)).pages[0].extract_text())
        with fitz.open(stream=annotated, filetype="pdf") as document:
            self.assertTrue(document[0].get_drawings())

    def test_pdf_annotation_validation_rejects_invalid_input(self):
        self.make_ready_house()
        status, uploaded = self.call_multipart(
            "/api/house-documents",
            "DOC-TEST",
            "survey-plan.pdf",
            self.make_test_pdf("Original plan content"),
        )
        self.assertEqual(status, 201, uploaded)
        status, result, _ = self.call(
            "POST",
            f"/api/house-documents/{uploaded['id']}/annotations",
            {"annotations": [{"page": 1, "tool": "pen", "color": "red"}]},
        )
        self.assertEqual(status, 400)
        self.assertIn("invalid", result["error"].lower())
        with server.connect_db() as db:
            count = db.execute("SELECT COUNT(*) FROM house_documents").fetchone()[0]
        self.assertEqual(count, 1)

    def test_share_pdf_appends_only_the_survey_note_on_a_separate_page(self):
        self.make_ready_house()
        original_pdf = self.make_test_pdf("Original plan content")
        status, uploaded = self.call_multipart(
            "/api/house-documents", "DOC-TEST", "survey-plan.pdf", original_pdf
        )
        self.assertEqual(status, 201, uploaded)
        survey = {
            "windows": [],
            "doors": [],
            "house_corners": [],
            "notes": "Survey note to share on a separate page.\nSecond observation for the house.",
        }
        with server.connect_db() as db:
            db.execute(
                "UPDATE provided_houses SET notes = 'Do not include this house note' "
                "WHERE id = 'DOC-TEST'"
            )
            db.execute(
                """
                INSERT INTO house_surveys(house_id, data, notes, updated_by, updated_at)
                VALUES ('DOC-TEST', ?, ?, 'Admin One', '2026-10-21T10:00:00')
                """,
                (json.dumps(survey), survey["notes"]),
            )

        status, content_type, disposition, shared_pdf = self.call_raw(
            "GET", f"/api/house-documents/{uploaded['id']}/share"
        )
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "application/pdf")
        self.assertIn("attachment", disposition)
        shared_reader = PdfReader(BytesIO(shared_pdf))
        self.assertEqual(len(shared_reader.pages), 2)
        self.assertIn("Original plan content", shared_reader.pages[0].extract_text())
        note_page = shared_reader.pages[1].extract_text()
        self.assertIn("House survey note", note_page)
        self.assertIn("BRUSNA  |  MATERIAL TRACKER", note_page)
        self.assertIn("SURVEY OBSERVATIONS", note_page)
        self.assertIn("DOC-TEST", note_page)
        self.assertIn("survey-plan.pdf", note_page)
        self.assertIn("PAGE 1", note_page)
        self.assertIn("Survey note to share on a separate page.", note_page)
        self.assertIn("Second observation for the house.", note_page)
        self.assertGreaterEqual(note_page.count("•"), 2)
        self.assertNotIn("Do not include this house note", note_page)

    def test_share_pdf_requires_a_saved_survey_note(self):
        self.make_ready_house()
        status, uploaded = self.call_multipart(
            "/api/house-documents",
            "DOC-TEST",
            "survey-plan.pdf",
            self.make_test_pdf("Original plan content"),
        )
        self.assertEqual(status, 201)
        status, _, _, response = self.call_raw(
            "GET", f"/api/house-documents/{uploaded['id']}/share"
        )
        self.assertEqual(status, 409)
        self.assertIn("Save a survey note", response.decode("utf-8"))

    def test_share_pdf_returns_clear_error_for_unreadable_uploaded_pdf(self):
        self.make_ready_house()
        status, uploaded = self.call_multipart(
            "/api/house-documents",
            "DOC-TEST",
            "broken-plan.pdf",
            b"%PDF-1.4\nThis is not a complete PDF.\n%%EOF",
        )
        self.assertEqual(status, 201)
        survey = {"windows": [], "doors": [], "house_corners": [], "notes": "Survey note"}
        with server.connect_db() as db:
            db.execute(
                """
                INSERT INTO house_surveys(house_id, data, notes, updated_by, updated_at)
                VALUES ('DOC-TEST', ?, 'Survey note', 'Admin One', '2026-10-21T10:00:00')
                """,
                (json.dumps(survey),),
            )
        status, content_type, _, response = self.call_raw(
            "GET", f"/api/house-documents/{uploaded['id']}/share"
        )
        self.assertEqual(status, 422)
        self.assertIn("application/json", content_type)
        self.assertIn("could not be read as a PDF", response.decode("utf-8"))

    def test_admin_and_surveyor_can_upload_list_and_view_ready_house_pdfs(self):
        self.make_ready_house()
        pdf_data = b"%PDF-1.4\nDemo survey plan\n%%EOF"

        status, uploaded = self.call_multipart(
            "/api/house-documents", "DOC-TEST", "survey-plan.pdf", pdf_data
        )
        self.assertEqual(status, 201, uploaded)
        document_id = uploaded["id"]

        status, result, _ = self.call("GET", "/api/house-documents?house_id=DOC-TEST")
        self.assertEqual(status, 200)
        self.assertEqual(result["documents"][0]["filename"], "survey-plan.pdf")
        self.assertEqual(result["documents"][0]["size_bytes"], len(pdf_data))

        status, content_type, disposition, body = self.call_raw(
            "GET", f"/api/house-documents/{document_id}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "application/pdf")
        self.assertIn("inline", disposition)
        self.assertEqual(body, pdf_data)

        status, _, _ = self.call(
            "POST",
            "/api/users",
            {
                "username": "survey-doc-user",
                "display_name": "Survey Document User",
                "pin": "234567",
                "role": "surveyor",
            },
        )
        self.assertEqual(status, 201)
        status, _, surveyor_cookie = self.call(
            "POST",
            "/api/auth/login",
            {"username": "survey-doc-user", "pin": "234567"},
        )
        self.assertEqual(status, 200)

        status, result, _ = self.call(
            "GET",
            "/api/house-documents?house_id=DOC-TEST",
            cookie=surveyor_cookie,
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["documents"][0]["id"], document_id)
        status, _, _, body = self.call_raw(
            "GET",
            f"/api/house-documents/{document_id}",
            cookie=surveyor_cookie,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, pdf_data)

        status, _, _ = self.call(
            "POST",
            "/api/users",
            {
                "username": "employee-doc-user",
                "display_name": "Employee Document User",
                "pin": "345678",
                "role": "employee",
            },
        )
        self.assertEqual(status, 201)
        status, _, employee_cookie = self.call(
            "POST",
            "/api/auth/login",
            {"username": "employee-doc-user", "pin": "345678"},
        )
        self.assertEqual(status, 200)
        status, result, _ = self.call(
            "GET",
            "/api/house-documents?house_id=DOC-TEST",
            cookie=employee_cookie,
        )
        self.assertEqual(status, 403)
        self.assertIn("Surveyor access", result["error"])
        status, result, _ = self.call(
            "POST",
            f"/api/house-documents/{document_id}/delete",
            cookie=employee_cookie,
        )
        self.assertEqual(status, 403)

        status, result, _ = self.call(
            "GET",
            "/api/house-documents?house_id=DOC-TEST",
            cookie="",
        )
        self.assertEqual(status, 401)

        status, result, _ = self.call(
            "POST",
            f"/api/house-documents/{document_id}/delete",
            cookie=surveyor_cookie,
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(result["filename"], "survey-plan.pdf")
        status, result, _ = self.call(
            "GET",
            f"/api/house-documents?house_id=DOC-TEST",
            cookie=surveyor_cookie,
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["documents"], [])

    def test_house_document_delete_requires_ready_inactive_house_and_reports_missing(self):
        self.make_ready_house()
        status, uploaded = self.call_multipart(
            "/api/house-documents", "DOC-TEST", "survey-plan.pdf", b"%PDF-1.4\n"
        )
        self.assertEqual(status, 201)
        document_id = uploaded["id"]

        status, result, _ = self.call(
            "POST", "/api/house-documents/999999/delete"
        )
        self.assertEqual(status, 404)

        with server.connect_db() as db:
            db.execute("INSERT INTO houses(id) VALUES ('DOC-TEST')")
        status, result, _ = self.call(
            "POST", f"/api/house-documents/{document_id}/delete"
        )
        self.assertEqual(status, 409)
        with server.connect_db() as db:
            db.execute("DELETE FROM houses WHERE id = 'DOC-TEST'")
            db.execute(
                "UPDATE provided_houses SET ready_for_start = 0 WHERE id = 'DOC-TEST'"
            )
        status, result, _ = self.call(
            "POST", f"/api/house-documents/{document_id}/delete"
        )
        self.assertEqual(status, 409)

        with server.connect_db() as db:
            db.execute(
                "UPDATE provided_houses SET ready_for_start = 1 WHERE id = 'DOC-TEST'"
            )
        status, result, _ = self.call(
            "POST", f"/api/house-documents/{document_id}/delete"
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(result["filename"], "survey-plan.pdf")
        status, result, _ = self.call(
            "POST", f"/api/house-documents/{document_id}/delete"
        )
        self.assertEqual(status, 404)

    def test_uploaded_documents_are_view_only_after_house_leaves_ready_status(self):
        self.make_ready_house()
        status, uploaded = self.call_multipart(
            "/api/house-documents", "DOC-TEST", "survey-plan.pdf", b"%PDF-1.4\n"
        )
        self.assertEqual(status, 201)
        with server.connect_db() as db:
            db.execute("UPDATE provided_houses SET ready_for_start = 0 WHERE id = 'DOC-TEST'")
        status, documents, _ = self.call("GET", "/api/house-documents?house_id=DOC-TEST")
        self.assertEqual(status, 200)
        self.assertEqual(
            [document["filename"] for document in documents["documents"]],
            ["survey-plan.pdf"],
        )
        status, _, _, body = self.call_raw(
            "GET", f"/api/house-documents/{uploaded['id']}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"%PDF-1.4\n")
        self.assertEqual(
            self.call_raw("GET", f"/api/house-documents/{uploaded['id']}/share")[0],
            409,
        )

    def test_document_upload_rejects_non_pdf_and_non_ready_house(self):
        self.make_ready_house()
        status, result = self.call_multipart(
            "/api/house-documents", "DOC-TEST", "plan.txt", b"%PDF-1.4\n"
        )
        self.assertEqual(status, 400)
        self.assertIn("PDF", result["error"])

        status, result = self.call_multipart(
            "/api/house-documents", "DOC-TEST", "invalid.pdf", b"not a pdf"
        )
        self.assertEqual(status, 400)
        self.assertIn("valid PDF", result["error"])

        self.provide("NOT-READY")
        status, result = self.call_multipart(
            "/api/house-documents", "NOT-READY", "plan.pdf", b"%PDF-1.4\n"
        )
        self.assertEqual(status, 404)
        self.assertIn("Ready house", result["error"])

    def test_document_upload_enforces_per_house_file_limit(self):
        self.make_ready_house()
        with server.connect_db() as db:
            db.executemany(
                """
                INSERT INTO house_documents
                    (house_id, filename, content, size_bytes, uploaded_by, uploaded_at)
                VALUES ('DOC-TEST', ?, ?, 9, 'Admin One', '2026-01-01T00:00:00')
                """,
                [(f"existing-{index}.pdf", b"%PDF-1.4") for index in range(25)],
            )
        status, result = self.call_multipart(
            "/api/house-documents", "DOC-TEST", "extra.pdf", b"%PDF-1.4\n"
        )
        self.assertEqual(status, 409)
        self.assertIn("maximum of 25", result["error"])

    def test_document_upload_enforces_size_limit(self):
        self.make_ready_house()
        with patch.object(server, "HOUSE_DOCUMENT_MAX_BYTES", 8):
            status, result = self.call_multipart(
                "/api/house-documents", "DOC-TEST", "large.pdf", b"%PDF-1.4\n"
            )
        self.assertEqual(status, 400)
        self.assertIn("8 MB", result["error"])


class SurveyorHouseNotesTests(HttpBase):
    def setUp(self):
        super().setUp()
        self.provide("NOTES-READY", postcode="D01 TEST")
        self.provide("NOTES-PENDING", postcode="D02 TEST")
        with server.connect_db() as db:
            db.execute(
                "UPDATE provided_houses SET ready_for_start = 1 WHERE id = 'NOTES-READY'"
            )
        status, _, _ = self.call(
            "POST",
            "/api/users",
            {
                "username": "house-notes-surveyor",
                "display_name": "House Notes Surveyor",
                "pin": "456789",
                "role": "surveyor",
            },
        )
        self.assertEqual(status, 201)
        status, _, self.surveyor_cookie = self.call(
            "POST",
            "/api/auth/login",
            {"username": "house-notes-surveyor", "pin": "456789"},
        )
        self.assertEqual(status, 200)

    def test_surveyor_can_save_notes_only_for_ready_houses(self):
        status, result, _ = self.call(
            "POST",
            "/api/house-notes",
            {"house_id": "NOTES-READY", "notes": "Call before entering"},
            cookie=self.surveyor_cookie,
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(result["notes"], "Call before entering")
        with server.connect_db() as db:
            saved_notes = db.execute(
                "SELECT notes FROM provided_houses WHERE id = 'NOTES-READY'"
            ).fetchone()["notes"]
        self.assertEqual(saved_notes, "Call before entering")

        status, result, _ = self.call(
            "POST",
            "/api/house-notes",
            {"house_id": "NOTES-PENDING", "notes": "Not permitted yet"},
            cookie=self.surveyor_cookie,
        )
        self.assertEqual(status, 409)
        self.assertIn("ready for start", result["error"])

        status, result, _ = self.call(
            "POST",
            "/api/house-notes",
            {"house_id": "NOTES-READY", "notes": "x" * 2001},
            cookie=self.surveyor_cookie,
        )
        self.assertEqual(status, 400)
        self.assertIn("2,000 characters", result["error"])

    def test_surveyor_can_save_a_separate_survey_note_without_losing_measurements(self):
        existing_survey = {
            "windows": [{"w": 1.2, "h": 1.4, "qty": 2}],
            "doors": [{"w": 0.9, "h": 2.0, "qty": 1}],
            "house_corners": [5.5],
            "notes": "",
        }
        with server.connect_db() as db:
            db.execute(
                """
                INSERT INTO house_surveys(house_id, data, notes, updated_by, updated_at)
                VALUES ('NOTES-READY', ?, '', 'Previous Surveyor', '2026-10-20T10:00:00')
                """,
                (json.dumps(existing_survey),),
            )
        status, result, _ = self.call(
            "POST",
            "/api/survey-notes",
            {"house_id": "NOTES-READY", "notes": "Survey access requires ladder."},
            cookie=self.surveyor_cookie,
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(result["survey"]["notes"], "Survey access requires ladder.")
        self.assertEqual(result["updated_by"], "House Notes Surveyor")
        self.assertEqual(result["survey"]["windows"], existing_survey["windows"])
        with server.connect_db() as db:
            row = db.execute(
                "SELECT data, notes, updated_by FROM house_surveys WHERE house_id = 'NOTES-READY'"
            ).fetchone()
        saved_survey = json.loads(row["data"])
        self.assertEqual(row["notes"], "Survey access requires ladder.")
        self.assertEqual(row["updated_by"], "House Notes Surveyor")
        self.assertEqual(saved_survey["doors"], existing_survey["doors"])
        self.assertEqual(saved_survey["house_corners"], existing_survey["house_corners"])

        status, result, _ = self.call(
            "POST",
            "/api/survey-notes",
            {"house_id": "NOTES-READY", "notes": "x" * 2001},
            cookie=self.surveyor_cookie,
        )
        self.assertEqual(status, 400)
        self.assertIn("2,000 characters", result["error"])

        status, result, _ = self.call(
            "POST",
            "/api/survey-notes",
            {"house_id": "NOTES-PENDING", "notes": "Not ready"},
            cookie=self.surveyor_cookie,
        )
        self.assertEqual(status, 409)
        self.assertIn("ready for start", result["error"])


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

    def mark_ready(self, house_id):
        with server.connect_db() as db:
            db.execute(
                "UPDATE provided_houses SET ready_for_start = 1 WHERE id = ?",
                (house_id,),
            )

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

    def test_seai_application_details_are_returned_only_to_survey_roles(self):
        details = {
            "Application Details": {"Applicant Name": "Sample Applicant"},
            "Measures": [{"Name": "Attic (Loft) Insulation", "Survey Quantity": "45"}],
        }
        with server.connect_db() as db:
            db.execute(
                "CREATE TABLE seai_applications "
                "(application_id TEXT PRIMARY KEY, detail_data_json TEXT NOT NULL)"
            )
            db.execute(
                "INSERT INTO seai_applications VALUES (?, ?)",
                ("WH2131", json.dumps(details)),
            )

        _, admin_result, _ = self.call(
            "GET", "/api/calculator/houses?status=not_started"
        )
        self.assertEqual(
            next(house for house in admin_result["houses"] if house["id"] == "WH2131")[
                "seai_details"
            ],
            details,
        )
        with server.connect_db() as db:
            db.execute(
                "UPDATE provided_houses SET ready_for_start = 1 WHERE id = 'WH2131'"
            )

        surveyor_status, surveyor_created, _ = self.call(
            "POST",
            "/api/users",
            {
                "username": "seai-detail-surveyor",
                "display_name": "SEAI Detail Surveyor",
                "pin": "456789",
                "role": "surveyor",
            },
        )
        self.assertEqual(surveyor_status, 201)
        assignment_status, _, _ = self.call(
            "POST",
            "/api/house-assignments",
            {
                "house_id": "WH2131",
                "surveyor_user_id": surveyor_created["user"]["id"],
            },
        )
        self.assertEqual(assignment_status, 200)
        _, _, surveyor_cookie = self.call(
            "POST",
            "/api/auth/login",
            {"username": "seai-detail-surveyor", "pin": "456789"},
        )
        _, surveyor_result, _ = self.call(
            "GET",
            "/api/calculator/houses?status=ready",
            cookie=surveyor_cookie,
        )
        surveyor_house = next(
            house for house in surveyor_result["houses"] if house["id"] == "WH2131"
        )
        self.assertEqual(surveyor_house["seai_details"], details)

        self.call(
            "POST",
            "/api/users",
            {
                "username": "seai-detail-employee",
                "display_name": "SEAI Detail Employee",
                "pin": "567890",
                "role": "employee",
            },
        )
        _, _, employee_cookie = self.call(
            "POST",
            "/api/auth/login",
            {"username": "seai-detail-employee", "pin": "567890"},
        )
        _, employee_result, _ = self.call(
            "GET",
            "/api/calculator/houses?status=ready",
            cookie=employee_cookie,
        )
        self.assertNotIn(
            "seai_details",
            next(house for house in employee_result["houses"] if house["id"] == "WH2131"),
        )

    def test_seai_measure_variation_can_be_updated_and_is_returned_on_cards(self):
        self.mark_ready("WH2131")
        details = {
            "Measures": [
                {
                    "Name": "External Wall Insulation",
                    "Survey Quantity": "94",
                    "Survey Qty including Variation": "104",
                }
            ]
        }
        with server.connect_db() as db:
            db.execute(
                "CREATE TABLE seai_applications "
                "(application_id TEXT PRIMARY KEY, detail_data_json TEXT NOT NULL)"
            )
            db.execute(
                "INSERT INTO seai_applications VALUES (?, ?)",
                ("WH2131", json.dumps(details)),
            )

        status, result, _ = self.call(
            "POST",
            "/api/seai-measure-variation",
            {"house_id": "WH2131", "measure_index": 0, "variation": "111.5"},
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(result["variation"], "111.5")
        self.assertEqual(
            result["seai_details"]["Measures"][0]["Survey Qty including Variation"],
            "111.5",
        )
        self.assertEqual(
            self.search("WH2131")[0]["seai_details"]["Measures"][0][
                "Survey Qty including Variation"
            ],
            "111.5",
        )
        with server.connect_db() as db:
            stored = json.loads(
                db.execute(
                    "SELECT detail_data_json FROM seai_applications "
                    "WHERE application_id = 'WH2131'"
                ).fetchone()[0]
            )
        self.assertEqual(
            stored["Measures"][0]["Survey Qty including Variation"], "111.5"
        )

        for invalid in ("-1", "1e3", "1.12345", "not a number"):
            status, _, _ = self.call(
                "POST",
                "/api/seai-measure-variation",
                {"house_id": "WH2131", "measure_index": 0, "variation": invalid},
            )
            self.assertEqual(status, 400, invalid)
        status, _, _ = self.call(
            "POST",
            "/api/seai-measure-variation",
            {"house_id": "WH2131", "measure_index": 3, "variation": "10"},
        )
        self.assertEqual(status, 404)

    def test_seai_pdf_documents_are_role_restricted_and_served_inline(self):
        house_id = "PDF-TEST-1"
        self.provide(house_id, "T58 V4X9", "3 Hillcrest, Cork", "SEAI Owner")
        self.mark_ready(house_id)
        pdf_buffer = BytesIO()
        pdf = canvas.Canvas(pdf_buffer)
        pdf.drawString(72, 720, "SEAI original page")
        pdf.save()
        original_pdf = pdf_buffer.getvalue()
        with server.connect_db() as db:
            db.execute(
                """
                CREATE TABLE seai_documents (
                    id INTEGER PRIMARY KEY,
                    application_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    extension TEXT NOT NULL DEFAULT '',
                    downloaded INTEGER NOT NULL DEFAULT 0
                )
                """
            )
        server.initialize_db()
        with server.connect_db() as db:
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(seai_documents)")
            }
            self.assertIn("content", columns)
            for category, filename, content in (
                ("Application", "application.pdf", b"%PDF-1.4 application"),
                ("Survey", "survey.pdf", b"%PDF-1.4 survey"),
                ("Variation", "variation.pdf", b"%PDF-1.4 variation"),
                ("Survey", "photo.jpg", b"not a PDF"),
            ):
                db.execute(
                    """
                    INSERT INTO seai_documents
                        (application_id, category, filename, extension, content)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        house_id,
                        category,
                        filename,
                        filename.rsplit(".", 1)[-1],
                        original_pdf if filename == "application.pdf" else content,
                    ),
                )

        status, result, _ = self.call(
            "GET", f"/api/seai-documents?house_id={house_id}"
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(
            [(doc["category"], doc["filename"]) for doc in result["documents"]],
            [
                ("Application", "application.pdf"),
                ("Survey", "survey.pdf"),
                ("Variation", "variation.pdf"),
            ],
        )
        self.assertTrue(all(not doc["is_annotated"] for doc in result["documents"]))
        self.assertNotIn("content", result["documents"][0])
        document_id = result["documents"][0]["id"]
        status, content_type, disposition, body = self.call_raw(
            "GET", f"/api/seai-documents/{document_id}/file"
        )
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "application/pdf")
        self.assertIn("inline;", disposition)
        self.assertEqual(body, original_pdf)

        self.call(
            "POST",
            "/api/users",
            {
                "username": "seai-pdf-employee",
                "display_name": "SEAI PDF Employee",
                "pin": "567890",
                "role": "employee",
            },
        )
        _, _, employee_cookie = self.call(
            "POST",
            "/api/auth/login",
            {"username": "seai-pdf-employee", "pin": "567890"},
        )
        status, _, _ = self.call(
            "GET",
            f"/api/seai-documents?house_id={house_id}",
            cookie=employee_cookie,
        )
        self.assertEqual(status, 403)
        status, _, _, _ = self.call_raw(
            "GET",
            f"/api/seai-documents/{document_id}/file",
            cookie=employee_cookie,
        )
        self.assertEqual(status, 403)
        status, _, _ = self.call(
            "POST",
            f"/api/seai-documents/{document_id}/annotations",
            {"annotations": [{"page": 1, "tool": "text", "color": "#245fbd",
                              "font_size": 0.02, "point": [0.2, 0.5], "text": "No access"}]},
            cookie=employee_cookie,
        )
        self.assertEqual(status, 403)

        self.call(
            "POST",
            "/api/users",
            {
                "username": "seai-pdf-surveyor",
                "display_name": "SEAI PDF Surveyor",
                "pin": "345678",
                "role": "surveyor",
            },
        )
        _, _, surveyor_cookie = self.call(
            "POST",
            "/api/auth/login",
            {"username": "seai-pdf-surveyor", "pin": "345678"},
        )
        status, _, _ = self.call(
            "GET",
            f"/api/seai-documents?house_id={house_id}",
            cookie=surveyor_cookie,
        )
        self.assertEqual(status, 200)
        status, headers, preview = self.call_raw_with_headers(
            "GET",
            f"/api/seai-documents/{document_id}/preview?page=1",
            cookie=surveyor_cookie,
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(headers["X-PDF-Page-Count"], "1")
        self.assertTrue(preview.startswith(b"\x89PNG\r\n\x1a\n"))

        annotations = [
            {
                "page": 1,
                "tool": "pen",
                "color": "#d93025",
                "width": 0.004,
                "points": [[0.2, 0.3], [0.5, 0.4]],
            },
            {
                "page": 1,
                "tool": "text",
                "color": "#245fbd",
                "font_size": 0.02,
                "point": [0.2, 0.5],
                "text": "Saved SEAI note",
            },
        ]
        status, saved, _ = self.call(
            "POST",
            f"/api/seai-documents/{document_id}/annotations",
            {"annotations": annotations},
            cookie=surveyor_cookie,
        )
        self.assertEqual(status, 201, saved)
        self.assertNotEqual(saved["id"], document_id)
        self.assertEqual(saved["source_id"], document_id)
        self.assertEqual(saved["filename"], "application-annotated.pdf")
        with server.connect_db() as db:
            rows = db.execute(
                "SELECT filename, content FROM seai_documents WHERE id = ?",
                (document_id,),
            ).fetchall()
            annotated_row = db.execute(
                "SELECT filename, content FROM seai_documents WHERE id = ?",
                (saved["id"],),
            ).fetchone()
            document_count = db.execute(
                "SELECT COUNT(*) FROM seai_documents WHERE application_id = ?",
                (house_id,),
            ).fetchone()[0]
        self.assertEqual(rows[0]["filename"], "application.pdf")
        self.assertEqual(rows[0]["content"], original_pdf)
        self.assertEqual(annotated_row["filename"], "application-annotated.pdf")
        self.assertNotEqual(annotated_row["content"], original_pdf)
        self.assertEqual(document_count, 5)
        status, _, _, original_body = self.call_raw(
            "GET", f"/api/seai-documents/{document_id}/file"
        )
        self.assertEqual(status, 200)
        self.assertEqual(original_body, original_pdf)
        status, document_list, _ = self.call(
            "GET", f"/api/seai-documents?house_id={house_id}"
        )
        self.assertEqual(status, 200)
        self.assertIn(
            "application-annotated.pdf",
            [document["filename"] for document in document_list["documents"]],
        )
        listed_documents = {
            document["filename"]: document["is_annotated"]
            for document in document_list["documents"]
        }
        self.assertFalse(listed_documents["application.pdf"])
        self.assertTrue(listed_documents["application-annotated.pdf"])
        with fitz.open(stream=annotated_row["content"], filetype="pdf") as annotated:
            self.assertIn("Saved SEAI note", annotated[0].get_text())
            self.assertTrue(annotated[0].get_drawings())

        status, second_saved, _ = self.call(
            "POST",
            f"/api/seai-documents/{document_id}/annotations",
            {
                "annotations": [
                    {
                        "page": 1,
                        "tool": "text",
                        "color": "#18804b",
                        "font_size": 0.02,
                        "point": [0.2, 0.65],
                        "text": "Another saved note",
                    }
                ]
            },
            cookie=surveyor_cookie,
        )
        self.assertEqual(status, 201, second_saved)
        self.assertEqual(second_saved["filename"], "application-annotated-2.pdf")
        status, document_list, _ = self.call(
            "GET", f"/api/seai-documents?house_id={house_id}"
        )
        self.assertEqual(status, 200)
        listed_documents = {
            document["filename"]: document["is_annotated"]
            for document in document_list["documents"]
        }
        self.assertTrue(listed_documents["application-annotated-2.pdf"])
        with server.connect_db() as db:
            original = db.execute(
                "SELECT content FROM seai_documents WHERE id = ?", (document_id,)
            ).fetchone()
            annotated_count = db.execute(
                "SELECT COUNT(*) FROM seai_documents "
                "WHERE application_id = ? AND filename LIKE '%-annotated%.pdf'",
                (house_id,),
            ).fetchone()[0]
        self.assertEqual(original["content"], original_pdf)
        self.assertEqual(annotated_count, 2)

    def test_seai_pdf_listing_identifies_repeated_annotated_suffixes(self):
        house_id = "PDF-LABEL-TEST"
        self.provide(house_id, "T58 V4X9", "3 Hillcrest, Cork", "SEAI Owner")
        self.mark_ready(house_id)
        with server.connect_db() as db:
            db.execute(
                """
                CREATE TABLE seai_documents (
                    id INTEGER PRIMARY KEY,
                    application_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    extension TEXT NOT NULL DEFAULT '',
                    downloaded INTEGER NOT NULL DEFAULT 0
                )
                """
            )
        server.initialize_db()
        with server.connect_db() as db:
            for filename in (
                "original.pdf",
                "original-annotated.pdf",
                "original-annotated-2.pdf",
                "original-annotated-annotated.pdf",
                "original-annotated-annotated-annotated-3.pdf",
            ):
                db.execute(
                    """
                    INSERT INTO seai_documents
                        (application_id, category, filename, extension, content)
                    VALUES (?, 'Survey', ?, '.pdf', x'255044462d312e34')
                    """,
                    (house_id, filename),
                )
        status, result, _ = self.call(
            "GET", f"/api/seai-documents?house_id={house_id}"
        )
        self.assertEqual(status, 200, result)
        labels = {
            document["filename"]: document["is_annotated"]
            for document in result["documents"]
        }
        self.assertEqual(labels, {
            "original.pdf": False,
            "original-annotated.pdf": True,
            "original-annotated-2.pdf": True,
            "original-annotated-annotated.pdf": True,
            "original-annotated-annotated-annotated-3.pdf": True,
        })

    def test_seai_share_combines_selected_pages_and_appends_survey_note(self):
        house_id = "PDF-SHARE-TEST"
        self.provide(house_id, "T58 V4X9", "3 Hillcrest, Cork", "SEAI Owner")
        self.mark_ready(house_id)
        source_pdfs = []
        for page_texts in (
            ("Application page one", "Application selected page two"),
            ("Survey selected page one", "Survey page two"),
        ):
            pdf_buffer = BytesIO()
            pdf = canvas.Canvas(pdf_buffer)
            for page_text in page_texts:
                pdf.drawString(72, 720, page_text)
                pdf.showPage()
            pdf.save()
            source_pdfs.append(pdf_buffer.getvalue())

        annotated_survey = fitz.open(
            stream=source_pdfs[1], filetype="pdf"
        )
        annotated_survey[0].insert_text(
            (72, 150), "Saved annotation mark", color=(0.1, 0.3, 0.8)
        )
        annotated_survey_pdf = annotated_survey.tobytes()
        annotated_survey.close()

        with server.connect_db() as db:
            db.execute(
                """
                CREATE TABLE seai_documents (
                    id INTEGER PRIMARY KEY,
                    application_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    extension TEXT NOT NULL DEFAULT '',
                    downloaded INTEGER NOT NULL DEFAULT 0
                )
                """
            )
        server.initialize_db()
        with server.connect_db() as db:
            db.execute(
                """
                INSERT INTO house_surveys(house_id, data, notes, updated_by, updated_at)
                VALUES (?, ?, ?, 'Admin One', '2026-10-21T10:00:00')
                """,
                (
                    house_id,
                    json.dumps({
                        "windows": [],
                        "doors": [],
                        "house_corners": [],
                        "notes": "Saved SEAI survey note.",
                    }),
                    "Saved SEAI survey note.",
                ),
            )
            source_document_ids = []
            for category, filename, content in (
                ("Application", "application.pdf", source_pdfs[0]),
                ("Survey", "survey.pdf", source_pdfs[1]),
            ):
                source_document_ids.append(
                    db.execute(
                        """
                        INSERT INTO seai_documents
                            (application_id, category, filename, extension, content)
                        VALUES (?, ?, ?, '.pdf', ?)
                        """,
                        (house_id, category, filename, content),
                    ).lastrowid
                )
            annotated_document_id = db.execute(
                """
                INSERT INTO seai_documents
                    (application_id, category, filename, extension, content)
                VALUES (?, 'Survey', 'survey-annotated.pdf', '.pdf', ?)
                """,
                (house_id, annotated_survey_pdf),
            ).lastrowid
        status, _, _ = self.call(
            "POST",
            "/api/users",
            {
                "username": "seai-share-surveyor",
                "display_name": "SEAI Share Surveyor",
                "pin": "345678",
                "role": "surveyor",
            },
        )
        self.assertEqual(status, 201)
        status, _, surveyor_cookie = self.call(
            "POST",
            "/api/auth/login",
            {"username": "seai-share-surveyor", "pin": "345678"},
        )
        self.assertEqual(status, 200)

        status, content_type, disposition, body = self.call_raw_json(
            "POST",
            "/api/seai-documents/share",
            {
                "house_id": house_id,
                "pages": [
                    {"document_id": source_document_ids[0], "page": 2},
                    {"document_id": source_document_ids[1], "page": 1},
                ],
            },
            cookie=surveyor_cookie,
        )
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "application/pdf")
        self.assertIn("attachment", disposition)
        reader = PdfReader(BytesIO(body))
        self.assertEqual(len(reader.pages), 3)
        self.assertIn(
            "Application selected page two", reader.pages[0].extract_text()
        )
        self.assertIn("Survey selected page one", reader.pages[1].extract_text())
        self.assertIn("Saved annotation mark", reader.pages[1].extract_text())
        self.assertIn("Saved SEAI survey note.", reader.pages[2].extract_text())
        with server.connect_db() as db:
            stored_sources = [
                (row["id"], row["content"])
                for row in db.execute(
                    "SELECT id, content FROM seai_documents WHERE application_id = ? ORDER BY id",
                    (house_id,),
                )
            ]
        self.assertEqual(
            stored_sources,
            [
                (source_document_ids[0], source_pdfs[0]),
                (source_document_ids[1], source_pdfs[1]),
                (annotated_document_id, annotated_survey_pdf),
            ],
        )

    def test_seai_annotated_copy_can_be_shared_with_survey_note(self):
        house_id = "PDF-ANNOTATED-SHARE"
        self.provide(house_id, "T58 V4X9", "3 Hillcrest, Cork", "SEAI Owner")
        self.mark_ready(house_id)
        original_buffer = BytesIO()
        original = canvas.Canvas(original_buffer)
        original.drawString(72, 720, "Original content")
        original.showPage()
        original.drawString(72, 720, "Second original page")
        original.save()
        annotated = fitz.open(stream=original_buffer.getvalue(), filetype="pdf")
        annotated[0].insert_text(
            (72, 150), "Saved annotation mark", color=(0.1, 0.3, 0.8)
        )
        annotated_content = annotated.tobytes()
        annotated.close()
        with server.connect_db() as db:
            db.execute(
                """
                CREATE TABLE seai_documents (
                    id INTEGER PRIMARY KEY,
                    application_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    extension TEXT NOT NULL DEFAULT '',
                    downloaded INTEGER NOT NULL DEFAULT 0
                )
                """
            )
        server.initialize_db()
        with server.connect_db() as db:
            document_id = db.execute(
                """
                INSERT INTO seai_documents
                    (application_id, category, filename, extension, content)
                VALUES (?, 'Survey', 'survey-annotated.pdf', '.pdf', ?)
                """,
                (house_id, annotated_content),
            ).lastrowid
            db.execute(
                """
                INSERT INTO house_surveys(house_id, data, notes, updated_by, updated_at)
                VALUES (?, ?, ?, 'Admin One', '2026-10-21T10:00:00')
                """,
                (
                    house_id,
                    json.dumps({"notes": "Survey note for annotated PDF"}),
                    "Survey note for annotated PDF",
                ),
            )
        status, _, _ = self.call(
            "POST",
            "/api/users",
            {
                "username": "seai-annotated-share-surveyor",
                "display_name": "SEAI Annotated Share Surveyor",
                "pin": "345678",
                "role": "surveyor",
            },
        )
        self.assertEqual(status, 201)
        status, _, surveyor_cookie = self.call(
            "POST",
            "/api/auth/login",
            {"username": "seai-annotated-share-surveyor", "pin": "345678"},
        )
        self.assertEqual(status, 200)

        status, content_type, disposition, body = self.call_raw_json(
            "POST",
            f"/api/seai-documents/{document_id}/share",
            {},
            cookie=surveyor_cookie,
        )
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "application/pdf")
        self.assertIn("attachment", disposition)
        reader = PdfReader(BytesIO(body))
        self.assertEqual(len(reader.pages), 3)
        self.assertIn("Original content", reader.pages[0].extract_text())
        self.assertIn("Saved annotation mark", reader.pages[0].extract_text())
        self.assertIn("Second original page", reader.pages[1].extract_text())
        self.assertIn(
            "Survey note for annotated PDF", reader.pages[2].extract_text()
        )
        with server.connect_db() as db:
            stored = db.execute(
                "SELECT content FROM seai_documents WHERE id = ?", (document_id,)
            ).fetchone()["content"]
        self.assertEqual(stored, annotated_content)

        with server.connect_db() as db:
            db.execute(
                "UPDATE house_surveys SET data = ? WHERE house_id = ?",
                (json.dumps({"notes": ""}), house_id),
            )
        status, _, _, error_body = self.call_raw_json(
            "POST",
            f"/api/seai-documents/{document_id}/share",
            {},
            cookie=surveyor_cookie,
        )
        self.assertEqual(status, 409)
        self.assertIn("Save a survey note", error_body.decode("utf-8"))

    def test_seai_share_rejects_invalid_selections_and_non_survey_roles(self):
        house_id = "PDF-SHARE-VALIDATION"
        self.provide(house_id, "T58 V4X9", "3 Hillcrest, Cork", "SEAI Owner")
        self.mark_ready(house_id)
        pdf_buffer = BytesIO()
        pdf = canvas.Canvas(pdf_buffer)
        pdf.drawString(72, 720, "Only page")
        pdf.save()
        with server.connect_db() as db:
            db.execute(
                """
                CREATE TABLE seai_documents (
                    id INTEGER PRIMARY KEY,
                    application_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    extension TEXT NOT NULL DEFAULT '',
                    downloaded INTEGER NOT NULL DEFAULT 0
                )
                """
            )
        server.initialize_db()
        with server.connect_db() as db:
            document_id = db.execute(
                """
                INSERT INTO seai_documents
                    (application_id, category, filename, extension, content)
                VALUES (?, 'Survey', 'survey.pdf', '.pdf', ?)
                """,
                (house_id, pdf_buffer.getvalue()),
            ).lastrowid
            db.execute(
                """
                INSERT INTO house_surveys(house_id, data, notes, updated_by, updated_at)
                VALUES (?, ?, 'Saved validation note', 'Admin One', '2026-10-21T10:00:00')
                """,
                (house_id, json.dumps({"notes": "Saved validation note"})),
            )
        status, _, _ = self.call(
            "POST",
            "/api/users",
            {
                "username": "seai-share-employee",
                "display_name": "SEAI Share Employee",
                "pin": "345678",
                "role": "employee",
            },
        )
        self.assertEqual(status, 201)
        status, _, employee_cookie = self.call(
            "POST",
            "/api/auth/login",
            {"username": "seai-share-employee", "pin": "345678"},
        )
        self.assertEqual(status, 200)
        payload = {
            "house_id": house_id,
            "pages": [{"document_id": document_id, "page": 1}],
        }
        status, _, _, body = self.call_raw_json(
            "POST", "/api/seai-documents/share", payload, cookie=employee_cookie
        )
        self.assertEqual(status, 403)
        self.assertIn(
            "Administrator or surveyor access is required",
            body.decode("utf-8"),
        )
        status, _, _, body = self.call_raw_json(
            "POST",
            "/api/seai-documents/share",
            {
                **payload,
                "pages": [
                    {"document_id": document_id, "page": 1},
                    {"document_id": document_id, "page": 1},
                ],
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("selected more than once", body.decode("utf-8"))
        status, _, _, body = self.call_raw_json(
            "POST",
            "/api/seai-documents/share",
            {**payload, "pages": [{"document_id": document_id, "page": 2}]},
        )
        self.assertEqual(status, 400)
        self.assertIn("does not exist", body.decode("utf-8"))
        with server.connect_db() as db:
            db.execute(
                "UPDATE house_surveys SET data = ? WHERE house_id = ?",
                (json.dumps({"notes": ""}), house_id),
            )
        status, _, _, body = self.call_raw_json(
            "POST", "/api/seai-documents/share", payload
        )
        self.assertEqual(status, 409)
        self.assertIn("Save a survey note", body.decode("utf-8"))

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
            "perimeter_m": 42.5,
            "notes": "Rear access confirmed; double-check corner by the kitchen.",
        }
        status, saved, _ = self.call(
            "POST", "/api/house-survey",
            {"house_id": "WH-SURVEY-1", "survey": survey},
        )
        self.assertEqual(status, 200)
        self.assertTrue(saved["saved"])
        self.assertEqual(saved["survey"]["notes"], survey["notes"])
        self.assertTrue(saved["updated_at"])

        _, listed, _ = self.call("GET", "/api/calculator/houses?q=WH-SURVEY-1")
        self.assertEqual(listed["houses"][0]["survey"], survey)
        self.assertEqual(listed["houses"][0]["survey_updated_at"], saved["updated_at"])
        status, _, _ = self.call("POST", "/api/houses", {
            "id": "WH-SURVEY-1", "postcode": "T1", "owner_name": "Survey Owner",
            "employee_name": "Assigned Worker",
            "perimeter_m": 42.5,
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

        details = {
            "id": house["id"],
            "postcode": house["postcode"],
            "owner_name": house["owner_name"],
            "employee_name": "Assigned Worker",
            "perimeter_m": 40,
        }
        self.assertEqual(self.call("POST", "/api/houses", details)[0], 409)
        self.assertEqual(self.call("POST", "/api/houses/plan", {"id": house["id"], "ready": True})[0], 200)
        self.assertEqual(
            self.call(
                "POST",
                "/api/house-survey",
                {
                    "house_id": house["id"],
                    "survey": {
                        "windows": [],
                        "doors": [],
                        "house_corners": [],
                        "notes": "",
                        "perimeter_m": 40,
                    },
                },
            )[0],
            200,
        )
        status, result, _ = self.call(
            "POST", "/api/houses", {**details, "employee_name": "  "}
        )
        self.assertEqual(status, 400)
        self.assertIn("Assign an employee", result["error"])
        with server.connect_db() as db:
            self.assertIsNone(
                db.execute(
                    "SELECT 1 FROM houses WHERE id = ?", (house["id"],)
                ).fetchone()
            )
        status, result, _ = self.call(
            "POST", "/api/houses", {**details, "perimeter_m": 0}
        )
        self.assertEqual(status, 400)
        self.assertIn("surveyed perimeter", result["error"])
        with server.connect_db() as db:
            self.assertIsNone(
                db.execute(
                    "SELECT 1 FROM houses WHERE id = ?", (house["id"],)
                ).fetchone()
            )
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
            "perimeter_m": 40,
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
                {
                    "id": house_id,
                    "postcode": "T1",
                    "owner_name": "Test Owner",
                    "employee_name": "Assigned Worker",
                    "perimeter_m": 40,
                    "employee_name": "Assigned Worker",
                },
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
            self.provide(n)
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
        self.provide("NEW")
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

    def test_request_lookup_returns_next_delivery_number_even_without_a_request(self):
        _, result, _ = self.call("GET", "/api/calculator/requests?house_id=WH1")
        self.assertEqual(result["next_delivery_number"], 1)
        self.move(self.board, "pickup", 40)
        self.move(self.board, "return", 10)
        _, result, _ = self.call("GET", "/api/calculator/requests?house_id=WH1")
        self.assertEqual(result["next_delivery_number"], 2)
        self.assertEqual(result["delivered"], {str(self.board): 30})


@unittest.skipUnless(shutil.which("node"), "node is required for the front-end logic tests")
class RequestPanelTests(unittest.TestCase):
    """Runs renderHouseRequest against a tiny DOM stub."""

    def run_panel(self, delivered, entries, batch=(), next_delivery_number=1):
        start = APP_JS.index("const requestCache")
        end = APP_JS.index("async function loadHouseRequest")
        script = """
const escapeHtml = (v) => String(v);
const state = {dashboard:{materials:[{id:1,name:"Board 100mm"},{id:2,name:"Mesh"}]}, batchLines: %s};
const list = {scrollTop: 0};
const dialog = {scrollTop: 0};
const extraDelivery = {checked:false, disabled:false};
const extraDeliveryHint = {textContent:""};
let renders = 0;
const box = {dataset:{}, classList:{add(){}, remove(){}}, closest: () => dialog,
  set innerHTML(v) { this._html = v; renders++; }, get innerHTML() { return this._html || ""; }};
const $$ = () => [];
const $ = (sel, root) => sel === "#house-request" ? box : sel === "#movement-house-id" ? {value:"WH1"}
  : sel === "#movement-kind" ? {value:"pickup"} : sel === "#extra-delivery" ? extraDelivery
  : sel === "#extra-delivery-hint" ? extraDeliveryHint
  : sel === ".req-list" ? (root && box.innerHTML ? list : null) : null;
%s
list.scrollTop = 0;
requestCache.WH1 = {id:1, created_by:"Tom", created_at:"2026-01-01T10:00:00", delivered: %s,
  next_delivery_number:%s, data:{items: %s, manual: []}};
renderHouseRequest();
list.scrollTop = 77;
dialog.scrollTop = 310;
state.batchLines.push({material_id: 1, qty: "150"});
renderHouseRequest();
state.batchLines.length = 0;
renderHouseRequest();
console.log(JSON.stringify({html: box.innerHTML, scroll: list.scrollTop, dialogScroll: dialog.scrollTop, renders,
  extraDelivery:extraDelivery.checked, extraDeliveryDisabled:extraDelivery.disabled,
  extraDeliveryHint:extraDeliveryHint.textContent}));
""" % (json.dumps(list(batch)), APP_JS[start:end], json.dumps(delivered), next_delivery_number, json.dumps(entries))
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

    def test_follow_up_delivery_is_marked_automatically(self):
        result = self.run_panel({"1": 40}, [self.BOARD], next_delivery_number=3)
        self.assertTrue(result["extraDelivery"])
        self.assertTrue(result["extraDeliveryDisabled"])
        self.assertIn("Delivery #3", result["extraDeliveryHint"])

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


class ReadyForStartResetTests(HttpBase):
    def test_returning_to_not_started_clears_survey_data_and_keeps_original_pdfs_viewable(self):
        house_id = "RESET-READY"
        self.provide(house_id, postcode="D01 TEST")
        status, _, _ = self.call(
            "POST", "/api/houses/plan", {"id": house_id, "ready": True}
        )
        self.assertEqual(status, 200)
        with server.connect_db() as db:
            db.execute(
                """
                CREATE TABLE seai_applications (
                    application_id TEXT PRIMARY KEY,
                    detail_data_json TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE seai_documents (
                    id INTEGER PRIMARY KEY,
                    application_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    extension TEXT NOT NULL DEFAULT '',
                    downloaded INTEGER NOT NULL DEFAULT 0,
                    content BLOB
                )
                """
            )
        server.initialize_db()
        with server.connect_db() as db:
            db.execute(
                """
                UPDATE provided_houses
                SET notes = 'House note',
                    plan_scaffolding = 'yes',
                    plan_esb = 'no',
                    plan_plumber = 'yes',
                    plan_scaffolding_required = 1,
                    plan_scaffolding_notes = 'Scaffolding note',
                    plan_esb_required = 0,
                    plan_esb_notes = 'ESB note',
                    plan_plumber_required = 1,
                    plan_plumber_notes = 'Plumber note'
                WHERE id = ?
                """,
                (house_id,),
            )
            db.execute(
                """
                INSERT INTO house_surveys(house_id, data, notes, updated_by, updated_at)
                VALUES (?, ?, 'Survey note', 'Test Surveyor', '2026-10-27T13:00:00')
                """,
                (
                    house_id,
                    json.dumps({
                        "windows": [{"w": 1.2, "h": 1.4, "qty": 2}],
                        "doors": [],
                        "house_corners": [],
                        "notes": "Survey note",
                    }),
                ),
            )
            db.execute(
                """
                INSERT INTO house_checklists
                    (house_id, scaffolding_status, esb_unclipped, plumber_status)
                VALUES (?, 'yes', 'no', 'yes')
                """,
                (house_id,),
            )
            db.execute(
                "INSERT INTO house_snags(house_id, text, created_at) VALUES (?, 'Snag', 1)",
                (house_id,),
            )
            original_pdf = b"%PDF-1.4 original"
            annotated_pdf = b"%PDF-1.4 annotated"
            house_original_id = db.execute(
                """
                INSERT INTO house_documents
                    (house_id, filename, content, size_bytes, uploaded_by, uploaded_at)
                VALUES (?, 'survey.pdf', ?, ?, 'Admin One', '2026-10-27T13:00:00')
                """,
                (house_id, original_pdf, len(original_pdf)),
            ).lastrowid
            db.execute(
                """
                INSERT INTO house_documents
                    (house_id, filename, content, size_bytes, uploaded_by, uploaded_at)
                VALUES (?, 'survey-annotated.pdf', ?, ?, 'Annotated by Admin One',
                    '2026-10-27T13:01:00')
                """,
                (house_id, annotated_pdf, len(annotated_pdf)),
            )
            seai_original_id = db.execute(
                """
                INSERT INTO seai_documents
                    (application_id, category, filename, extension, content)
                VALUES (?, 'Survey', 'seai-survey.pdf', '.pdf', ?)
                """,
                (house_id, original_pdf),
            ).lastrowid
            db.execute(
                """
                INSERT INTO seai_documents
                    (application_id, category, filename, extension, content)
                VALUES (?, 'Survey', 'seai-survey-annotated.pdf', '.pdf', ?)
                """,
                (house_id, annotated_pdf),
            )
            db.execute(
                "INSERT INTO seai_applications VALUES (?, ?)",
                (
                    house_id,
                    json.dumps({
                        "Measures": [{
                            "Name": "External Wall Insulation",
                            "Survey Quantity": "94",
                            "Survey Qty including Variation": "111.5",
                        }]
                    }),
                ),
            )

        status, result, _ = self.call(
            "POST", "/api/houses/plan", {"id": house_id, "ready": False}
        )
        self.assertEqual(status, 200, result)

        with server.connect_db() as db:
            house = db.execute(
                """
                SELECT ready_for_start, notes, plan_scaffolding,
                       plan_scaffolding_required, plan_scaffolding_notes,
                       plan_esb_required, plan_esb_notes,
                       plan_plumber_required, plan_plumber_notes
                FROM provided_houses WHERE id = ?
                """,
                (house_id,),
            ).fetchone()
            self.assertEqual(house["ready_for_start"], 0)
            self.assertEqual(house["notes"], "")
            self.assertIsNone(house["plan_scaffolding"])
            self.assertIsNone(house["plan_scaffolding_required"])
            self.assertEqual(house["plan_scaffolding_notes"], "")
            self.assertIsNone(house["plan_esb_required"])
            self.assertEqual(house["plan_esb_notes"], "")
            self.assertIsNone(house["plan_plumber_required"])
            self.assertEqual(house["plan_plumber_notes"], "")
            for table in ("house_surveys", "house_checklists", "house_snags"):
                self.assertEqual(
                    db.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE house_id = ?",
                        (house_id,),
                    ).fetchone()[0],
                    0,
                )
            self.assertEqual(
                db.execute(
                    "SELECT filename FROM house_documents WHERE house_id = ?",
                    (house_id,),
                ).fetchall()[0]["filename"],
                "survey.pdf",
            )
            self.assertEqual(
                db.execute(
                    "SELECT filename FROM seai_documents WHERE application_id = ?",
                    (house_id,),
                ).fetchall()[0]["filename"],
                "seai-survey.pdf",
            )
            variation = json.loads(
                db.execute(
                    "SELECT detail_data_json FROM seai_applications "
                    "WHERE application_id = ?",
                    (house_id,),
                ).fetchone()[0]
            )["Measures"][0]["Survey Qty including Variation"]
            self.assertEqual(variation, "111.5")
            self.assertEqual(
                db.execute(
                    "SELECT status FROM house_status_history WHERE house_id = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (house_id,),
                ).fetchone()["status"],
                "not_started",
            )

        status, documents, _ = self.call(
            "GET", f"/api/house-documents?house_id={house_id}"
        )
        self.assertEqual(status, 200, documents)
        self.assertEqual([document["filename"] for document in documents["documents"]], ["survey.pdf"])
        status, _, _, body = self.call_raw(
            "GET", f"/api/house-documents/{house_original_id}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, original_pdf)
        self.assertEqual(
            self.call_raw("GET", f"/api/house-documents/{house_original_id}/share")[0],
            409,
        )
        status, seai_documents, _ = self.call(
            "GET", f"/api/seai-documents?house_id={house_id}"
        )
        self.assertEqual(status, 200, seai_documents)
        self.assertEqual(
            [document["filename"] for document in seai_documents["documents"]],
            ["seai-survey.pdf"],
        )
        status, _, _, body = self.call_raw(
            "GET", f"/api/seai-documents/{seai_original_id}/file"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, original_pdf)
        status, result, _ = self.call(
            "POST",
            f"/api/seai-documents/{seai_original_id}/annotations",
            {"annotations": []},
        )
        self.assertEqual(status, 409, result)
        self.assertIn("view-only", result["error"])
        self.assertEqual(
            self.call_raw_json(
                "POST", f"/api/seai-documents/{seai_original_id}/share", {}
            )[0],
            409,
        )
        status, result, _ = self.call(
            "POST",
            "/api/seai-documents/share",
            {
                "house_id": house_id,
                "pages": [{"document_id": seai_original_id, "page": 1}],
            },
        )
        self.assertEqual(status, 409, result)
        status, result, _ = self.call(
            "POST",
            "/api/seai-measure-variation",
            {"house_id": house_id, "measure_index": 0, "variation": "95"},
        )
        self.assertEqual(status, 409, result)
        status, result, _ = self.call(
            "POST",
            "/api/house-survey",
            {
                "house_id": house_id,
                "survey": {"windows": [], "doors": [], "house_corners": [], "notes": ""},
            },
        )
        self.assertEqual(status, 409, result)
        status, result, _ = self.call(
            "POST", "/api/house-notes", {"house_id": house_id, "notes": "blocked"}
        )
        self.assertEqual(status, 409, result)
        self.assertEqual(
            self.call_raw_json(
                "POST",
                f"/api/house-documents/{house_original_id}/annotations",
                {"annotations": []},
            )[0],
            404,
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

    def test_assign_can_only_mark_a_house_ready(self):
        status, _, _ = self.call(
            "POST",
            "/api/users",
            {
                "username": "assign-house-user",
                "display_name": "Assign House User",
                "pin": "234567",
                "role": "assign",
            },
        )
        self.assertEqual(status, 201)
        status, _, assign_cookie = self.call(
            "POST",
            "/api/auth/login",
            {"username": "assign-house-user", "pin": "234567"},
        )
        self.assertEqual(status, 200)

        status, result, _ = self.call(
            "POST",
            "/api/houses/plan",
            {"id": "WH1", "ready": True},
            cookie=assign_cookie,
        )
        self.assertEqual(status, 200, result)

        status, result, _ = self.call(
            "POST",
            "/api/houses/plan",
            {"id": "WH1", "ready": False},
            cookie=assign_cookie,
        )
        self.assertEqual(status, 403)
        self.assertIn("only mark houses ready", result["error"])
        status, result, _ = self.call(
            "POST",
            "/api/houses/plan",
            {"id": "WH2", "notes": "not allowed"},
            cookie=assign_cookie,
        )
        self.assertEqual(status, 403)
        self.assertIn("only mark houses ready", result["error"])
        with server.connect_db() as db:
            self.assertEqual(
                db.execute(
                    "SELECT ready_for_start FROM provided_houses WHERE id = 'WH1'"
                ).fetchone()["ready_for_start"],
                1,
            )
            self.assertEqual(
                db.execute(
                    "SELECT notes FROM provided_houses WHERE id = 'WH2'"
                ).fetchone()["notes"],
                "",
            )
        self.assertEqual(
            self.call("GET", "/api/movements", cookie=assign_cookie)[0],
            403,
        )

    def test_newly_ready_house_is_first_in_ready_queue(self):
        with server.connect_db() as db:
            with db:
                db.execute(
                    "UPDATE provided_houses SET ready_for_start = 1 WHERE id = 'WH2'"
                )
                db.execute(
                    """
                    INSERT INTO house_status_history(house_id,status,changed_by,changed_at)
                    VALUES ('WH2','ready','Earlier readiness','2020-01-01T10:00:00')
                    """
                )

        status, _, _ = self.plan({"id": "WH1", "ready": True})
        self.assertEqual(status, 200)
        _, result, _ = self.call("GET", "/api/calculator/houses?status=ready")
        self.assertEqual(
            [house["id"] for house in result["houses"]],
            ["WH1", "WH2"],
        )

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

    def test_map_listing_includes_located_not_started_ready_and_active_houses(self):
        self.provide("WH3", "T3", "3 Elm", "Owner3")
        with server.connect_db() as db:
            db.execute(
                "UPDATE provided_houses SET latitude = 53.4, longitude = -8.1 "
                "WHERE id = 'WH3'"
            )
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
            {"WH1": "ready", "WH3": "not_started"},
        )
        self.plan({"id": "WH1", "ready": False})
        self.activate({
            "id": "WH1",
            "postcode": "T1",
            "owner_name": "Owner",
            "perimeter_m": 51.2,
        })
        with server.connect_db() as db:
            db.execute("UPDATE houses SET stage = 2 WHERE id = 'WH1'")
        _, mapped, _ = self.call("GET", "/api/calculator/houses?status=map")
        self.assertEqual(
            {house["id"]: (house["status"], house["stage"]) for house in mapped["houses"]},
            {"WH1": ("active", 2), "WH3": ("not_started", 1)},
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
            db.execute(
                "CREATE TABLE seai_applications "
                "(application_id TEXT PRIMARY KEY, detail_data_json TEXT NOT NULL)"
            )
            db.execute(
                "INSERT INTO seai_applications VALUES (?, ?)",
                (
                    "WH1",
                    json.dumps({
                        "Measures": [{
                            "Name": "External Wall Insulation",
                            "Survey Quantity": "94",
                            "Survey Qty including Variation": "104",
                        }]
                    }),
                ),
            )
        self.plan({"id": "WH1", "ready": True, "notes": "Key with neighbour"})
        status, _, _ = self.call("POST", "/api/house-checklist", {
            "house_id": "WH1", "scaffolding_status": "done",
            "esb_unclipped": "no", "plumber_status": "not_necessary"})
        self.assertEqual(status, 200)
        _, planned, _ = self.call("GET", "/api/house-checklist/WH1")
        self.assertEqual(planned["scaffolding_status"], "done")
        self.activate({
            "id": "WH1",
            "postcode": "T1",
            "owner_name": "Owner",
            "perimeter_m": 51.2,
        })
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
        self.assertEqual(details["house"]["survey_perimeter_m"], 51.2)
        self.assertEqual(
            details["house"]["seai_details"]["Measures"][0][
                "Survey Qty including Variation"
            ],
            "104",
        )
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
        self.assertEqual(
            self.call(
                "POST",
                "/api/house-survey",
                {
                    "house_id": "WH-ACTIVE",
                    "survey": {
                        "windows": [],
                        "doors": [],
                        "house_corners": [],
                        "notes": "",
                        "perimeter_m": 51.2,
                    },
                },
            )[0],
            200,
        )

        status, _, _ = self.call(
            "POST",
            "/api/houses",
            {
                "id": "WH-ACTIVE",
                "postcode": "WRONG",
                "owner_name": "Wrong owner",
                "employee_name": "Assigned Worker",
                "perimeter_m": 51.2,
            },
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

    def test_provided_house_needs_ready_status_and_survey_before_activation(self):
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
        status, result, _ = self.call("POST", "/api/houses", {"id": "WH1"})
        self.assertEqual(status, 409)
        self.assertIn("Surveyor review is required", result["error"])
        self.assertEqual(self.call("POST", "/api/movements", {**movement, "material_id": mesh})[0], 409)
        status, survey_result, _ = self.call(
            "POST",
            "/api/house-survey",
            {
                "house_id": "WH1",
                "survey": {
                    "windows": [],
                    "doors": [],
                    "house_corners": [],
                    "notes": "",
                    "perimeter_m": 40,
                },
            },
        )
        self.assertEqual(status, 200, survey_result)
        self.assertEqual(
            self.call(
                "POST",
                "/api/houses",
                {"id": "WH1", "employee_name": "Assigned Worker", "perimeter_m": 40},
            )[0],
            201,
        )
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


class HouseRoleAssignmentTests(HttpBase):
    def setUp(self):
        super().setUp()
        self.provide("ASSIGN-1", "T1", "1 Elm", "Assignment Owner")
        self.call(
            "POST",
            "/api/users",
            {
                "username": "assign-operator",
                "display_name": "Assign Operator",
                "pin": "234567",
                "role": "assign",
            },
        )
        self.call(
            "POST",
            "/api/users",
            {
                "username": "house-surveyor",
                "display_name": "House Surveyor",
                "pin": "345678",
                "role": "surveyor",
            },
        )
        self.call(
            "POST",
            "/api/users",
            {
                "username": "house-supervisor",
                "display_name": "House Supervisor",
                "pin": "456789",
                "role": "supervisor",
            },
        )
        _, _, self.assign_cookie = self.call(
            "POST",
            "/api/auth/login",
            {"username": "assign-operator", "pin": "234567"},
        )
        self.call(
            "POST",
            "/api/houses/plan",
            {"id": "ASSIGN-1", "ready": True},
            cookie=self.assign_cookie,
        )
        with server.connect_db() as db:
            self.surveyor_id = db.execute(
                "SELECT id FROM app_users WHERE username = 'house-surveyor'"
            ).fetchone()["id"]
            self.supervisor_id = db.execute(
                "SELECT id FROM app_users WHERE username = 'house-supervisor'"
            ).fetchone()["id"]

    def test_assign_dashboard_reports_surveyor_workload(self):
        self.provide("ASSIGN-COMPLETED", "T2", "2 Elm", "Completed Owner")
        self.call(
            "POST",
            "/api/users",
            {
                "username": "available-surveyor",
                "display_name": "Available Surveyor",
                "pin": "654321",
                "role": "surveyor",
            },
        )
        with server.connect_db() as db:
            db.execute(
                """
                INSERT INTO house_role_assignments
                    (house_id, role, user_id, assigned_by, assigned_at)
                VALUES ('ASSIGN-1', 'surveyor', ?, 'Assign Operator', '2026-10-29T10:00:00')
                """,
                (self.surveyor_id,),
            )
            db.execute(
                """
                INSERT INTO house_role_assignments
                    (house_id, role, user_id, assigned_by, assigned_at)
                VALUES ('ASSIGN-COMPLETED', 'surveyor', ?, 'Assign Operator', '2026-10-29T10:00:00')
                """,
                (self.surveyor_id,),
            )
            db.execute(
                """
                INSERT INTO house_surveys(house_id, data, updated_by, updated_at)
                VALUES ('ASSIGN-COMPLETED', '{}', 'House Surveyor', '2026-10-29T12:00:00')
                """
            )

        status, dashboard, _ = self.call(
            "GET", "/api/dashboard", cookie=self.assign_cookie
        )
        self.assertEqual(status, 200, dashboard)
        workload = {
            surveyor["name"]: surveyor
            for surveyor in dashboard["surveyor_workload"]
        }
        self.assertEqual(
            workload["House Surveyor"],
            {
                "user_id": self.surveyor_id,
                "name": "House Surveyor",
                "pending_house_ids": ["ASSIGN-1"],
                "completed_count": 1,
                "pending_count": 1,
                "busy": True,
            },
        )
        self.assertEqual(workload["Available Surveyor"]["pending_count"], 0)
        self.assertFalse(workload["Available Surveyor"]["busy"])
        self.assertNotIn("surveyor_workload", self.call("GET", "/api/dashboard")[1])

    def test_change_revisions_track_dashboard_and_user_updates_separately(self):
        before = self.call("GET", "/api/updates")[1]["revisions"]
        self.provide("REVISION-TEST-1", "TR1", "1 Elm", "Revision Owner")
        status, _, _ = self.call(
            "POST",
            "/api/houses/plan",
            {"id": "REVISION-TEST-1", "ready": True},
        )
        self.assertEqual(status, 200)
        after_house = self.call("GET", "/api/updates")[1]["revisions"]
        self.assertEqual(after_house["dashboard"], before["dashboard"] + 1)
        self.assertEqual(after_house["movements"], before["movements"])
        self.assertEqual(after_house["users"], before["users"])

        status, _, _ = self.call(
            "POST",
            "/api/users",
            {
                "username": "revision-user",
                "display_name": "Revision User",
                "pin": "654321",
                "role": "surveyor",
            },
        )
        self.assertEqual(status, 201)
        after_user = self.call("GET", "/api/updates")[1]["revisions"]
        self.assertEqual(after_user["dashboard"], after_house["dashboard"])
        self.assertEqual(after_user["movements"], after_house["movements"])
        self.assertEqual(after_user["users"], after_house["users"] + 1)

    def test_surveyor_only_sees_ready_houses_assigned_to_their_account(self):
        self.provide("ASSIGN-2", "T2", "2 Elm", "Second Owner")
        self.provide("ASSIGN-3", "T3", "3 Elm", "Unassigned Owner")
        self.call(
            "POST",
            "/api/users",
            {
                "username": "second-surveyor",
                "display_name": "Second Surveyor",
                "pin": "654321",
                "role": "surveyor",
            },
        )
        with server.connect_db() as db:
            second_surveyor_id = db.execute(
                "SELECT id FROM app_users WHERE username = 'second-surveyor'"
            ).fetchone()["id"]

        for house_id in ("ASSIGN-1", "ASSIGN-2", "ASSIGN-3"):
            status, result, _ = self.call(
                "POST",
                "/api/houses/plan",
                {"id": house_id, "ready": True},
            )
            self.assertEqual(status, 200, result)

        for house_id, surveyor_id in (
            ("ASSIGN-1", self.surveyor_id),
            ("ASSIGN-2", second_surveyor_id),
        ):
            status, result, _ = self.call(
                "POST",
                "/api/house-assignments",
                {"house_id": house_id, "surveyor_user_id": surveyor_id},
            )
            self.assertEqual(status, 200, result)

        _, _, surveyor_cookie = self.call(
            "POST",
            "/api/auth/login",
            {"username": "house-surveyor", "pin": "345678"},
        )
        status, result, _ = self.call(
            "GET",
            "/api/calculator/houses?status=all",
            cookie=surveyor_cookie,
        )
        self.assertEqual(status, 200, result)
        self.assertEqual([house["id"] for house in result["houses"]], ["ASSIGN-1"])

    def test_assignment_dashboard_activation_prerequisites_and_material_restrictions(self):
        status, result, _ = self.call(
            "GET",
            "/api/house-assignments?house_id=ASSIGN-1",
            cookie=self.assign_cookie,
        )
        self.assertEqual(status, 200, result)
        self.assertFalse(result["requirements"]["has_survey"])
        self.assertFalse(result["requirements"]["has_assigned_surveyor"])
        self.assertEqual(len(result["users"]["surveyor"]), 1)
        self.assertEqual(len(result["users"]["supervisor"]), 1)

        status, result, _ = self.call(
            "POST",
            "/api/house-assignments",
            {
                "house_id": "ASSIGN-1",
                "surveyor_user_id": self.surveyor_id,
            },
            cookie=self.assign_cookie,
        )
        self.assertEqual(status, 200, result)
        status, result, _ = self.call(
            "GET",
            "/api/house-assignments?house_id=ASSIGN-1",
            cookie=self.assign_cookie,
        )
        self.assertTrue(result["requirements"]["has_assigned_surveyor"])
        self.assertFalse(
            any(assignment["role"] == "supervisor" for assignment in result["assignments"])
        )

        status, result, _ = self.call(
            "GET",
            "/api/calculator/houses?status=ready",
            cookie=self.assign_cookie,
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(result["houses"][0]["assigned_surveyor_name"], "House Surveyor")
        self.assertTrue(result["houses"][0]["surveyor_assigned_at"])
        self.assertNotIn("surveyor_assigned_by", result["houses"][0])
        self.assertFalse(result["houses"][0]["survey"])

        status, result, _ = self.call(
            "POST",
            "/api/houses",
            {"id": "ASSIGN-1", "supervisor_user_id": self.supervisor_id},
            cookie=self.assign_cookie,
        )
        self.assertEqual(status, 409)
        self.assertIn("Surveyor review", result["error"])

        with server.connect_db() as db:
            db.execute(
                """
                INSERT INTO house_surveys(house_id, data, notes, updated_by, updated_at)
                VALUES (?, ?, '', 'House Surveyor', '2026-10-29T10:00:00')
                """,
                (
                    "ASSIGN-1",
                    json.dumps(
                        {
                            "windows": [],
                            "doors": [],
                            "house_corners": [],
                            "notes": "",
                        }
                    ),
                ),
            )
        status, result, _ = self.call(
            "GET",
            "/api/house-assignments?house_id=ASSIGN-1",
            cookie=self.assign_cookie,
        )
        self.assertEqual(status, 200, result)
        self.assertTrue(result["requirements"]["has_survey"])
        self.assertFalse(result["requirements"]["has_surveyed_perimeter"])
        status, result, _ = self.call(
            "POST",
            "/api/houses",
            {"id": "ASSIGN-1", "supervisor_user_id": self.supervisor_id},
            cookie=self.assign_cookie,
        )
        self.assertEqual(status, 409)
        self.assertIn("valid surveyed perimeter", result["error"])
        with server.connect_db() as db:
            db.execute(
                "UPDATE house_surveys SET data = ? WHERE house_id = ?",
                (
                    json.dumps(
                        {
                            "windows": [],
                            "doors": [],
                            "house_corners": [],
                            "notes": "",
                            "perimeter_m": 42.5,
                        }
                    ),
                    "ASSIGN-1",
                ),
            )
        status, result, _ = self.call(
            "GET",
            "/api/calculator/houses?status=ready",
            cookie=self.assign_cookie,
        )
        self.assertEqual(result["houses"][0]["assigned_surveyor_name"], "House Surveyor")
        self.assertTrue(result["houses"][0]["surveyor_assigned_at"])
        self.assertNotIn("surveyor_assigned_by", result["houses"][0])
        self.assertTrue(result["houses"][0]["survey"])
        self.assertTrue(result["houses"][0]["has_assigned_surveyor"])
        self.assertTrue(result["houses"][0]["has_surveyed_perimeter"])

        status, result, _ = self.call(
            "POST",
            "/api/houses",
            {"id": "ASSIGN-1", "supervisor_user_id": self.surveyor_id},
            cookie=self.assign_cookie,
        )
        self.assertEqual(status, 400)
        self.assertIn("active Supervisor", result["error"])

        status, result, _ = self.call(
            "POST",
            "/api/houses",
            {"id": "ASSIGN-1", "supervisor_user_id": self.supervisor_id},
            cookie=self.assign_cookie,
        )
        self.assertEqual(status, 201, result)
        self.assertEqual(
            self.call(
                "POST",
                "/api/houses",
                {
                    "id": "ASSIGN-1",
                    "supervisor_user_id": self.supervisor_id,
                },
                cookie=self.assign_cookie,
            )[0],
            409,
        )
        with server.connect_db() as db:
            house = db.execute(
                "SELECT employee_name FROM houses WHERE id = 'ASSIGN-1'"
            ).fetchone()
            survey = db.execute(
                "SELECT updated_by, data FROM house_surveys WHERE house_id = 'ASSIGN-1'"
            ).fetchone()
            supervisor_assignment = db.execute(
                """
                SELECT user_id FROM house_role_assignments
                WHERE house_id = 'ASSIGN-1' AND role = 'supervisor'
                """
            ).fetchone()
        self.assertEqual(house["employee_name"], "House Supervisor")
        self.assertEqual(survey["updated_by"], "House Surveyor")
        self.assertEqual(json.loads(survey["data"])["perimeter_m"], 42.5)
        self.assertEqual(supervisor_assignment["user_id"], self.supervisor_id)
        self.assertEqual(self.call("GET", "/api/movements", cookie=self.assign_cookie)[0], 403)

    def test_surveyor_can_be_assigned_before_house_is_ready(self):
        self.provide("ASSIGN-NOT-READY", "T2", "2 Elm", "Pending Owner")

        status, result, _ = self.call(
            "GET",
            "/api/house-assignments?house_id=ASSIGN-NOT-READY",
            cookie=self.assign_cookie,
        )
        self.assertEqual(status, 200, result)
        status, result, _ = self.call(
            "POST",
            "/api/house-assignments",
            {
                "house_id": "ASSIGN-NOT-READY",
                "surveyor_user_id": self.surveyor_id,
            },
            cookie=self.assign_cookie,
        )
        self.assertEqual(status, 200, result)

        status, result, _ = self.call(
            "GET",
            "/api/calculator/houses?status=not_started",
            cookie=self.assign_cookie,
        )
        self.assertEqual(status, 200, result)
        house = next(
            item for item in result["houses"] if item["id"] == "ASSIGN-NOT-READY"
        )
        self.assertEqual(house["assigned_surveyor_name"], "House Surveyor")
        self.assertFalse(house["survey"])
        self.assertNotIn("surveyor_assigned_by", house)
        with server.connect_db() as db:
            assignment = db.execute(
                """
                SELECT assigned_by FROM house_role_assignments
                WHERE house_id = 'ASSIGN-NOT-READY' AND role = 'surveyor'
                """
            ).fetchone()
        self.assertEqual(assignment["assigned_by"], "Assign Operator")

    def test_only_admin_and_assign_users_can_manage_house_assignments(self):
        self.call(
            "POST",
            "/api/users",
            {
                "username": "ordinary-supervisor",
                "display_name": "Ordinary Supervisor",
                "pin": "567890",
                "role": "supervisor",
            },
        )
        _, _, supervisor_cookie = self.call(
            "POST",
            "/api/auth/login",
            {"username": "ordinary-supervisor", "pin": "567890"},
        )
        self.assertEqual(
            self.call(
                "GET",
                "/api/house-assignments?house_id=ASSIGN-1",
                cookie=supervisor_cookie,
            )[0],
            403,
        )
        self.assertEqual(
            self.call(
                "POST",
                "/api/house-assignments",
                {
                    "house_id": "ASSIGN-1",
                    "surveyor_user_id": self.surveyor_id,
                    "supervisor_user_id": self.supervisor_id,
                },
                cookie=supervisor_cookie,
            )[0],
            403,
        )

    def test_supervisor_dashboard_is_scoped_and_can_assign_delivery_details(self):
        self.provide("SUPERVISOR-OTHER", "T2", "2 Elm", "Other Owner")
        self.provide("SUPERVISOR-UNASSIGNED", "T3", "3 Elm", "Unassigned Owner")
        for username, display_name, is_active in (
            ("teamlead-one", "Team Lead One", True),
            ("teamlead-two", "Team Lead Two", True),
            ("teamlead-inactive", "Inactive Team Lead", False),
        ):
            status, _, _ = self.call(
                "POST",
                "/api/users",
                {
                    "username": username,
                    "display_name": display_name,
                    "pin": "654321",
                    "role": "teamlead",
                },
            )
            self.assertEqual(status, 201)
            if not is_active:
                with server.connect_db() as db:
                    db.execute(
                        "UPDATE app_users SET is_active = 0 WHERE username = ?",
                        (username,),
                    )
        self.call(
            "POST",
            "/api/users",
            {
                "username": "other-supervisor",
                "display_name": "Other Supervisor",
                "pin": "567890",
                "role": "supervisor",
            },
        )
        with server.connect_db() as db:
            self.other_supervisor_id = db.execute(
                "SELECT id FROM app_users WHERE username = 'other-supervisor'"
            ).fetchone()["id"]
            with db:
                server._upsert_house(db, "ASSIGN-1")
                server._upsert_house(db, "SUPERVISOR-OTHER")
                server._upsert_house(db, "SUPERVISOR-UNASSIGNED")
                db.executemany(
                    """
                    INSERT INTO house_role_assignments
                        (house_id, role, user_id, assigned_by, assigned_at)
                    VALUES (?, 'supervisor', ?, 'Test Admin', '2026-10-29T10:00:00')
                    """,
                    (
                        ("ASSIGN-1", self.supervisor_id),
                        ("SUPERVISOR-OTHER", self.other_supervisor_id),
                    ),
                )
        _, _, supervisor_cookie = self.call(
            "POST",
            "/api/auth/login",
            {"username": "house-supervisor", "pin": "456789"},
        )

        status, dashboard, _ = self.call(
            "GET", "/api/dashboard", cookie=supervisor_cookie
        )
        self.assertEqual(status, 200)
        self.assertEqual([house["id"] for house in dashboard["house_list"]], ["ASSIGN-1"])
        self.assertEqual(dashboard["materials"], [])
        self.assertEqual(dashboard["movements"], [])
        self.assertEqual(dashboard["teams"], [])
        self.assertEqual(dashboard["locations"], [])
        self.assertEqual(dashboard["house_summary"]["active"], 1)
        self.assertEqual(
            dashboard["supervisor_assignment_options"]["delivery_people"],
            ["Gary Lowther", "Karol", "Team Lead One", "Team Lead Two"],
        )
        self.assertEqual(
            dashboard["supervisor_assignment_options"]["teams"],
            ["Team Lead One", "Team Lead Two"],
        )

        status, result, _ = self.call(
            "POST",
            "/api/supervisor/house-assignment",
            {
                "house_id": "ASSIGN-1",
                "delivery_person": "Karol",
                "delivery_team": "Crew North",
            },
            cookie=supervisor_cookie,
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(result["delivery_person"], "Karol")
        self.assertEqual(result["delivery_team"], "Crew North")

        _, dashboard, _ = self.call(
            "GET", "/api/dashboard", cookie=supervisor_cookie
        )
        house = dashboard["house_list"][0]
        self.assertEqual(house["delivery_person"], "Karol")
        self.assertEqual(house["delivery_team"], "Crew North")

        self.assertEqual(
            self.call(
                "POST",
                "/api/supervisor/house-assignment",
                {
                    "house_id": "SUPERVISOR-OTHER",
                    "delivery_person": "Karol",
                    "delivery_team": "Crew North",
                },
                cookie=supervisor_cookie,
            )[0],
            404,
        )
        self.assertEqual(
            self.call(
                "POST",
                "/api/supervisor/house-assignment",
                {
                    "house_id": "ASSIGN-1",
                    "delivery_person": "Karol",
                    "delivery_team": "Crew North",
                },
                cookie=self.cookie,
            )[0],
            403,
        )


class FrontEndLogicTests(unittest.TestCase):
    def test_audit_date_time_uses_readable_day_month_year_and_24_hour_time(self):
        start = APP_JS.index("function formatAuditDateTime")
        end = APP_JS.index("\nfunction houseAuditHtml", start)
        script = (
            APP_JS[start:end]
            + '\nconsole.log(formatAuditDateTime("2026-10-22T12:30:00"));'
        )
        out = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, timeout=20
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "22 Oct 2026, 12:30")

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
        self.assertIn('${h.survey ? "Review survey" : "Surveyor room"}', APP_JS)
        self.assertIn("seaiInsulationSummaryHtml(h.seai_details, h.id)", APP_JS)
        self.assertIn("View Surveyor room", APP_JS)
        self.assertIn('id="survey-room-seai-details"', (ROOT / "static" / "index.html").read_text(encoding="utf-8"))

    def test_surveyor_room_lists_seai_pdfs_for_annotation(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn('id="survey-room-seai-documents"', html)
        self.assertIn('id="pdf-annotator-dialog"', html)
        self.assertIn(
            "loadSeaiDocuments(houseId, state.surveyRoomRequest, isReady)",
            APP_JS,
        )
        self.assertIn('`/api/seai-documents?house_id=${encodeURIComponent(houseId)}`', APP_JS)
        self.assertIn('data-document-id="${Number(document.id)}"', APP_JS)
        self.assertIn('"seai-documents" : "house-documents"', APP_JS)
        self.assertIn('"The original SEAI PDF stays unchanged.', APP_JS)
        self.assertIn("The original is unchanged.", APP_JS)
        self.assertIn(".seai-document-category", css)
        self.assertIn("document.is_annotated ? \"Annotated copy\" : \"Original\"", APP_JS)
        self.assertIn(".seai-document-status.original", css)
        self.assertIn(".seai-document-status.annotated", css)
        self.assertIn('data-seai-share-selected', APP_JS)
        self.assertIn('data-seai-document-share="${Number(document.id)}"', APP_JS)
        self.assertIn("Share annotated PDF", APP_JS)
        self.assertIn('id="pdf-annotator-page-selection"', html)
        self.assertIn('data-seai-share-clear', APP_JS)
        self.assertIn("/api/seai-documents/share", APP_JS)
        self.assertIn("View-only PDFs.", APP_JS)
        self.assertIn("Uploading, annotations, and sharing are disabled", APP_JS)
        self.assertIn(
            '`<a class="button button-secondary" href="/api/seai-documents/${Number(document.id)}/file"',
            APP_JS,
        )
        self.assertIn("loadHouseDocuments(houseId, state.surveyRoomRequest, isReady)", APP_JS)
        self.assertIn('data-not-started-only', html)

    def test_pdf_annotator_uses_scroll_for_page_navigation_without_arrows(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="pdf-annotator-page-count"', html)
        self.assertNotIn('id="pdf-annotator-previous"', html)
        self.assertNotIn('id="pdf-annotator-next"', html)
        self.assertIn('addEventListener("wheel", scrollPdfPage', APP_JS)
        self.assertIn("Scroll to change pages", APP_JS)
        self.assertIn("event.preventDefault();", APP_JS)
        start = APP_JS.index("function scrollPdfPage(event)")
        end = APP_JS.index("function openHouseDocumentAnnotator", start)
        script = """
          const state = { pdfAnnotator: {
            page: 1, pageCount: 4, zoom: 1, wheelDelta: 0,
            wheelLocked: false, loading: false
          }};
          let viewport = { scrollHeight: 100, clientHeight: 100, scrollTop: 0 };
          const $ = () => viewport;
          const requestedPages = [];
          const loadHouseDocumentPage = page => requestedPages.push(page);
          const prevented = [];
        """ + APP_JS[start:end] + """
          const wheel = deltaY => scrollPdfPage({
            deltaY, preventDefault: () => prevented.push(deltaY)
          });
          wheel(20);
          if (requestedPages.length) throw new Error("Small wheel delta changed page.");
          wheel(20);
          if (requestedPages.join(",") !== "2") throw new Error("Scroll did not advance one page.");
          state.pdfAnnotator.wheelLocked = false;
          state.pdfAnnotator.page = 2;
          state.pdfAnnotator.zoom = 1.5;
          viewport = { scrollHeight: 300, clientHeight: 100, scrollTop: 100 };
          wheel(50);
          if (requestedPages.length !== 1) throw new Error("Scroll changed page before reaching the zoomed page edge.");
          viewport.scrollTop = 200;
          wheel(50);
          if (requestedPages.join(",") !== "2,3") throw new Error("Scroll at the page edge did not advance.");
          console.log("PDF scroll navigation passed");
        """;
        result = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode:
            raise AssertionError(result.stderr)
        self.assertIn("PDF scroll navigation passed", result.stdout)

    def test_insulation_card_shows_both_seai_reported_quantities(self):
        start = APP_JS.index("const SEAI_INSULATION_GROUPS")
        end = APP_JS.index("async function loadSurveyorHouses", start)
        script = (
            'const escapeHtml=value=>String(value??"").replaceAll("&","&amp;").replaceAll("<","&lt;");\n'
            + APP_JS[start:end]
            + '\nconsole.log(seaiInsulationSummaryHtml({Measures:['
            + '{"Name":"Attic (Loft) Insulation 100 mm top-up","Survey Quantity":"45","Survey Qty including Variation":"45"},'
            + '{"Name":"External Wall Insulation: Greater than 85m2","Survey Quantity":"94","Survey Qty including Variation":"104"},'
            + '{"Name":"Internal Wall Insulation: Vertical Surface","Survey Quantity":"10","Survey Qty including Variation":"0"},'
            + '{"Name":"Cavity Wall Insulation Bonded Bead","Survey Quantity":"16","Survey Qty including Variation":"16"}'
            + ']}));'
        )
        result = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Attic (Loft) Insulation", result.stdout)
        self.assertIn("Survey qty", result.stdout)
        self.assertIn("Including variation", result.stdout)
        self.assertIn(">45 m\u00b2<", result.stdout)
        self.assertIn(">94 m\u00b2<", result.stdout)
        self.assertIn(">104 m\u00b2<", result.stdout)
        self.assertIn(">10 m\u00b2<", result.stdout)
        self.assertIn(">0 m\u00b2<", result.stdout)
        self.assertIn(">16 m\u00b2<", result.stdout)
        self.assertNotIn("seai-card-variation-edit", result.stdout)
        self.assertNotIn("seai-card-variation-save", result.stdout)
        self.assertIn("Attic (Loft) Insulation", result.stdout)
        self.assertIn("Internal Wall Insulation", result.stdout)
        css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn('.house-insulation-row > [role="cell"] { display: flex; flex-direction: column; align-items: stretch;', css)
        self.assertIn(".house-insulation-missing { display: flex; flex: 1 0 auto; align-items: flex-start; border: 1px solid #ead8a6;", css)
        self.assertIn("background: #fff8e6; color: #82621a;", css)
        self.assertIn('class="seai-variation-input"', APP_JS)
        self.assertIn('survey-room-seai-measures', APP_JS)
        self.assertIn('html[data-theme="dark"] .survey-room-seai-measures .survey-room-seai-table thead', css)
        self.assertIn('html[data-theme="dark"] .survey-room-seai-measures .seai-variation-input', css)
        self.assertIn('request("/api/seai-measure-variation"', APP_JS)
        self.assertIn('class="button button-secondary seai-variation-save"', APP_JS)
        self.assertNotIn("seai-card-variation-edit", APP_JS)
        start = APP_JS.index("function seaiDetailValue")
        end = APP_JS.index("async function saveSeaiMeasureVariation", start)
        render_result = subprocess.run(
            [
                "node",
                "-e",
                'const escapeHtml=value=>String(value??"").replaceAll("&","&amp;").replaceAll("<","&lt;");\n'
                + APP_JS[start:end]
                + '\nconsole.log(seaiDetailSectionHtml("Measures",[{'
                + '"Name":"External Wall Insulation","Survey Qty including Variation":"104"}]));',
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(render_result.returncode, 0, render_result.stderr)
        self.assertIn('type="number"', render_result.stdout)
        self.assertIn('data-seai-measure-index="0"', render_result.stdout)
        self.assertIn('class="button button-secondary seai-variation-edit"', render_result.stdout)
        self.assertIn(">Save</button>", render_result.stdout)
        self.assertIn('event.target.closest(".seai-variation-edit")', APP_JS)
        self.assertIn('.seai-variation-editor[hidden] { display: none; }', css)

    def test_seai_activation_dialog_uses_external_wall_variation_as_ewi_area(self):
        start = APP_JS.index("function fillSeaiHouse")
        end = APP_JS.index("function renderAll", start)
        script = (
            'const form={elements:{id:{},postcode:{},owner_name:{},employee_name:{},perimeter_m:{value:""}}};\n'
            + 'const info={innerHTML:"",classList:{remove(){}}};\n'
            + 'const results={classList:{add(){}}};\n'
            + 'const search={value:""};\n'
            + 'const perimeterInput={value:""};\n'
            + 'const $=selector=>({"#house-form":form,"#seai-info":info,"#seai-results":results,"#seai-search":search,"#survey-perimeter":perimeterInput}[selector]);\n'
            + 'const escapeHtml=value=>String(value??"").replaceAll("&","&amp;").replaceAll("<","&lt;");\n'
            + 'const formatQuantity=value=>new Intl.NumberFormat(undefined,{maximumFractionDigits:2}).format(value);\n'
            + APP_JS[start:end]
            + '\nfillSeaiHouse({id:"WH-TEST",seai_details:{Measures:['
            + '{"Name":"External Wall Insulation: Greater than 85m2",'
            + '"Survey Qty including Variation":"104"}]},'
            + 'survey:{house_corners:[2.4,3.1],perimeter_m:42.75},perimeter_m:51.2});\n'
            + 'console.log(JSON.stringify([info.innerHTML,form.elements.perimeter_m.value]));'
        )
        result = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, timeout=20
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("EWI Area: <b>104 m", result.stdout)
        self.assertIn("`${ewiArea} m²`", APP_JS)
        self.assertEqual(json.loads(result.stdout)[1], 42.75)
        self.assertIn("Perimeter: <b>42.75 m</b>", json.loads(result.stdout)[0])
        self.assertIn('form.elements.perimeter_m.value = h.survey?.perimeter_m ?? "";', APP_JS)
        self.assertIn('$("#survey-perimeter").value = survey.perimeter_m ?? "";', APP_JS)
        self.assertIn('perimeter_m: $("#survey-perimeter").value.trim()', APP_JS)
        self.assertNotIn("Area: <b>—</b>", result.stdout)

    def test_house_activation_requires_employee_and_perimeter(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        employee_input = re.search(
            r'<label class="field">Assigned employee(<input[^>]+>)', html
        )
        self.assertIsNotNone(employee_input)
        self.assertIn("required", employee_input.group(1))

        perimeter_input = re.search(
            r'<label class="field">Perimeter \(m\)(<input[^>]+>)', html
        )
        self.assertIsNotNone(perimeter_input)
        self.assertIn("required", perimeter_input.group(1))
        survey_perimeter = re.search(
            r'id="survey-perimeter"([^>]*)', html
        )
        self.assertIsNotNone(survey_perimeter)
        self.assertIn("required", survey_perimeter.group(1))

        validation = APP_JS[
            APP_JS.index("function validateHouseActivation"):
            APP_JS.index("async function submitHouse(event)")
        ]
        script = validation + """
const employee={value:"  ",setCustomValidity(message){this.message=message;},
  };
const perimeter={value:"42.5",setCustomValidity(message){this.message=message;}};
const form={elements:{employee_name:employee,perimeter_m:perimeter},
  reportValidity(){return !employee.message&&!perimeter.message;}};
const blankValid=validateHouseActivation(form);
const blankMessage=employee.message;
employee.value="  Assigned Worker  ";
const assignedValid=validateHouseActivation(form);
perimeter.value="";
const missingPerimeterValid=validateHouseActivation(form);
const perimeterMessage=perimeter.message;
console.log(JSON.stringify([blankValid,blankMessage,assignedValid,employee.value,missingPerimeterValid,perimeterMessage]));
"""
        result = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, timeout=20
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            [
                False,
                "Assign an employee before opening the calculator.",
                True,
                "Assigned Worker",
                False,
                "Enter a surveyed perimeter greater than 0 and no more than 10,000 metres.",
            ],
        )
        self.assertIn(
            '$("#house-form").elements.employee_name.addEventListener("input"',
            APP_JS,
        )

    def test_house_details_show_survey_perimeter_and_seai_ewi_area_only(self):
        detail_renderer = APP_JS[
            APP_JS.index("function renderHouseDetail"):
            APP_JS.index("async function loadHouseDetail")
        ]
        self.assertIn('house.seai_details?.Measures?.find', detail_renderer)
        self.assertIn('house.survey_perimeter_m ?? house.perimeter_m', detail_renderer)
        self.assertIn('["EWI area", ewiArea ? `${ewiArea} m²` : ""]', detail_renderer)
        self.assertIn('["Perimeter", surveyPerimeter == null ? "" : `${formatQuantity(surveyPerimeter)} m`]', detail_renderer)
        self.assertNotIn('"Floor area"', detail_renderer)

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
        ready_cards = APP_JS[
            APP_JS.index("async function renderNotStartedHouses"):
            APP_JS.index("function openHouseNote")
        ]
        self.assertNotIn("snagsHtml(h)", ready_cards)
        self.assertIn('house.status === "ready" || house.status === "not_started"', APP_JS)
        self.assertIn('id="house-detail-snag-section"', (ROOT / "static" / "index.html").read_text(encoding="utf-8"))
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
        action_bar = CALC_HTML[
            CALC_HTML.index('<section class="card action-bar">'):
            CALC_HTML.index("</section>", CALC_HTML.index('<section class="card action-bar">'))
        ]
        self.assertIn("Print / Save PDF", action_bar)
        self.assertIn(
            ".calculator-main .action-bar .actions{display:flex;width:100%;flex:0 0 100%;justify-content:flex-start",
            CALC_HTML,
        )
        self.assertIn(".calculator-main .action-bar #saveToHouse{background:#218653}", CALC_HTML)
        self.assertEqual(action_bar.count("<button"), 2)
        self.assertNotIn("Calculate", action_bar)
        self.assertNotIn("Create email request", action_bar)
        self.assertIn('field.addEventListener(field.tagName==="SELECT"?"change":"input",calc)', CALC_HTML)

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

    def test_auto_calculator_uses_seai_ewi_area_and_survey_perimeter(self):
        helpers = CALC_HTML[
            CALC_HTML.index("function calculatorArea"):
            CALC_HTML.index("function closeHouseResults")
        ]
        script = helpers + """
const house={
  area_m2:null,
  perimeter_m:51.2,
  survey:{perimeter_m:42.75},
  seai_details:{Measures:[
    {Name:"External Wall Insulation: Greater than 85m2","Survey Qty including Variation":"104"}
  ]}
};
console.log(JSON.stringify([
  calculatorArea(house),
  calculatorPerimeter(house),
  calculatorArea({area_m2:25,seai_details:{Measures:[]}}),
  calculatorPerimeter({perimeter_m:18,survey:{}})
]));
"""
        result = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, timeout=20
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            [104, 42.75, 25, 18],
        )
        self.assertIn('document.getElementById("wall").value=wallArea??""', CALC_HTML)
        self.assertIn('document.getElementById("per").value=perimeter??""', CALC_HTML)

    def test_saved_survey_is_marked_and_has_a_review_action(self):
        self.assertIn("function surveyStatusBadge(survey, completedAt = \"\")", APP_JS)
        self.assertIn('class="status-pill survey-complete">Survey completed', APP_JS)
        self.assertIn('${h.survey ? "Review survey" : "Surveyor room"}', APP_JS)
        self.assertIn('${h.survey ? "survey-reviewed" : "survey-needed"}', APP_JS)
        css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn(".house-card .house-card-actions .house-survey-open.survey-reviewed::before", css)
        self.assertIn(".status-pill.survey-complete", css)

    def test_ready_house_actions_have_distinct_accessible_colors(self):
        for action in ("seai-activate", "house-survey-open"):
            self.assertIn(f'class="text-button {action}', APP_JS)
        ready_renderer = APP_JS[
            APP_JS.index("async function renderNotStartedHouses"):
            APP_JS.index("function openHouseNote")
        ]
        self.assertNotIn("house-note-open", ready_renderer)
        self.assertNotIn("house-checklist-open", ready_renderer)
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
        self.assertNotIn("noteHtml(cardNote)", ready_renderer)
        self.assertNotIn("Pre-start checklist", ready_renderer)
        self.assertNotIn("house-note-open", ready_renderer)
        self.assertNotIn("Edit checklist", ready_renderer)
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
        self.assertIn(".house-grid.house-grid-ready .house-plan-ready .house-card-actions .seai-activate { grid-column: auto;", css)
        self.assertIn(".house-grid.house-grid-ready .house-plan-ready .house-card-actions .text-button { height: 39px; min-height: 39px;", css)
        self.assertIn(".house-grid.house-grid-ready > .house-plan-ready .house-body,\n.house-grid.house-grid-ready > .house-plan-ready .house-card-actions { max-height: none; overflow: visible; }", css)
        self.assertIn(".house-grid.house-grid-ready { grid-auto-rows: 1fr; align-items: stretch; }", css)

    def test_returning_house_to_not_started_has_no_confirmation_prompt(self):
        toggle_ready = APP_JS[
            APP_JS.index("async function toggleHouseReady"):
            APP_JS.index("const HOUSE_MAP_TILE_SIZE")
        ]
        self.assertNotIn("confirm(", toggle_ready)
        self.assertIn("variation quantities and original PDFs kept", toggle_ready)

    def test_not_started_queue_cards_fit_their_content(self):
        css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn(
            'class="card house-card ${isReady ? "house-ready house-plan-ready" : "house-pending house-plan-pending"}"',
            APP_JS,
        )
        self.assertIn(
            '<div class="house-body" data-house-id="${id}"${!isReady ? \' tabindex="0" role="region" aria-label="House status"\' : ""}>',
            APP_JS,
        )
        self.assertIn('id="surveyor-work-plan-form"', (ROOT / "static" / "index.html").read_text(encoding="utf-8"))
        self.assertIn("function updateHouseChecklistStatus(event)", APP_JS)
        self.assertIn("class=\"house-checklist-status\"", APP_JS)
        self.assertIn(".house-grid.house-grid-compact .house-plan-pending .house-card-actions .plan-ready-toggle[data-ready=\"true\"] { grid-column: 1 / -1;", css)
        self.assertIn(".house-grid.house-grid-compact { grid-auto-rows: auto; align-items: stretch; }", css)
        self.assertIn(".house-grid.house-grid-compact > .house-plan-pending { height: auto; min-height: 0; align-self: stretch; padding: 14px; }", css)
        self.assertIn(".house-grid.house-grid-compact > .house-plan-pending .house-body { flex: 1 0 auto; min-height: 0; max-height: none; overflow: visible;", css)
        self.assertIn(".house-grid.house-grid-compact > .house-plan-pending .house-card-actions { flex: 0 0 auto; height: auto;", css)
        self.assertNotIn("overflow-y: auto;", css[css.index(".house-grid.house-grid-compact > .house-plan-pending .house-body"):css.index(".house-grid > .house-card.house-finished")])

    def test_house_cards_size_to_content_across_status_tabs(self):
        css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn(
            ".house-grid.house-grid-ready { grid-auto-rows: auto; }\n.house-grid.house-grid-ready > .house-plan-ready { height: auto; min-height: 0; }\n.house-grid.house-grid-compact { grid-auto-rows: auto; align-items: stretch; }",
            css,
        )

    def test_ready_and_not_started_cards_fit_content_without_inner_scroll(self):
        css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn(
            ".house-grid.house-grid-ready { grid-auto-rows: 1fr; align-items: stretch; }",
            css,
        )
        self.assertIn(
            ".house-grid.house-grid-ready > .house-plan-ready .house-body,\n.house-grid.house-grid-ready > .house-plan-ready .house-card-actions { max-height: none; overflow: visible; }",
            css,
        )
        self.assertNotIn(
            ".house-grid.house-grid-ready,\n"
            ".house-grid.house-grid-compact { grid-auto-rows: 390px;",
            css,
        )
        self.assertNotIn(
            ".house-grid.house-grid-ready > .house-plan-ready .house-body,\n"
            ".house-grid.house-grid-compact > .house-plan-pending .house-body {\n"
            "  flex: 1 1 auto;\n"
            "  min-height: 0;\n"
            "  overflow-x: hidden;\n"
            "  overflow-y: auto;",
            css,
        )

    def test_dark_inventory_uses_readable_material_cards(self):
        css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn('html[data-theme="dark"] #page-inventory .table-wrap table {\n  display: table;', css)
        self.assertIn('html[data-theme="dark"] #inventory-table {\n  display: table-row-group;', css)
        self.assertIn('html[data-theme="dark"] #inventory-table tr {\n  display: table-row;', css)
        self.assertNotIn('html[data-theme="dark"] #inventory-table {\n  display: grid;', css)
        self.assertIn('html[data-theme="dark"] #inventory-table .inventory-cell-label {', css)
        self.assertIn('html[data-theme="dark"] #inventory-table td[data-label="Material"] .material-name {', css)
        self.assertIn('html[data-theme="dark"] #inventory-table td[data-label="Material"] {', css)
        self.assertIn(
            'html[data-theme="dark"] #inventory-table .text-button {\n  padding: 6px 12px;',
            css,
        )
        self.assertIn('data-label="Warehouse"', APP_JS)
        self.assertIn('data-label="With teams"', APP_JS)
        self.assertIn('data-label="On houses"', APP_JS)

    def test_dark_overview_stock_material_names_are_visible(self):
        css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn('html[data-theme="dark"] #overview-stock td:first-child {', css)
        self.assertIn('html[data-theme="dark"] #overview-stock .material-name {', css)
        self.assertIn('html[data-theme="dark"] #overview-stock .material-dot {', css)
        self.assertIn(
            'html[data-theme="dark"] #overview-stock tr:hover,\nhtml[data-theme="dark"] #overview-stock tr:hover td {\n  background: #293a32;\n  color: #edf6f0;',
            css,
        )
        self.assertIn('$("#overview-stock").innerHTML', APP_JS)

    def test_dark_search_fields_have_consistent_background_and_placeholders(self):
        css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn(
            'html[data-theme="dark"] .search-box {\n  border-color: #465951;\n  background: #17211e;\n  color: #a9bbb0;',
            css,
        )
        self.assertIn('html[data-theme="dark"] .search-box input::placeholder,', css)
        self.assertIn('html[data-theme="dark"] input[type="search"]::placeholder {', css)
        self.assertIn('color-scheme: dark;', css[css.index('html[data-theme="dark"] input[type="search"] {'):])

    def test_dark_mode_cards_use_cohesive_dark_surfaces(self):
        css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn('html[data-theme="dark"] .card,\nhtml[data-theme="dark"] .stat-card,\nhtml[data-theme="dark"] .team-card,\nhtml[data-theme="dark"] .house-card,', css)
        self.assertIn('html[data-theme="dark"] .kpi.seai { border-left-color: #86a9ba; background: #202d32; }', css)
        self.assertIn('html[data-theme="dark"] .house-tile.active { border-color: #3e7054; background: #20382b; }', css)
        self.assertIn('html[data-theme="dark"] .house-grid.house-grid-ready > .house-plan-ready,', css)
        self.assertIn('html[data-theme="dark"] #houses-list.house-grid-compact > .house-plan-pending .house-workflow-heading strong {\n  color: #edf5ef;', css)
        self.assertIn('html[data-theme="dark"] .back-button {\n  border-color: #4a6656;', css)
        self.assertIn('html[data-theme="dark"] .back-button:hover {\n  border-color: #75a987;', css)
        self.assertIn('html[data-theme="dark"] .activity-item { border-top-color: #34463e; }', css)

    def test_house_card_scrollbars_are_visible_and_theme_aware(self):
        css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn("--scroll-track: #e8efeb;", css)
        self.assertIn("--scroll-track: #26352e;", css)
        self.assertIn("scroll-behavior: auto; scroll-snap-type: none;", css)
        self.assertIn(".house-card .house-body::-webkit-scrollbar-thumb,", css)
        self.assertIn(".house-grid-scrollable::-webkit-scrollbar-thumb,", css)
        self.assertIn("scrollbar-width: auto;", css)
        self.assertIn("html[data-theme=\"dark\"] .house-grid-scrollable::-webkit-scrollbar-thumb:hover,", css)

    def test_dark_ready_house_card_has_one_scroll_area_and_no_light_panels(self):
        css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn(
            'html[data-theme="dark"] #houses-list.house-grid-ready > .house-plan-ready .house-top {\n'
            '  height: auto;\n'
            '  min-height: 0;\n'
            '  overflow: visible;',
            css,
        )
        self.assertIn(
            'html[data-theme="dark"] #houses-list.house-grid-ready > .house-plan-ready .house-body {\n'
            '  flex: 0 0 auto;',
            css,
        )
        self.assertIn('overflow: visible;', css[css.index(
            'html[data-theme="dark"] #houses-list.house-grid-ready > .house-plan-ready .house-body {'
        ):])
        self.assertIn(
            'html[data-theme="dark"] #houses-list.house-grid-ready {\n'
            '  grid-auto-rows: max-content;',
            css,
        )
        self.assertIn(
            'html[data-theme="dark"] #houses-list.house-grid-ready > .house-plan-ready .house-insulation-summary {\n'
            '  border-color: #394d43;\n'
            '  background: #192622;',
            css,
        )
        self.assertIn(
            'html[data-theme="dark"] #houses-list.house-grid-compact > .house-plan-pending .house-insulation-summary {\n'
            '  border-color: #394d43;\n'
            '  background: #192622;',
            css,
        )
        self.assertIn(
            'html[data-theme="dark"] #houses-list.house-grid-compact > .house-plan-pending .house-card-actions {\n'
            '  gap: 8px;',
            css,
        )
        self.assertIn(
            'html[data-theme="dark"] #houses-list.house-grid-compact > .house-plan-pending .house-card-actions .plan-ready-toggle[data-ready="true"] {\n'
            '  grid-column: 1 / -1;',
            css,
        )
        self.assertIn(
            'html[data-theme="dark"] #houses-list.house-grid-ready > .house-plan-ready .house-card-actions {',
            css,
        )
        self.assertIn(
            'html[data-theme="dark"] #houses-list.house-grid-ready > .house-plan-ready .house-workflow-steps li.current > span {',
            css,
        )
        self.assertIn(
            'html[data-theme="dark"] #houses-list.house-grid-ready > .house-plan-ready .house-card-actions .seai-activate {',
            css,
        )
        index_html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertRegex(index_html, r'href="/app\.css\?v=[^"]+"')
        self.assertRegex(index_html, r'src="/app\.js\?v=[^"]+"')

    def test_active_house_cards_have_equal_height_and_scroll_overflow(self):
        css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
        active_card_layout = css[css.index(
            "#houses-list.house-grid-active,\n.house-grid#houses-list.house-grid-closed {"
        ):]
        active_card_layout = active_card_layout[:active_card_layout.index("\n}")]
        active_card = css[css.index(
            "#houses-list.house-grid-active > .house-active,\n"
            ".house-grid#houses-list.house-grid-closed > .house-finished {"
        ):]
        active_card = active_card[:active_card.index("\n}")]
        active_top = css[css.index(
            "#houses-list.house-grid-active > .house-active .house-top,\n"
            ".house-grid#houses-list.house-grid-closed > .house-finished .house-top {"
        ):]
        active_top = active_top[:active_top.index("\n}")]
        active_body = css[css.index(
            "#houses-list.house-grid-active > .house-active .house-body,\n"
            ".house-grid#houses-list.house-grid-closed > .house-finished .house-body {"
        ):]
        active_body = active_body[:active_body.index("\n}")]
        active_actions = css[
            css.index(
                "#houses-list.house-grid-active > .house-active .house-card-actions,\n"
                ".house-grid#houses-list.house-grid-closed > .house-finished .house-card-actions {"
            ):
        ]
        active_actions = active_actions[:active_actions.index("\n}")]
        self.assertIn("grid-auto-rows: auto;", active_card_layout)
        self.assertIn("align-items: stretch;", active_card_layout)
        self.assertIn("height: auto;", active_card)
        self.assertIn("max-height: 150px;", active_top)
        self.assertIn("overflow-y: auto;", active_top)
        self.assertIn("scrollbar-width: thin;", active_top)
        self.assertIn("overflow-y: auto;", active_body)
        self.assertIn("max-height: 260px;", active_body)
        self.assertIn("flex: 0 1 auto;", active_body)
        self.assertIn("margin-top: 7px;", active_actions)
        self.assertIn("max-height: none;", active_actions)
        self.assertIn("overflow: visible;", active_actions)
        self.assertIn(
            "#houses-list:not(.house-grid-ready):not(.house-grid-compact) > .house-active .house-map-open {\n  grid-column: 1 / -1;",
            css,
        )
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
        self.assertNotIn("noteHtml(h.notes)", not_started_html)
        self.assertNotIn("Pre-start checklist", not_started_html)
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
        self.assertIn('data-map-stage-filter="not_started"', legend)
        self.assertIn('data-map-stage-filter="closed"', legend)
        self.assertIn("Closed", legend)
        self.assertIn("Ready for start", legend)
        self.assertIn("Active · Stage 1", legend)
        self.assertIn("Active · Stage 2", legend)
        self.assertIn('role="group" aria-label="Filter map pins by house stage"', legend)
        self.assertIn('if (tile.dataset.houseView === "")', APP_JS)
        self.assertIn('const status = state.user?.role === "assign" ? "all" : "map"', APP_JS)
        self.assertIn('request(`/api/calculator/houses?status=${status}`)', APP_JS)
        self.assertIn('request(`/api/calculator/houses?status=${mode}`)', APP_JS)
        self.assertIn(".house-map-layout { grid-template-columns: minmax(0, 1.8fr) minmax(310px, .82fr);", css)
        self.assertIn("scroll-snap-type: none", css)
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
        eircode_helper = map_code[
            map_code.index("function googleMapsEircodeUrl"):
            map_code.index("function projectMapPoint")
        ]
        eircode_result = subprocess.run(
            [
                "node",
                "-e",
                eircode_helper
                + '\nconsole.log(JSON.stringify(['
                + 'googleMapsEircodeUrl({postcode:"K34 ET85",address:"32 Shenick Avenue, Skerries"}),'
                + 'googleMapsEircodeUrl({postcode:"",address:"32 Shenick Avenue, Skerries"})'
                + "]));",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(eircode_result.returncode, 0, eircode_result.stderr)
        self.assertEqual(
            json.loads(eircode_result.stdout),
            [
                "https://www.google.com/maps/search/?api=1&query=K34%20ET85",
                "",
            ],
        )
        map_selection = APP_JS[
            APP_JS.index("function selectMapHouse"):
            APP_JS.index("function openHouseMap")
        ]
        self.assertIn("house-map-eircode-link", map_selection)
        self.assertIn('target="_blank" rel="noopener noreferrer"', map_selection)
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
        self.assertIn('house.status === "not_started" ? "N" : "R"', APP_JS)
        self.assertIn("mapHouseActions(displayHouse)", APP_JS)
        self.assertIn("https://tile.openstreetmap.org/", APP_JS)
        self.assertIn("map-status-active[data-stage=\"2\"]", css)
        self.assertIn(".house-map-selection .house-card-actions .seai-activate", css)
        self.assertIn(".house-map-selection .house-card-actions .map-stage-caption", css)
        self.assertIn('.house-map-selection .house-card-actions .stage-btn[data-stage="2"].on', css)
        self.assertIn('class="map-stage-caption">WORK STAGE</span>', APP_JS)
        self.assertIn(".house-map-list-item:focus-visible", css)
        self.assertIn(".house-map-empty strong", css)
        self.assertIn("The map has no saved coordinates", map_code)
        self.assertIn(".house-map-marker-approximate", css)
        self.assertIn(".house-map-approximate", css)
        self.assertIn("house-map-sidebar-heading", html)
        self.assertIn("mappedCount", APP_JS)
        self.assertIn(".house-map-summary", css)
        self.assertIn("Google receives only the Eircode when you click that link.", html)

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
