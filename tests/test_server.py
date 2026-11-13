import http.client
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from threading import Event, Lock
from unittest.mock import patch

from openpyxl import Workbook

import server
import seed_demo
from tests.support import UvicornTestServer


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = server.DB_PATH
        server.DB_PATH = Path(self.temp_dir.name) / "tracker.sqlite3"
        server.initialize_db()

    def tearDown(self):
        server.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_cloudflare_https_forwarding_marks_session_cookie_secure_only_when_enabled(self):
        context = server.FastAPIRequestContext(
            "POST", "/", {"x-forwarded-proto": "https"}, b"", None
        )
        cookie = dict(context._session_headers("token"))["Set-Cookie"]
        self.assertNotIn("Secure", cookie)

        with patch.object(server, "TRUST_CLOUDFLARE_PROXY", True):
            cookie = dict(context._session_headers("token"))["Set-Cookie"]
            self.assertIn("Secure", cookie)

            local_context = server.FastAPIRequestContext(
                "POST", "/", {"x-forwarded-proto": "http"}, b"", None
            )
            cookie = dict(local_context._session_headers("token"))["Set-Cookie"]
            self.assertNotIn("Secure", cookie)

    def test_vent_team_does_not_see_assignment_from_before_reset(self):
        with server.connect_db() as db:
            teamlead_id = server._insert_user(
                db,
                "vent-reset-teamlead",
                "Vent Reset Teamlead",
                "234567",
                "teamlead",
                team_name="Ventilation",
            )
            db.executemany(
                """
                INSERT INTO provided_houses
                    (id, owner_name, plan_vent_required, plan_vent_notes)
                VALUES (?, ?, 1, 'Vent work details')
                """,
                [
                    ("VENT-OLD-CYCLE", "Old cycle",),
                    ("VENT-NEW-CYCLE", "New cycle",),
                ],
            )
            db.executemany(
                """
                INSERT INTO house_status_history
                    (house_id, status, changed_by, changed_at)
                VALUES (?, 'not_started', 'Assign', '2026-11-05T10:00:00')
                """,
                [("VENT-OLD-CYCLE",), ("VENT-NEW-CYCLE",)],
            )
            db.executemany(
                """
                INSERT INTO house_vent_assignments
                    (house_id, teamlead_user_id, assigned_by, assigned_at)
                VALUES (?, ?, 'Vent Coordinator', ?)
                """,
                [
                    ("VENT-OLD-CYCLE", teamlead_id, "2026-11-05T09:00:00"),
                    ("VENT-NEW-CYCLE", teamlead_id, "2026-11-05T11:00:00"),
                ],
            )

            houses = server._ventilation_house_list(db, teamlead_id)

        self.assertEqual([house["id"] for house in houses], ["VENT-NEW-CYCLE"])

    def test_vent_team_sees_selected_annotated_seai_copy_for_house(self):
        with server.connect_db() as db:
            with db:
                teamlead_id = server._insert_user(
                    db,
                    "vent-selected-pdf",
                    "Vent Selected PDF",
                    "234567",
                    "teamlead",
                    team_name="Ventilation",
                )
                db.execute(
                    """
                    INSERT INTO provided_houses
                        (id, owner_name, plan_vent_required, ready_for_start)
                    VALUES ('VENT-ANNOTATED-PDF', 'Vent PDF Owner', 1, 1)
                    """
                )
                db.execute(
                    """
                    INSERT INTO house_vent_assignments
                        (house_id, teamlead_user_id, assigned_by, assigned_at)
                    VALUES ('VENT-ANNOTATED-PDF', ?, 'Admin', '2026-11-10T12:00:00')
                    """,
                    (teamlead_id,),
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
                cursor = db.execute(
                    """
                    INSERT INTO seai_documents
                        (application_id, category, filename, content)
                    VALUES ('SEAI-APP-VENT-ANNOTATED', 'Survey',
                            'survey-annotated.pdf', x'255044462d312e34')
                    """
                )
                db.execute(
                    """
                    INSERT INTO selected_seai_documents
                        (document_id, house_id, created_by, created_at)
                    VALUES (?, 'VENT-ANNOTATED-PDF', 'Surveyor', '2026-11-10T12:01:00')
                    """,
                    (cursor.lastrowid,),
                )

            houses = server._ventilation_house_list(db, teamlead_id)

        self.assertEqual(len(houses), 1)
        self.assertEqual(
            [document["filename"] for document in houses[0]["survey_documents"]],
            ["survey-annotated.pdf"],
        )

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

    def test_role_catalog_and_permission_tables_are_initialized_idempotently(self):
        expected_roles = {
            "admin": "Admin",
            "assign": "Assign",
            "boss": "Boss",
            "surveyor": "Surveyor",
            "warehouse": "Warehouse",
            "delivery": "Delivery",
            "supervisor": "Supervisor",
            "teamlead": "Teamlead",
            "plumber": "Plumber",
            "plumbing_coordinator": "Plumbing Coordinator",
            "ventilation_coordinator": "Vent Coordinator",
            "employee": "Employee",
        }
        with server.connect_db() as db:
            roles = {
                row["role_key"]: row["display_name"]
                for row in db.execute(
                    "SELECT role_key, display_name FROM app_roles"
                )
            }
            self.assertEqual(roles, expected_roles)
            permissions = {
                row["permission_key"]: row["description"]
                for row in db.execute(
                    "SELECT permission_key, description FROM app_permissions"
                )
            }
            self.assertEqual(
                set(permissions),
                {
                    "*",
                    "access.admin",
                    "access.assign",
                    "access.boss",
                    "access.surveyor",
                    "access.warehouse",
                    "access.delivery",
                    "access.supervisor",
                    "access.teamlead",
                    "access.plumber",
                    "access.plumbing_coordinator",
                    "access.ventilation_coordinator",
                    "access.vent_photos",
                    "access.employee",
                },
            )
            self.assertEqual(
                {
                    (row["role_key"], row["permission_key"], row["is_allowed"])
                    for row in db.execute(
                        "SELECT role_key, permission_key, is_allowed "
                        "FROM app_role_permissions"
                    )
                },
                {
                    ("admin", "*", 1),
                    *(
                        (role, f"access.{role}", 1)
                        for role in expected_roles
                        if role != "admin"
                    ),
                },
            )

        server.initialize_db()

        with server.connect_db() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM app_roles").fetchone()[0],
                len(expected_roles),
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM app_role_permissions").fetchone()[0],
                len(expected_roles),
            )
            self.assertEqual(
                db.execute("PRAGMA foreign_key_check").fetchall(),
                [],
            )

    def test_admin_can_create_accounts_for_every_configured_role(self):
        roles = (
            "admin",
            "assign",
            "boss",
            "surveyor",
            "warehouse",
            "delivery",
            "supervisor",
            "teamlead",
            "plumber",
            "plumbing_coordinator",
            "ventilation_coordinator",
        )
        httpd, thread = self.start_test_server()
        try:
            status, admin, admin_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/setup",
                {
                    "username": "role-admin",
                    "display_name": "Role Admin",
                    "pin": "123456",
                },
            )
            self.assertEqual(status, 201)

            for role in roles:
                status, result, _ = self.http_request(
                    httpd,
                    "POST",
                    "/api/users",
                    {
                        "username": f"user-{role}",
                        "display_name": f"{role.title()} User",
                        "pin": "234567",
                        "role": role,
                    },
                    admin_cookie,
                )
                self.assertEqual(status, 201, role)
                self.assertEqual(result["user"]["role"], role)

            with server.connect_db() as db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM app_users").fetchone()[0],
                    len(roles) + 1,
                )
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_ventilation_coordinator_assigns_required_work_to_ventilation_teamlead(self):
        httpd, thread = self.start_test_server()
        try:
            status, admin, admin_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/setup",
                {
                    "username": "vent-role-admin",
                    "display_name": "Vent Role Admin",
                    "pin": "123456",
                },
            )
            self.assertEqual(status, 201)
            with server.connect_db() as db:
                vent_department_id = db.execute(
                    "SELECT id FROM departments WHERE name = 'Ventilation'"
                ).fetchone()["id"]
                db.execute(
                    """
                    INSERT INTO provided_houses
                        (id, owner_name, ready_for_start,
                         plan_vent_required, plan_vent_notes)
                    VALUES ('VENT-COORDINATOR-1', 'Vent House', 1, 1, 'Fit vents')
                    """
                )
                db.execute(
                    """
                    INSERT INTO provided_houses (id, owner_name, plan_vent_required)
                    VALUES ('VENT-COORDINATOR-2', 'Other House', 0)
                    """
                )
                db.execute(
                    """
                    INSERT INTO provided_houses (id, owner_name, ready_for_start)
                    VALUES ('VENT-COORDINATOR-3', 'Unsurveyed House', 1)
                    """
                )
                db.execute(
                    """
                    INSERT INTO house_surveys
                        (house_id, data, notes, updated_by, updated_at)
                    VALUES ('VENT-COORDINATOR-1', '{"perimeter_m": 120}',
                            'Survey notes', 'Vent Role Admin',
                            '2026-11-05T09:30:00')
                    """
                )
                db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS seai_applications (
                        application_id TEXT PRIMARY KEY,
                        detail_data_json TEXT NOT NULL
                    )
                    """
                )
                db.execute(
                    """
                    INSERT INTO seai_applications (application_id, detail_data_json)
                    VALUES (
                        'VENT-COORDINATOR-1',
                        '{"Measures":[{"Name":"Attic (Loft) Insulation","Survey Quantity":"120"},{"Name":"Cavity Wall Insulation","Survey Quantity":"85"}]}'
                    )
                    """
                )
                db.execute(
                    """
                    INSERT INTO seai_applications (application_id, detail_data_json)
                    VALUES (
                        'VENT-COORDINATOR-3',
                        '{"Measures":[{"Name":"Attic (Loft) Insulation","Survey Quantity":"45"}]}'
                    )
                    """
                )
                db.execute(
                    """
                    INSERT INTO house_role_assignments
                        (house_id, role, user_id, assigned_by, assigned_at)
                    SELECT 'VENT-COORDINATOR-1', 'surveyor', id, 'Admin',
                           '2026-11-05T09:00:00'
                    FROM app_users WHERE username = 'vent-role-admin'
                    """
                )
            status, coordinator, _ = self.http_request(
                httpd,
                "POST",
                "/api/users",
                {
                    "username": "yvonne.oregan",
                    "display_name": "Yvonne O’Reagan",
                    "pin": "123456",
                    "role": "ventilation_coordinator",
                    "department_ids": [vent_department_id],
                },
                admin_cookie,
            )
            self.assertEqual(status, 201)
            status, cavity_teamlead, _ = self.http_request(
                httpd,
                "POST",
                "/api/users",
                {
                    "username": "cavity-teamlead",
                    "display_name": "Cavity Teamlead",
                    "pin": "345678",
                    "role": "teamlead",
                    "team_name": "Cavity",
                    "department_ids": [vent_department_id],
                },
                admin_cookie,
            )
            self.assertEqual(status, 201)
            status, attic_teamlead, _ = self.http_request(
                httpd,
                "POST",
                "/api/users",
                {
                    "username": "attic-teamlead",
                    "display_name": "Attic Teamlead",
                    "pin": "456789",
                    "role": "teamlead",
                    "team_name": "Attic",
                    "department_ids": [vent_department_id],
                },
                admin_cookie,
            )
            self.assertEqual(status, 201)
            with server.connect_db() as db:
                self.assertTrue(
                    server._role_has_access(db, "ventilation_coordinator", "ventilation_coordinator")
                )
            status, teamlead, _ = self.http_request(
                httpd,
                "POST",
                "/api/users",
                {
                    "username": "vent-teamlead",
                    "display_name": "Vent Teamlead",
                    "pin": "234567",
                    "role": "teamlead",
                    "team_name": "Ventilation",
                    "department_ids": [vent_department_id],
                },
                admin_cookie,
            )
            self.assertEqual(status, 201)
            status, _, coordinator_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/login",
                {"username": "yvonne.oregan", "pin": "123456"},
            )
            self.assertEqual(status, 200)
            status, dashboard, _ = self.http_request(
                httpd, "GET", "/api/dashboard", cookie=coordinator_cookie
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                [house["id"] for house in dashboard["ventilation_coordinator_houses"]],
                ["VENT-COORDINATOR-1"],
            )
            self.assertEqual(
                dashboard["ventilation_coordinator_houses"][0]["surveyor_name"],
                "Vent Role Admin",
            )
            self.assertIn(
                teamlead["user"]["id"],
                [lead["id"] for lead in dashboard["ventilation_teamleads"]],
            )
            ready_house = dashboard["ventilation_coordinator_houses"][0]
            self.assertTrue(ready_house["attic_required"])
            self.assertEqual(ready_house["attic_survey_quantity"], "120")
            self.assertTrue(ready_house["cavity_required"])
            self.assertEqual(ready_house["cavity_survey_quantity"], "85")
            self.assertEqual(len(dashboard["insulation_teamleads"]), 3)
            for work_type, wrong_lead in (
                ("attic", cavity_teamlead),
                ("cavity", attic_teamlead),
            ):
                status, _, _ = self.http_request(
                    httpd,
                    "POST",
                    "/api/ventilation-coordinator/insulation-assignment",
                    {
                        "house_id": "VENT-COORDINATOR-1",
                        "work_type": work_type,
                        "teamlead_user_id": wrong_lead["user"]["id"],
                    },
                    coordinator_cookie,
                )
                self.assertEqual(status, 400)
            for work_type, lead in (
                ("attic", attic_teamlead),
                ("cavity", cavity_teamlead),
            ):
                status, insulation_assignment, _ = self.http_request(
                    httpd,
                    "POST",
                    "/api/ventilation-coordinator/insulation-assignment",
                    {
                        "house_id": "VENT-COORDINATOR-1",
                        "work_type": work_type,
                        "teamlead_user_id": lead["user"]["id"],
                    },
                    coordinator_cookie,
                )
                self.assertEqual(status, 200)
                self.assertEqual(
                    insulation_assignment["teamlead_user_id"], lead["user"]["id"]
                )
            status, _, teamlead_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/login",
                {"username": "attic-teamlead", "pin": "456789"},
            )
            self.assertEqual(status, 200)
            status, teamlead_dashboard, _ = self.http_request(
                httpd, "GET", "/api/dashboard", cookie=teamlead_cookie
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                [(job["id"], job["work_type"]) for job in teamlead_dashboard["teamlead_insulation_houses"]],
                [("VENT-COORDINATOR-1", "attic")],
            )
            status, completion, _ = self.http_request(
                httpd,
                "POST",
                "/api/teamlead/insulation-work/complete",
                {"house_id": "VENT-COORDINATOR-1", "work_type": "attic"},
                teamlead_cookie,
            )
            self.assertEqual(status, 200)
            self.assertEqual(completion["completed_by"], "Attic Teamlead")
            status, _, cavity_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/login",
                {"username": "cavity-teamlead", "pin": "345678"},
            )
            self.assertEqual(status, 200)
            status, cavity_dashboard, _ = self.http_request(
                httpd, "GET", "/api/dashboard", cookie=cavity_cookie
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                [(job["id"], job["work_type"]) for job in cavity_dashboard["teamlead_insulation_houses"]],
                [("VENT-COORDINATOR-1", "cavity")],
            )
            status, house_detail, _ = self.http_request(
                httpd,
                "GET",
                "/api/houses/VENT-COORDINATOR-1",
                cookie=coordinator_cookie,
            )
            self.assertEqual(status, 200)
            self.assertTrue(house_detail["house"]["coordinator_view_only"])
            self.assertFalse(house_detail["house"]["photo_view_only"])
            self.assertEqual(
                house_detail["house"]["survey_data"]["perimeter_m"], 120
            )
            self.assertEqual(house_detail["house"]["survey_notes"], "Survey notes")
            self.assertTrue(house_detail["house"]["attic_required"])
            status, assignment, _ = self.http_request(
                httpd,
                "POST",
                "/api/ventilation-coordinator/assignment",
                {
                    "house_id": "VENT-COORDINATOR-1",
                    "teamlead_user_id": teamlead["user"]["id"],
                },
                coordinator_cookie,
            )
            self.assertEqual(status, 200)
            self.assertEqual(assignment["teamlead_name"], "Vent Teamlead")
            status, dashboard, _ = self.http_request(
                httpd, "GET", "/api/dashboard", cookie=coordinator_cookie
            )
            self.assertEqual(status, 200)
            assigned_house = dashboard["ventilation_coordinator_houses"][0]
            self.assertEqual(
                assigned_house["assignment_assigned_by"], "Yvonne O’Reagan"
            )
            self.assertTrue(assigned_house["assignment_assigned_at"])
            self.assertEqual(assigned_house["surveyor_name"], "Vent Role Admin")
            status, house_detail, _ = self.http_request(
                httpd,
                "GET",
                "/api/houses/VENT-COORDINATOR-1",
                cookie=coordinator_cookie,
            )
            self.assertEqual(status, 200)
            self.assertTrue(house_detail["house"]["coordinator_view_only"])
            self.assertTrue(house_detail["house"]["can_view_vent_photos"])
            self.assertEqual(house_detail["house"]["id"], "VENT-COORDINATOR-1")
            hidden_detail_status, _, _ = self.http_request(
                httpd,
                "GET",
                "/api/houses/VENT-COORDINATOR-2",
                cookie=coordinator_cookie,
            )
            self.assertEqual(hidden_detail_status, 403)
            status, _, teamlead_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/login",
                {"username": "vent-teamlead", "pin": "234567"},
            )
            self.assertEqual(status, 200)
            status, completion, _ = self.http_request(
                httpd,
                "POST",
                "/api/house-work-plan/complete",
                {
                    "house_id": "VENT-COORDINATOR-1",
                    "trade": "vent",
                    "done": True,
                },
                teamlead_cookie,
            )
            self.assertEqual(status, 200)
            self.assertEqual(completion["done_by"], "Vent Teamlead")
            self.assertTrue(completion["done_at"])
            status, dashboard, _ = self.http_request(
                httpd, "GET", "/api/dashboard", cookie=coordinator_cookie
            )
            self.assertEqual(status, 200)
            completed_house = dashboard["ventilation_coordinator_houses"][0]
            self.assertEqual(completed_house["done_by"], "Vent Teamlead")
            self.assertEqual(completed_house["done_at"], completion["done_at"])
            self.assertEqual(
                completed_house["assignment_assigned_by"], "Yvonne O’Reagan"
            )
            self.assertEqual(completed_house["surveyor_name"], "Vent Role Admin")
            with server.connect_db() as db:
                saved_assignment = db.execute(
                    "SELECT teamlead_user_id, assigned_by FROM house_vent_assignments "
                    "WHERE house_id = 'VENT-COORDINATOR-1'"
                ).fetchone()
                self.assertEqual(
                    saved_assignment["teamlead_user_id"], teamlead["user"]["id"]
                )
                self.assertEqual(saved_assignment["assigned_by"], "Yvonne O’Reagan")
            status, error, _ = self.http_request(
                httpd,
                "POST",
                "/api/ventilation-coordinator/assignment",
                {
                    "house_id": "VENT-COORDINATOR-2",
                    "teamlead_user_id": teamlead["user"]["id"],
                },
                coordinator_cookie,
            )
            self.assertEqual(status, 409)
            status, error, _ = self.http_request(
                httpd,
                "GET",
                "/api/users",
                cookie=coordinator_cookie,
            )
            self.assertEqual(status, 403)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_admin_manages_departments_custom_roles_permissions_and_reporting_lines(self):
        httpd, thread = self.start_test_server()
        try:
            status, admin, admin_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/setup",
                {
                    "username": "organization-admin",
                    "display_name": "Organization Admin",
                    "pin": "123456",
                },
            )
            self.assertEqual(status, 201)
            status, result, _ = self.http_request(
                httpd,
                "GET",
                "/api/users",
                cookie=admin_cookie,
            )
            self.assertEqual(status, 200)
            departments = {
                department["name"]: department["id"]
                for department in result["departments"]
            }
            status, cathy, _ = self.http_request(
                httpd,
                "POST",
                "/api/users",
                {
                    "username": "cathy-assign",
                    "display_name": "Cathy",
                    "pin": "234567",
                    "role": "assign",
                    "department_ids": [departments["Administration"]],
                    "manager_user_id": admin["user"]["id"],
                },
                admin_cookie,
            )
            self.assertEqual(status, 201)

            status, result, _ = self.http_request(
                httpd,
                "POST",
                "/api/departments",
                {"name": "Quality Assurance"},
                admin_cookie,
            )
            self.assertEqual(status, 201)
            self.assertEqual(result["department"]["name"], "Quality Assurance")

            status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/roles",
                {
                    "role_key": "quality_checker",
                    "display_name": "Quality Checker",
                },
                admin_cookie,
            )
            self.assertEqual(status, 201)
            status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/roles/quality_checker/permissions",
                {"permissions": ["access.surveyor"]},
                admin_cookie,
            )
            self.assertEqual(status, 200)

            status, karol, _ = self.http_request(
                httpd,
                "POST",
                "/api/users",
                {
                    "username": "karol-supervisor",
                    "display_name": "Karol",
                    "pin": "234567",
                    "role": "supervisor",
                    "supervisor_specialty": "External Wall Insulation",
                    "department_ids": [departments["External Wall Insulation"]],
                },
                admin_cookie,
            )
            self.assertEqual(status, 201)
            status, lead, _ = self.http_request(
                httpd,
                "POST",
                "/api/users",
                {
                    "username": "ewi-teamlead",
                    "display_name": "EWI Teamlead",
                    "pin": "345678",
                    "role": "teamlead",
                    "team_name": "EWI Crew",
                    "department_ids": [departments["External Wall Insulation"]],
                    "manager_user_id": karol["user"]["id"],
                },
                admin_cookie,
            )
            self.assertEqual(status, 201)
            status, error, _ = self.http_request(
                httpd,
                "POST",
                f"/api/users/{cathy['user']['id']}/organization",
                {
                    "department_ids": [departments["Administration"]],
                    "manager_user_id": lead["user"]["id"],
                },
                admin_cookie,
            )
            self.assertEqual(status, 409)
            self.assertIn("Assign accounts can report only to an Admin", error["error"])
            status, quality_user, _ = self.http_request(
                httpd,
                "POST",
                "/api/users",
                {
                    "username": "quality-checker",
                    "display_name": "Quality Checker",
                    "pin": "456789",
                    "role": "quality_checker",
                    "department_ids": [departments["Surveyors"]],
                },
                admin_cookie,
            )
            self.assertEqual(status, 201)

            status, user_list, _ = self.http_request(
                httpd,
                "GET",
                "/api/users",
                cookie=admin_cookie,
            )
            self.assertEqual(status, 200)
            saved_lead = next(
                user for user in user_list["users"]
                if user["id"] == lead["user"]["id"]
            )
            saved_cathy = next(
                user for user in user_list["users"]
                if user["id"] == cathy["user"]["id"]
            )
            self.assertEqual(saved_cathy["manager_user_id"], admin["user"]["id"])
            self.assertEqual(saved_lead["manager_user_id"], karol["user"]["id"])
            self.assertEqual(
                [item["name"] for item in saved_lead["departments"]],
                ["External Wall Insulation"],
            )

            status, member, _ = self.http_request(
                httpd,
                "POST",
                "/api/users",
                {
                    "username": "ewi-member",
                    "display_name": "EWI Member",
                    "pin": "678901",
                    "role": "employee",
                    "department_ids": [departments["External Wall Insulation"]],
                    "manager_user_id": lead["user"]["id"],
                },
                admin_cookie,
            )
            self.assertEqual(status, 201)
            status, members, _ = self.http_request(
                httpd,
                "GET",
                "/api/users",
                cookie=admin_cookie,
            )
            self.assertEqual(status, 200)
            saved_member = next(
                user for user in members["users"]
                if user["id"] == member["user"]["id"]
            )
            self.assertEqual(saved_member["manager_user_id"], lead["user"]["id"])

            status, error, _ = self.http_request(
                httpd,
                "POST",
                "/api/users",
                {
                    "username": "unmanaged-member",
                    "display_name": "Unmanaged Member",
                    "pin": "789012",
                    "role": "employee",
                    "department_ids": [departments["External Wall Insulation"]],
                    "manager_user_id": karol["user"]["id"],
                },
                admin_cookie,
            )
            self.assertEqual(status, 409)
            self.assertIn("Team members can report only to a Teamlead", error["error"])

            status, error, _ = self.http_request(
                httpd,
                "POST",
                f"/api/users/{quality_user['user']['id']}/organization",
                {
                    "department_ids": [departments["Surveyors"]],
                    "manager_user_id": lead["user"]["id"],
                },
                admin_cookie,
            )
            self.assertEqual(status, 409)

            status, _, _ = self.http_request(
                httpd,
                "POST",
                f"/api/users/{karol['user']['id']}/organization",
                {
                    "department_ids": [departments["External Wall Insulation"]],
                    "manager_user_id": lead["user"]["id"],
                },
                admin_cookie,
            )
            self.assertEqual(status, 409)

            status, logged_in, quality_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/login",
                {"username": "quality-checker", "pin": "456789"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(logged_in["user"]["permissions"], ["access.surveyor"])
            self.assertEqual(
                [item["name"] for item in logged_in["user"]["departments"]],
                ["Surveyors"],
            )
            status, _, _ = self.http_request(
                httpd,
                "GET",
                "/api/calculator/houses",
                cookie=quality_cookie,
            )
            self.assertEqual(status, 200)
            status, error, _ = self.http_request(
                httpd,
                "POST",
                "/api/users",
                {
                    "username": "not-admin",
                    "display_name": "Not Admin",
                    "pin": "567890",
                },
                quality_cookie,
            )
            self.assertEqual(status, 403)
            self.assertIn("Surveyor access", error["error"])

            status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/roles/quality_checker/permissions",
                {"permissions": []},
                admin_cookie,
            )
            self.assertEqual(status, 200)
            status, error, _ = self.http_request(
                httpd,
                "GET",
                "/api/updates",
                cookie=quality_cookie,
            )
            self.assertEqual(status, 403)
            self.assertIn("no active access permissions", error["error"])
            self.assertEqual(quality_user["user"]["role"], "quality_checker")
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_supervisor_and_teamlead_manage_only_their_rosters(self):
        httpd, thread = self.start_test_server()
        try:
            status, _, admin_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/setup",
                {
                    "username": "roster-scope-admin",
                    "display_name": "Roster Scope Admin",
                    "pin": "123456",
                },
            )
            self.assertEqual(status, 201)
            status, account_data, _ = self.http_request(
                httpd,
                "GET",
                "/api/users",
                cookie=admin_cookie,
            )
            self.assertEqual(status, 200)
            ewi_id = next(
                department["id"]
                for department in account_data["departments"]
                if department["name"] == "External Wall Insulation"
            )

            status, supervisor, _ = self.http_request(
                httpd,
                "POST",
                "/api/users",
                {
                    "username": "roster-supervisor",
                    "display_name": "EWI Supervisor",
                    "pin": "234567",
                    "role": "supervisor",
                    "supervisor_specialty": "External Wall Insulation",
                    "department_ids": [ewi_id],
                },
                admin_cookie,
            )
            self.assertEqual(status, 201)
            status, lead_one, _ = self.http_request(
                httpd,
                "POST",
                "/api/users",
                {
                    "username": "lead-one",
                    "display_name": "Lead One",
                    "pin": "345678",
                    "role": "teamlead",
                    "department_ids": [ewi_id],
                    "manager_user_id": supervisor["user"]["id"],
                },
                admin_cookie,
            )
            self.assertEqual(status, 201)
            status, lead_two, _ = self.http_request(
                httpd,
                "POST",
                "/api/users",
                {
                    "username": "lead-two",
                    "display_name": "Lead Two",
                    "pin": "456789",
                    "role": "teamlead",
                    "department_ids": [ewi_id],
                },
                admin_cookie,
            )
            self.assertEqual(status, 201)

            status, _, supervisor_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/login",
                {"username": "roster-supervisor", "pin": "234567"},
            )
            self.assertEqual(status, 200)
            status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/team-roster",
                {
                    "teamlead_id": lead_one["user"]["id"],
                    "member_name": "Lead One Worker",
                },
                supervisor_cookie,
            )
            self.assertEqual(status, 201)
            status, error, _ = self.http_request(
                httpd,
                "POST",
                "/api/team-roster",
                {
                    "teamlead_id": lead_two["user"]["id"],
                    "member_name": "Unauthorized Worker",
                },
                supervisor_cookie,
            )
            self.assertEqual(status, 403)
            self.assertIn("only your own roster", error["error"])
            status, supervisor_dashboard, _ = self.http_request(
                httpd,
                "GET",
                "/api/dashboard",
                cookie=supervisor_cookie,
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                [roster["teamlead_id"] for roster in supervisor_dashboard["team_rosters"]],
                [lead_one["user"]["id"]],
            )

            status, _, lead_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/login",
                {"username": "lead-one", "pin": "345678"},
            )
            self.assertEqual(status, 200)
            status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/team-roster",
                {
                    "teamlead_id": lead_one["user"]["id"],
                    "member_name": "Self Managed Worker",
                },
                lead_cookie,
            )
            self.assertEqual(status, 201)
            status, error, _ = self.http_request(
                httpd,
                "POST",
                "/api/team-roster",
                {
                    "teamlead_id": lead_two["user"]["id"],
                    "member_name": "Other Team Worker",
                },
                lead_cookie,
            )
            self.assertEqual(status, 403)
            self.assertIn("only your own roster", error["error"])
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_ewi_assignment_queue_is_scoped_and_teamlead_can_complete_work(self):
        httpd, thread = self.start_test_server()
        try:
            status, _, admin_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/setup",
                {
                    "username": "ewi-queue-admin",
                    "display_name": "EWI Queue Admin",
                    "pin": "123456",
                },
            )
            self.assertEqual(status, 201)
            status, accounts, _ = self.http_request(
                httpd, "GET", "/api/users", cookie=admin_cookie
            )
            self.assertEqual(status, 200)
            ewi_id = next(
                department["id"]
                for department in accounts["departments"]
                if department["name"] == "External Wall Insulation"
            )

            def create_user(username, name, pin, role, manager_id=None):
                payload = {
                    "username": username,
                    "display_name": name,
                    "pin": pin,
                    "role": role,
                    "department_ids": [ewi_id],
                }
                if role == "supervisor":
                    payload["supervisor_specialty"] = "External Wall Insulation"
                if manager_id is not None:
                    payload["manager_user_id"] = manager_id
                status, result, _ = self.http_request(
                    httpd, "POST", "/api/users", payload, admin_cookie
                )
                self.assertEqual(status, 201)
                return result["user"]["id"]

            supervisor_id = create_user(
                "ewi-queue-supervisor",
                "EWI Queue Supervisor",
                "234567",
                "supervisor",
            )
            own_teamlead_id = create_user(
                "ewi-queue-lead",
                "EWI Queue Lead",
                "345678",
                "teamlead",
                supervisor_id,
            )
            other_supervisor_id = create_user(
                "ewi-queue-other-supervisor",
                "Other EWI Supervisor",
                "456789",
                "supervisor",
            )
            other_teamlead_id = create_user(
                "ewi-queue-other-lead",
                "Other EWI Lead",
                "567890",
                "teamlead",
                other_supervisor_id,
            )

            with server.connect_db() as db:
                with db:
                    for house_id, owner in (
                        ("EWI-QUEUE-OWN", supervisor_id),
                        ("EWI-QUEUE-OTHER", other_supervisor_id),
                    ):
                        db.execute(
                            """
                            INSERT INTO provided_houses(id, owner_name, source)
                            VALUES (?, ?, 'government')
                            """,
                            (house_id, house_id),
                        )
                        server._upsert_house(db, house_id, owner_name=house_id)
                        db.execute(
                            """
                            INSERT INTO house_role_assignments
                                (house_id, role, user_id, assigned_by, assigned_at)
                            VALUES (?, 'supervisor', ?, 'Cathy', '2026-11-09T09:00:00')
                            """,
                            (house_id, owner),
                        )
                    db.execute(
                        """
                        INSERT INTO house_ewi_assignments
                            (house_id, teamlead_user_id, supervisor_user_id,
                             assigned_by, assigned_at)
                        VALUES (?, ?, ?, 'Other EWI Supervisor', '2026-11-09T09:01:00')
                        """,
                        ("EWI-QUEUE-OTHER", other_teamlead_id, other_supervisor_id),
                    )

            status, _, supervisor_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/login",
                {"username": "ewi-queue-supervisor", "pin": "234567"},
            )
            self.assertEqual(status, 200)
            status, assignment, _ = self.http_request(
                httpd,
                "POST",
                "/api/supervisor/ewi-assignment",
                {
                    "house_id": "EWI-QUEUE-OWN",
                    "teamlead_user_id": own_teamlead_id,
                },
                supervisor_cookie,
            )
            self.assertEqual(status, 200)
            self.assertEqual(assignment["teamlead_name"], "EWI Queue Lead")
            status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/supervisor/ewi-assignment",
                {
                    "house_id": "EWI-QUEUE-OWN",
                    "teamlead_user_id": other_teamlead_id,
                },
                supervisor_cookie,
            )
            self.assertEqual(status, 400)

            status, error, _ = self.http_request(
                httpd,
                "POST",
                "/api/supervisor/ewi-assignment",
                {
                    "house_id": "EWI-QUEUE-OTHER",
                    "teamlead_user_id": own_teamlead_id,
                },
                supervisor_cookie,
            )
            self.assertEqual(status, 404)
            status, dashboard, _ = self.http_request(
                httpd, "GET", "/api/dashboard", cookie=supervisor_cookie
            )
            self.assertEqual(status, 200)
            own_house = next(
                house for house in dashboard["house_list"]
                if house["id"] == "EWI-QUEUE-OWN"
            )
            self.assertEqual(own_house["ewi_teamlead_user_id"], own_teamlead_id)
            self.assertEqual(dashboard["supervisor_assignment_options"]["ewi_teamleads"], [
                {"id": own_teamlead_id, "name": "EWI Queue Lead"}
            ])
            self.assertIn(
                "EWI Queue Lead",
                dashboard["supervisor_assignment_options"]["delivery_people"],
            )

            status, _, lead_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/login",
                {"username": "ewi-queue-lead", "pin": "345678"},
            )
            self.assertEqual(status, 200)
            status, dashboard, _ = self.http_request(
                httpd, "GET", "/api/dashboard", cookie=lead_cookie
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                [house["id"] for house in dashboard["teamlead_ewi_houses"]],
                ["EWI-QUEUE-OWN"],
            )
            status, completed, _ = self.http_request(
                httpd,
                "POST",
                "/api/teamlead/ewi-work/complete",
                {"house_id": "EWI-QUEUE-OWN"},
                lead_cookie,
            )
            self.assertEqual(status, 200)
            self.assertEqual(completed["completed_by"], "EWI Queue Lead")
            with server.connect_db() as db:
                self.assertEqual(
                    db.execute(
                        "SELECT is_finished FROM houses WHERE id = 'EWI-QUEUE-OWN'"
                    ).fetchone()["is_finished"],
                    0,
                )
                self.assertEqual(
                    db.execute(
                        "SELECT completed_by FROM house_ewi_assignments "
                        "WHERE house_id = 'EWI-QUEUE-OWN'"
                    ).fetchone()["completed_by"],
                    "EWI Queue Lead",
                )

            status, _, other_lead_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/login",
                {"username": "ewi-queue-other-lead", "pin": "567890"},
            )
            self.assertEqual(status, 200)
            status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/teamlead/ewi-work/complete",
                {"house_id": "EWI-QUEUE-OWN"},
                other_lead_cookie,
            )
            self.assertEqual(status, 404)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_admin_manages_non_login_team_rosters_and_teamlead_sees_own(self):
        httpd, thread = self.start_test_server()
        try:
            status, _, admin_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/setup",
                {
                    "username": "roster-admin",
                    "display_name": "Roster Admin",
                    "pin": "123456",
                },
            )
            self.assertEqual(status, 201)
            status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/users",
                {
                    "username": "kamil-teamlead",
                    "display_name": "Kamil Bulczak",
                    "pin": "123456",
                    "role": "teamlead",
                    "team_name": "Ventilation",
                },
                admin_cookie,
            )
            self.assertEqual(status, 201)
            status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/users",
                {
                    "username": "other-teamlead",
                    "display_name": "Other Teamlead",
                    "pin": "234567",
                    "role": "teamlead",
                },
                admin_cookie,
            )
            self.assertEqual(status, 201)
            status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/users",
                {
                    "username": "roster-employee",
                    "display_name": "Roster Employee",
                    "pin": "345678",
                    "role": "employee",
                },
                admin_cookie,
            )
            self.assertEqual(status, 201)

            member_ids = []
            for name in ("Grzegorz Kaminski", "Edem Ametov", "Kasper Janowski"):
                status, result, _ = self.http_request(
                    httpd,
                    "POST",
                    "/api/team-roster",
                    {"teamlead_id": 2, "member_name": name},
                    admin_cookie,
                )
                self.assertEqual(status, 201)
                self.assertEqual(result["member"]["name"], name)
                member_ids.append(result["member"]["id"])

            status, result, _ = self.http_request(
                httpd,
                "GET",
                "/api/dashboard",
                cookie=admin_cookie,
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                result["team_rosters"][0]["teamlead_name"], "Kamil Bulczak"
            )
            self.assertEqual(result["team_rosters"][0]["team_name"], "Ventilation")
            self.assertEqual(
                [member["name"] for member in result["team_rosters"][0]["members"]],
                ["Edem Ametov", "Grzegorz Kaminski", "Kasper Janowski"],
            )

            status, result, _ = self.http_request(
                httpd,
                "GET",
                "/api/users",
                cookie=admin_cookie,
            )
            self.assertEqual(status, 200)
            teamlead_user = next(
                user for user in result["users"] if user["username"] == "kamil-teamlead"
            )
            self.assertEqual(teamlead_user["team_name"], "Ventilation")

            status, result, _ = self.http_request(
                httpd,
                "POST",
                f"/api/users/{teamlead_user['id']}/team-name",
                {"team_name": "Ventilation Crew"},
                admin_cookie,
            )
            self.assertEqual(status, 200)
            self.assertEqual(result["user"]["team_name"], "Ventilation Crew")

            status, result, _ = self.http_request(
                httpd,
                "POST",
                f"/api/users/{teamlead_user['id']}/team-name",
                {"team_name": "Ventilation"},
                admin_cookie,
            )
            self.assertEqual(status, 200)

            status, result, _ = self.http_request(
                httpd,
                "POST",
                "/api/team-roster",
                {"teamlead_id": 2, "member_name": "grzegorz kaminski"},
                admin_cookie,
            )
            self.assertEqual(status, 409)

            status, _, teamlead_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/login",
                {"username": "kamil-teamlead", "pin": "123456"},
            )
            self.assertEqual(status, 200)
            status, result, _ = self.http_request(
                httpd, "GET", "/api/dashboard", cookie=teamlead_cookie
            )
            self.assertEqual(status, 200)
            self.assertEqual(len(result["team_rosters"]), 1)
            self.assertEqual(
                result["team_rosters"][0]["teamlead_name"], "Kamil Bulczak"
            )

            status, result, employee_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/login",
                {"username": "roster-employee", "pin": "345678"},
            )
            self.assertEqual(status, 200)
            status, result, _ = self.http_request(
                httpd,
                "POST",
                "/api/team-roster",
                {"teamlead_id": 2, "member_name": "Unauthorized Member"},
                employee_cookie,
            )
            self.assertEqual(status, 403)

            status, result, _ = self.http_request(
                httpd,
                "POST",
                f"/api/team-roster/{member_ids[0]}/delete",
                cookie=admin_cookie,
            )
            self.assertEqual(status, 200)
            self.assertNotIn(
                "Grzegorz Kaminski",
                [member["name"] for member in result["team_rosters"][0]["members"]],
            )
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_admin_can_save_supervisor_specialty(self):
        httpd, thread = self.start_test_server()
        try:
            status, _, admin_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/setup",
                {
                    "username": "specialty-admin",
                    "display_name": "Specialty Admin",
                    "pin": "123456",
                },
            )
            self.assertEqual(status, 201)
            status, result, _ = self.http_request(
                httpd,
                "POST",
                "/api/users",
                {
                    "username": "plumbing-supervisor",
                    "display_name": "Gerry Boland",
                    "pin": "234567",
                    "role": "supervisor",
                    "supervisor_specialty": "  Plumbing  ",
                },
                admin_cookie,
            )
            self.assertEqual(status, 201)
            self.assertEqual(
                result["user"]["supervisor_specialty"], "Plumbing"
            )
            user_id = result["user"]["id"]

            status, result, _ = self.http_request(
                httpd, "GET", "/api/users", cookie=admin_cookie
            )
            supervisor = next(
                user for user in result["users"] if user["id"] == user_id
            )
            self.assertEqual(supervisor["supervisor_specialty"], "Plumbing")

            status, result, _ = self.http_request(
                httpd,
                "POST",
                f"/api/users/{user_id}/specialty",
                {"supervisor_specialty": "External Wall Insulation"},
                admin_cookie,
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                result["user"]["supervisor_specialty"], "External Wall Insulation"
            )

            status, result, _ = self.http_request(
                httpd,
                "POST",
                f"/api/users/{user_id}/specialty",
                {"supervisor_specialty": ""},
                admin_cookie,
            )
            self.assertEqual(status, 200)
            self.assertEqual(result["user"]["supervisor_specialty"], "")
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_assign_cannot_view_material_data_or_access_material_endpoints(self):
        httpd, thread = self.start_test_server()
        try:
            status, _, admin_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/setup",
                {
                    "username": "assign-test-admin",
                    "display_name": "Assign Test Admin",
                    "pin": "123456",
                },
            )
            self.assertEqual(status, 201)
            material_id = self.add_material("Restricted Board", opening=10)
            self.add_movement(
                material_id,
                "collection",
                2,
                person="Test Person",
                team="Test Team",
            )
            with server.connect_db() as db:
                db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS seai_applications (
                        application_id TEXT PRIMARY KEY,
                        detail_data_json TEXT NOT NULL
                    )
                    """
                )
                db.execute(
                    """
                    INSERT INTO provided_houses(id, postcode, owner_name, ready_for_start)
                    VALUES ('ASSIGN-HOUSE', 'A00 0000', 'Test Owner', 0)
                    """
                )
                db.execute(
                    """
                    INSERT INTO houses(id, postcode, owner_name, is_finished)
                    VALUES ('ASSIGN-HOUSE', 'A00 0000', 'Test Owner', 0)
                    """
                )
                db.execute(
                    """
                    INSERT INTO house_surveys(house_id, data, notes, updated_by, updated_at)
                    VALUES (?, ?, '', 'Test User', '2026-10-29T08:00:00')
                    """,
                    (
                        "ASSIGN-HOUSE",
                        json.dumps(
                            {
                                "perimeter_m": 21,
                                "windows": [{"w": 1, "h": 1, "qty": 2}],
                            }
                        ),
                    ),
                )
                db.execute(
                    "INSERT INTO seai_applications "
                    "(application_id, detail_data_json) VALUES (?, ?)",
                    (
                        "ASSIGN-HOUSE",
                        json.dumps(
                            {
                                "Application Details": {
                                    "Applicant Name": "Test Applicant"
                                },
                                "Measures": [
                                    {
                                        "Name": "External Wall Insulation",
                                        "Survey Quantity": "94",
                                        "Survey Qty including Variation": "108",
                                    }
                                ],
                            }
                        ),
                    ),
                )
            status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/users",
                {
                    "username": "assign-test-user",
                    "display_name": "Assign Test User",
                    "pin": "234567",
                    "role": "assign",
                },
                admin_cookie,
            )
            self.assertEqual(status, 201)
            status, _, assign_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/login",
                {"username": "assign-test-user", "pin": "234567"},
            )
            self.assertEqual(status, 200)
            connection = http.client.HTTPConnection(
                "127.0.0.1", httpd.server_address[1], timeout=5
            )
            connection.request(
                "GET",
                "/api/survey-room-test-pdfs/application-letter.pdf",
                headers={"Cookie": assign_cookie},
            )
            response = connection.getresponse()
            test_pdf = response.read()
            self.assertEqual(response.status, 200)
            self.assertTrue(test_pdf.startswith(b"%PDF"))
            connection.close()

            status, dashboard, _ = self.http_request(
                httpd, "GET", "/api/dashboard", cookie=assign_cookie
            )
            self.assertEqual(status, 200)
            self.assertEqual(dashboard["materials"], [])
            self.assertEqual(dashboard["movements"], [])
            self.assertNotIn("Restricted Board", json.dumps(dashboard))
            self.assertNotIn("warehouse", dashboard["locations"])
            assign_house = next(
                house for house in dashboard["house_list"]
                if house["id"] == "ASSIGN-HOUSE"
            )
            self.assertEqual(
                assign_house["seai_summary"]["Measures"][0]["Survey Quantity"],
                "94",
            )
            self.assertEqual(
                assign_house["seai_summary"]["Measures"][0][
                    "Survey Qty including Variation"
                ],
                "108",
            )
            self.assertNotIn("seai_details", assign_house)

            status, admin_dashboard, _ = self.http_request(
                httpd, "GET", "/api/dashboard", cookie=admin_cookie
            )
            self.assertEqual(status, 200)
            self.assertEqual(admin_dashboard["materials"][0]["name"], "Restricted Board")

            status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/users",
                {
                    "username": "employee-dashboard-user",
                    "display_name": "Employee Dashboard User",
                    "pin": "345678",
                    "role": "employee",
                },
                admin_cookie,
            )
            self.assertEqual(status, 201)
            status, _, employee_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/login",
                {"username": "employee-dashboard-user", "pin": "345678"},
            )
            self.assertEqual(status, 200)
            status, employee_dashboard, _ = self.http_request(
                httpd,
                "GET",
                "/api/dashboard",
                cookie=employee_cookie,
            )
            self.assertEqual(status, 200)
            employee_house = next(
                house for house in employee_dashboard["house_list"]
                if house["id"] == "ASSIGN-HOUSE"
            )
            self.assertNotIn("seai_summary", employee_house)

            status, houses_result, _ = self.http_request(
                httpd,
                "GET",
                "/api/calculator/houses?status=all",
                cookie=assign_cookie,
            )
            self.assertEqual(status, 200)
            self.assertIs(houses_result["houses"][0]["survey"], True)
            self.assertIsNone(houses_result["houses"][0]["perimeter_m"])
            self.assertNotIn('"windows":', json.dumps(houses_result))
            self.assertNotIn("Restricted Board", json.dumps(houses_result))

            for path in (
                "/api/movements",
                "/api/calculator/requests?house_id=H-1",
                "/calculator",
            ):
                status, result, _ = self.http_request(
                    httpd, "GET", path, cookie=assign_cookie
                )
                self.assertEqual(status, 403, path)
                self.assertIn("cannot access material information", result["error"])

            for path in (
                "/api/movements",
                "/api/movements/batch",
                "/api/materials",
                "/api/stocktakes",
                "/api/calculator/requests",
                "/api/import",
                "/api/houses/status",
            ):
                status, result, _ = self.http_request(
                    httpd, "POST", path, {}, assign_cookie
                )
                self.assertEqual(status, 403, path)
                self.assertIn("cannot access material information", result["error"])
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def start_test_server(self):
        httpd = UvicornTestServer()
        thread = httpd.start()
        return httpd, thread

    def test_brusna_curious_cat_logo_is_served_for_app_branding(self):
        httpd, thread = self.start_test_server()
        connection = http.client.HTTPConnection(
            "127.0.0.1", httpd.server_address[1], timeout=5
        )
        try:
            connection.request("GET", "/brusna-curious-cat-logo.png")
            response = connection.getresponse()
            image = response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Type"), "image/png")
            self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertEqual(image[25], 6)
        finally:
            connection.close()
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_cat_favicon_is_served_for_browser_tab(self):
        httpd, thread = self.start_test_server()
        try:
            for path, content_type, signature in (
                ("/brusna-cat-v3.ico", "image/x-icon", b"\x00\x00\x01\x00"),
                ("/brusna-cat-v3.png", "image/png", b"\x89PNG\r\n\x1a\n"),
            ):
                connection = http.client.HTTPConnection(
                    "127.0.0.1", httpd.server_address[1], timeout=5
                )
                try:
                    connection.request("GET", path)
                    response = connection.getresponse()
                    icon = response.read()
                    self.assertEqual(response.status, 200, path)
                    self.assertEqual(response.getheader("Content-Type"), content_type)
                    self.assertTrue(icon.startswith(signature), path)
                finally:
                    connection.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_ventilation_product_icons_are_served_safely(self):
        httpd, thread = self.start_test_server()
        connection = http.client.HTTPConnection(
            "127.0.0.1", httpd.server_address[1], timeout=5
        )
        try:
            connection.request(
                "GET",
                "/ventilation-icons/core-drill-ventilator.svg",
            )
            response = connection.getresponse()
            image = response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Type"), "image/svg+xml")
            self.assertIn(b"<svg", image)
            self.assertIn(b"5\xe2\x80\x9d Core Drill Ventilator", image)

            connection.request("GET", "/ventilation-icons/..%2Fapp.js")
            response = connection.getresponse()
            self.assertEqual(response.status, 404)
        finally:
            connection.close()
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_oldest_house_test_page_and_photos_require_login(self):
        httpd, thread = self.start_test_server()
        try:
            status, _, _ = self.http_request(httpd, "GET", "/oldest-house-test")
            self.assertEqual(status, 401)
            status, _, _ = self.http_request(
                httpd, "GET", "/oldest-house-test/photos/front.svg"
            )
            self.assertEqual(status, 401)
            status, _, _ = self.http_request(
                httpd, "GET", "/oldest-house-test/pdfs/survey-report.pdf"
            )
            self.assertEqual(status, 401)
            status, _, _ = self.http_request(
                httpd,
                "GET",
                "/api/survey-room-test-pdfs/application-letter.pdf",
            )
            self.assertEqual(status, 401)

            status, _, admin_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/setup",
                {
                    "username": "oldest-house-admin",
                    "display_name": "Test Admin",
                    "pin": "123456",
                },
            )
            self.assertEqual(status, 201)

            connection = http.client.HTTPConnection(
                "127.0.0.1", httpd.server_address[1], timeout=5
            )
            connection.request(
                "GET", "/oldest-house-test", headers={"Cookie": admin_cookie}
            )
            response = connection.getresponse()
            page = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn("Example application", page)
            self.assertIn("owner@example.invalid", page)
            connection.close()

            connection = http.client.HTTPConnection(
                "127.0.0.1", httpd.server_address[1], timeout=5
            )
            connection.request(
                "GET",
                "/oldest-house-test/photos/front.svg",
                headers={"Cookie": admin_cookie},
            )
            response = connection.getresponse()
            image = response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Type"), "image/svg+xml")
            self.assertIn(b"SYNTHETIC DEMO ILLUSTRATION", image)
            connection.close()

            for name in (
                "application-letter.pdf",
                "survey-report.pdf",
                "survey-sketch.pdf",
                "floor-plan-1.pdf",
                "floor-plan-2.pdf",
                "floor-plan-3.pdf",
            ):
                connection = http.client.HTTPConnection(
                    "127.0.0.1", httpd.server_address[1], timeout=10
                )
                connection.request(
                    "GET",
                    f"/oldest-house-test/pdfs/{name}",
                    headers={"Cookie": admin_cookie},
                )
                response = connection.getresponse()
                pdf = response.read()
                self.assertEqual(response.status, 200, name)
                self.assertEqual(
                    response.getheader("Content-Type"), "application/pdf", name
                )
                self.assertIn("inline", response.getheader("Content-Disposition"))
                self.assertTrue(pdf.startswith(b"%PDF"), name)
                connection.close()

            for name in server.SURVEY_ROOM_TEST_PDFS:
                connection = http.client.HTTPConnection(
                    "127.0.0.1", httpd.server_address[1], timeout=10
                )
                connection.request(
                    "GET",
                    f"/api/survey-room-test-pdfs/{name}",
                    headers={"Cookie": admin_cookie},
                )
                response = connection.getresponse()
                pdf = response.read()
                self.assertEqual(response.status, 200, name)
                self.assertEqual(
                    response.getheader("Content-Type"), "application/pdf", name
                )
                self.assertIn("inline", response.getheader("Content-Disposition"))
                self.assertEqual(
                    pdf,
                    (
                        server.STATIC_DIR
                        / "oldest-house-test"
                        / "synthetic"
                        / "pdfs"
                        / name
                    ).read_bytes(),
                )
                connection.close()

            status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/users",
                {
                    "username": "oldest-house-employee",
                    "display_name": "Test Employee",
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
                {"username": "oldest-house-employee", "pin": "345678"},
            )
            self.assertEqual(status, 200)
            status, error, _ = self.http_request(
                httpd,
                "GET",
                "/oldest-house-test",
                cookie=employee_cookie,
            )
            self.assertEqual(status, 403)
            self.assertIn("Surveyor access", error["error"])
            status, error, _ = self.http_request(
                httpd,
                "GET",
                "/api/survey-room-test-pdfs/application-letter.pdf",
                cookie=employee_cookie,
            )
            self.assertEqual(status, 403)
            self.assertIn("Admin, Surveyor, or Assign access", error["error"])
            status, error, _ = self.http_request(
                httpd,
                "GET",
                "/oldest-house-test/pdfs/survey-report.pdf",
                cookie=employee_cookie,
            )
            self.assertEqual(status, 403)
            self.assertIn("Surveyor access", error["error"])
            status, error, _ = self.http_request(
                httpd,
                "GET",
                "/oldest-house-test/photos/front.svg",
                cookie=employee_cookie,
            )
            self.assertEqual(status, 403)
            self.assertIn("Surveyor access", error["error"])

            status, error, _ = self.http_request(
                httpd,
                "GET",
                "/oldest-house-test/photos/not-allowed.jpg",
                cookie=admin_cookie,
            )
            self.assertEqual(status, 404)
            self.assertEqual(error["error"], "Photo not found.")
            status, error, _ = self.http_request(
                httpd,
                "GET",
                "/oldest-house-test/pdfs/not-allowed.pdf",
                cookie=admin_cookie,
            )
            self.assertEqual(status, 404)
            self.assertEqual(error["error"], "PDF not found.")
            status, error, _ = self.http_request(
                httpd,
                "GET",
                "/api/survey-room-test-pdfs/not-allowed.pdf",
                cookie=admin_cookie,
            )
            self.assertEqual(status, 404)
            self.assertEqual(error["error"], "Test PDF was not found.")
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_survey_room_test_pdf_annotations_do_not_persist_separately(self):
        httpd, thread = self.start_test_server()
        try:
            status, _, admin_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/setup",
                {
                    "username": "test-pdf-admin",
                    "display_name": "Test PDF Admin",
                    "pin": "123456",
                },
            )
            self.assertEqual(status, 201)
            house_id = "TEST-PDF-ANNOTATIONS"
            with server.connect_db() as db:
                db.execute(
                    """
                    INSERT INTO provided_houses (id, ready_for_start, source)
                    VALUES (?, 1, 'tracker')
                    """,
                    (house_id,),
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
            source_path = (
                server.STATIC_DIR
                / "oldest-house-test"
                / "pdfs"
                / "application-letter.pdf"
            )
            original_pdf = source_path.read_bytes()
            connection = http.client.HTTPConnection(
                "127.0.0.1", httpd.server_address[1], timeout=5
            )
            connection.request(
                "GET",
                f"/api/survey-room-test-pdfs/application-letter.pdf/preview?house_id={house_id}&page=1",
                headers={"Cookie": admin_cookie},
            )
            response = connection.getresponse()
            preview = response.read()
            headers = dict(response.getheaders())
            self.assertEqual(response.status, 200)
            self.assertEqual(headers["Content-Type"], "image/png")
            self.assertTrue(preview.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertEqual(headers["X-PDF-Page-Count"], "1")
            connection.close()

            status, result, _ = self.http_request(
                httpd,
                "POST",
                "/api/survey-room-test-pdfs/application-letter.pdf/annotations",
                {
                    "house_id": house_id,
                    "annotations": [
                        {
                            "page": 1,
                            "tool": "ventilation_icon",
                            "color": "#ffffff",
                            "point": [0.5, 0.5],
                            "icon": "trickle-vent",
                            "width": 0.18,
                            "height": 0.12,
                            "rotation": 0,
                        }
                    ],
                },
                cookie=admin_cookie,
            )
            self.assertEqual(status, 409, result)
            self.assertIn("Review selected pages", result["error"])
            with server.connect_db() as db:
                self.assertEqual(
                    db.execute(
                        "SELECT COUNT(*) FROM seai_documents WHERE application_id = ?",
                        (house_id,),
                    ).fetchone()[0],
                    0,
                )
            self.assertEqual(source_path.read_bytes(), original_pdf)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_met_eireann_county_forecast_parses_mayo_and_sligo(self):
        feed = b"""<forecast issued="2026-10-22T21:00:00">
          <county><name>MAYO</name><day>
            <date>2026-10-23T00:00:00</date><min_temp>5</min_temp>
            <max_temp>11</max_temp><weather>light_rain</weather>
            <wind_speed units="kmh">30</wind_speed><wind_dir>WNW</wind_dir>
            <rainfall_6_18>1.3</rainfall_6_18><rainfall_18_6>0.7</rainfall_18_6>
          </day></county>
          <county><name>SLIGO</name><day>
            <date>2026-10-23T00:00:00</date><min_temp>4</min_temp>
            <max_temp>10</max_temp><weather>scattered_clouds</weather>
            <wind_speed units="kmh">25</wind_speed><wind_dir>W</wind_dir>
            <rainfall_6_18>0.2</rainfall_6_18><rainfall_18_6>0.1</rainfall_18_6>
          </day></county>
        </forecast>"""

        result = server._parse_met_eireann_county_forecast(feed)

        self.assertEqual(result["issued"], "2026-10-22T21:00:00")
        self.assertEqual(
            [area["county"] for area in result["areas"]], ["Mayo", "Sligo"]
        )
        self.assertEqual(result["areas"][0]["days"][0]["rainfall_mm"], 2.0)
        self.assertEqual(result["areas"][1]["days"][0]["wind_direction"], "W")

    def test_weather_endpoint_requires_login_and_returns_county_data(self):
        httpd, thread = self.start_test_server()
        forecast = {
            "issued": "2026-10-22T21:00:00",
            "areas": [
                {"county": "Mayo", "days": []},
                {"county": "Sligo", "days": []},
            ],
            "source": "Met Éireann Open Data",
            "source_url": "https://www.met.ie/",
        }
        try:
            status, _, _ = self.http_request(httpd, "GET", "/api/weather")
            self.assertEqual(status, 401)
            status, _, admin_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/setup",
                {
                    "username": "weather-admin",
                    "display_name": "Weather Admin",
                    "pin": "123456",
                },
            )
            self.assertEqual(status, 201)
            with patch.object(
                server, "_met_eireann_county_forecast", return_value=forecast
            ):
                status, result, _ = self.http_request(
                    httpd, "GET", "/api/weather", cookie=admin_cookie
                )
            self.assertEqual(status, 200)
            self.assertEqual(result, forecast)
        finally:
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

    def test_house_checklist_is_readable_to_all_roles_and_editable_by_staff(self):
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
                    "scaffolding_status": "done",
                },
                employee_cookie,
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
            self.assertEqual(checklist["scaffolding_status"], "done")
            self.assertIsNone(checklist["esb_unclipped"])
            self.assertIsNone(checklist["plumber_status"])

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
            self.assertEqual(status, 403)
            self.assertIn("original checker", result["error"])
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

            status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/users",
                {
                    "username": "plumber1",
                    "display_name": "Tracker Plumber",
                    "pin": "456789",
                    "role": "plumber",
                },
                admin_cookie,
            )
            self.assertEqual(status, 201)
            status, _, plumber_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/login",
                {"username": "plumber1", "pin": "456789"},
            )
            self.assertEqual(status, 200)
            status, result, _ = self.http_request(
                httpd,
                "POST",
                "/api/house-checklist",
                {
                    "house_id": "H-CHECKLIST",
                    "plumber_status": "done",
                },
                plumber_cookie,
            )
            self.assertEqual(status, 200)
            self.assertTrue(result["saved"])
            status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/houses/plan",
                {"id": "H-CHECKLIST", "ready": True},
                plumber_cookie,
            )
            self.assertEqual(status, 403)
            status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/users",
                {
                    "username": "plumber2",
                    "display_name": "Another Plumber",
                    "pin": "456789",
                    "role": "plumber",
                },
                plumber_cookie,
            )
            self.assertEqual(status, 403)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_surveyors_can_save_ready_house_work_plans(self):
        with server.connect_db() as db:
            db.execute(
                "INSERT INTO provided_houses"
                "(id, postcode, owner_name, source, plan_vent_required) "
                "VALUES ('H-WORKPLAN', 'T1', 'Owner', 'fictional_sample', 1)"
            )
            db.execute(
                "UPDATE provided_houses SET ready_for_start = 1 WHERE id = 'H-WORKPLAN'"
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
            with server.connect_db() as db:
                db.execute(
                    """
                    INSERT INTO house_vent_assignments
                        (house_id, teamlead_user_id, assigned_by, assigned_at)
                    SELECT 'H-WORKPLAN', id, 'Earlier Vent Coordinator',
                           '2026-10-30T11:00:00'
                    FROM app_users WHERE username = 'admin1'
                    """
                )
            status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/users",
                {
                    "username": "surveyor1",
                    "display_name": "Tracker Surveyor",
                    "pin": "345678",
                    "pin_confirm": "345678",
                    "role": "surveyor",
                },
                admin_cookie,
            )
            self.assertEqual(status, 201)
            status, _, surveyor_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/login",
                {"username": "surveyor1", "pin": "345678"},
            )
            self.assertEqual(status, 200)
            with server.connect_db() as db:
                db.execute(
                    """
                    INSERT INTO house_role_assignments
                        (house_id, role, user_id, assigned_by, assigned_at)
                    SELECT 'H-WORKPLAN', 'surveyor', id, 'Tracker Admin',
                           '2026-10-30T12:00:00'
                    FROM app_users WHERE username = 'surveyor1'
                    """
                )
            status, result, _ = self.http_request(
                httpd,
                "POST",
                "/api/house-work-plan",
                {
                    "house_id": "H-WORKPLAN",
                    "scaffolding_required": True,
                    "scaffolding_notes": "Rear access only",
                    "esb_required": False,
                    "esb_notes": "",
                    "plumber_required": True,
                    "plumber_notes": "Coordinate with owner",
                    "vent_required": False,
                    "vent_notes": "",
                    "ceramic_fuse_board": False,
                    "solar_pv_required": False,
                    "pir_required": False,
                    "order_variations": False,
                    "windows_required": False,
                },
                surveyor_cookie,
            )
            self.assertEqual(status, 200)
            self.assertTrue(result["saved"])
            self.assertFalse(result["published"])
            status, response, _ = self.http_request(
                httpd,
                "GET",
                "/api/calculator/houses?status=ready",
                cookie=surveyor_cookie,
            )
            self.assertEqual(status, 200)
            house = next(item for item in response["houses"] if item["id"] == "H-WORKPLAN")
            self.assertIsNone(house["plan_scaffolding_required"])
            self.assertTrue(house["draft_plan_scaffolding_required"])
            self.assertEqual(house["draft_plan_scaffolding_notes"], "Rear access only")
            invalid_survey_status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/house-survey",
                {
                    "house_id": "H-WORKPLAN",
                    "survey": {
                        "windows": [],
                        "doors": [],
                        "house_corners": [],
                        "perimeter_m": None,
                        "notes": "",
                    },
                },
                surveyor_cookie,
            )
            self.assertEqual(invalid_survey_status, 400)
            with server.connect_db() as db:
                unchanged_plan = db.execute(
                    "SELECT plan_scaffolding_required, plan_esb_required, "
                    "plan_plumber_required FROM provided_houses "
                    "WHERE id = 'H-WORKPLAN'"
                ).fetchone()
                self.assertEqual(tuple(unchanged_plan), (None, None, None))
                self.assertIsNotNone(
                    db.execute(
                        "SELECT 1 FROM house_work_plan_drafts "
                        "WHERE house_id = 'H-WORKPLAN'"
                    ).fetchone()
                )
            survey_status, survey_result, _ = self.http_request(
                httpd,
                "POST",
                "/api/house-survey",
                {
                    "house_id": "H-WORKPLAN",
                    "survey": {
                        "windows": [],
                        "doors": [],
                        "house_corners": [],
                        "perimeter_m": 42.5,
                        "notes": "",
                    },
                },
                surveyor_cookie,
            )
            self.assertEqual(survey_status, 200, survey_result)
            self.assertTrue(survey_result["work_plan_published"])
            with server.connect_db() as db:
                published_vent_requirement = db.execute(
                    "SELECT plan_vent_required FROM provided_houses "
                    "WHERE id = 'H-WORKPLAN'"
                ).fetchone()["plan_vent_required"]
                self.assertEqual(published_vent_requirement, 0)
                self.assertIsNone(
                    db.execute(
                        "SELECT 1 FROM house_vent_assignments "
                        "WHERE house_id = 'H-WORKPLAN'"
                    ).fetchone()
                )
            admin_plan_status, admin_plan_result, _ = self.http_request(
                httpd,
                "POST",
                "/api/house-work-plan",
                {
                    "house_id": "H-WORKPLAN",
                    "scaffolding_required": True,
                    "scaffolding_notes": "Rear access only",
                    "esb_required": False,
                    "esb_notes": "",
                    "plumber_required": True,
                    "plumber_notes": "Coordinate with owner",
                    "ceramic_fuse_board": False,
                    "solar_pv_required": False,
                    "pir_required": False,
                    "order_variations": False,
                    "windows_required": False,
                },
                admin_cookie,
            )
            self.assertEqual(admin_plan_status, 200, admin_plan_result)
            self.assertTrue(admin_plan_result["published"])
            status, response, _ = self.http_request(
                httpd,
                "GET",
                "/api/calculator/houses?status=ready",
                cookie=admin_cookie,
            )
            self.assertEqual(status, 200)
            house = next(item for item in response["houses"] if item["id"] == "H-WORKPLAN")
            self.assertEqual(house["plan_scaffolding_required"], 1)
            self.assertEqual(house["plan_scaffolding_notes"], "Rear access only")
            self.assertEqual(house["plan_esb_required"], 0)
            self.assertEqual(house["plan_plumber_notes"], "Coordinate with owner")
            with server.connect_db() as db:
                with db:
                    server._upsert_house(db, "H-WORKPLAN")
                checklist = db.execute(
                    "SELECT scaffolding_status, esb_unclipped, plumber_status "
                    "FROM house_checklists WHERE house_id = 'H-WORKPLAN'"
                ).fetchone()
            self.assertEqual(
                tuple(checklist),
                ("not_done", "not_esb", "not_done"),
            )
            with server.connect_db() as db:
                plumbing_supervisor_id = server._insert_user(
                    db,
                    "plumbing-workplan-supervisor",
                    "Plumbing Workplan Supervisor",
                    "456789",
                    "supervisor",
                    supervisor_specialty="Plumbing",
                )
                db.execute(
                    """
                    INSERT INTO house_role_assignments
                        (house_id, role, user_id, assigned_by, assigned_at)
                    VALUES ('H-WORKPLAN', 'supervisor', ?,
                            'Plumbing Coordinator', '2026-10-30T13:00:00')
                    """,
                    (plumbing_supervisor_id,),
                )
            draft_status, draft_result, _ = self.http_request(
                httpd,
                "POST",
                "/api/house-work-plan",
                {
                    "house_id": "H-WORKPLAN",
                    "scaffolding_required": True,
                    "scaffolding_notes": "Rear access only",
                    "esb_required": False,
                    "esb_notes": "",
                    "plumber_required": False,
                    "plumber_notes": "",
                    "ceramic_fuse_board": False,
                    "solar_pv_required": False,
                    "pir_required": False,
                    "order_variations": False,
                    "windows_required": False,
                },
                surveyor_cookie,
            )
            self.assertEqual(draft_status, 409, draft_result)
            self.assertIn("Ready for start", draft_result["error"])
            survey_status, survey_result, _ = self.http_request(
                httpd,
                "POST",
                "/api/house-survey",
                {
                    "house_id": "H-WORKPLAN",
                    "survey": {
                        "windows": [],
                        "doors": [],
                        "house_corners": [],
                        "perimeter_m": 42.5,
                        "notes": "",
                    },
                },
                surveyor_cookie,
            )
            self.assertEqual(survey_status, 409, survey_result)
            self.assertIn("ready for start", survey_result["error"])
            with server.connect_db() as db:
                published_plan = db.execute(
                    "SELECT plan_plumber_required FROM provided_houses "
                    "WHERE id = 'H-WORKPLAN'"
                ).fetchone()
                self.assertEqual(published_plan["plan_plumber_required"], 1)
                self.assertIsNotNone(
                    db.execute(
                        "SELECT 1 FROM house_role_assignments "
                        "WHERE house_id = 'H-WORKPLAN' AND role = 'supervisor'"
                    ).fetchone()
                )
            admin_plan_status, admin_plan_result, _ = self.http_request(
                httpd,
                "POST",
                "/api/house-work-plan",
                {
                    "house_id": "H-WORKPLAN",
                    "scaffolding_required": True,
                    "scaffolding_notes": "Rear access only",
                    "esb_required": False,
                    "esb_notes": "",
                    "plumber_required": True,
                    "plumber_notes": "Coordinate with owner",
                },
                admin_cookie,
            )
            self.assertEqual(admin_plan_status, 409, admin_plan_result)
            self.assertIn("Ready for start", admin_plan_result["error"])
            with server.connect_db() as db:
                plumbing_house = next(
                    house
                    for house in server._plumbing_coordinator_dashboard(db)[
                        "plumbing_houses"
                    ]
                    if house["id"] == "H-WORKPLAN"
                )
                self.assertEqual(
                    plumbing_house["supervisor_user_id"], plumbing_supervisor_id
                )
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

    def test_dashboard_counts_open_and_closed_houses_by_assigned_employee(self):
        with server.connect_db() as db:
            with db:
                server._upsert_house(db, "EMPLOYEE-OPEN", employee_name="Assigned Worker")
                server._upsert_house(db, "EMPLOYEE-CLOSED", employee_name="Assigned Worker")
                db.execute(
                    "UPDATE houses SET is_finished = 1 WHERE id = 'EMPLOYEE-CLOSED'"
                )
            dashboard = server._dashboard(db)
            locations = server.known_locations(db)

        location = server.employee_location("Assigned Worker")
        self.assertEqual(
            dashboard["team_house_counts"][location],
            {
                "open": 1,
                "closed": 1,
                "open_house_ids": ["EMPLOYEE-OPEN"],
                "closed_house_ids": ["EMPLOYEE-CLOSED"],
            },
        )
        self.assertIn(location, locations)

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
                    db.execute(
                        """
                        UPDATE provided_houses
                        SET plan_scaffolding_required = 0,
                            plan_esb_required = 0,
                            plan_plumber_required = 0,
                            plan_ceramic_fuse_board = 0,
                            plan_solar_pv_required = 0,
                            plan_pir_required = 0,
                            plan_order_variations = 0,
                            plan_windows_required = 0
                        WHERE id = 'H-READY'
                        """
                    )
                    db.execute(
                        """
                        INSERT INTO house_role_assignments
                            (house_id, role, user_id, assigned_by, assigned_at)
                        SELECT 'H-READY', 'surveyor', id, 'Admin',
                               '2026-10-30T12:00:00'
                        FROM app_users WHERE username = 'survey1'
                        """
                    )
            status, result, _ = self.http_request(
                httpd, "GET", "/api/calculator/houses?status=not_started", cookie=survey_cookie
            )
            self.assertEqual(status, 200)
            self.assertEqual([h["id"] for h in result["houses"]], ["H-READY"])
            survey = {
                "windows": [{"w": 1.2, "h": 1.0, "qty": 3}],
                "doors": [{"w": 0.9, "h": 2.0, "qty": 1}],
                "house_corners": [2.5], "perimeter_m": 42.75,
                "notes": "Check access",
            }
            for invalid_survey in [
                {**survey, "windows": [{"w": 1.2, "h": 1.0, "qty": 0}]},
                {**survey, "doors": [{"w": float("nan"), "h": 2.0, "qty": 1}]},
                {**survey, "house_corners": [-1]},
                {**survey, "perimeter_m": float("nan")},
                {**survey, "perimeter_m": -1},
                {**survey, "perimeter_m": None},
                {key: value for key, value in survey.items() if key != "perimeter_m"},
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
            with server.connect_db() as db:
                db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS seai_documents (
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
            connection = http.client.HTTPConnection(
                "127.0.0.1", httpd.server_address[1], timeout=5
            )
            connection.request(
                "GET",
                "/api/survey-room-test-pdfs/application-letter.pdf/preview"
                "?house_id=H-READY&page=1",
                headers={"Cookie": survey_cookie},
            )
            response = connection.getresponse()
            preview = response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Type"), "image/png")
            self.assertTrue(preview.startswith(b"\x89PNG\r\n\x1a\n"))
            connection.close()
            status, result, _ = self.http_request(
                httpd,
                "POST",
                "/api/survey-room-test-pdfs/application-letter.pdf/annotations",
                {
                    "house_id": "H-READY",
                    "annotations": [
                        {
                            "page": 1,
                            "tool": "ventilation_icon",
                            "color": "#ffffff",
                            "point": [0.5, 0.5],
                            "icon": "trickle-vent",
                            "width": 0.18,
                            "height": 0.12,
                            "rotation": 0,
                        }
                    ],
                },
                survey_cookie,
            )
            self.assertEqual(status, 409, result)
            self.assertIn("Review selected pages", result["error"])
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

    def test_user_role_migration_preserves_users_and_sessions(self):
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
                user_columns = {
                    row["name"] for row in db.execute("PRAGMA table_info(app_users)")
                }
                self.assertTrue({
                    "work_phone", "home_phone", "company_email", "private_email",
                }.issubset(user_columns))
                self.assertEqual(
                    tuple(db.execute(
                        "SELECT work_phone, home_phone, company_email, private_email "
                        "FROM app_users WHERE id = 1"
                    ).fetchone()),
                    (None, None, None, None),
                )
                for role in (
                    "assign", "boss", "surveyor", "warehouse", "delivery",
                    "supervisor", "teamlead", "plumber", "employee",
                ):
                    db.execute("UPDATE app_users SET role = ? WHERE id = 1", (role,))
        finally:
            server.DB_PATH = original_path

    def test_user_migrations_preserve_optional_contact_fields(self):
        legacy_path = Path(self.temp_dir.name) / "legacy-contact.sqlite3"
        with sqlite3.connect(legacy_path) as db:
            db.executescript("""
                CREATE TABLE app_users (
                    id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    display_name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('admin', 'employee')),
                    password_salt BLOB NOT NULL, password_hash BLOB NOT NULL,
                    password_iterations INTEGER NOT NULL,
                    credential_type TEXT NOT NULL DEFAULT 'pin',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    supervisor_specialty TEXT NOT NULL DEFAULT '',
                    team_name TEXT NOT NULL DEFAULT '',
                    work_phone TEXT, home_phone TEXT, work_email TEXT, private_email TEXT
                );
                CREATE TABLE app_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
                    expires_at INTEGER NOT NULL
                );
                INSERT INTO app_users (
                    id, username, display_name, role, password_salt, password_hash,
                    password_iterations, work_phone, home_phone, work_email, private_email
                ) VALUES (
                    1, 'admin1', 'Admin', 'admin', X'01', X'02', 1,
                    '0100', '0200', 'work@example.com', 'private@example.com'
                );
            """)
        db.close()
        original_path = server.DB_PATH
        try:
            server.DB_PATH = legacy_path
            server.initialize_db()
            with server.connect_db() as db:
                self.assertEqual(
                    tuple(db.execute(
                        "SELECT work_phone, home_phone, company_email, private_email "
                        "FROM app_users WHERE id = 1"
                    ).fetchone()),
                    ("0100", "0200", "work@example.com", "private@example.com"),
                )
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

    def test_movement_api_keeps_every_location_and_dashboard_in_sync(self):
        material_id = self.add_material(name="Lifecycle board", opening=50)
        with server.connect_db() as db:
            with db:
                server._upsert_house(db, "FLOW-HOUSE-1")
                server._upsert_house(db, "FLOW-HOUSE-2")
        httpd, thread = self.start_test_server()
        try:
            status, _, admin_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/setup",
                {
                    "username": "flow-admin",
                    "display_name": "Flow Admin",
                    "pin": "123456",
                },
            )
            self.assertEqual(status, 201)
            common = {
                "date": date.today().isoformat(),
                "person": "Flow Worker",
                "items": [{"material_id": material_id, "qty": 10}],
            }

            def post_movement(kind, **values):
                payload = {**common, "kind": kind, **values}
                return self.http_request(
                    httpd,
                    "POST",
                    "/api/movements/batch",
                    payload,
                    admin_cookie,
                )

            status, collection, _ = post_movement(
                "collection", team="Flow Crew"
            )
            self.assertEqual(status, 201)
            self.assertEqual(len(collection["ids"]), 1)
            status, first_delivery, _ = post_movement(
                "delivery",
                items=[{"material_id": material_id, "qty": 4}],
                house_id="FLOW-HOUSE-1",
                team="Flow Crew",
                source_kind="team",
                source_team="Flow Crew",
            )
            self.assertEqual(status, 201)
            self.assertEqual(first_delivery["delivery_number"], 1)
            status, second_delivery, _ = post_movement(
                "pickup",
                items=[{"material_id": material_id, "qty": 5}],
                house_id="FLOW-HOUSE-1",
                team="Flow Crew",
                is_extra_delivery=True,
            )
            self.assertEqual(status, 201)
            self.assertEqual(second_delivery["delivery_number"], 2)
            status, use_result, _ = post_movement(
                "use",
                items=[{"material_id": material_id, "qty": 2}],
                house_id="FLOW-HOUSE-1",
                team="Flow Crew",
            )
            self.assertEqual(status, 201)
            status, _, _ = post_movement(
                "return",
                items=[{"material_id": material_id, "qty": 1}],
                house_id="FLOW-HOUSE-1",
                team="Flow Crew",
            )
            self.assertEqual(status, 201)
            status, _, _ = post_movement(
                "transfer",
                items=[{"material_id": material_id, "qty": 3}],
                source_kind="team",
                source_reference="Flow Crew",
                destination_kind="house",
                destination_reference="FLOW-HOUSE-2",
                team="Flow Crew",
            )
            self.assertEqual(status, 201)
            self.assertEqual(len(use_result["ids"]), 1)

            status, dashboard, _ = self.http_request(
                httpd, "GET", "/api/dashboard", cookie=admin_cookie
            )
            self.assertEqual(status, 200)
            material = next(
                row for row in dashboard["materials"] if row["id"] == material_id
            )
            self.assertEqual(material["balances"]["warehouse"], 36)
            self.assertEqual(material["balances"]["team:Flow Crew"], 3)
            self.assertEqual(material["balances"]["house:FLOW-HOUSE-1"], 6)
            self.assertEqual(material["balances"]["house:FLOW-HOUSE-2"], 3)
            self.assertEqual(material["balances"]["consumed"], 2)
            self.assertEqual(material["warehouse_qty"], 36)
            self.assertEqual(
                dashboard["house_summary"]["active"],
                2,
            )
            self.assertEqual(
                next(
                    house["stage"]
                    for house in dashboard["house_list"]
                    if house["id"] == "FLOW-HOUSE-1"
                ),
                1,
            )

            status, ledger, _ = self.http_request(
                httpd, "GET", "/api/movements", cookie=admin_cookie
            )
            self.assertEqual(status, 200)
            self.assertEqual(len(ledger["movements"]), 5)
            self.assertEqual(
                sum(
                    row["qty"]
                    for row in ledger["movements"]
                    if row["kind"] in {"pickup", "delivery"}
                ),
                9,
            )
            with server.connect_db() as db:
                saved = server.balances_for(db, material_id)
                self.assertEqual(saved, material["balances"])
                self.assertEqual(
                    db.execute(
                        "SELECT COUNT(*) FROM movements WHERE material_id = ?",
                        (material_id,),
                    ).fetchone()[0],
                    6,
                )
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_concurrent_movements_serialize_stock_validation(self):
        material_id = self.add_material(name="Concurrent board", opening=10)
        httpd, thread = self.start_test_server()
        first_balance_checked = Event()
        second_balance_checked = Event()
        release_first = Event()
        calls_lock = Lock()
        calls = 0
        original_balances_for = server.balances_for
        try:
            status, _, admin_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/setup",
                {
                    "username": "race-admin",
                    "display_name": "Race Admin",
                    "pin": "123456",
                },
            )
            self.assertEqual(status, 201)

            def pause_first_balance_check(db, requested_material_id):
                nonlocal calls
                balances = original_balances_for(db, requested_material_id)
                with calls_lock:
                    calls += 1
                    call_number = calls
                if call_number == 1:
                    first_balance_checked.set()
                    if not release_first.wait(timeout=5):
                        raise TimeoutError("Timed out waiting to release the first movement.")
                else:
                    second_balance_checked.set()
                return balances

            def post_collection(person):
                return self.http_request(
                    httpd,
                    "POST",
                    "/api/movements/batch",
                    {
                        "kind": "collection",
                        "date": date.today().isoformat(),
                        "person": person,
                        "team": "Concurrent Crew",
                        "items": [{"material_id": material_id, "qty": 7}],
                    },
                    admin_cookie,
                )

            with patch.object(server, "balances_for", pause_first_balance_check):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(post_collection, "Worker One")
                    self.assertTrue(first_balance_checked.wait(timeout=5))
                    second = pool.submit(post_collection, "Worker Two")
                    second_checked_while_first_holds_lock = second_balance_checked.wait(
                        timeout=0.25
                    )
                    release_first.set()
                    first_result = first.result(timeout=8)
                    second_result = second.result(timeout=8)

            self.assertFalse(second_checked_while_first_holds_lock)
            self.assertEqual(
                sorted((first_result[0], second_result[0])),
                [201, 409],
            )
            with server.connect_db() as db:
                self.assertEqual(
                    server.balances_for(db, material_id)["warehouse"], 3
                )
                self.assertEqual(
                    db.execute(
                        "SELECT COUNT(*) FROM movements WHERE material_id = ?",
                        (material_id,),
                    ).fetchone()[0],
                    1,
                )
        finally:
            release_first.set()
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_movement_validation_rollbacks_and_stocktake_updates_balances(self):
        material_id = self.add_material(name="Validation board", opening=5)
        with server.connect_db() as db:
            with db:
                server._upsert_house(db, "CLOSED-MOVEMENT-TEST")
                db.execute(
                    "UPDATE houses SET is_finished = 1 WHERE id = ?",
                    ("CLOSED-MOVEMENT-TEST",),
                )
        httpd, thread = self.start_test_server()
        try:
            status, _, admin_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/setup",
                {
                    "username": "validation-admin",
                    "display_name": "Validation Admin",
                    "pin": "123456",
                },
            )
            self.assertEqual(status, 201)
            payload = {
                "kind": "collection",
                "date": date.today().isoformat(),
                "person": "Validation Worker",
                "team": "Validation Crew",
                "items": [{"material_id": material_id, "qty": 1}],
            }
            invalid_payloads = [
                ({**payload, "items": []}, 400),
                (
                    {**payload, "items": [{"material_id": material_id, "qty": 0}]},
                    400,
                ),
                (
                    {**payload, "items": [{"material_id": material_id, "qty": -1}]},
                    400,
                ),
                (
                    {**payload, "items": [{"material_id": material_id, "qty": "no"}]},
                    400,
                ),
                ({**payload, "person": ""}, 400),
                (
                    {**payload, "items": [{"material_id": material_id, "qty": 6}]},
                    409,
                ),
                (
                    {
                        **payload,
                        "items": [
                            {"material_id": material_id, "qty": 2},
                            {"material_id": material_id, "qty": 6},
                        ],
                    },
                    409,
                ),
            ]
            for invalid, expected_status in invalid_payloads:
                status, _, _ = self.http_request(
                    httpd,
                    "POST",
                    "/api/movements/batch",
                    invalid,
                    admin_cookie,
                )
                self.assertEqual(status, expected_status, invalid)

            status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/movements/batch",
                {
                    **payload,
                    "kind": "pickup",
                    "house_id": "CLOSED-MOVEMENT-TEST",
                },
                admin_cookie,
            )
            self.assertEqual(status, 409)
            status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/stocktakes",
                {
                    "material_id": material_id,
                    "location": "warehouse",
                    "quantity": 8,
                    "counted_by": "",
                },
                admin_cookie,
            )
            self.assertEqual(status, 400)
            status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/stocktakes",
                {
                    "material_id": material_id,
                    "location": "warehouse",
                    "quantity": 8,
                    "counted_by": "Counter",
                },
                admin_cookie,
            )
            self.assertEqual(status, 201)
            status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/movements/batch",
                {
                    **payload,
                    "items": [{"material_id": material_id, "qty": 6}],
                },
                admin_cookie,
            )
            self.assertEqual(status, 201)
            status, dashboard, _ = self.http_request(
                httpd, "GET", "/api/dashboard", cookie=admin_cookie
            )
            self.assertEqual(status, 200)
            updated_material = next(
                row for row in dashboard["materials"] if row["id"] == material_id
            )
            self.assertEqual(updated_material["warehouse_qty"], 2)
            self.assertEqual(
                updated_material["balances"]["team:Validation Crew"], 6
            )
            with server.connect_db() as db:
                self.assertEqual(server.balances_for(db, material_id)["warehouse"], 2)
                self.assertEqual(
                    db.execute(
                        "SELECT COUNT(*) FROM movements WHERE material_id = ?",
                        (material_id,),
                    ).fetchone()[0],
                    1,
                )
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_stage_api_validates_and_persists_updates_for_dashboard(self):
        with server.connect_db() as db:
            server._upsert_house(db, "STAGE-API-TEST")
        httpd, thread = self.start_test_server()
        try:
            status, _, admin_cookie = self.http_request(
                httpd,
                "POST",
                "/api/auth/setup",
                {
                    "username": "stage-admin",
                    "display_name": "Stage Admin",
                    "pin": "123456",
                },
            )
            self.assertEqual(status, 201)
            for stage in (0, 3, True, "2"):
                status, result, _ = self.http_request(
                    httpd,
                    "POST",
                    "/api/houses/stage",
                    {"id": "STAGE-API-TEST", "stage": stage},
                    admin_cookie,
                )
                self.assertEqual(status, 400)
                self.assertIn("Stage must be 1 or 2", result["error"])

            status, _, _ = self.http_request(
                httpd,
                "POST",
                "/api/houses/stage",
                {"id": "MISSING-STAGE-HOUSE", "stage": 2},
                admin_cookie,
            )
            self.assertEqual(status, 404)
            status, result, _ = self.http_request(
                httpd,
                "POST",
                "/api/houses/stage",
                {"id": "STAGE-API-TEST", "stage": 2},
                admin_cookie,
            )
            self.assertEqual(status, 200)
            self.assertEqual(result["stage"], 2)
            status, dashboard, _ = self.http_request(
                httpd, "GET", "/api/dashboard", cookie=admin_cookie
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                next(
                    house["stage"]
                    for house in dashboard["house_list"]
                    if house["id"] == "STAGE-API-TEST"
                ),
                2,
            )
            with server.connect_db() as db:
                self.assertEqual(
                    db.execute(
                        "SELECT stage FROM houses WHERE id = 'STAGE-API-TEST'"
                    ).fetchone()[0],
                    2,
                )
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

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

    def test_dashboard_includes_house_status_survey_and_movement_audit(self):
        with server.connect_db() as db:
            with db:
                server._upsert_house(
                    db,
                    "H-AUDIT",
                    employee_name="Assigned Worker",
                )
                db.execute(
                    """
                    INSERT INTO house_surveys(house_id, data, notes, updated_by, updated_at)
                    VALUES ('H-AUDIT', '{}', '', 'Surveyor Name', '2026-05-02T10:30:00')
                    """
                )
                db.execute(
                    """
                    INSERT INTO material_requests(house_id, created_at, created_by, data)
                    VALUES ('H-AUDIT', '2026-05-02T10:35:00', 'Materials Planner', '{}')
                    """
                )
                server._record_house_status(db, "H-AUDIT", "ready", "Admin Name")
                server._record_house_status(db, "H-AUDIT", "active", "Employee Name")
                server._record_house_status(db, "H-AUDIT", "closed", "Closing Admin")
                server._record_house_status(db, "H-AUDIT", "stage_2", "Stage Actor")
        material_id = self.add_material()
        self.add_movement(
            material_id,
            "pickup",
            2,
            person="Movement Person",
            team="Crew A",
            house_id="H-AUDIT",
        )
        with server.connect_db() as db:
            house = next(
                item for item in server._dashboard(db)["house_list"]
                if item["id"] == "H-AUDIT"
            )

        self.assertEqual(house["employee_name"], "Assigned Worker")
        self.assertEqual(
            [event["status"] for event in house["status_history"]],
            ["ready", "active", "closed"],
        )
        self.assertEqual(house["status_history"][0]["changed_by"], "Admin Name")
        self.assertEqual(house["survey_updated_by"], "Surveyor Name")
        self.assertEqual(house["last_movement"]["person"], "Movement Person")
        self.assertTrue(house["last_movement"]["recorded_at"])
        activity_by_type = {}
        for entry in house["activity_history"]:
            activity_by_type.setdefault(entry["type"], []).append(entry)
        self.assertEqual(
            [entry["status"] for entry in activity_by_type["status"]],
            ["ready", "active", "closed"],
        )
        self.assertEqual(
            activity_by_type["status"][-1]["changed_by"], "Closing Admin"
        )
        self.assertEqual(
            activity_by_type["survey"][0]["changed_by"], "Surveyor Name"
        )
        self.assertEqual(
            activity_by_type["material_request"][0]["created_by"],
            "Materials Planner",
        )
        self.assertEqual(
            activity_by_type["movement"][0]["person"], "Movement Person"
        )

    def test_house_status_history_migration_preserves_events_without_active_house_fk(self):
        with server.connect_db() as db:
            with db:
                db.execute("DROP INDEX house_status_history_house_idx")
                db.execute("DROP TABLE house_status_history")
                db.execute(
                    """
                    CREATE TABLE house_status_history (
                        id INTEGER PRIMARY KEY,
                        house_id TEXT NOT NULL REFERENCES houses(id) ON DELETE CASCADE,
                        status TEXT NOT NULL,
                        changed_by TEXT NOT NULL DEFAULT '',
                        changed_at TEXT NOT NULL
                    )
                    """
                )
                db.execute("INSERT INTO houses(id) VALUES ('H-OLD-AUDIT')")
                db.execute(
                    """
                    INSERT INTO house_status_history(house_id, status, changed_by, changed_at)
                    VALUES ('H-OLD-AUDIT', 'active', 'Old Actor', '2026-05-02T10:30:00')
                    """
                )

        server.initialize_db()

        with server.connect_db() as db:
            foreign_keys = db.execute(
                "PRAGMA foreign_key_list(house_status_history)"
            ).fetchall()
            event = db.execute(
                "SELECT house_id, status, changed_by FROM house_status_history"
            ).fetchone()
        self.assertEqual(foreign_keys, [])
        self.assertEqual(
            dict(event),
            {
                "house_id": "H-OLD-AUDIT",
                "status": "active",
                "changed_by": "Old Actor",
            },
        )

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
