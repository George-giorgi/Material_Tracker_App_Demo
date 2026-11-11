"""End-to-end browser test for the guided workspace navigation flow."""
from io import BytesIO
import http.client
import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path
from urllib.parse import urlsplit

import server
from tests.support import UvicornTestServer
from pypdf import PdfReader
from reportlab.pdfgen import canvas as pdf_canvas

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


@unittest.skipUnless(
    sync_playwright,
    "install requirements-test.txt and run `py -m playwright install chromium`",
)
class NavigationBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = server.DB_PATH
        server.DB_PATH = Path(self.temp_dir.name) / "navigation-test.sqlite3"
        server.initialize_db()
        self.httpd = UvicornTestServer()
        self.server_thread = self.httpd.start()
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch()
        self.page = self.browser.new_page()
        self.page.route(
            "https://tile.openstreetmap.org/**",
            lambda route: route.abort(),
        )
        self.page.route(
            "**/api/weather",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "issued": "2026-10-22T21:00:00",
                        "areas": [
                            {
                                "county": "Mayo",
                                "days": [
                                    {
                                        "date": "2026-10-23",
                                        "weather": "light_rain",
                                        "min_temp": 5,
                                        "max_temp": 11,
                                        "wind_speed_kmh": 30,
                                        "wind_direction": "WNW",
                                        "rainfall_mm": 2,
                                    }
                                ],
                            },
                            {
                                "county": "Sligo",
                                "days": [
                                    {
                                        "date": "2026-10-23",
                                        "weather": "scattered_clouds",
                                        "min_temp": 4,
                                        "max_temp": 10,
                                        "wind_speed_kmh": 25,
                                        "wind_direction": "W",
                                        "rainfall_mm": 1,
                                    }
                                ],
                            },
                        ],
                        "source": "Met Éireann Open Data",
                        "source_url": "https://www.met.ie/",
                    }
                ),
            ),
        )
        self.browser_errors = []
        self.page.on("pageerror", lambda error: self.browser_errors.append(str(error)))
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.page.goto(self.base_url, wait_until="domcontentloaded")
        self.page.locator("#auth-display-name").fill("Navigation Test Admin")
        self.page.locator("#auth-username").fill("navigation-test-admin")
        self.page.locator("#auth-pin").fill("123456")
        self.page.locator("#auth-pin-confirm").fill("123456")
        self.page.locator("#auth-submit").click()
        self.page.locator(".app-shell:not(.hidden)").wait_for()

    def tearDown(self):
        if hasattr(self, "browser"):
            self.browser.close()
        if hasattr(self, "playwright"):
            self.playwright.stop()
        if hasattr(self, "httpd"):
            self.httpd.shutdown()
            self.httpd.server_close()
        if hasattr(self, "server_thread"):
            self.server_thread.join(timeout=5)
        server.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def active_page(self):
        return self.page.locator(".page.active").get_attribute("data-page")

    def wait_for_pdf_canvas_layout(self):
        self.page.evaluate(
            """async () => {
              const canvas = document.querySelector('#pdf-annotator-canvas');
              let previous = '';
              let stableFrames = 0;
              while (stableFrames < 3) {
                await new Promise(requestAnimationFrame);
                const rect = canvas.getBoundingClientRect();
                const size = `${rect.width}:${rect.height}`;
                stableFrames = size === previous ? stableFrames + 1 : 0;
                previous = size;
              }
            }"""
        )

    def create_seai_document_storage(self):
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

    def test_primary_navigation_and_overview_house_shortcuts_work(self):
        for tab in ("overview", "inventory", "houses", "movements"):
            nav_button = self.page.locator(f'.main-nav [data-tab="{tab}"]')
            nav_button.click()
            self.assertEqual(self.active_page(), tab)
            self.assertEqual(nav_button.get_attribute("aria-current"), "page")

        self.page.locator('[data-tab="overview"]').click()
        self.page.locator("#stat-ready").click()
        self.page.locator("#house-team-filter").wait_for(state="visible")
        self.assertEqual(
            self.page.locator("#house-team-filter-label").inner_text(),
            "Showing houses ready for start",
        )

        self.page.locator("#go-overview").click()
        self.page.locator("#stat-provided").click()
        self.page.locator("#house-map-view").wait_for(state="visible")
        self.page.locator("#go-overview").click()
        self.page.set_viewport_size({"width": 375, "height": 800})
        self.assertLessEqual(
            self.page.locator(".main-nav").evaluate("(nav) => nav.scrollWidth"),
            self.page.locator(".main-nav").evaluate("(nav) => nav.clientWidth"),
        )
        self.assertLessEqual(
            self.page.evaluate("document.documentElement.scrollWidth"),
            self.page.evaluate("document.documentElement.clientWidth"),
        )
        self.assertEqual(self.browser_errors, [])

    def test_second_account_can_sign_in_without_sidebar_navigation(self):
        created = self.page.request.post(
            f"{self.base_url}/api/users",
            data={
                "username": "second-surveyor",
                "display_name": "Second Surveyor",
                "pin": "234567",
                "role": "surveyor",
            },
        )
        self.assertEqual(created.status, 201, created.text())
        self.assertEqual(
            created.json()["user"]["supervisor_specialty"],
            "",
        )

        login = self.page.request.post(
            f"{self.base_url}/api/auth/login",
            data={"username": "second-surveyor", "pin": "234567"},
        )
        self.assertEqual(login.status, 200, login.text())
        cookie = login.headers["set-cookie"].split(";", 1)[0]
        name, value = cookie.split("=", 1)
        self.page.context.add_cookies(
            [{"name": name, "value": value, "url": self.base_url}]
        )
        self.page.goto(self.base_url, wait_until="domcontentloaded")
        self.page.locator(".app-shell:not(.hidden)").wait_for()
        self.page.wait_for_function(
            "document.querySelector('#account-name').textContent === 'Second Surveyor'"
        )

        self.assertTrue(self.page.locator("#auth-error").evaluate(
            "element => element.classList.contains('hidden')"
        ))
        self.assertTrue(self.page.locator("#new-movement-top").is_hidden())
        self.assertTrue(self.page.locator("#start-navigation-tour").is_hidden())
        self.assertEqual(self.browser_errors, [])

    def test_plumbing_supervisor_gets_a_plumbing_work_queue_dashboard(self):
        created = self.page.request.post(
            f"{self.base_url}/api/users",
            data={
                "username": "plumbing-team-supervisor",
                "display_name": "Plumbing Team Supervisor",
                "pin": "456789",
                "role": "supervisor",
                "supervisor_specialty": "Plumbing",
            },
        )
        self.assertEqual(created.status, 201, created.text())
        teamlead_created = self.page.request.post(
            f"{self.base_url}/api/users",
            data={
                "username": "plumbing-work-teamlead",
                "display_name": "Plumbing Teamlead",
                "pin": "456780",
                "role": "teamlead",
                "team_name": "Plumbing",
            },
        )
        self.assertEqual(teamlead_created.status, 201, teamlead_created.text())
        other_supervisor_created = self.page.request.post(
            f"{self.base_url}/api/users",
            data={
                "username": "other-plumbing-supervisor",
                "display_name": "Other Plumbing Supervisor",
                "pin": "456782",
                "role": "supervisor",
                "supervisor_specialty": "Plumbing",
            },
        )
        self.assertEqual(
            other_supervisor_created.status,
            201,
            other_supervisor_created.text(),
        )
        supervisor_id = created.json()["user"]["id"]
        teamlead_id = teamlead_created.json()["user"]["id"]
        with server.connect_db() as db:
            db.execute(
                """
                INSERT INTO provided_houses (id, owner_name, source)
                VALUES ('PLUMBING-SUPERVISOR-DASHBOARD', 'Supervisor House', 'government')
                """
            )
            server._upsert_house(
                db,
                "PLUMBING-SUPERVISOR-DASHBOARD",
                owner_name="Supervisor House",
            )
            db.execute(
                """
                INSERT INTO provided_houses
                    (id, owner_name, source, plan_plumber_required)
                VALUES ('PLUMBING-SUPERVISOR-COMPLETE', 'Completed Plumbing House',
                        'government', 1)
                """
            )
            db.execute(
                """
                INSERT INTO provided_houses
                    (id, owner_name, source, plan_plumber_required)
                VALUES ('PLUMBING-SUPERVISOR-PENDING', 'Pending Plumbing House',
                        'government', 1)
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
                VALUES (?, ?)
                """,
                (
                    "PLUMBING-SUPERVISOR-PENDING",
                    json.dumps(
                        {
                            "Application Details": {
                                "Application ID": "SEAI-PENDING-001"
                            }
                        }
                    ),
                ),
            )
            server._upsert_house(
                db,
                "PLUMBING-SUPERVISOR-COMPLETE",
                owner_name="Completed Plumbing House",
            )
            db.execute(
                """
                UPDATE provided_houses SET plan_plumber_required = 1
                WHERE id = 'PLUMBING-SUPERVISOR-DASHBOARD'
                """
            )
            db.execute(
                """
                INSERT INTO house_role_assignments
                    (house_id, role, user_id, assigned_by, assigned_at)
                VALUES ('PLUMBING-SUPERVISOR-DASHBOARD', 'supervisor', ?,
                        'Dashboard Test', '2026-11-04T09:30:00')
                """,
                (supervisor_id,),
            )
            db.execute(
                """
                INSERT INTO house_role_assignments
                    (house_id, role, user_id, assigned_by, assigned_at)
                VALUES ('PLUMBING-SUPERVISOR-COMPLETE', 'supervisor', ?,
                        'Dashboard Test', '2026-11-04T09:30:00')
                """,
                (supervisor_id,),
            )
            db.execute(
                """
                INSERT INTO house_role_assignments
                    (house_id, role, user_id, assigned_by, assigned_at)
                VALUES ('PLUMBING-SUPERVISOR-PENDING', 'supervisor', ?,
                        'Dashboard Test', '2026-11-04T09:30:00')
                """,
                (supervisor_id,),
            )
            db.execute(
                """
                INSERT INTO house_checklists (house_id, plumber_status)
                VALUES ('PLUMBING-SUPERVISOR-COMPLETE', 'done')
                """
            )
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
            pdf_content = b"%PDF-1.4\nSurvey PDF for pending house\n%%EOF"
            db.execute(
                """
                INSERT INTO house_documents
                    (house_id, filename, content, size_bytes, uploaded_by, uploaded_at)
                VALUES ('PLUMBING-SUPERVISOR-PENDING', 'survey-plan.pdf', ?, ?,
                        'Surveyor Example', '2026-11-04T10:00:00')
                """,
                (pdf_content, len(pdf_content)),
            )
            seai_pdf_content = b"%PDF-1.4\nSelected survey PDF\n%%EOF"
            seai_pdf_id = db.execute(
                """
                INSERT INTO seai_documents
                    (application_id, category, filename, extension, downloaded, content)
                VALUES ('PLUMBING-SUPERVISOR-PENDING', 'Survey',
                        'selected-survey.pdf', '.pdf', 1, ?)
                """,
                (seai_pdf_content,),
            ).lastrowid
            db.execute(
                """
                INSERT INTO selected_seai_documents
                    (document_id, house_id, created_by, created_at)
                VALUES (?, 'PLUMBING-SUPERVISOR-PENDING', 'Surveyor Example',
                        '2026-11-04T10:01:00')
                """,
                (seai_pdf_id,),
            )
            db.execute(
                """
                UPDATE houses
                SET delivery_person = 'Gary Lowther',
                    delivery_team = 'Plumbing Teamlead'
                WHERE id = 'PLUMBING-SUPERVISOR-COMPLETE'
                """
            )
            db.execute(
                """
                INSERT INTO user_reporting_lines
                    (manager_user_id, report_user_id, relationship)
                VALUES (?, ?, 'manager')
                """,
                (supervisor_id, teamlead_id),
            )
            db.execute(
                """
                INSERT INTO team_roster_members (teamlead_user_id, member_name)
                VALUES (?, 'Crew Member One')
                """,
                (teamlead_id,),
            )
            plumber_ids = [
                server._insert_user(
                    db,
                    username,
                    name,
                    "456781",
                    "plumber",
                )
                for username, name in (
                    ("danny-conroy", "Danny Conroy"),
                    ("liam-donegan", "Liam Donegan"),
                )
            ]
            db.executemany(
                """
                INSERT INTO user_reporting_lines
                    (manager_user_id, report_user_id, relationship)
                VALUES (?, ?, 'manager')
                """,
                [(teamlead_id, plumber_id) for plumber_id in plumber_ids],
            )
        login = self.page.request.post(
            f"{self.base_url}/api/auth/login",
            data={"username": "plumbing-team-supervisor", "pin": "456789"},
        )
        self.assertEqual(login.status, 200, login.text())
        cookie = login.headers["set-cookie"].split(";", 1)[0]
        name, value = cookie.split("=", 1)
        self.page.context.add_cookies(
            [{"name": name, "value": value, "url": self.base_url}]
        )
        self.page.goto(self.base_url, wait_until="domcontentloaded")
        self.page.locator(".app-shell:not(.hidden)").wait_for()
        self.page.wait_for_function(
            "document.querySelector('#account-name').textContent === "
            "'Plumbing Team Supervisor'"
        )

        self.assertEqual(
            self.page.locator("#overview-title").inner_text(),
            "My plumbing dashboard",
            self.page.locator("body").get_attribute("class"),
        )
        self.assertTrue(self.page.locator("#overview-work-summary").is_hidden())
        self.assertTrue(
            self.page.locator("#plumbing-supervisor-workload .plumbing-coordinator-greeting").is_visible()
        )
        self.assertTrue(
            self.page.locator("#plumbing-supervisor-workload").is_visible()
        )
        self.assertIn(
            "Welcome back, Plumbing Team Supervisor",
            self.page.locator("#plumbing-supervisor-greeting-title").inner_text(),
        )
        crew = self.page.locator("#plumbing-supervisor-crew")
        self.assertTrue(crew.is_visible())
        self.assertFalse(crew.evaluate("element => element.open"))
        self.assertEqual(
            self.page.locator("#plumbing-supervisor-crew-count").inner_text(),
            "1 Teamlead · 3 crew members",
        )
        self.page.locator("#plumbing-supervisor-crew-summary").click()
        self.assertTrue(crew.evaluate("element => element.open"))
        self.assertIn("Plumbing Teamlead", crew.inner_text())
        self.assertIn("Crew Member One", crew.inner_text())
        self.assertIn("Danny Conroy", crew.inner_text())
        self.assertIn("Liam Donegan", crew.inner_text())
        self.assertEqual(
            crew.locator(".plumbing-supervisor-crew-members li small").all_text_contents(),
            ["Plumber", "Plumber", "Crew member"],
        )
        self.assertTrue(self.page.locator(".main-nav.workspace-nav").is_hidden())
        filters = self.page.locator(
            "#plumbing-supervisor-house-filters [data-plumbing-supervisor-filter]"
        )
        self.assertEqual(filters.count(), 3)
        self.assertEqual(
            filters.all_text_contents(),
            ["All houses 3", "Active houses 2", "Plumbing complete 1"],
        )
        self.assertFalse(
            self.page.locator(".plumbing-supervisor-summary").count()
        )
        self.assertEqual(
            self.page.locator(
                "#plumbing-supervisor-workload-title, "
                "#plumbing-supervisor-workload-count"
            ).count(),
            0,
        )
        self.assertTrue(
            self.page.locator(
                "#plumbing-supervisor-workload .plumbing-workload-intro"
            ).is_visible()
        )
        self.page.locator(
            '#plumbing-supervisor-house-filters [data-plumbing-supervisor-filter="active"]'
        ).click()
        self.assertEqual(
            self.page.locator(
                "#plumbing-supervisor-house-list article[data-house-id]"
            ).count(),
            2,
        )
        self.assertTrue(
            self.page.locator(
                '#plumbing-supervisor-house-list article[data-house-id="PLUMBING-SUPERVISOR-PENDING"]'
            ).count()
        )
        self.assertFalse(
            self.page.locator(
                '#plumbing-supervisor-house-list article[data-house-id="PLUMBING-SUPERVISOR-COMPLETE"]'
            ).count()
        )
        self.page.locator(
            '#plumbing-supervisor-house-filters [data-plumbing-supervisor-filter="all"]'
        ).click()
        card = self.page.locator(
            '#plumbing-supervisor-house-list article[data-house-id="PLUMBING-SUPERVISOR-DASHBOARD"]'
        )
        card.wait_for()
        self.assertTrue(card.locator(".plumbing-trade-icon svg").is_visible())
        self.assertIn(
            "Assigned to you by Dashboard Test",
            card.locator(".supervisor-house-assignment-credit").inner_text(),
        )
        pending_card = self.page.locator(
            '#plumbing-supervisor-house-list article[data-house-id="PLUMBING-SUPERVISOR-PENDING"]'
        )
        self.assertEqual(
            pending_card.locator(".house-meta h2").inner_text(),
            "SEAI-PENDING-001",
        )
        self.assertIn(
            "Assigned to you by Dashboard Test",
            pending_card.locator(".supervisor-house-assignment-credit").inner_text(),
        )
        pending_pdfs = pending_card.locator(".plumbing-survey-pdfs")
        self.assertEqual(
            pending_pdfs.locator(".plumbing-survey-pdf-count").inner_text(), "2"
        )
        pending_pdfs.locator("summary").click()
        self.assertIn("survey-plan.pdf", pending_pdfs.inner_text())
        self.assertIn("selected-survey.pdf", pending_pdfs.inner_text())
        self.assertIn("Saved by Surveyor Example", pending_pdfs.inner_text())
        for pdf_name, expected_content in (
            ("survey-plan.pdf", pdf_content),
            ("selected-survey.pdf", seai_pdf_content),
        ):
            pdf_link = pending_pdfs.locator("li").filter(
                has_text=pdf_name
            ).get_by_role("link", name="View")
            pdf_url = f"{self.base_url}{pdf_link.get_attribute('href')}"
            pdf_response = self.page.request.get(pdf_url)
            self.assertEqual(pdf_response.status, 200, pdf_response.text())
            self.assertEqual(pdf_response.headers["content-type"], "application/pdf")
            self.assertIn(expected_content, pdf_response.body())
        other_supervisor_context = self.playwright.request.new_context(
            base_url=self.base_url
        )
        try:
            other_login = other_supervisor_context.post(
                "/api/auth/login",
                data={
                    "username": "other-plumbing-supervisor",
                    "pin": "456782",
                },
            )
            self.assertEqual(other_login.status, 200, other_login.text())
            for pdf_name in ("survey-plan.pdf", "selected-survey.pdf"):
                pdf_link = pending_pdfs.locator("li").filter(
                    has_text=pdf_name
                ).get_by_role("link", name="View")
                pdf_url = pdf_link.get_attribute("href")
                denied_pdf = other_supervisor_context.get(pdf_url)
                self.assertEqual(denied_pdf.status, 404, denied_pdf.text())
        finally:
            other_supervisor_context.dispose()
        self.assertFalse(
            pending_card.get_by_text("Awaiting activation", exact=True).count()
        )
        self.assertTrue(
            pending_card.get_by_text("Plumbing outstanding", exact=True).is_visible()
        )
        pending_completion = pending_card.locator(".plumbing-completion-toggle")
        self.assertEqual(
            pending_completion.get_attribute("aria-label"),
            "Mark plumbing complete for PLUMBING-SUPERVISOR-PENDING",
        )
        with self.page.expect_response("**/api/plumbing-status") as pending_response:
            pending_completion.click()
        self.assertEqual(
            pending_response.value.status, 200, pending_response.value.text()
        )
        self.page.get_by_text(
            "Plumbing marked complete for PLUMBING-SUPERVISOR-PENDING.",
            exact=True,
        ).wait_for()
        pending_card = self.page.locator(
            '#plumbing-supervisor-house-list article[data-house-id="PLUMBING-SUPERVISOR-PENDING"]'
        )
        self.assertTrue(
            pending_card.get_by_text("Plumbing complete", exact=True).is_visible()
        )
        with server.connect_db() as db:
            pending_status = db.execute(
                """
                SELECT plumber_status FROM house_checklists
                WHERE house_id = 'PLUMBING-SUPERVISOR-PENDING'
                """
            ).fetchone()
        self.assertEqual(pending_status["plumber_status"], "done")
        completion_button = card.locator(".plumbing-completion-toggle")
        self.assertEqual(
            completion_button.get_attribute("aria-label"),
            "Mark plumbing complete for PLUMBING-SUPERVISOR-DASHBOARD",
        )
        with self.page.expect_response("**/api/plumbing-status") as response_info:
            completion_button.click()
        self.assertEqual(response_info.value.status, 200, response_info.value.text())
        self.page.get_by_text(
            "Plumbing marked complete for PLUMBING-SUPERVISOR-DASHBOARD.",
            exact=True,
        ).wait_for()
        card = self.page.locator(
            '#plumbing-supervisor-house-list article[data-house-id="PLUMBING-SUPERVISOR-DASHBOARD"]'
        )
        self.assertTrue(
            card.get_by_text("Plumbing complete", exact=True).is_visible()
        )
        admin_login = self.page.request.post(
            f"{self.base_url}/api/auth/login",
            data={"username": "navigation-test-admin", "pin": "123456"},
        )
        self.assertEqual(admin_login.status, 200, admin_login.text())
        admin_cookie = admin_login.headers["set-cookie"].split(";", 1)[0]
        shared_dashboard = self.page.request.get(
            f"{self.base_url}/api/dashboard",
            headers={"Cookie": admin_cookie},
        ).json()
        shared_house = next(
            house for house in shared_dashboard["house_list"]
            if house["id"] == "PLUMBING-SUPERVISOR-DASHBOARD"
        )
        self.assertEqual(shared_house["plumber_status"], "done")
        denied_update = self.page.request.post(
            f"{self.base_url}/api/plumbing-status",
            data={
                "house_id": "PLUMBING-SUPERVISOR-DASHBOARD",
                "plumber_status": "not_done",
            },
            headers={"Cookie": admin_cookie},
        )
        self.assertEqual(denied_update.status, 403, denied_update.text())
        self.page.context.add_cookies(
            [{"name": name, "value": value, "url": self.base_url}]
        )
        completion_button = card.locator(".plumbing-completion-toggle")
        self.assertEqual(
            completion_button.get_attribute("aria-label"),
            "Reopen plumbing work for PLUMBING-SUPERVISOR-DASHBOARD",
        )
        completion_button.click()
        self.page.get_by_text(
            "Plumbing work reopened for PLUMBING-SUPERVISOR-DASHBOARD.",
            exact=True,
        ).wait_for()
        shared_dashboard = self.page.request.get(
            f"{self.base_url}/api/dashboard",
            headers={"Cookie": admin_cookie},
        ).json()
        shared_house = next(
            house for house in shared_dashboard["house_list"]
            if house["id"] == "PLUMBING-SUPERVISOR-DASHBOARD"
        )
        self.assertEqual(shared_house["plumber_status"], "not_done")
        card = self.page.locator(
            '#plumbing-supervisor-house-list article[data-house-id="PLUMBING-SUPERVISOR-DASHBOARD"]'
        )
        self.assertTrue(card.locator(".supervisor-house-assignment").is_visible())
        self.assertEqual(
            self.page.locator("#plumbing-supervisor-workload .supervisor-shared-seai").count(),
            0,
        )
        theme_toggle = self.page.locator("#theme-toggle")
        was_dark = theme_toggle.get_attribute("aria-pressed") == "true"
        if not was_dark:
            theme_toggle.click()
        self.page.wait_for_timeout(250)
        self.assertEqual(
            card.evaluate("element => getComputedStyle(element).backgroundColor"),
            "rgb(32, 44, 41)",
        )
        self.assertEqual(
            card.locator(".house-meta h2").evaluate(
                "element => getComputedStyle(element).color"
            ),
            "rgb(239, 247, 241)",
        )
        greeting = self.page.locator(
            "#plumbing-supervisor-workload .plumbing-coordinator-greeting"
        )
        self.assertEqual(
            greeting.locator("h2").evaluate(
                "element => getComputedStyle(element).color"
            ),
            "rgb(240, 247, 242)",
        )
        self.assertEqual(
            greeting.locator("p").evaluate(
                "element => getComputedStyle(element).color"
            ),
            "rgb(181, 200, 187)",
        )
        self.assertEqual(
            crew.locator(".plumbing-supervisor-crew-card").evaluate(
                "element => getComputedStyle(element).backgroundColor"
            ),
            "rgb(32, 44, 41)",
        )
        self.assertEqual(
            crew.locator(".plumbing-supervisor-crew-teamlead strong").evaluate(
                "element => getComputedStyle(element).color"
            ),
            "rgb(234, 244, 239)",
        )
        if not was_dark:
            theme_toggle.click()
        self.page.locator("#plumbing-supervisor-crew-summary").click()
        self.assertFalse(crew.evaluate("element => element.open"))
        self.assertFalse(
            crew.locator(".plumbing-supervisor-crew-card").is_visible()
        )
        completed_card = self.page.locator(
            '#plumbing-supervisor-house-list article[data-house-id="PLUMBING-SUPERVISOR-COMPLETE"]'
        )
        self.assertTrue(
            completed_card.get_by_text("Plumbing complete", exact=True).is_visible()
        )
        self.page.locator(
            '#plumbing-supervisor-house-filters [data-plumbing-supervisor-filter="plumbing_complete"]'
        ).click()
        self.assertEqual(
            self.page.locator(
                "#plumbing-supervisor-house-list article[data-house-id]"
            ).count(),
            2,
        )
        self.page.locator(
            '#plumbing-supervisor-house-filters [data-plumbing-supervisor-filter="all"]'
        ).click()
        self.assertEqual(
            self.page.locator(
                "#plumbing-supervisor-house-list article[data-house-id]"
            ).count(),
            3,
        )
        card = self.page.locator(
            '#plumbing-supervisor-house-list article[data-house-id="PLUMBING-SUPERVISOR-DASHBOARD"]'
        )
        card.locator('[name="delivery_person"]').select_option(label="Karol")
        card.locator('[name="delivery_team"]').select_option(label="Plumbing Teamlead")
        card.locator('button[type="submit"]').click()
        self.page.get_by_text(
            "Delivery assignments saved for PLUMBING-SUPERVISOR-DASHBOARD.",
            exact=True,
        ).wait_for()
        dashboard = self.page.request.get(f"{self.base_url}/api/dashboard").json()
        assigned_house = next(
            house for house in dashboard["house_list"]
            if house["id"] == "PLUMBING-SUPERVISOR-DASHBOARD"
        )
        self.assertEqual(assigned_house["delivery_person"], "Karol")
        self.assertEqual(assigned_house["delivery_team"], "Plumbing Teamlead")
        self.assertEqual(self.browser_errors, [])

    def test_plumbing_coordinator_gets_a_searchable_filtered_work_dashboard(self):
        created = self.page.request.post(
            f"{self.base_url}/api/users",
            data={
                "username": "plumbing-dashboard",
                "display_name": "Plumbing Dashboard",
                "pin": "345678",
                "role": "plumbing_coordinator",
            },
        )
        self.assertEqual(created.status, 201, created.text())
        supervisor_created = self.page.request.post(
            f"{self.base_url}/api/users",
            data={
                "username": "plumbing-team-supervisor",
                "display_name": "Plumbing Team Supervisor",
                "pin": "456789",
                "role": "supervisor",
            },
        )
        self.assertEqual(supervisor_created.status, 201, supervisor_created.text())
        surveyor_created = self.page.request.post(
            f"{self.base_url}/api/users",
            data={
                "username": "plumbing-work-surveyor",
                "display_name": "Surveyor Example",
                "pin": "456780",
                "role": "surveyor",
            },
        )
        self.assertEqual(surveyor_created.status, 201, surveyor_created.text())
        with server.connect_db() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO user_departments
                    (user_id, department_id, is_primary)
                SELECT ?, id, 0 FROM departments
                WHERE name = 'Plumbing' COLLATE NOCASE
                """,
                (supervisor_created.json()["user"]["id"],),
            )
            db.execute(
                """
                INSERT INTO provided_houses
                    (id, postcode, address, owner_name, plan_plumber_required,
                     plan_plumber_notes)
                VALUES ('PLUMBING-DASHBOARD', 'P1 1AA', '1 Pipe Road',
                        'Plumbing Owner', 1, 'Check water pressure')
                """
            )
            db.execute(
                """
                INSERT INTO house_surveys (house_id, data, notes, updated_by, updated_at)
                VALUES ('PLUMBING-DASHBOARD', '{}', '', 'Surveyor Example',
                        '2026-11-04T09:30:00')
                """
            )
            db.execute(
                """
                INSERT INTO house_role_assignments
                    (house_id, role, user_id, assigned_by, assigned_at)
                VALUES ('PLUMBING-DASHBOARD', 'surveyor', ?, 'Dashboard Test',
                        '2026-11-04T09:30:00')
                """,
                (surveyor_created.json()["user"]["id"],),
            )
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

        login = self.page.request.post(
            f"{self.base_url}/api/auth/login",
            data={"username": "plumbing-dashboard", "pin": "345678"},
        )
        self.assertEqual(login.status, 200, login.text())
        cookie = login.headers["set-cookie"].split(";", 1)[0]
        name, value = cookie.split("=", 1)
        self.page.context.add_cookies(
            [{"name": name, "value": value, "url": self.base_url}]
        )
        self.page.goto(self.base_url, wait_until="domcontentloaded")
        self.page.locator(".app-shell:not(.hidden)").wait_for()
        self.page.wait_for_function(
            "document.querySelector('#account-name').textContent === 'Plumbing Dashboard'"
        )

        self.assertEqual(
            self.page.locator("#overview-title").inner_text(),
            "My plumbing dashboard",
        )
        self.assertTrue(self.page.locator(".main-nav.workspace-nav").is_hidden())
        self.assertTrue(self.page.locator("#plumbing-coordinator-workload").is_visible())
        greeting_title = self.page.locator(
            "#plumbing-coordinator-greeting-title"
        ).inner_text()
        self.assertTrue(
            greeting_title.startswith(
                ("Good morning, ", "Good afternoon, ", "Good evening, ")
            ),
            greeting_title,
        )
        self.assertTrue(greeting_title.endswith("Plumbing Dashboard"))
        self.assertGreaterEqual(
            float(self.page.locator(
                "#plumbing-coordinator-workload .plumbing-coordinator-greeting h2"
            ).evaluate("element => parseFloat(getComputedStyle(element).fontSize)")),
            19,
        )
        self.assertTrue(
            self.page.locator(
                "#plumbing-coordinator-workload > .plumbing-coordinator-greeting "
                ".plumbing-coordinator-greeting-icon svg"
            ).is_visible()
        )
        self.assertTrue(
            self.page.locator(
                "#plumbing-coordinator-workload .plumbing-coordinator-greeting-tag"
            ).is_visible()
        )
        self.assertTrue(
            self.page.locator(
                "#plumbing-coordinator-workload .plumbing-coordinator-greeting"
            ).is_visible()
        )
        self.assertEqual(
            self.page.locator("#plumbing-coordinator-count").count(),
            0,
        )
        self.assertEqual(
            self.page.locator("#plumbing-filter-needs-assignment-count").inner_text(),
            "1",
        )
        self.assertFalse(
            self.page.locator("#plumbing-house-filters").get_by_text(
                "Not active", exact=False
            ).count()
        )
        self.assertTrue(
            self.page.locator("#plumbing-house-list").get_by_text(
                "PLUMBING-DASHBOARD", exact=True
            ).is_visible()
        )
        self.assertTrue(
            self.page.locator(
                "#plumbing-house-list .plumbing-trade-icon svg"
            ).is_visible()
        )
        self.assertEqual(
            self.page.locator(".plumbing-job-status").inner_text(),
            "Needs assignment",
        )
        self.assertEqual(
            self.page.locator("#plumbing-house-filters").get_by_text(
                "Needs assignment", exact=False
            ).count(),
            1,
        )
        self.assertTrue(
            self.page.get_by_text("Surveyor Example", exact=True).is_visible()
        )
        self.assertTrue(
            self.page.locator(
                '#plumbing-house-list input[type="radio"][name="supervisor_user_id"]'
            ).is_enabled()
        )
        card = self.page.locator("#plumbing-house-list .plumbing-house-card")
        self.assertEqual(card.count(), 1)
        layout = card.evaluate(
            """card => {
              const details = card.querySelector('.plumbing-work-detail').getBoundingClientRect();
              const assignment = card.querySelector('.plumbing-house-assignment').getBoundingClientRect();
              return {
                columns: getComputedStyle(card.parentElement).gridTemplateColumns.split(' ').length,
                detailLeft: details.left,
                assignmentLeft: assignment.left,
                assignmentTop: assignment.top
              };
            }"""
        )
        self.assertEqual(layout["columns"], 1)
        self.assertGreater(layout["assignmentLeft"], layout["detailLeft"])
        self.assertLessEqual(layout["assignmentTop"], card.locator(".ventilation-house-top").evaluate(
            "element => element.getBoundingClientRect().top + 1"
        ))
        self.assertEqual(
            self.page.locator(
                '#plumbing-house-list select[name="supervisor_user_id"]'
            ).count(),
            0,
        )
        assignment_button = self.page.locator(
            '#plumbing-house-list button[type="submit"]'
        )
        self.assertTrue(assignment_button.is_disabled())
        self.assertEqual(assignment_button.inner_text(), "Select a supervisor")
        supervisor_id = supervisor_created.json()["user"]["id"]
        self.page.locator(
            f'#plumbing-house-list input[name="supervisor_user_id"][value="{supervisor_id}"]'
        ).check()
        selected_option = self.page.locator(".plumbing-supervisor-option.selected")
        self.assertEqual(selected_option.count(), 1)
        self.assertEqual(
            selected_option.locator(".plumbing-supervisor-check").inner_text(),
            "Selected",
        )
        self.assertTrue(assignment_button.is_enabled())
        self.assertEqual(assignment_button.inner_text(), "Assign selected supervisor")
        self.page.locator("#theme-toggle").click()
        self.assertEqual(
            self.page.locator("html").get_attribute("data-theme"),
            "dark",
        )
        self.page.wait_for_function(
            """getComputedStyle(document.querySelector('.plumbing-house-card'))
                .backgroundColor === 'rgb(32, 44, 41)'"""
        )
        self.assertEqual(
            self.page.locator(".plumbing-house-card").evaluate(
                "element => getComputedStyle(element).backgroundColor"
            ),
            "rgb(32, 44, 41)",
        )
        self.assertEqual(
            self.page.locator(".plumbing-supervisor-option").evaluate(
                "element => getComputedStyle(element).backgroundColor"
            ),
            "rgb(32, 53, 42)",
        )
        assignment_button.click()
        self.page.get_by_text(
            "PLUMBING-DASHBOARD assigned to Plumbing Team Supervisor.",
            exact=True,
        ).wait_for()
        self.assertEqual(
            self.page.locator(".plumbing-job-status").inner_text(),
            "Assigned",
        )
        dashboard = self.page.request.get(f"{self.base_url}/api/dashboard").json()
        self.assertEqual(
            dashboard["plumbing_houses"][0]["supervisor_user_id"],
            supervisor_id,
        )
        assigned_card = self.page.locator("#plumbing-house-list .plumbing-house-card")
        self.assertEqual(assigned_card.locator(".plumbing-house-assignment").count(), 0)
        self.assertEqual(
            assigned_card.locator(".plumbing-assigned-supervisor strong").inner_text(),
            "Plumbing Team Supervisor",
        )
        self.assertNotIn("Assigned by", assigned_card.inner_text())
        self.assertIn("Assigned on", assigned_card.inner_text())
        self.assertNotIn("Save supervisor", assigned_card.inner_text())
        self.page.locator('[data-plumbing-filter="assigned"]').click()
        self.assertEqual(assigned_card.count(), 1)
        self.assertEqual(
            assigned_card.locator(".plumbing-house-assignment").count(),
            0,
        )
        self.page.locator('[data-plumbing-filter="all"]').click()

        self.page.locator("#plumbing-house-search").fill("no matching house")
        self.assertEqual(
            self.page.locator("#plumbing-house-empty strong").inner_text(),
            "No matching houses",
        )
        self.page.locator("#plumbing-house-search").fill("")
        self.page.locator('[data-plumbing-filter="needs_assignment"]').click()
        self.assertEqual(
            self.page.locator("#plumbing-house-empty strong").inner_text(),
            "No houses in this group",
        )
        self.page.locator('[data-plumbing-filter="all"]').click()
        with server.connect_db() as db:
            db.execute(
                """
                UPDATE house_role_assignments
                SET assigned_at = '2026-11-04T10:00:00'
                WHERE house_id = 'PLUMBING-DASHBOARD' AND role = 'supervisor'
                """
            )
            db.execute(
                """
                INSERT INTO house_checklists (house_id, plumber_status)
                VALUES ('PLUMBING-DASHBOARD', 'done')
                ON CONFLICT(house_id) DO UPDATE SET plumber_status = 'done'
                """
            )
            db.execute(
                """
                UPDATE provided_houses
                SET plan_plumber_done_by = 'Plumber Example',
                    plan_plumber_done_at = '2026-11-04T11:00:00'
                WHERE id = 'PLUMBING-DASHBOARD'
                """
            )
        self.page.locator("#refresh-plumbing-houses").click()
        self.page.locator('[data-plumbing-filter="completed"]').click()
        self.assertEqual(
            self.page.locator("#plumbing-filter-completed-count").inner_text(),
            "1",
        )
        self.assertTrue(
            self.page.get_by_text("Plumbing work completed", exact=True).is_visible()
        )
        self.assertTrue(
            self.page.locator(".plumbing-job-status.is-complete").is_visible()
        )
        self.assertEqual(
            self.page.locator(".plumbing-job-status.is-complete").inner_text(),
            "Completed",
        )
        self.assertTrue(
            self.page.get_by_text("Completed by", exact=True).is_visible()
        )
        self.assertTrue(
            self.page.get_by_text("Plumber Example", exact=True).is_visible()
        )
        self.assertTrue(
            self.page.get_by_text("Completed on", exact=True).is_visible()
        )
        self.assertTrue(
            self.page.locator(".plumbing-completion-meta time").is_visible()
        )
        self.assertEqual(
            self.page.locator(".plumbing-completion-meta time").get_attribute(
                "datetime"
            ),
            "2026-11-04T11:00:00",
        )
        self.assertTrue(
            self.page.get_by_text("Time to complete", exact=True).is_visible()
        )
        self.assertTrue(
            self.page.get_by_text("1 hour", exact=True).is_visible()
        )
        self.assertTrue(
            self.page.get_by_text("Surveyor Example", exact=True).is_visible()
        )
        completed_card = self.page.locator(
            "#plumbing-house-list .plumbing-house-card"
        )
        completed_card_text = completed_card.inner_text()
        self.assertNotIn("Assigned to", completed_card_text)
        self.assertNotIn("Assigned by", completed_card_text)
        self.assertNotIn("Assigned on", completed_card_text)
        self.page.locator('[data-plumbing-filter="all"]').click()
        self.page.wait_for_function(
            """getComputedStyle(document.querySelector('.plumbing-house-card'))
                .backgroundColor === 'rgb(32, 44, 41)'"""
        )
        self.assertEqual(
            self.page.locator(".plumbing-house-card").evaluate(
                "element => getComputedStyle(element).backgroundColor"
            ),
            "rgb(32, 44, 41)",
        )
        self.assertEqual(
            self.page.locator(
                ".plumbing-work-detail"
            ).evaluate("element => getComputedStyle(element).backgroundColor"),
            "rgb(27, 44, 35)",
        )
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.assertEqual(
            self.page.locator("#plumbing-house-list").evaluate(
                "element => getComputedStyle(element).gridTemplateColumns.split(' ').length"
            ),
            1,
        )
        self.assertLessEqual(
            self.page.locator("#plumbing-coordinator-workload").evaluate(
                "element => element.scrollWidth"
            ),
            self.page.locator("#plumbing-coordinator-workload").evaluate(
                "element => element.clientWidth"
            ),
        )
        self.page.locator("#refresh-plumbing-houses").click()
        self.assertEqual(self.browser_errors, [])
        login = self.page.request.post(
            f"{self.base_url}/api/auth/login",
            data={"username": "plumbing-team-supervisor", "pin": "456789"},
        )
        self.assertEqual(login.status, 200, login.text())
        cookie = login.headers["set-cookie"].split(";", 1)[0]
        name, value = cookie.split("=", 1)
        self.page.context.add_cookies(
            [{"name": name, "value": value, "url": self.base_url}]
        )
        self.page.goto(self.base_url, wait_until="domcontentloaded")
        self.page.wait_for_function(
            "document.querySelector('#account-name').textContent === "
            "'Plumbing Team Supervisor'"
        )
        supervisor_dashboard_response = self.page.request.get(
            f"{self.base_url}/api/dashboard"
        )
        self.assertEqual(
            supervisor_dashboard_response.status,
            200,
            supervisor_dashboard_response.text(),
        )
        supervisor_dashboard = supervisor_dashboard_response.json()
        self.assertIn(
            "PLUMBING-DASHBOARD",
            [house["id"] for house in supervisor_dashboard["house_list"]],
        )
        assigned_house = self.page.locator(
            '#plumbing-supervisor-house-list article[data-house-id="PLUMBING-DASHBOARD"]'
        )
        assigned_house.wait_for()
        self.assertFalse(assigned_house.locator(".status-pill").count())
        self.assertFalse(assigned_house.locator(".supervisor-assignment-note").count())
        self.assertFalse(
            assigned_house.get_by_text("Requested by Surveyor Example", exact=True).count()
        )
        self.assertFalse(assigned_house.locator(".supervisor-house-assignment").count())
        self.assertEqual(self.browser_errors, [])

    def test_ventilation_department_uses_surveyor_style_dashboard_without_survey_controls(self):
        pdf_document = server.fitz.open()
        pdf_document.new_page().insert_text((72, 72), "Annotatable survey")
        annotatable_pdf = pdf_document.tobytes()
        pdf_document.close()
        selected_seai_pdf_buffer = BytesIO()
        selected_seai_pdf = pdf_canvas.Canvas(
            selected_seai_pdf_buffer, pageCompression=0
        )
        selected_seai_pdf.drawString(72, 720, "selected SEAI pages")
        selected_seai_pdf.save()
        selected_seai_pdf_content = selected_seai_pdf_buffer.getvalue()
        annotated_seai_pdf_buffer = BytesIO()
        annotated_seai_pdf = pdf_canvas.Canvas(
            annotated_seai_pdf_buffer, pageCompression=0
        )
        annotated_seai_pdf.drawString(72, 720, "annotated SEAI source")
        annotated_seai_pdf.save()
        annotated_seai_pdf_content = annotated_seai_pdf_buffer.getvalue()
        with server.connect_db() as db:
            ventilation_id = db.execute(
                "SELECT id FROM departments WHERE name = 'Ventilation'"
            ).fetchone()["id"]
            db.executemany(
                """
                INSERT INTO provided_houses
                    (id, postcode, address, owner_name, ready_for_start,
                     plan_vent_required, plan_vent_notes, plan_vent_done_by,
                     plan_vent_done_at)
                VALUES (?, ?, ?, ?, 1, 1, ?, ?, ?)
                """,
                [
                    (
                        "VENT-DASHBOARD-PENDING",
                        "V1 1AA",
                        "1 Vent Road",
                        "Pending Owner",
                        "Check bedroom vents",
                        "",
                        "",
                    ),
                    (
                        "VENT-DASHBOARD-COMPLETE",
                        "V2 2BB",
                        "2 Vent Road",
                        "Complete Owner",
                        "Airflow checked",
                        "Ventilation Teammate",
                        "2026-11-03T09:00:00",
                    ),
                    (
                        "VENT-DASHBOARD-UNASSIGNED",
                        "V3 3CC",
                        "3 Vent Road",
                        "Unassigned Owner",
                        "",
                        "",
                        "",
                    ),
                ],
            )
            db.execute(
                """
                INSERT INTO house_vent_documents
                    (house_id, filename, content, size_bytes, shared_by, shared_at)
                VALUES (
                    'VENT-DASHBOARD-PENDING', 'annotated-survey.pdf',
                    ?, ?, 'Surveyor Teammate', '2026-11-03T09:00:00'
                )
                """,
                (b"%PDF-1.4\nannotated survey\n", len(b"%PDF-1.4\nannotated survey\n")),
            )
            db.execute(
                """
                INSERT INTO house_vent_documents
                    (house_id, filename, content, size_bytes, shared_by, shared_at)
                VALUES (
                    'VENT-DASHBOARD-PENDING', 'survey-plan.pdf',
                    ?, ?, 'Surveyor Teammate', '2026-11-03T09:00:00'
                )
                """,
                (annotatable_pdf, len(annotatable_pdf)),
            )
            db.execute(
                """
                INSERT INTO house_documents
                    (house_id, filename, content, size_bytes, uploaded_by, uploaded_at)
                VALUES (
                    'VENT-DASHBOARD-PENDING', 'saved-house-survey.pdf',
                    ?, ?, 'Surveyor Teammate', '2026-11-03T09:00:00'
                )
                """,
                (b"%PDF-1.4\nsaved house survey\n", len(b"%PDF-1.4\nsaved house survey\n")),
            )
            db.execute(
                """
                INSERT INTO house_documents
                    (house_id, filename, content, size_bytes, uploaded_by, uploaded_at)
                VALUES (
                    'VENT-DASHBOARD-PENDING', 'saved-house-survey-annotated.pdf',
                    ?, ?, 'Annotated by Surveyor Teammate', '2026-11-03T10:00:00'
                )
                """,
                (b"%PDF-1.4\nannotated saved house survey\n", len(b"%PDF-1.4\nannotated saved house survey\n")),
            )
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
            selected_seai_id = db.execute(
                """
                INSERT INTO seai_documents
                    (application_id, category, filename, content)
                VALUES (
                    'VENT-DASHBOARD-PENDING', 'Survey',
                    'VENT-DASHBOARD-selected-seai-pages.pdf', ?
                )
                """,
                (selected_seai_pdf_content,),
            ).lastrowid
            db.execute(
                """
                INSERT INTO selected_seai_documents
                    (document_id, house_id, created_by, created_at)
                VALUES (?, 'VENT-DASHBOARD-PENDING', 'Surveyor Teammate', '2026-11-03T10:30:00')
                """,
                (selected_seai_id,),
            )
            db.execute(
                """
                INSERT INTO seai_documents
                    (application_id, category, filename, content)
                VALUES (
                    'VENT-DASHBOARD-PENDING', 'Survey',
                    'VENT-DASHBOARD-survey-annotated.pdf', ?
                )
                """,
                (annotated_seai_pdf_content,),
            )
        created = self.page.request.post(
            f"{self.base_url}/api/users",
            data={
                "username": "ventilation-dashboard",
                "display_name": "Ventilation Dashboard",
                "pin": "234567",
                "role": "teamlead",
                "team_name": "Ventilation",
                "department_ids": [ventilation_id],
            },
        )
        self.assertEqual(created.status, 201, created.text())
        teamlead_id = created.json()["user"]["id"]
        with server.connect_db() as db:
            db.execute(
                """
                INSERT INTO team_roster_members (teamlead_user_id, member_name)
                VALUES (?, 'Nariman Ibrahimov')
                """,
                (teamlead_id,),
            )
            db.executemany(
                """
                INSERT INTO house_vent_assignments
                    (house_id, teamlead_user_id, assigned_by, assigned_at)
                VALUES (?, ?, 'Vent Coordinator', '2026-11-04T09:00:00')
                """,
                [
                    ("VENT-DASHBOARD-PENDING", teamlead_id),
                    ("VENT-DASHBOARD-COMPLETE", teamlead_id),
                ],
            )
            db.execute(
                "INSERT INTO houses (id, is_finished) VALUES ('VENT-DASHBOARD-COMPLETE', 0)"
            )

        login = self.page.request.post(
            f"{self.base_url}/api/auth/login",
            data={"username": "ventilation-dashboard", "pin": "234567"},
        )
        self.assertEqual(login.status, 200, login.text())
        cookie = login.headers["set-cookie"].split(";", 1)[0]
        name, value = cookie.split("=", 1)
        self.page.context.add_cookies(
            [{"name": name, "value": value, "url": self.base_url}]
        )
        self.page.goto(self.base_url, wait_until="domcontentloaded")
        self.page.get_by_role("heading", name="My ventilation dashboard").wait_for()

        self.assertEqual(self.active_page(), "overview")
        self.assertTrue(
            self.page.locator("body").evaluate(
                "element => element.classList.contains('ventilation-mode')"
            )
        )
        self.assertFalse(
            self.page.locator("body").evaluate(
                "element => element.classList.contains('surveyor-mode')"
            )
        )
        self.assertTrue(self.page.locator(".surveyor-brand img").is_visible())
        self.assertTrue(self.page.locator(".main-nav.workspace-nav").is_hidden())
        self.assertTrue(self.page.locator("#new-movement-top").is_hidden())
        self.assertTrue(self.page.locator("#start-navigation-tour").is_hidden())
        self.assertEqual(
            self.page.locator("#overview-eyebrow").inner_text(),
            "YOUR WORK QUEUE",
        )
        self.assertEqual(
            self.page.locator("#overview-summary-title").inner_text(),
            "Welcome, Ventilation Dashboard",
        )
        self.assertEqual(
            self.page.locator("#ventilation-dashboard-greeting-title").inner_text(),
            "Welcome back, Ventilation Dashboard",
        )
        self.assertTrue(
            self.page.locator(
                "#ventilation-workload .ventilation-dashboard-greeting"
            ).is_visible()
        )
        team_roster = self.page.locator("#ventilation-team-roster")
        self.assertTrue(team_roster.is_visible())
        self.assertFalse(team_roster.evaluate("element => element.open"))
        self.assertEqual(
            self.page.locator("#ventilation-team-roster-count").inner_text(),
            "1 Teamlead · 1 crew member",
        )
        team_roster.locator("summary").click()
        self.assertTrue(team_roster.evaluate("element => element.open"))
        self.assertIn(
            "Nariman Ibrahimov",
            self.page.locator("#ventilation-team-roster-content").inner_text(),
        )
        self.assertTrue(self.page.locator("#today-label").is_hidden())
        self.assertTrue(self.page.locator("#overview-work-summary").is_hidden())
        self.assertEqual(self.page.locator("#ventilation-dashboard-stats").count(), 0)
        self.assertEqual(
            self.page.locator("#ventilation-workload .surveyor-dashboard-stats").count(),
            0,
        )
        self.assertFalse(self.page.locator(".ventilation-summary-progress").count())
        self.assertTrue(self.page.locator("#page-overview .overview-grid").is_hidden())
        self.assertTrue(self.page.locator("#ventilation-house-filters").is_visible())
        self.assertEqual(self.page.locator("#stat-provided").text_content(), "2")
        self.assertEqual(self.page.locator("#stat-houses").text_content(), "1")
        self.assertEqual(self.page.locator("#stat-ready").text_content(), "1")
        self.assertTrue(self.page.locator("#stat-ready").is_hidden())
        self.assertEqual(self.page.locator("#stat-ready-label").inner_text(), "To do")
        self.assertFalse(
            self.page.locator(
                "#ventilation-workload .plumbing-coordinator-greeting-eyebrow, "
                "#ventilation-workload .plumbing-coordinator-greeting-tag"
            ).count()
        )
        self.assertIn(
            "sr-only",
            self.page.locator("#ventilation-workload-title").get_attribute("class").split(),
        )
        self.assertTrue(self.page.locator("#ventilation-house-search").is_visible())
        self.assertTrue(self.page.locator("#refresh-ventilation-houses").is_visible())
        self.assertEqual(
            self.page.locator(".ventilation-house-card").count(),
            2,
        )
        self.assertNotIn(
            "VENT-DASHBOARD-UNASSIGNED",
            self.page.locator("#ventilation-house-list").inner_text(),
        )
        self.page.locator("#ventilation-house-search").fill("VENT-DASHBOARD-PENDING")
        self.assertEqual(self.page.locator(".ventilation-house-card").count(), 1)
        self.page.locator("#ventilation-house-search").fill("")
        self.assertEqual(self.page.locator(".ventilation-house-card").count(), 2)
        self.page.locator("#refresh-ventilation-houses").click()
        self.assertEqual(self.page.locator(".ventilation-house-card").count(), 2)
        pending_card = self.page.locator(
            ".ventilation-house-card"
        ).filter(has_text="VENT-DASHBOARD-PENDING")
        complete_card = self.page.locator(
            ".ventilation-house-card"
        ).filter(has_text="VENT-DASHBOARD-COMPLETE")
        self.assertIn("Assigned by: Vent Coordinator", pending_card.inner_text())
        self.assertNotIn("Assigned Vent team:", pending_card.inner_text())
        shared_pdf = pending_card.locator(".ventilation-shared-pdfs")
        self.assertFalse(shared_pdf.evaluate("element => element.open"))
        complete_card.wait_for(state="visible")
        complete_card_height = complete_card.evaluate(
            "element => element.getBoundingClientRect().height"
        )
        shared_pdf.locator("summary").click()
        self.assertTrue(shared_pdf.evaluate("element => element.open"))
        shared_pdf_metrics = shared_pdf.locator(
            ".ventilation-shared-pdf-list"
        ).evaluate(
            """list => {
              const original = list.firstElementChild;
              for (let index = 0; index < 8; index += 1) {
                list.appendChild(original.cloneNode(true));
              }
              return {
                clientHeight: list.clientHeight,
                scrollHeight: list.scrollHeight,
                overflowY: getComputedStyle(list).overflowY
              };
            }"""
        )
        self.assertEqual(shared_pdf_metrics["overflowY"], "auto")
        self.assertGreater(
            shared_pdf_metrics["scrollHeight"],
            shared_pdf_metrics["clientHeight"],
        )
        self.assertAlmostEqual(
            complete_card.evaluate("element => element.getBoundingClientRect().height"),
            complete_card_height,
            delta=1,
        )
        shared_pdf.locator(".ventilation-shared-pdf-list").evaluate(
            "list => list.querySelectorAll('li').forEach((item, index) => { if (index >= 2) item.remove(); })"
        )
        self.assertTrue(
            pending_card.locator(".ventilation-house-completion").evaluate(
                "element => element.parentElement.classList.contains('ventilation-house-top')"
            )
        )
        self.assertIn("annotated-survey.pdf", pending_card.inner_text())
        self.assertIn("Shared by Surveyor Teammate", pending_card.inner_text())
        attached_pdfs_button = pending_card.locator("[data-vent-survey-pdfs]")
        self.assertIn("Combined PDFs by Surveyor", attached_pdfs_button.inner_text())
        self.assertEqual(attached_pdfs_button.locator(".vent-survey-pdfs-count").inner_text(), "1")
        with server.connect_db() as db:
            db.execute(
                """
                INSERT INTO house_documents
                    (house_id, filename, content, size_bytes, uploaded_by, uploaded_at)
                VALUES (
                    'VENT-DASHBOARD-PENDING', 'saved-after-dashboard.pdf',
                    ?, ?, 'Surveyor Teammate', '2026-11-03T11:00:00'
                )
                """,
                (b"%PDF-1.4\nsaved after dashboard\n", len(b"%PDF-1.4\nsaved after dashboard\n")),
            )
        attached_pdfs_button.click()
        attached_pdfs_dialog = self.page.locator("#vent-survey-pdfs-dialog")
        self.assertTrue(attached_pdfs_dialog.evaluate("element => element.open"))
        self.assertIn("VENT-DASHBOARD-PENDING", attached_pdfs_dialog.locator("h2").inner_text())
        self.assertIn("Combined PDFs by Surveyor", attached_pdfs_dialog.locator("h2").inner_text())
        self.assertEqual(attached_pdfs_button.locator(".vent-survey-pdfs-count").inner_text(), "1")
        attached_pdf = attached_pdfs_dialog.locator(
            ".vent-survey-pdf-entry [data-survey-room-pdf-preview]"
        )
        attached_pdf.wait_for(state="visible")
        self.assertIn("Combined PDF by Surveyor Teammate", attached_pdfs_dialog.inner_text())
        attached_pdf_response = self.page.evaluate(
            """async url => {
              const response = await fetch(url);
              return { status: response.status, content: await response.text() };
            }""",
            attached_pdf.get_attribute("data-survey-room-pdf-preview"),
        )
        self.assertEqual(attached_pdf_response["status"], 200)
        self.assertIn("selected SEAI pages", attached_pdf_response["content"])
        self.assertEqual(attached_pdf.inner_text(), "View PDF")
        self.assertEqual(
            attached_pdfs_dialog.locator(
                ".vent-survey-pdf-entry a[download]"
            ).get_attribute("href"),
            attached_pdf.get_attribute("data-survey-room-pdf-preview"),
        )
        self.assertEqual(
            attached_pdfs_dialog.locator(
                ".vent-survey-pdf-entry a[download]"
            ).get_attribute("download"),
            "VENT-DASHBOARD-selected-seai-pages.pdf",
        )
        self.assertEqual(
            attached_pdfs_dialog.locator(
                ".vent-survey-pdf-entry [data-survey-room-pdf-preview]"
            ).count(),
            1,
        )
        self.assertFalse(self.page.locator("#pdf-annotator-dialog").evaluate("dialog => dialog.open"))
        attached_pdf.click()
        preview_dialog = self.page.locator("#survey-room-pdf-preview-dialog")
        preview_dialog.wait_for(state="visible")
        iframe_url = urlsplit(
            preview_dialog.locator("iframe").get_attribute("src")
        )
        document_url = urlsplit(
            attached_pdf.get_attribute("data-survey-room-pdf-preview")
        )
        self.assertEqual(
            (iframe_url.path, iframe_url.query),
            (document_url.path, document_url.query),
        )
        self.assertIn(
            "VENT-DASHBOARD-selected-seai-pages.pdf",
            preview_dialog.locator("h2").inner_text(),
        )
        preview_dialog.get_by_role("button", name="Back to dashboard").click()
        preview_dialog.wait_for(state="hidden")
        self.assertFalse(attached_pdfs_dialog.evaluate("dialog => dialog.open"))
        annotated_seai_pdf = attached_pdfs_dialog.locator(
            '.vent-survey-pdf-entry [data-survey-room-pdf-preview][aria-label="View VENT-DASHBOARD-survey-annotated.pdf"]'
        )
        self.assertFalse(annotated_seai_pdf.count())
        self.assertFalse(attached_pdfs_dialog.locator(".vent-survey-pdf-entry").filter(has_text="saved-house-survey").count())
        self.assertFalse(attached_pdfs_dialog.evaluate("element => element.open"))
        self.assertNotIn("annotated-survey.pdf", complete_card.inner_text())
        pdf_view = pending_card.locator(
            '[data-vent-pdf-preview][aria-label="Open and annotate annotated-survey.pdf"]'
        )
        self.assertTrue(pdf_view.is_visible())
        pdf_view.click()
        self.page.locator("#pdf-annotator-dialog[open]").wait_for()
        self.assertIn(
            "annotated-survey.pdf",
            self.page.locator("#pdf-annotator-title").inner_text(),
        )
        self.assertEqual(
            self.page.locator("#pdf-annotator-icon-list .pdf-annotator-icon-card").count(),
            9,
        )
        pdf_url = pdf_view.get_attribute("data-pdf-url")
        pdf_response = self.page.evaluate(
            """async url => {
              const response = await fetch(url);
              return {
                status: response.status,
                contentType: response.headers.get("content-type"),
                content: await response.text()
              };
            }""",
            pdf_url,
        )
        self.assertEqual(pdf_response["status"], 200)
        self.assertTrue(pdf_response["contentType"].startswith("application/pdf"))
        self.assertIn("annotated survey", pdf_response["content"])
        self.page.keyboard.press("Escape")
        self.page.locator("#pdf-annotator-dialog").wait_for(state="hidden")
        self.page.set_viewport_size({"width": 390, "height": 844})
        annotate_button = pending_card.get_by_role(
            "button", name="Annotate survey-plan.pdf"
        )
        self.assertTrue(annotate_button.is_visible())
        button_layout = annotate_button.evaluate(
            """button => {
              const rect = button.getBoundingClientRect();
              const row = button.closest('li').getBoundingClientRect();
              return {
                display: getComputedStyle(button).display,
                left: rect.left,
                right: rect.right,
                rowLeft: row.left,
                rowRight: row.right
              };
            }"""
        )
        self.assertNotEqual(button_layout["display"], "none")
        self.assertGreaterEqual(button_layout["left"], button_layout["rowLeft"])
        self.assertLessEqual(button_layout["right"], button_layout["rowRight"])
        self.page.set_viewport_size({"width": 1280, "height": 720})
        pending_card.get_by_role(
            "button", name="Annotate survey-plan.pdf"
        ).click()
        self.page.locator("#pdf-annotator-dialog[open]").wait_for()
        self.page.wait_for_function(
            "document.querySelector('#pdf-annotator-image').naturalWidth > 0"
        )
        self.page.set_viewport_size({"width": 1280, "height": 900})
        canvas = self.page.locator("#pdf-annotator-canvas")
        self.page.evaluate(
            """() => {
              const card = document.querySelector('#pdf-annotator-icon-list .pdf-annotator-icon-card');
              const canvas = document.querySelector('#pdf-annotator-canvas');
              const transfer = new DataTransfer();
              card.dispatchEvent(new DragEvent('dragstart', {
                bubbles: true,
                dataTransfer: transfer
              }));
              const rect = canvas.getBoundingClientRect();
              canvas.dispatchEvent(new DragEvent('drop', {
                bubbles: true,
                dataTransfer: transfer,
                clientX: rect.left + rect.width / 2,
                clientY: rect.top + rect.height / 2
              }));
            }"""
        )
        canvas_box = canvas.bounding_box()
        self.page.mouse.move(canvas_box["x"] + 30, canvas_box["y"] + 30)
        self.page.mouse.down()
        self.page.mouse.move(canvas_box["x"] + 130, canvas_box["y"] + 90)
        self.page.mouse.up()
        save_annotated_copy = self.page.get_by_role(
            "button", name="Save annotated copy"
        )
        self.assertTrue(save_annotated_copy.is_enabled())
        save_annotated_copy.click()
        self.page.locator("#pdf-annotator-dialog").wait_for(state="hidden")
        shared_pdf = pending_card.locator(".ventilation-shared-pdfs")
        self.page.wait_for_function(
            """() => [...document.querySelectorAll('.ventilation-house-card')]
              .some(card => card.textContent.includes('survey-plan-vent-annotated.pdf'))"""
        )
        self.assertFalse(shared_pdf.evaluate("element => element.open"))
        shared_pdf.locator("summary").click()
        saved_annotation = pending_card.locator(
            ".ventilation-pdf-card.is-annotated"
        ).filter(has_text="survey-plan-vent-annotated.pdf")
        saved_annotation.wait_for()
        self.assertIn("Annotated by", saved_annotation.inner_text())
        self.assertIn("Ventilation Dashboard", saved_annotation.inner_text())
        self.assertIn("Open & annotate", saved_annotation.inner_text())
        self.assertIn("2026", saved_annotation.inner_text())
        annotated_pdf_preview = saved_annotation.locator("[data-vent-pdf-preview]")
        annotated_pdf_preview.click()
        self.page.locator("#pdf-annotator-dialog[open]").wait_for()
        self.assertIn(
            "survey-plan-vent-annotated.pdf",
            self.page.locator("#pdf-annotator-title").inner_text(),
        )
        self.assertTrue(
            self.page.locator("#pdf-annotator-icon-palette").is_visible()
        )
        self.page.locator("#pdf-annotator-close").click()
        self.assertFalse(
            self.page.locator("#pdf-annotator-dialog").evaluate(
                "element => element.open"
            )
        )
        with server.connect_db() as db:
            original = db.execute(
                """
                SELECT content FROM house_vent_documents
                WHERE house_id = 'VENT-DASHBOARD-PENDING' AND filename = 'survey-plan.pdf'
                """
            ).fetchone()
            annotated = db.execute(
                """
                SELECT content, shared_by FROM house_vent_documents
                WHERE house_id = 'VENT-DASHBOARD-PENDING'
                  AND filename = 'survey-plan-vent-annotated.pdf'
                """
            ).fetchone()
        self.assertEqual(original["content"], annotatable_pdf)
        self.assertNotEqual(annotated["content"], annotatable_pdf)
        self.assertEqual(annotated["shared_by"], "Vent annotated by Ventilation Dashboard")
        annotated_row = shared_pdf.locator("li").filter(
            has_text="survey-plan-vent-annotated.pdf"
        )
        edit_saved_pdf = annotated_row.locator("[data-vent-pdf-preview]")
        edit_saved_pdf.click()
        self.page.wait_for_function(
            "document.querySelector('#pdf-annotator-image').naturalWidth > 0"
        )
        canvas = self.page.locator("#pdf-annotator-canvas")
        canvas_box = canvas.bounding_box()
        self.page.mouse.click(
            canvas_box["x"] + canvas_box["width"] / 2,
            canvas_box["y"] + canvas_box["height"] / 2,
        )
        self.assertTrue(self.page.locator("#pdf-annotator-icon-edit").is_visible())
        self.assertEqual(
            self.page.locator("#pdf-annotator-selected-icon-name").inner_text(),
            "Window trickle vent",
        )
        self.page.locator("#pdf-annotator-icon-rotate-right").click()
        self.page.get_by_role("button", name="Save changes").click()
        self.page.locator("#pdf-annotator-dialog").wait_for(state="hidden")
        with server.connect_db() as db:
            updated_count = db.execute(
                """
                SELECT COUNT(*) FROM house_vent_documents
                WHERE house_id = 'VENT-DASHBOARD-PENDING'
                  AND filename = 'survey-plan-vent-annotated.pdf'
                """
            ).fetchone()[0]
            updated_record = db.execute(
                """
                SELECT content FROM house_vent_documents
                WHERE house_id = 'VENT-DASHBOARD-PENDING'
                  AND filename = 'survey-plan-vent-annotated.pdf'
                """
            ).fetchone()
        self.assertEqual(updated_count, 1)
        self.assertNotEqual(updated_record["content"], annotated["content"])
        self.assertEqual(
            len(
                self.page.locator("#ventilation-house-list").evaluate(
                    "element => getComputedStyle(element).gridTemplateColumns.split(' ').filter(Boolean)"
                )
            ),
            2,
        )
        ventilation_card_shape = self.page.locator(
            ".ventilation-house-card"
        ).first.evaluate(
            """card => ({
              minHeight: getComputedStyle(card).minHeight,
              borderRadius: getComputedStyle(card).borderRadius,
              padding: getComputedStyle(card).padding,
              boxShadow: getComputedStyle(card).boxShadow
            })"""
        )
        self.assertEqual(ventilation_card_shape["minHeight"], "190px")
        self.assertEqual(ventilation_card_shape["borderRadius"], "14px")
        self.assertEqual(ventilation_card_shape["padding"], "18px")
        completed_vent_card = self.page.locator(
            ".ventilation-house-card.is-complete"
        )
        ventilation_house = self.page.locator(".ventilation-house-card").filter(
            has_text="VENT-DASHBOARD-COMPLETE"
        )
        self.assertIn("Ventilation complete", ventilation_house.inner_text())
        self.assertIn("Completed by Ventilation Teammate", ventilation_house.inner_text())
        ventilation_house.get_by_role(
            "button", name="Mark Vent work outstanding"
        ).click()
        self.page.wait_for_function(
            """() => [...document.querySelectorAll('.ventilation-house-card')]
              .some(card => card.textContent.includes('VENT-DASHBOARD-COMPLETE') &&
                !card.classList.contains('is-complete'))"""
        )
        self.assertIn("Ventilation outstanding", ventilation_house.inner_text())
        ventilation_house.get_by_role(
            "button", name="Mark Vent work done"
        ).click()
        self.page.wait_for_function(
            """() => [...document.querySelectorAll('.ventilation-house-card')]
              .some(card => card.textContent.includes('VENT-DASHBOARD-COMPLETE') &&
                card.classList.contains('is-complete'))"""
        )
        self.assertIn(
            "Ventilation outstanding",
            self.page.locator(".ventilation-house-card:not(.is-complete)").inner_text(),
        )
        self.assertEqual(
            self.page.locator("#ventilation-filter-outstanding-count").inner_text(),
            "1",
        )
        self.page.locator('[data-ventilation-filter="outstanding"]').click()
        self.assertEqual(self.page.locator(".ventilation-house-card").count(), 1)
        self.assertIn(
            "VENT-DASHBOARD-PENDING",
            self.page.locator(".ventilation-house-card").inner_text(),
        )
        self.page.locator('[data-ventilation-filter="all"]').click()
        self.assertEqual(self.page.locator(".ventilation-house-card").count(), 2)
        self.page.locator('[data-ventilation-filter="complete"]').click()
        self.assertEqual(self.page.locator(".ventilation-house-card").count(), 1)
        self.assertTrue(
            self.page.locator(".ventilation-house-card.is-complete").is_visible()
        )
        self.page.locator('[data-ventilation-filter="all"]').click()
        self.assertEqual(self.page.locator(".ventilation-house-card").count(), 2)
        pending_card = self.page.locator(
            ".ventilation-house-card"
        ).filter(has_text="VENT-DASHBOARD-PENDING")
        self.assertTrue(
            pending_card.get_by_role(
                "button", name="Mark Vent work done"
            ).is_visible()
        )
        if self.page.locator("html").get_attribute("data-theme") != "dark":
            self.page.locator("#theme-toggle").click()
        dark_card_style = pending_card.evaluate(
            """card => ({
              background: getComputedStyle(card).backgroundColor,
              color: getComputedStyle(card).color,
              buttonBackground: getComputedStyle(card.querySelector('.ventilation-complete-button')).backgroundColor,
              buttonColor: getComputedStyle(card.querySelector('.ventilation-complete-button')).color,
              pdfBackground: getComputedStyle(card.querySelector('.survey-room-seai-pdf-list li')).backgroundColor
            })"""
        )
        self.assertEqual(self.page.locator("html").get_attribute("data-theme"), "dark")
        self.assertNotEqual(dark_card_style["background"], "rgba(0, 0, 0, 0)")
        self.assertNotEqual(dark_card_style["buttonBackground"], dark_card_style["background"])
        self.assertEqual(dark_card_style["buttonColor"], "rgb(255, 255, 255)")
        self.assertNotEqual(dark_card_style["pdfBackground"], "rgb(255, 255, 255)")
        if not shared_pdf.evaluate("element => element.open"):
            shared_pdf.locator("summary").click()
        self.assertIn("Vent team PDFs", shared_pdf.inner_text())
        pdf_actions = shared_pdf.locator(".ventilation-pdf-actions").first
        self.assertEqual(
            pdf_actions.evaluate("element => getComputedStyle(element).display"),
            "grid",
        )
        self.assertTrue(pdf_actions.locator("[data-vent-pdf-preview]").is_visible())
        dark_pdf_style = shared_pdf.locator(".ventilation-shared-pdf-list li").first.evaluate(
            """row => ({
              background: getComputedStyle(row).backgroundColor,
              borderColor: getComputedStyle(row).borderTopColor,
              filename: getComputedStyle(row.querySelector('.ventilation-pdf-name strong')).color,
              metadata: getComputedStyle(row.querySelector('.ventilation-pdf-name small')).color
            })"""
        )
        self.assertNotEqual(dark_pdf_style["background"], "rgb(255, 255, 255)")
        self.assertNotEqual(dark_pdf_style["filename"], dark_pdf_style["background"])
        self.assertNotEqual(dark_pdf_style["metadata"], dark_pdf_style["background"])
        attached_pdfs_button.click()
        attached_pdf = self.page.locator(
            "#vent-survey-pdfs-dialog [data-survey-room-pdf-preview]"
        )
        attached_pdf.wait_for(state="visible")
        self.assertEqual(attached_pdf.inner_text(), "View PDF")
        self.assertTrue(
            self.page.locator("#vent-survey-pdfs-dialog a[download]").is_visible()
        )
        self.assertFalse(
            self.page.locator("#pdf-annotator-dialog").evaluate(
                "dialog => dialog.open"
            )
        )
        self.page.locator("#vent-survey-pdfs-dialog").get_by_role(
            "button", name="Close attached PDFs"
        ).click()
        pending_card.get_by_role(
            "button", name="Annotate survey-plan.pdf"
        ).click()
        self.page.wait_for_function(
            "document.querySelector('#pdf-annotator-image').naturalWidth > 0"
        )
        annotator_dark_style = self.page.locator("#pdf-annotator-dialog").evaluate(
            """dialog => ({
              dialog: getComputedStyle(dialog).backgroundColor,
              header: getComputedStyle(dialog.querySelector('.pdf-annotator-header')).backgroundColor,
              toolbar: getComputedStyle(dialog.querySelector('.pdf-annotator-toolbar')).backgroundColor,
              title: getComputedStyle(dialog.querySelector('.pdf-annotator-title h2')).color,
              help: getComputedStyle(dialog.querySelector('.pdf-annotator-help')).color,
              viewport: getComputedStyle(dialog.querySelector('.pdf-annotator-viewport')).backgroundColor,
              pdfPage: getComputedStyle(dialog.querySelector('.pdf-page-surface')).backgroundColor,
              activeTool: getComputedStyle(dialog.querySelector('.pdf-tool-button.active')).backgroundColor
            })"""
        )
        self.assertNotEqual(annotator_dark_style["dialog"], "rgb(255, 255, 255)")
        self.assertNotEqual(annotator_dark_style["header"], "rgb(255, 255, 255)")
        self.assertNotEqual(annotator_dark_style["toolbar"], "rgb(255, 255, 255)")
        self.assertNotEqual(annotator_dark_style["viewport"], "rgb(255, 255, 255)")
        self.assertEqual(annotator_dark_style["pdfPage"], "rgb(255, 255, 255)")
        self.assertNotEqual(annotator_dark_style["activeTool"], "rgba(0, 0, 0, 0)")
        icon_palette = self.page.locator("#pdf-annotator-icon-palette")
        self.assertTrue(icon_palette.is_visible())
        icon_cards = icon_palette.locator(".pdf-annotator-icon-card")
        self.assertEqual(icon_cards.count(), 9)
        first_icon = icon_cards.first
        self.assertIn("Window trickle vent", first_icon.inner_text())
        first_icon.click()
        canvas = self.page.locator("#pdf-annotator-canvas")
        canvas_box = canvas.bounding_box()
        self.page.mouse.click(
            canvas_box["x"] + canvas_box["width"] / 2,
            canvas_box["y"] + canvas_box["height"] / 2,
        )
        self.assertTrue(self.page.locator("#pdf-annotator-icon-edit").is_visible())
        self.assertEqual(
            self.page.locator("#pdf-annotator-selected-icon-name").inner_text(),
            "Window trickle vent",
        )
        self.page.mouse.click(
            canvas_box["x"] + canvas_box["width"] / 2,
            canvas_box["y"] + canvas_box["height"] / 2,
        )
        self.assertGreater(
            canvas.evaluate(
                """canvas => {
                  const pixels = canvas.getContext('2d').getImageData(
                    0, 0, canvas.width, canvas.height
                  ).data;
                  let greenSelectionPixels = 0;
                  for (let index = 0; index < pixels.length; index += 4) {
                    if (pixels[index] < 150 &&
                        pixels[index + 1] > 170 &&
                        pixels[index + 2] > 120 &&
                        pixels[index + 2] < 190 &&
                        pixels[index + 3] > 0) greenSelectionPixels += 1;
                  }
                  return greenSelectionPixels;
                }"""
            ),
            0,
            "Selecting a placed icon should show its green resize outline",
        )
        self.assertTrue(self.page.locator("#pdf-annotator-save").is_enabled())
        self.page.locator("#pdf-annotator-icon-rotate-right").click()
        self.page.locator("#pdf-annotator-icon-larger").click()
        center_x = canvas_box["x"] + canvas_box["width"] / 2
        center_y = canvas_box["y"] + canvas_box["height"] / 2
        self.page.mouse.move(center_x, center_y)
        self.page.mouse.down()
        self.page.mouse.move(center_x + 45, center_y + 20)
        self.page.mouse.up()
        handle_start = self.page.evaluate(
            """canvas => {
              const rect = canvas.getBoundingClientRect();
              const width = rect.width * 0.20;
              const height = rect.height * 0.14;
              const angle = 15 * Math.PI / 180;
              const cx = rect.left + rect.width / 2 + 45;
              const cy = rect.top + rect.height / 2 + 20;
              return {
                x: cx + width / 2 * Math.cos(angle) + height / 2 * Math.sin(angle),
                y: cy + width / 2 * Math.sin(angle) - height / 2 * Math.cos(angle)
              };
            }""",
            canvas.element_handle(),
        )
        self.page.mouse.move(handle_start["x"], handle_start["y"])
        self.page.mouse.down()
        self.page.mouse.move(handle_start["x"] + 20, handle_start["y"] - 10)
        self.page.mouse.up()
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.page.locator("#pdf-annotator-dialog[open]").wait_for()
        icon_palette.wait_for(state="visible")
        self.page.wait_for_function(
            """() => {
              const rect = document.querySelector(
                '#pdf-annotator-icon-palette'
              ).getBoundingClientRect();
              return rect.width > 0 && rect.height > 0;
            }"""
        )
        palette_bounds = icon_palette.evaluate(
            """element => {
              const rect = element.getBoundingClientRect();
              return {left: rect.left, right: rect.right};
            }"""
        )
        self.assertGreaterEqual(palette_bounds["left"], 0)
        self.assertLessEqual(palette_bounds["right"], 390)
        self.assertTrue(icon_cards.first.is_visible())
        self.page.set_viewport_size({"width": 1280, "height": 720})
        self.page.locator("#pdf-annotator-close").click()
        self.page.locator("#pdf-annotator-dialog").wait_for(state="hidden")
        pending_card.get_by_role(
            "button", name="Mark Vent work done"
        ).click()
        self.page.wait_for_function(
            """() => [...document.querySelectorAll('.ventilation-house-card')]
              .some(card => card.textContent.includes('VENT-DASHBOARD-PENDING') &&
                card.classList.contains('is-complete'))"""
        )
        self.assertIn("Ventilation Dashboard", pending_card.inner_text())
        self.assertTrue(pending_card.locator(".ventilation-completion-mark.is-done").is_visible())
        undo_button_style = pending_card.locator(
            ".ventilation-complete-button.is-undo"
        ).evaluate(
            """button => ({
              background: getComputedStyle(button).backgroundColor,
              borderStyle: getComputedStyle(button).borderTopStyle,
              borderColor: getComputedStyle(button).borderTopColor,
              color: getComputedStyle(button).color
            })"""
        )
        self.assertEqual(undo_button_style["background"], "rgba(0, 0, 0, 0)")
        self.assertEqual(undo_button_style["borderStyle"], "solid")
        self.assertNotEqual(undo_button_style["borderColor"], "rgba(0, 0, 0, 0)")
        self.assertNotEqual(undo_button_style["color"], "rgb(255, 255, 255)")
        self.assertEqual(
            pending_card.locator("[data-ventilation-complete]").get_attribute(
                "data-done"
            ),
            "false",
        )
        pending_card.get_by_role(
            "button", name="Mark Vent work outstanding"
        ).click()
        self.page.wait_for_function(
            """() => [...document.querySelectorAll('.ventilation-house-card')]
              .some(card => card.textContent.includes('VENT-DASHBOARD-PENDING') &&
                !card.classList.contains('is-complete'))"""
        )
        self.assertIn("Ventilation outstanding", pending_card.inner_text())
        self.assertNotIn("Completed by Ventilation Dashboard", pending_card.inner_text())
        self.assertFalse(pending_card.locator(".ventilation-completion-mark.is-done").count())
        self.assertTrue(
            pending_card.get_by_role(
                "button", name="Mark Vent work done"
            ).is_visible()
        )
        self.assertEqual(
            self.page.locator("#page-overview [data-goto*='survey']").count(),
            0,
        )
        self.assertEqual(self.browser_errors, [])

    def test_cross_user_updates_refresh_the_current_view_and_keep_other_views_stale(self):
        self.page.wait_for_function(
            "document.querySelector('#stat-ready').textContent !== '—'"
        )
        ready_count = self.page.locator("#stat-ready").inner_text()
        self.page.evaluate(
            """() => {
                const originalFetch = window.fetch.bind(window);
                window.__dashboardRequests = 0;
                window.fetch = (input, options) => {
                    if (String(input).startsWith("/api/dashboard")) {
                        window.__dashboardRequests += 1;
                    }
                    return originalFetch(input, options);
                };
            }"""
        )
        self.page.locator(".nav-item[data-tab='inventory']").click()
        self.assertEqual(self.active_page(), "inventory")

        with server.connect_db() as db:
            db.execute(
                """
                INSERT INTO provided_houses(id, postcode, owner_name)
                VALUES ('CROSS-USER-UPDATE-1', 'CU1', 'Cross User Owner')
                """
            )
            server._insert_user(
                db, "cross-user-admin", "Cross User Admin", "654321", "admin"
            )

        connection = http.client.HTTPConnection(
            "127.0.0.1", self.httpd.server_address[1], timeout=5
        )
        connection.request(
            "POST",
            "/api/auth/login",
            body=json.dumps({"username": "cross-user-admin", "pin": "654321"}),
            headers={"Content-Type": "application/json"},
        )
        login_response = connection.getresponse()
        login_response.read()
        self.assertEqual(login_response.status, 200)
        other_user_cookie = login_response.getheader("Set-Cookie").split(";", 1)[0]
        connection.request(
            "POST",
            "/api/houses/plan",
            body=json.dumps({"id": "CROSS-USER-UPDATE-1", "ready": True}),
            headers={
                "Content-Type": "application/json",
                "Cookie": other_user_cookie,
            },
        )
        update_response = connection.getresponse()
        update_response.read()
        connection.close()
        self.assertEqual(update_response.status, 200)

        self.page.evaluate("pollTrackerChanges()")
        self.assertEqual(self.active_page(), "inventory")
        self.assertEqual(self.page.locator("#stat-ready").inner_text(), ready_count)
        self.assertEqual(
            self.page.evaluate("window.__dashboardRequests"),
            1,
        )

        self.page.locator(".nav-item[data-tab='overview']").click()
        self.page.wait_for_function(
            f"document.querySelector('#stat-ready').textContent !== {json.dumps(ready_count)}"
        )
        self.assertEqual(
            self.page.evaluate("window.__dashboardRequests"),
            1,
        )
        self.assertEqual(self.browser_errors, [])

        self.page.locator("#account-button").click()
        self.page.locator("#account-manage-users").click()
        self.page.locator("#users-dialog[open]").wait_for()
        other_connection = http.client.HTTPConnection(
            "127.0.0.1", self.httpd.server_address[1], timeout=5
        )
        other_connection.request(
            "POST",
            "/api/users",
            body=json.dumps(
                {
                    "username": "cross-user-surveyor",
                    "display_name": "Cross User Surveyor",
                    "pin": "345678",
                    "role": "surveyor",
                }
            ),
            headers={
                "Content-Type": "application/json",
                "Cookie": other_user_cookie,
            },
        )
        user_response = other_connection.getresponse()
        user_response.read()
        other_connection.close()
        self.assertEqual(user_response.status, 201)
        self.page.evaluate("pollTrackerChanges()")
        self.page.get_by_text("cross-user-surveyor", exact=True).wait_for()

    def test_surveyor_gets_a_focused_assigned_houses_dashboard(self):
        with server.connect_db() as db:
            surveyor_id = server._insert_user(
                db, "dashboard-surveyor", "Dashboard Surveyor", "345678", "surveyor"
            )
            db.execute(
                """
                INSERT INTO provided_houses(id, postcode, address, owner_name, ready_for_start)
                VALUES ('DASHBOARD-1', 'T1', '1 Elm Road', 'Survey Owner', 1)
                """
            )
            db.execute(
                """
                INSERT INTO house_role_assignments(house_id, role, user_id, assigned_by, assigned_at)
                VALUES ('DASHBOARD-1', 'surveyor', ?, 'Test Admin', '2026-10-29T10:00:00')
                """,
                (surveyor_id,),
            )
            db.execute(
                """
                INSERT INTO provided_houses(id, postcode, address, owner_name, ready_for_start)
                VALUES ('DASHBOARD-2', 'T2', '2 Oak Road', 'Second Owner', 1)
                """
            )
            db.execute(
                """
                INSERT INTO house_role_assignments(house_id, role, user_id, assigned_by, assigned_at)
                VALUES ('DASHBOARD-2', 'surveyor', ?, 'Test Admin', '2026-10-29T10:00:00')
                """,
                (surveyor_id,),
            )
            db.execute(
                """
                INSERT INTO house_surveys(house_id, data, notes, updated_by, updated_at)
                VALUES ('DASHBOARD-2', ?, '', 'Dashboard Surveyor', '2026-10-29T10:30:00')
                """,
                (json.dumps({"windows": [], "doors": [], "house_corners": [], "perimeter_m": 20}),),
            )

        self.page.locator("#account-button").click()
        self.page.locator("#account-sign-out").click()
        self.page.locator("#auth-screen:not(.hidden)").wait_for()
        self.page.locator("#auth-username").fill("dashboard-surveyor")
        self.page.locator("#auth-pin").fill("345678")
        self.page.locator("#auth-submit").click()

        self.page.get_by_role("heading", name="My survey dashboard").wait_for()
        self.assertTrue(self.page.locator(".main-nav.workspace-nav").is_hidden())
        self.assertTrue(self.page.locator('.nav-item[data-tab="houses"]').is_hidden())
        self.assertTrue(self.page.locator("#houses-teams-tabs").is_hidden())
        self.assertTrue(self.page.locator("#teams-section").is_hidden())
        self.assertTrue(self.page.locator("#houses-section").is_visible())
        self.page.locator("#surveyor-dashboard-summary").wait_for(state="visible")
        logo = self.page.locator(".surveyor-brand img")
        self.assertTrue(logo.is_visible())
        self.assertGreater(logo.evaluate("(image) => image.naturalWidth"), 0)
        self.assertEqual(self.page.locator(".surveyor-brand .surveyor-cat-eye").count(), 2)
        mouse = self.page.locator(".surveyor-brand .surveyor-logo-mouse")
        self.assertEqual(mouse.get_attribute("aria-hidden"), "true")
        self.assertFalse(self.page.locator(".surveyor-brand").evaluate(
            "(link) => link.classList.contains('has-new-information')"
        ))
        animation_names = self.page.locator(".surveyor-brand").evaluate(
            """link => ({
              eyes: getComputedStyle(link.querySelector(".surveyor-cat-eye")).animationName,
              mouse: getComputedStyle(link.querySelector(".surveyor-logo-mouse")).animationName,
              mouseIterationCount: getComputedStyle(link.querySelector(".surveyor-logo-mouse")).animationIterationCount,
              mouseFontSize: getComputedStyle(link.querySelector(".surveyor-logo-mouse")).fontSize,
              mouseBackground: getComputedStyle(link.querySelector(".surveyor-logo-mouse")).backgroundColor
            })"""
        )
        self.assertEqual(animation_names["eyes"], "none")
        self.assertEqual(animation_names["mouse"], "none")
        self.assertEqual(animation_names["mouseFontSize"], "19px")
        self.assertEqual(animation_names["mouseBackground"], "rgb(255, 255, 255)")
        self.assertIn("Welcome, Dashboard Surveyor", self.page.locator("#surveyor-dashboard-greeting").inner_text())
        self.assertEqual(self.page.locator("#surveyor-stat-assigned").inner_text(), "2")
        self.assertEqual(self.page.locator("#surveyor-stat-needed").inner_text(), "1")
        self.assertEqual(self.page.locator("#surveyor-stat-complete").inner_text(), "1")
        self.assertTrue(
            self.page.evaluate(
                """() => surveyHouses.find(house => house.id === "DASHBOARD-2")
                  ?.survey_completed"""
            )
        )
        self.assertTrue(
            self.page.evaluate(
                """() => surveyIsComplete(
                  surveyHouses.find(house => house.id === "DASHBOARD-2")
                )"""
            )
        )
        self.assertTrue(self.page.locator(".nav-item[data-tab='overview']").is_hidden())
        self.page.evaluate(
            """() => {
              window.surveyorMouseAnimationStarts = 0;
              document.querySelector(".surveyor-brand .surveyor-logo-mouse")
                .addEventListener("animationstart", () => window.surveyorMouseAnimationStarts += 1);
            }"""
        )
        self.page.evaluate("loadSurveyorHouses()")
        self.assertFalse(self.page.locator(".surveyor-brand").evaluate(
            "(link) => link.classList.contains('has-new-information')"
        ))
        with server.connect_db() as db:
            db.execute(
                "UPDATE house_surveys SET data = ? WHERE house_id = 'DASHBOARD-2'",
                (json.dumps({"windows": [{"w": 1.2, "h": 1.1, "qty": 2}], "doors": [], "house_corners": [], "perimeter_m": 20}),),
            )
        self.page.evaluate("loadSurveyorHouses()")
        self.assertTrue(self.page.locator(".surveyor-brand").evaluate(
            "(link) => link.classList.contains('has-new-information')"
        ))
        triggered_animation = self.page.locator(".surveyor-brand").evaluate(
            """link => ({
              eyes: getComputedStyle(link.querySelector(".surveyor-cat-eye")).animationName,
              mouse: getComputedStyle(link.querySelector(".surveyor-logo-mouse")).animationName,
              iterations: getComputedStyle(link.querySelector(".surveyor-logo-mouse")).animationIterationCount
            })"""
        )
        self.assertEqual(triggered_animation["eyes"], "surveyor-cat-look")
        self.assertEqual(triggered_animation["mouse"], "surveyor-mouse-run")
        self.assertEqual(triggered_animation["iterations"], "1")
        self.page.wait_for_function("window.surveyorMouseAnimationStarts === 1")
        with server.connect_db() as db:
            db.execute(
                "UPDATE house_surveys SET data = ? WHERE house_id = 'DASHBOARD-2'",
                (json.dumps({"windows": [{"w": 1.6, "h": 1.5, "qty": 1}], "doors": [], "house_corners": [], "perimeter_m": 20}),),
            )
        self.page.evaluate("loadSurveyorHouses()")
        self.page.wait_for_function("window.surveyorMouseAnimationStarts === 2")
        self.assertEqual(
            self.page.locator(".surveyor-brand .surveyor-logo-mouse").evaluate(
                "(mouse) => getComputedStyle(mouse).animationIterationCount"
            ),
            "1",
        )
        self.page.reload()
        self.page.get_by_role("heading", name="My survey dashboard").wait_for()
        self.page.locator("#houses-list article[data-house-id='DASHBOARD-1']").wait_for()
        assignment_credit = self.page.locator(
            "#houses-list article[data-house-id='DASHBOARD-1'] "
            ".surveyor-house-assignment-credit"
        )
        self.assertIn("Assigned to you by Test Admin", assignment_credit.inner_text())
        assignment_time = assignment_credit.locator("time")
        self.assertEqual(assignment_time.get_attribute("datetime"), "2026-10-29T10:00:00")
        self.assertIn("29 Oct 2026", assignment_time.inner_text())
        self.assertIn("10:00", assignment_time.inner_text())
        self.assertEqual(
            self.page.locator(
                "#houses-list article[data-house-id='DASHBOARD-2'] "
                ".surveyor-house-assignment-credit"
            ).count(),
            1,
        )
        self.assertFalse(self.page.locator(".surveyor-brand").evaluate(
            "(link) => link.classList.contains('has-new-information')"
        ))
        with server.connect_db() as db:
            db.execute(
                "UPDATE house_surveys SET data = ? WHERE house_id = 'DASHBOARD-2'",
                (json.dumps({"windows": [{"w": 1.4, "h": 1.3, "qty": 1}], "doors": [], "house_corners": [], "perimeter_m": 20}),),
            )
        self.page.reload()
        self.page.get_by_role("heading", name="My survey dashboard").wait_for()
        self.page.locator("#houses-list article[data-house-id='DASHBOARD-1']").wait_for()
        self.assertTrue(self.page.locator(".surveyor-brand").evaluate(
            "(link) => link.classList.contains('has-new-information')"
        ))
        self.page.set_viewport_size({"width": 390, "height": 844})
        card = self.page.locator("#houses-list article[data-house-id='DASHBOARD-1']")
        self.assertTrue(card.is_visible())
        self.assertEqual(card.locator(".surveyor-house-details").count(), 0)
        insulation = card.locator(".house-insulation-summary")
        self.assertTrue(insulation.is_visible())
        self.assertIn("No verified SEAI insulation quantities are available", insulation.inner_text())
        self.assertNotIn("DEMO SAMPLE", insulation.inner_text())
        self.assertEqual(insulation.locator(".house-insulation-row").count(), 4)
        self.assertEqual(
            " ".join(card.locator(".surveyor-house-action").inner_text().split()),
            "Start survey · View your room →",
        )
        self.assertEqual(
            card.locator(".surveyor-house-action").get_attribute("aria-label"),
            "Start survey and view your room",
        )
        button_layout = card.evaluate(
            """element => {
              const button = element.querySelector(".surveyor-house-action").getBoundingClientRect();
              const bounds = element.getBoundingClientRect();
              const details = element.querySelector(".house-insulation-summary").getBoundingClientRect();
              return {
                buttonWidth: button.width,
                buttonBottom: button.bottom,
                cardBottom: bounds.bottom,
                detailsTop: details.top,
                viewportHeight: window.innerHeight
              };
            }"""
        )
        self.assertGreater(button_layout["buttonWidth"], 0)
        self.assertLessEqual(button_layout["buttonBottom"], button_layout["cardBottom"])
        self.assertLess(button_layout["buttonBottom"], button_layout["viewportHeight"])
        self.assertLess(button_layout["buttonBottom"], button_layout["detailsTop"])
        self.assertEqual(
            " ".join(self.page.locator("#houses-list article[data-house-id='DASHBOARD-2'] .surveyor-house-action").inner_text().split()),
            "Review survey · View your room →",
        )
        completed_card = self.page.locator("#houses-list article[data-house-id='DASHBOARD-2']")
        self.assertTrue(completed_card.evaluate("(element) => element.classList.contains('is-surveyed')"))
        completed_badge = completed_card.locator(".status-pill.survey-complete")
        self.assertEqual(completed_badge.inner_text(), "Survey completed · 29 Oct 2026")
        self.assertIn("Survey saved by Dashboard Surveyor", completed_card.locator(".surveyor-house-update").inner_text())
        light_badge_colors = completed_badge.evaluate(
            """badge => ({
              background: getComputedStyle(badge).backgroundColor,
              color: getComputedStyle(badge).color
            })"""
        )
        self.assertEqual(light_badge_colors["background"], "rgb(231, 246, 236)")
        self.assertEqual(light_badge_colors["color"], "rgb(23, 107, 58)")
        self.page.locator("#theme-toggle").click()
        completed_colors = completed_card.evaluate(
            """card => ({
              cardBorder: getComputedStyle(card).borderTopColor,
              badgeBackground: getComputedStyle(card.querySelector(".status-pill.survey-complete")).backgroundColor,
              badgeColor: getComputedStyle(card.querySelector(".status-pill.survey-complete")).color,
              updateColor: getComputedStyle(card.querySelector(".surveyor-house-update")).color
            })"""
        )
        self.assertEqual(completed_colors["badgeBackground"], "rgb(32, 61, 42)")
        self.assertEqual(completed_colors["badgeColor"], "rgb(185, 239, 198)")
        self.page.locator("[data-surveyor-filter='completed']").click()
        self.assertTrue(self.page.locator("#houses-list article[data-house-id='DASHBOARD-2']").is_visible())
        self.assertFalse(card.is_visible())
        self.page.locator("[data-surveyor-filter='all']").click()
        requests = []
        self.page.on("request", lambda request: requests.append(request.url))
        self.page.evaluate("loadSurveyorHouses()")
        card.wait_for(state="visible")
        self.assertTrue(any("/api/calculator/houses?status=ready" in url for url in requests))
        self.assertFalse(any("/api/dashboard" in url or "/api/movements" in url for url in requests))
        self.assertNotIn("Live stock refresh failed", self.page.locator("#toast").inner_text())
        self.page.locator("#refresh-surveyor-houses").click()
        card.wait_for(state="visible")
        self.page.locator(".surveyor-house-action").first.click()
        self.page.locator("#surveyor-room:not(.hidden)").wait_for()
        self.assertTrue(self.page.locator(".survey-room-nav a[href='#survey-room-work-plan']").is_visible())
        self.assertTrue(self.page.locator("#surveyor-work-plan-form").is_visible())
        self.assertTrue(self.page.locator("#house-survey-form").is_visible())
        self.assertTrue(self.page.locator("#go-overview").is_hidden())
        self.assertEqual(self.page.locator("#survey-room-notes").count(), 0)
        self.assertEqual(
            self.page.locator(".survey-room-nav a[href='#survey-room-notes']").count(),
            0,
        )
        dark_styles = self.page.locator("#surveyor-room").evaluate(
            """root => Object.fromEntries([
              ['nav', '.survey-room-nav'],
              ['navLink', '.survey-room-nav a'],
              ['houseFact', '.survey-room-house-notes'],
              ['workPlanCard', '.survey-room-work-plan-card'],
              ['measurementHint', '.survey-room-form > .helper-text']
            ].map(([key, selector]) => {
              const style = getComputedStyle(root.querySelector(selector));
              return [key, {background: style.backgroundColor, color: style.color}];
            }))"""
        )
        self.assertEqual(dark_styles["navLink"]["background"], "rgb(38, 53, 47)")
        self.assertEqual(dark_styles["houseFact"]["background"], "rgb(25, 38, 34)")
        self.assertEqual(dark_styles["workPlanCard"]["background"], "rgb(28, 41, 37)")
        self.assertEqual(dark_styles["measurementHint"]["background"], "rgb(25, 42, 32)")
        room_buttons = self.page.locator("#surveyor-room").evaluate(
            """root => Object.fromEntries([
              ['addRow', '.survey-add-row'],
              ['saveSurvey', '.survey-room-form .dialog-actions > .button-primary']
            ].map(([key, selector]) => {
              const button = root.querySelector(selector);
              const style = getComputedStyle(button);
              return [key, {
                height: button.getBoundingClientRect().height,
                background: style.backgroundColor,
                color: style.color
              }];
            }))"""
        )
        self.assertGreaterEqual(room_buttons["addRow"]["height"], 40)
        self.assertGreaterEqual(room_buttons["saveSurvey"]["height"], 44)
        self.assertEqual(room_buttons["addRow"]["background"], "rgb(32, 55, 42)")
        self.assertEqual(room_buttons["saveSurvey"]["background"], "rgb(35, 133, 91)")
        self.page.set_viewport_size({"width": 390, "height": 844})
        room_layout = self.page.locator("#surveyor-room").evaluate(
            """root => {
              const nav = getComputedStyle(root.querySelector(".survey-room-nav"));
              const summary = getComputedStyle(root.querySelector(".survey-room-summary"));
              const topbar = getComputedStyle(document.querySelector("body.surveyor-mode .topbar"));
              const link = root.querySelector(".survey-room-nav a").getBoundingClientRect();
              return {
                navWrap: nav.flexWrap,
                navOverflow: nav.overflowX,
                navLinkHeight: link.height,
                summaryColumns: summary.gridTemplateColumns,
                topbarPadding: topbar.paddingTop,
                topbarHeight: topbar.height
              };
            }"""
        )
        self.assertEqual(room_layout["navWrap"], "nowrap")
        self.assertEqual(room_layout["navOverflow"], "auto")
        self.assertGreaterEqual(room_layout["navLinkHeight"], 40)
        self.assertEqual(room_layout["topbarPadding"], "8px")
        self.assertEqual(room_layout["topbarHeight"], "64px")
        self.assertLessEqual(
            self.page.locator("body").evaluate("(element) => element.scrollWidth"),
            self.page.evaluate("window.innerWidth"),
        )
        self.assertFalse(self.browser_errors, self.browser_errors)

    def test_assign_user_sees_house_dashboard_and_status_filters(self):
        with server.connect_db() as db:
            db.execute(
                """
                INSERT INTO provided_houses(
                    id, owner_name, ready_for_start, plan_plumber_required
                )
                VALUES ('READY-ASSIGN', 'Ready Owner', 1, 1)
                """
            )
            db.execute(
                """
                INSERT INTO house_checklists(house_id, plumber_status)
                VALUES ('READY-ASSIGN', 'done')
                """
            )
            db.execute(
                """
                INSERT INTO provided_houses(id, owner_name, ready_for_start)
                VALUES ('PENDING-ASSIGN', 'Pending Owner', 0)
                """
            )
            db.execute(
                """
                INSERT INTO provided_houses(
                    id, owner_name, ready_for_start,
                    plan_scaffolding_required, plan_esb_required,
                    plan_plumber_required, plan_vent_required,
                    plan_plumber_done_by, plan_plumber_done_at,
                    plan_vent_done_by, plan_vent_done_at
                )
                VALUES (
                    'ACTIVE-ASSIGN', 'Active Owner', 0, 1, 1, 1, 1,
                    'Plumber Browser', '2026-11-08T09:00:00',
                    'Vent Browser', '2026-11-08T09:30:00'
                )
                """
            )
            db.execute(
                """
                INSERT INTO houses(id, owner_name, employee_name, is_finished)
                VALUES ('ACTIVE-ASSIGN', 'Active Owner', 'Assign Browser', 0)
                """
            )
            db.execute(
                """
                INSERT INTO house_checklists
                    (house_id, scaffolding_status, esb_unclipped, plumber_status)
                VALUES ('ACTIVE-ASSIGN', 'not_done', 'no', 'done')
                """
            )
            db.execute(
                """
                INSERT INTO provided_houses(id, owner_name, ready_for_start)
                VALUES ('CLOSED-ASSIGN', 'Closed Owner', 0)
                """
            )
            db.execute(
                """
                INSERT INTO houses(id, owner_name, employee_name, is_finished)
                VALUES ('CLOSED-ASSIGN', 'Closed Owner', 'Assign Browser', 1)
                """
            )
            db.execute(
                "CREATE TABLE seai_applications "
                "(application_id TEXT PRIMARY KEY, detail_data_json TEXT NOT NULL)"
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
            db.execute(
                """
                INSERT INTO seai_applications(application_id, detail_data_json)
                VALUES (?, ?)
                """,
                (
                    "ACTIVE-ASSIGN",
                    json.dumps(
                        {
                            "Application": [
                                {
                                    "Application ID": "ACTIVE-ASSIGN",
                                    "Portal owner": "SEAI Owner",
                                }
                            ],
                            "Measures": [
                                {
                                    "Name": "External Wall Insulation",
                                    "Survey Quantity": "120",
                                    "Survey Qty including Variation": "135",
                                }
                            ],
                        }
                    ),
                ),
            )
            for category, filename in (
                ("application", "original-application.pdf"),
                ("Variation", "original-variation.pdf"),
            ):
                db.execute(
                    """
                    INSERT INTO seai_documents
                        (application_id, category, filename, extension, downloaded, content)
                    VALUES (?, ?, ?, '.pdf', 1, ?)
                    """,
                    (
                        "ACTIVE-ASSIGN",
                        category,
                        filename,
                        b"%PDF-1.4\nOriginal SEAI PDF\n%%EOF",
                    ),
                )
            selected_document_id = db.execute(
                """
                INSERT INTO seai_documents
                    (application_id, category, filename, extension, downloaded, content)
                VALUES (?, 'Survey', ?, '.pdf', 1, ?)
                """,
                (
                    "ACTIVE-ASSIGN",
                    "selected-survey-pages.pdf",
                    b"%PDF-1.4\nSelected survey pages\n%%EOF",
                ),
            ).lastrowid
            db.execute(
                """
                INSERT INTO selected_seai_documents(document_id, house_id, created_by, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    selected_document_id,
                    "ACTIVE-ASSIGN",
                    "Surveyor Browser",
                    "2026-11-05T13:30:00",
                ),
            )
            db.execute(
                """
                INSERT INTO house_documents
                    (house_id, filename, content, size_bytes, uploaded_by, uploaded_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "ACTIVE-ASSIGN",
                    "survey-plan-annotated.pdf",
                    b"%PDF-1.4\nAnnotated survey plan\n%%EOF",
                    len(b"%PDF-1.4\nAnnotated survey plan\n%%EOF"),
                    "Surveyor Browser",
                    "2026-11-05T13:30:00",
                ),
            )
            db.execute(
                """
                INSERT INTO seai_measure_variation_history
                    (house_id, measure_index, measure_name, survey_quantity,
                     previous_variation, variation, changed_by,
                     changed_by_username, changed_at)
                VALUES (?, 0, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "ACTIVE-ASSIGN",
                    "External Wall Insulation",
                    "120",
                    "135",
                    "142.5",
                    "Variation Surveyor",
                    "variation-surveyor",
                    "2026-11-05T13:30:00",
                ),
            )

        result = self.page.evaluate(
            """async () => {
              const response = await fetch('/api/users', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                  username: 'assign-dashboard',
                  display_name: 'Assign Dashboard',
                  pin: '234567',
                  role: 'assign',
                }),
              });
              return {status: response.status, body: await response.json()};
            }"""
        )
        self.assertEqual(result["status"], 201, result)
        admin_record_access = self.page.evaluate(
            """async () => (await fetch(
              '/api/assign/seai-record/ACTIVE-ASSIGN'
            )).status"""
        )
        self.assertEqual(admin_record_access, 200)
        self.page.locator("#account-button").click()
        self.page.locator("#account-sign-out").click()
        self.page.locator("#auth-screen:not(.hidden)").wait_for()
        self.page.locator("#auth-username").fill("assign-dashboard")
        self.page.locator("#auth-pin").fill("234567")
        self.page.locator("#auth-submit").click()

        dashboard = self.page.locator("#assign-houses-dashboard")
        dashboard.wait_for(state="visible")
        self.page.locator("[data-assign-house-filter='ready']").click()
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator("#assign-houses-dashboard").wait_for(state="visible")
        self.page.wait_for_function(
            """() => document.querySelector(
              "#assign-houses-dashboard [data-assign-house-filter='ready']"
            )?.getAttribute("aria-pressed") === "true" """
        )
        self.page.locator("[data-assign-house-filter='active']").click()
        self.assertTrue(self.page.locator(".main-nav.workspace-nav").is_hidden())
        self.assertTrue(self.page.locator("#houses-teams-tabs").is_hidden())
        self.assertTrue(self.page.locator("#teams-section").is_hidden())
        self.assertTrue(self.page.locator("#houses-section").is_visible())
        self.assertTrue(self.page.locator("#house-team-filter").is_hidden())
        self.assertEqual(
            self.page.locator("#houses-page-title").inner_text(),
            "Welcome, Assign Dashboard",
        )
        weather_panel = self.page.locator("#assign-weather-panel")
        self.assertTrue(weather_panel.is_visible())
        self.page.wait_for_function(
            "() => document.querySelector('#assign-weather-status').textContent.includes('1-day forecast')"
        )
        self.assertFalse(weather_panel.evaluate("panel => panel.open"))
        weather_panel.locator("summary").click()
        self.assertTrue(weather_panel.locator(".weather-area").first.is_visible())
        self.assertIn("County Mayo", weather_panel.inner_text())
        heading_style = self.page.locator("#houses-list-heading").evaluate(
            """element => ({
              family: getComputedStyle(element).fontFamily,
              size: getComputedStyle(element).fontSize,
              weight: getComputedStyle(element).fontWeight,
              color: getComputedStyle(element).color,
              statusColor: getComputedStyle(
                document.querySelector("[data-assign-house-filter='active']")
              ).borderLeftColor
            })"""
        )
        self.assertIn("Manrope", heading_style["family"])
        self.assertEqual(heading_style["size"], "19px")
        self.assertEqual(heading_style["weight"], "750")
        self.assertEqual(heading_style["color"], heading_style["statusColor"])
        self.page.set_viewport_size({"width": 375, "height": 800})
        self.assertLessEqual(
            self.page.evaluate("document.documentElement.scrollWidth"),
            self.page.evaluate("document.documentElement.clientWidth"),
        )
        self.page.set_viewport_size({"width": 1280, "height": 720})
        self.assertEqual(self.active_page(), "houses")
        self.assertEqual(
            self.page.locator("#houses-page-title").inner_text(),
            "Welcome, Assign Dashboard",
        )
        self.assertEqual(
            [
                self.page.locator(f"#assign-stat-{key}").inner_text()
                for key in ("seai", "active", "closed", "ready", "not-started")
            ],
            ["4", "1", "1", "1", "1"],
        )
        self.assertEqual(
            self.page.locator("#houses-list-heading").get_attribute("data-status"),
            "active",
        )
        self.assertEqual(
            self.page.locator(
                "[data-assign-house-filter='active']"
            ).get_attribute("aria-pressed"),
            "true",
        )
        active_card = self.page.locator("#houses-list .house-active").filter(
            has=self.page.locator("h2", has_text="ACTIVE-ASSIGN")
        )
        active_card.wait_for()
        missing_surveyor = active_card.locator(
            ".house-audit-summary-field.surveyor-assignment-field.surveyor-assignment-missing"
        )
        missing_surveyor.wait_for()
        self.assertIn("Assigned Surveyor", missing_surveyor.inner_text())
        self.assertIn("Not assigned", missing_surveyor.inner_text())
        self.assertEqual(active_card.locator(".stage-switch").count(), 0)
        self.assertEqual(active_card.locator(".house-note-open").count(), 0)
        self.assertEqual(active_card.locator(".house-snags-open").count(), 0)
        scaffolding_toggle = active_card.locator(
            '.house-checklist-status[data-checklist-field="scaffolding_status"]'
        )
        esb_toggle = active_card.locator(
            '.house-checklist-status[data-checklist-field="esb_unclipped"]'
        )
        self.assertTrue(scaffolding_toggle.is_visible())
        self.assertTrue(esb_toggle.is_visible())
        self.assertEqual(
            active_card.locator(
                '.house-checklist-status[data-checklist-field="plumber_status"]'
            ).count(),
            0,
        )
        plumber_row = active_card.locator(".checklist-card-row").filter(
            has_text="Plumber work"
        )
        vent_row = active_card.locator(".checklist-card-row").filter(
            has_text="Vent work"
        )
        self.assertEqual(plumber_row.locator(".checklist-requirement").count(), 0)
        self.assertEqual(vent_row.locator(".checklist-requirement").count(), 0)
        self.assertIn("Completed by Plumber Browser", plumber_row.inner_text())
        self.assertIn("Completed by Vent Browser", vent_row.inner_text())
        scaffolding_toggle.check()
        self.page.wait_for_function(
            """() => document.querySelector(
              '#houses-list .house-active:has(.house-body[data-house-id="ACTIVE-ASSIGN"]) ' +
              '.checklist-card-row:has([data-checklist-field="scaffolding_status"]) ' +
              '.checklist-completion-audit'
            )?.textContent.includes('Checked by Assign Dashboard')"""
        )
        esb_toggle.check()
        self.page.wait_for_function(
            """() => document.querySelector(
              '#houses-list .house-active:has(.house-body[data-house-id="ACTIVE-ASSIGN"]) ' +
              '.checklist-card-row:has([data-checklist-field="esb_unclipped"]) ' +
              '.checklist-completion-audit'
            )?.textContent.includes('Checked by Assign Dashboard')"""
        )
        active_card.locator(".assign-seai-details").click()
        self.page.locator("#assign-seai-house-detail").wait_for(state="visible")
        self.assertEqual(
            self.page.locator("#assign-seai-house-detail-title").inner_text(),
            "ACTIVE-ASSIGN",
        )
        self.page.wait_for_function(
            """() => document.querySelector('#assign-seai-house-record')
              ?.textContent.includes('External Wall Insulation')"""
        )
        self.assertTrue(
            self.page.locator("#assign-seai-house-ready").is_hidden()
        )
        self.assertEqual(
            self.page.locator("#assign-seai-house-status").inner_text(),
            "Active",
        )
        self.page.locator("#assign-seai-house-pdf-sections .survey-room-seai-section").filter(
            has_text="Application",
        ).locator("summary").click()
        self.page.locator(
            "#assign-seai-house-record .survey-room-seai-section"
        ).filter(has_text="Application").locator("summary").click()
        self.page.locator("#assign-seai-house-record .survey-room-seai-measures summary").click()
        self.assertIn(
            "SEAI Owner",
            self.page.locator("#assign-seai-house-record").inner_text(),
        )
        self.assertIn(
            "External Wall Insulation",
            self.page.locator("#assign-seai-house-record").inner_text(),
        )
        pdf_sections = self.page.locator("#assign-seai-house-pdf-sections")
        house_pdfs = pdf_sections.locator(".survey-room-saved-pdfs")
        house_pdfs.wait_for()
        for section_title in (
            "Application PDFs",
            "Survey PDFs",
            "Variation PDFs",
        ):
            self.assertTrue(
                pdf_sections.get_by_text(section_title, exact=True).is_visible(),
                section_title,
            )
        survey_section = pdf_sections.locator(".survey-room-seai-section").filter(
            has_text="Survey PDFs",
        )
        survey_section.locator("summary").click()
        self.assertIn("TEST PDF · survey-report.pdf", survey_section.inner_text())
        self.assertNotIn("selected-survey-pages.pdf", survey_section.inner_text())
        self.assertIn(
            "/api/survey-room-test-pdfs/survey-report.pdf",
            survey_section.locator("a").first.get_attribute("href"),
        )
        pdf_sections.locator(".survey-room-seai-section").filter(
            has_text="Variation PDFs",
        ).locator("summary").click()
        application_pdf_section = pdf_sections.locator(
            ".survey-room-seai-section"
        ).filter(
            has_text="Application PDFs",
        )
        if not application_pdf_section.evaluate("section => section.open"):
            application_pdf_section.locator("summary").click()
        self.assertIn("ORIGINAL PDF · original-application.pdf", pdf_sections.inner_text())
        self.assertIn("ORIGINAL PDF · original-variation.pdf", pdf_sections.inner_text())
        self.assertEqual(pdf_sections.get_by_text("Vent team PDFs", exact=True).count(), 0)
        self.assertTrue(house_pdfs.is_visible())
        self.assertIn("Saved PDFs", house_pdfs.locator("h3").inner_text())
        self.assertTrue(
            house_pdfs.evaluate(
                "(element) => element.parentElement.firstElementChild === element"
            )
        )
        self.assertTrue(
            pdf_sections.evaluate(
                "(element) => element.compareDocumentPosition("
                "document.querySelector('#assign-seai-toolbar')) & "
                "Node.DOCUMENT_POSITION_FOLLOWING"
            )
        )
        self.assertNotIn("survey-plan-annotated.pdf", house_pdfs.inner_text())
        self.assertNotIn("ANNOTATED COPY", house_pdfs.inner_text())
        self.assertIn("selected-survey-pages.pdf", house_pdfs.inner_text())
        self.assertIn(
            "original-application.pdf",
            pdf_sections.inner_text(),
        )
        self.assertIn(
            "original-variation.pdf",
            pdf_sections.inner_text(),
        )
        self.assertTrue(
            pdf_sections.get_by_role(
                "link", name="View original-application.pdf"
            ).is_visible()
        )
        self.assertTrue(
            pdf_sections.get_by_role(
                "link", name="View original-variation.pdf"
            ).is_visible()
        )
        self.assertEqual(house_pdfs.locator(".saved-pdf-entry").count(), 1)
        pdf_view_links = house_pdfs.locator(".saved-pdf-view")
        self.assertEqual(pdf_view_links.count(), 1)
        self.assertTrue(pdf_view_links.first.is_visible())
        self.assertEqual(house_pdfs.locator("a[download]").count(), 1)
        history = self.page.locator(".assign-seai-variation-history")
        self.assertTrue(history.is_visible())
        self.assertIn("120", history.inner_text())
        self.assertIn("135 → 142.5", history.inner_text())
        self.assertIn("Variation Surveyor (variation-surveyor)", history.inner_text())
        self.assertIn("05 Nov 2026", history.inner_text())
        self.assertEqual(
            self.page.locator("#assign-seai-house-record .seai-variation-edit").count(),
            0,
        )
        self.page.locator("#assign-seai-house-back").click()
        active_card.wait_for(state="visible")

        self.assertTrue(
            self.page.locator("#assign-stat-seai").locator("..").locator(
                ".assign-map-icon"
            ).is_visible()
        )
        self.page.locator("[data-assign-house-filter='seai']").click()
        self.page.locator("#house-map-view").wait_for(state="visible")
        map_back_button = self.page.locator("#house-map-list-view")
        self.assertEqual(map_back_button.inner_text(), "Back to houses")
        self.assertEqual(map_back_button.locator("svg.house-map-back-icon").count(), 1)
        self.assertEqual(
            map_back_button.locator("svg").get_attribute("aria-hidden"),
            "true",
        )
        light_back_button_style = map_back_button.evaluate(
            """button => ({
              height: getComputedStyle(button).minHeight,
              radius: getComputedStyle(button).borderRadius,
              weight: getComputedStyle(button).fontWeight,
              background: getComputedStyle(button).backgroundColor,
              iconBackground: getComputedStyle(button.querySelector('svg')).backgroundColor,
            })"""
        )
        self.assertEqual(map_back_button.inner_text(), "Back to houses")
        self.assertEqual(light_back_button_style["height"], "34px")
        self.assertEqual(light_back_button_style["radius"], "999px")
        self.assertEqual(light_back_button_style["weight"], "600")
        self.assertNotEqual(light_back_button_style["iconBackground"], "rgba(0, 0, 0, 0)")
        map_back_button.hover()
        self.page.wait_for_timeout(180)
        self.assertNotEqual(
            map_back_button.evaluate(
                "button => getComputedStyle(button).backgroundColor"
            ),
            light_back_button_style["background"],
        )
        self.page.mouse.move(0, 0)
        self.page.evaluate("document.documentElement.dataset.theme = 'dark'")
        dark_back_button_style = map_back_button.evaluate(
            """button => ({
              background: getComputedStyle(button).backgroundColor,
              iconBackground: getComputedStyle(button.querySelector('svg')).backgroundColor,
            })"""
        )
        self.assertNotEqual(
            dark_back_button_style["background"],
            light_back_button_style["background"],
        )
        self.assertNotEqual(
            dark_back_button_style["iconBackground"],
            light_back_button_style["iconBackground"],
        )
        self.page.evaluate("document.documentElement.dataset.theme = 'light'")
        self.page.locator(
            "#house-map-markers .house-map-marker[data-location-accuracy='approximate']"
        ).first.wait_for()
        self.assertEqual(
            self.page.locator("#house-map-markers .house-map-marker").count(),
            4,
        )
        coordinate_spread = self.page.evaluate(
            """() => {
              const houses = state.houseMapData;
              return {
                latitude: Math.max(...houses.map((house) => house.latitude))
                  - Math.min(...houses.map((house) => house.latitude)),
                longitude: Math.max(...houses.map((house) => house.longitude))
                  - Math.min(...houses.map((house) => house.longitude)),
              };
            }"""
        )
        self.assertGreater(coordinate_spread["latitude"], 1)
        self.assertGreater(coordinate_spread["longitude"], 2)
        self.assertIn(
            "Approximate demo pins",
            self.page.locator("#house-map-canvas").inner_text(),
        )
        self.assertEqual(
            self.page.locator("#house-map-house-list [data-map-house-id]").count(),
            4,
        )
        self.assertTrue(self.page.locator("#house-map-activity").is_hidden())
        self.page.locator("#theme-toggle").click()
        self.assertEqual(
            self.page.locator("html").get_attribute("data-theme"),
            "dark",
        )
        self.assertIn(
            "invert",
            self.page.locator(".house-map-tile").first.evaluate(
                "(tile) => getComputedStyle(tile).filter"
            ),
        )
        self.page.wait_for_function(
            "getComputedStyle(document.querySelector('.house-map-side')).backgroundColor !== 'rgb(255, 255, 255)'"
        )
        dark_map_colors = self.page.locator(
            "#house-map-legend [data-map-stage-filter='not_started']"
        ).evaluate(
            """button => ({
              background: getComputedStyle(button).backgroundColor,
              color: getComputedStyle(button).color,
              side: getComputedStyle(document.querySelector('.house-map-side')).backgroundColor,
            })"""
        )
        self.assertNotEqual(dark_map_colors["background"], "rgb(255, 255, 255)")
        self.assertNotEqual(dark_map_colors["side"], "rgb(255, 255, 255)")
        self.page.locator("#theme-toggle").click()
        not_started_filter = self.page.locator(
            "#house-map-legend [data-map-stage-filter='not_started']"
        )
        not_started_filter.click()
        self.assertEqual(
            self.page.locator("#house-map-markers .house-map-marker").count(),
            3,
        )
        self.assertEqual(
            self.page.locator("#house-map-house-list [data-map-house-id]").count(),
            3,
        )
        not_started_filter.click()
        self.assertEqual(
            self.page.locator("#house-map-markers .house-map-marker").count(),
            4,
        )
        active_stage_one_filter = self.page.locator(
            "#house-map-legend [data-map-stage-filter='active-1']"
        )
        active_stage_one_filter.click()
        self.assertEqual(
            self.page.locator("#house-map-markers .house-map-marker").count(),
            3,
        )
        active_stage_one_filter.click()
        self.assertEqual(
            self.page.locator("#house-map-markers .house-map-marker").count(),
            4,
        )
        self.page.locator("#house-map-markers [data-map-house-id='PENDING-ASSIGN']").click()
        self.assertIn(
            "PENDING-ASSIGN",
            self.page.locator("#house-map-selection").inner_text(),
        )
        self.assertNotIn(
            "materials",
            self.page.locator("#page-houses").inner_text().lower(),
        )
        self.page.locator("#house-map-list-view").click()
        self.assertTrue(self.page.locator("#assign-houses-dashboard").is_visible())
        self.page.locator("[data-assign-house-filter='ready']").click()
        self.assertEqual(
            self.page.locator("#houses-list-heading").get_attribute("data-status"),
            "ready",
        )
        self.assertEqual(
            self.page.locator("#houses-list-heading").evaluate(
                "(element) => getComputedStyle(element).color"
            ),
            self.page.locator(
                "[data-assign-house-filter='ready']"
            ).evaluate("(element) => getComputedStyle(element).borderLeftColor"),
        )
        self.page.get_by_role("heading", name="READY-ASSIGN").wait_for()
        ready_plumbing_row = self.page.locator(
            "#houses-list article[data-house-id='READY-ASSIGN'] .work-plan-row"
        ).filter(has_text="Plumber work")
        self.assertEqual(
            ready_plumbing_row.locator(".work-plan-badge").inner_text(), "Done"
        )
        self.assertFalse(
            ready_plumbing_row.locator(".work-plan-done-by").count()
        )
        shared_checklist = self.page.request.get(
            f"{self.base_url}/api/house-checklist/READY-ASSIGN"
        )
        self.assertEqual(shared_checklist.status, 200, shared_checklist.text())
        self.assertEqual(shared_checklist.json()["plumber_status"], "done")
        self.page.locator("[data-assign-house-filter='closed']").click()
        self.assertEqual(
            self.page.locator("#houses-list-heading").get_attribute("data-status"),
            "closed",
        )
        self.assertEqual(
            self.page.locator("#houses-list-heading").evaluate(
                "(element) => getComputedStyle(element).color"
            ),
            self.page.locator(
                "[data-assign-house-filter='closed']"
            ).evaluate("(element) => getComputedStyle(element).borderLeftColor"),
        )
        self.page.get_by_role("heading", name="CLOSED-ASSIGN").wait_for()
        self.page.locator("[data-assign-house-filter='not_started']").click()
        self.assertEqual(
            self.page.locator("#houses-list-heading").get_attribute("data-status"),
            "not_started",
        )
        self.assertEqual(
            self.page.locator("#houses-list-heading").evaluate(
                "(element) => getComputedStyle(element).color"
            ),
            self.page.locator(
                "[data-assign-house-filter='not_started']"
            ).evaluate("(element) => getComputedStyle(element).borderLeftColor"),
        )
        self.assertTrue(self.page.locator("#house-team-filter").is_hidden())
        with server.connect_db() as db:
            db.execute(
                """
                INSERT INTO provided_houses(id, owner_name, ready_for_start)
                VALUES ('READY-NEEDS-SURVEY', 'Survey Needed Owner', 1)
                """
            )
            db.execute(
                """
                INSERT INTO house_surveys(house_id, data, notes, updated_by, updated_at)
                VALUES (
                    'READY-ASSIGN',
                    '{"windows":[],"doors":[],"house_corners":[],"perimeter_m":42,"notes":""}',
                    '', 'Completed Surveyor', '2026-11-03T10:00:00'
                )
                """
            )
        self.page.locator("#refresh-surveyor-houses").click()
        self.page.wait_for_function(
            "() => !document.querySelector('#refresh-surveyor-houses').hasAttribute('aria-busy')"
        )
        self.page.locator("[data-assign-house-filter='ready']").click()
        survey_filters = self.page.locator("#assign-survey-filters")
        survey_filters.wait_for(state="visible")
        self.page.wait_for_function(
            "() => document.querySelector('#assign-survey-all-count').textContent === '2'"
        )
        self.assertEqual(
            [
                self.page.locator(f"#assign-survey-{key}-count").inner_text()
                for key in ("all", "needed", "completed")
            ],
            ["2", "1", "1"],
        )
        self.page.locator("[data-assign-survey-filter='needed']").click()
        self.page.locator(
            "#houses-list article[data-house-id='READY-NEEDS-SURVEY']"
        ).wait_for()
        self.assertFalse(
            self.page.locator(
                "#houses-list article[data-house-id='READY-ASSIGN']"
            ).count()
        )
        self.page.locator("[data-assign-survey-filter='completed']").click()
        self.page.locator(
            "#houses-list article[data-house-id='READY-ASSIGN']"
        ).wait_for()
        self.assertFalse(
            self.page.locator(
                "#houses-list article[data-house-id='READY-NEEDS-SURVEY']"
            ).count()
        )
        self.page.locator("[data-assign-house-filter='not_started']").click()
        pending_card = self.page.locator(
            "#houses-list article[data-house-id='PENDING-ASSIGN']"
        )
        pending_card.locator(".house-insulation-summary").wait_for()
        self.assertEqual(
            pending_card.locator(".house-insulation-row").count(),
            4,
        )
        for measure in (
            "External Wall Insulation",
            "Internal Wall Insulation",
            "Attic (Loft) Insulation",
            "Cavity Wall Insulation",
        ):
            self.assertTrue(
                pending_card.locator(".house-insulation-row").filter(
                    has_text=measure
                ).count()
            )
        self.assertIn(
            "No verified SEAI insulation quantities are available",
            pending_card.locator(".house-insulation-summary").text_content(),
        )
        self.assertNotIn(
            "DEMO DATA",
            pending_card.locator(".house-insulation-summary").inner_text(),
        )
        pending_action_rows = pending_card.evaluate(
            """card => {
              const details = card.querySelector('.assign-seai-details').getBoundingClientRect();
              const ready = card.querySelector('.plan-ready-toggle[data-ready="true"]').getBoundingClientRect();
              return {details: details.top, ready: ready.top};
            }"""
        )
        self.assertEqual(pending_card.locator(".house-card-actions .house-action-icon").count(), 2)
        self.assertAlmostEqual(
            pending_action_rows["details"],
            pending_action_rows["ready"],
        )
        light_action_colors = pending_card.evaluate(
            """card => ({
              details: getComputedStyle(card.querySelector('.assign-seai-details')).backgroundColor,
              ready: getComputedStyle(card.querySelector('.plan-ready-toggle[data-ready="true"]')).backgroundColor,
              readyHeight: card.querySelector('.plan-ready-toggle[data-ready="true"]').getBoundingClientRect().height,
            })"""
        )
        self.assertNotEqual(light_action_colors["details"], light_action_colors["ready"])
        self.assertGreaterEqual(light_action_colors["readyHeight"], 42)
        ready_action = pending_card.locator(".plan-ready-toggle[data-ready='true']")
        ready_action.hover()
        self.page.wait_for_timeout(180)
        light_hover_color = ready_action.evaluate(
            "button => getComputedStyle(button).backgroundColor"
        )
        self.assertNotEqual(light_hover_color, light_action_colors["ready"])
        self.page.mouse.move(0, 0)
        self.page.evaluate("document.documentElement.dataset.theme = 'dark'")
        self.page.mouse.move(1200, 0)
        self.page.wait_for_timeout(180)
        dark_pending_action_rows = pending_card.evaluate(
            """card => {
              const details = card.querySelector('.assign-seai-details').getBoundingClientRect();
              const ready = card.querySelector('.plan-ready-toggle[data-ready="true"]').getBoundingClientRect();
              return {details: details.top, ready: ready.top};
            }"""
        )
        self.assertAlmostEqual(
            dark_pending_action_rows["details"],
            dark_pending_action_rows["ready"],
        )
        dark_action_colors = pending_card.evaluate(
            """card => ({
              details: getComputedStyle(card.querySelector('.assign-seai-details')).backgroundColor,
              ready: getComputedStyle(card.querySelector('.plan-ready-toggle[data-ready="true"]')).backgroundColor,
            })"""
        )
        self.assertNotEqual(dark_action_colors["details"], dark_action_colors["ready"])
        ready_action.hover()
        self.page.wait_for_timeout(180)
        dark_hover_color = ready_action.evaluate(
            "button => getComputedStyle(button).backgroundColor"
        )
        self.assertNotEqual(dark_hover_color, dark_action_colors["ready"])
        self.page.mouse.move(0, 0)
        self.page.evaluate("document.documentElement.dataset.theme = 'light'")
        pending_card.locator(".assign-seai-details").click()
        self.page.locator("#assign-seai-house-detail").wait_for(state="visible")
        ready_button = self.page.locator("#assign-seai-house-ready")
        ready_button.wait_for(state="visible")
        self.assertTrue(ready_button.is_visible())
        ready_button.click()
        self.page.locator("#assign-houses-dashboard").wait_for(state="visible")
        self.page.wait_for_function(
            """() => document.querySelector('#houses-list-heading')
              ?.dataset.status === 'ready'"""
        )
        self.assertEqual(
            self.page.locator("#houses-list-heading").get_attribute("data-status"),
            "ready",
        )
        self.assertTrue(self.page.locator("#assign-seai-house-detail").is_hidden())
        self.assertTrue(ready_button.is_hidden())
        self.assertTrue(pending_card.locator(".status-pill.ready").is_visible())
        self.assertNotIn(
            "materials",
            self.page.locator("#page-houses").inner_text().lower(),
        )
        ready_card = self.page.locator(
            "#houses-list article.house-card[data-house-id='PENDING-ASSIGN']"
        )
        ready_card.locator(".status-pill.ready").wait_for()
        empty_work_plan = ready_card.locator(".work-plan-empty")
        self.assertIn("Work plan not added yet", empty_work_plan.inner_text())
        self.assertIn(
            "The Surveyor will add the pre-start work details here.",
            empty_work_plan.inner_text(),
        )
        self.assertEqual(empty_work_plan.get_attribute("role"), "status")
        light_work_plan_style = empty_work_plan.evaluate(
            """element => ({
              background: getComputedStyle(element).backgroundColor,
              title: getComputedStyle(element.querySelector('strong')).color,
              description: getComputedStyle(element.querySelector('small')).color,
            })"""
        )
        self.assertIn("assign-ready-arrival", ready_card.get_attribute("class"))
        self.assertEqual(
            ready_card.evaluate("(card) => getComputedStyle(card).animationName"),
            "assign-ready-arrival",
        )
        self.assertNotIn(
            "Back to not started",
            ready_card.inner_text(),
        )
        self.assertEqual(ready_card.locator(".house-card-actions .text-button").count(), 4)
        self.assertEqual(ready_card.locator(".house-card-actions .house-action-icon").count(), 4)
        self.assertTrue(
            ready_card.locator(".house-action-icon").first.get_attribute("aria-hidden")
        )
        self.assertTrue(ready_card.locator(".assign-seai-details").is_visible())
        action_rows = ready_card.evaluate(
            """card => {
              const rect = selector => {
                const {top, height} = card.querySelector(selector).getBoundingClientRect();
                return {top, height};
              };
              return {
                activate: rect('.assign-house-activate'),
                assign: rect('.assign-house-team'),
                details: rect('.assign-seai-details')
              };
            }"""
        )
        self.assertAlmostEqual(action_rows["activate"]["top"], action_rows["assign"]["top"])
        self.assertAlmostEqual(action_rows["details"]["top"], action_rows["assign"]["top"])
        self.assertTrue(ready_card.locator(".assign-house-activate").is_disabled())
        self.assertEqual(
            ready_card.locator(".assign-house-activate").evaluate(
                "button => getComputedStyle(button).backgroundColor"
            ),
            "rgb(237, 241, 238)",
        )
        self.page.evaluate("document.documentElement.dataset.theme = 'dark'")
        self.page.wait_for_timeout(200)
        self.assertEqual(
            self.page.locator("html").get_attribute("data-theme"),
            "dark",
        )
        self.assertEqual(
            ready_card.locator(".assign-house-activate").evaluate(
                "button => getComputedStyle(button).backgroundColor"
            ),
            "rgb(38, 51, 44)",
        )
        dark_work_plan_style = empty_work_plan.evaluate(
            """element => ({
              background: getComputedStyle(element).backgroundColor,
              title: getComputedStyle(element.querySelector('strong')).color,
              description: getComputedStyle(element.querySelector('small')).color,
            })"""
        )
        self.assertNotEqual(
            dark_work_plan_style["background"],
            light_work_plan_style["background"],
        )
        self.assertNotEqual(
            dark_work_plan_style["title"],
            light_work_plan_style["title"],
        )
        self.assertNotEqual(
            dark_work_plan_style["description"],
            light_work_plan_style["description"],
        )
        self.page.evaluate("document.documentElement.dataset.theme = 'light'")
        self.assertEqual(
            ready_card.locator(".assign-house-team").inner_text(),
            "Assign Surveyor",
        )
        self.page.evaluate("window.__logoClickToken = 'assign-dashboard'")
        self.page.locator(".surveyor-brand").click()
        self.assertEqual(
            self.page.evaluate("window.__logoClickToken"),
            "assign-dashboard",
        )
        self.assertTrue(self.page.locator("#go-overview").is_hidden())
        self.page.set_viewport_size({"width": 357, "height": 480})
        self.assertTrue(self.page.locator(".main-nav.workspace-nav").is_hidden())
        self.assertEqual(self.browser_errors, [])

    def test_login_cat_reacts_to_username_and_pin_typing(self):
        self.page.locator("#account-button").click()
        self.page.locator("#account-sign-out").click()
        self.page.locator("#auth-screen:not(.hidden)").wait_for()

        cat = self.page.locator("#auth-cat")
        self.assertTrue(cat.locator("svg.auth-cat-art").is_visible())
        self.assertTrue(self.page.locator("#auth-description").is_hidden())
        username = self.page.locator("#auth-username")
        username.fill("typing-user")
        self.assertEqual(cat.get_attribute("data-mood"), "typing")
        self.assertEqual(cat.get_attribute("data-field"), "username")

        pin = self.page.locator("#auth-pin")
        pin.fill("123456")
        self.assertEqual(cat.get_attribute("data-mood"), "typing")
        self.assertEqual(cat.get_attribute("data-field"), "pin")
        self.page.locator("[data-auth-pin-toggle='auth-pin']").click()
        self.assertEqual(pin.get_attribute("type"), "text")
        self.assertEqual(pin.input_value(), "123456")
        self.assertEqual(
            self.page.locator("[data-auth-pin-toggle='auth-pin']").inner_text(),
            "Hide",
        )
        self.page.locator("[data-auth-pin-toggle='auth-pin']").click()
        self.assertEqual(pin.get_attribute("type"), "password")
        self.assertEqual(pin.input_value(), "123456")
        self.page.wait_for_function(
            "document.querySelector('#auth-cat').dataset.mood === 'watching'",
            timeout=2000,
        )
        self.assertEqual(self.browser_errors, [])

    def test_login_dark_mode_uses_refined_high_contrast_surfaces(self):
        self.page.locator("#account-button").click()
        self.page.locator("#account-sign-out").click()
        self.page.locator("#auth-screen:not(.hidden)").wait_for()

        cat = self.page.locator("#auth-cat")
        eyebrow = self.page.locator("#auth-eyebrow")
        title = self.page.locator("#auth-title")
        self.assertEqual(eyebrow.inner_text(), "SECURE WORKSPACE")
        self.assertTrue(title.is_hidden())
        self.assertEqual(title.inner_text(), "")
        self.assertLess(
            eyebrow.bounding_box()["y"],
            self.page.locator(".auth-logo").bounding_box()["y"],
        )
        self.assertLess(
            title.evaluate("element => parseFloat(getComputedStyle(element).fontSize)"),
            18,
        )
        eyebrow_box = eyebrow.bounding_box()
        icon_box = self.page.locator("#auth-theme-toggle span").bounding_box()
        self.assertAlmostEqual(
            eyebrow_box["y"] + eyebrow_box["height"] / 2,
            icon_box["y"] + icon_box["height"] / 2,
            delta=2,
        )
        self.assertEqual(
            self.page.locator(".auth-card").evaluate(
                "element => getComputedStyle(element).display"
            ),
            "grid",
        )
        self.assertGreater(
            self.page.locator("#auth-form").bounding_box()["x"],
            self.page.locator(".auth-logo").bounding_box()["x"],
        )
        card_box = self.page.locator(".auth-card").bounding_box()
        self.assertLess(
            eyebrow_box["x"] + eyebrow_box["width"] / 2,
            card_box["x"] + card_box["width"] / 2,
        )
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.assertEqual(
            self.page.locator(".auth-card").evaluate(
                "element => getComputedStyle(element).gridTemplateColumns.split(' ').length"
            ),
            1,
        )
        self.assertGreater(
            self.page.locator("#auth-form").bounding_box()["y"],
            self.page.locator("#auth-cat").bounding_box()["y"],
        )
        self.assertLess(
            self.page.locator(".auth-logo").bounding_box()["y"],
            cat.bounding_box()["y"],
        )
        self.page.locator("#auth-theme-toggle").click()
        username = self.page.locator("#auth-username")
        username.focus()
        self.page.wait_for_function(
            """() => getComputedStyle(
              document.querySelector('#auth-username')
            ).backgroundColor === 'rgb(19, 30, 24)'"""
        )
        self.assertEqual(
            self.page.locator("#auth-theme-toggle").get_attribute("aria-pressed"),
            "true",
        )
        self.assertEqual(
            username.evaluate("element => getComputedStyle(element).backgroundColor"),
            "rgb(19, 30, 24)",
        )
        self.assertEqual(
            username.evaluate("element => getComputedStyle(element).color"),
            "rgb(237, 245, 239)",
        )
        self.assertIn(
            "radial-gradient",
            self.page.locator(".auth-card").evaluate(
                "element => getComputedStyle(element).backgroundImage"
            ),
        )
        self.assertNotEqual(
            username.evaluate("element => getComputedStyle(element).boxShadow"),
            "none",
        )
        self.assertEqual(self.browser_errors, [])

    def test_login_lights_control_disables_and_darkens_credentials(self):
        self.page.locator("#account-button").click()
        self.page.locator("#account-sign-out").click()
        self.page.locator("#auth-screen:not(.hidden)").wait_for()

        lights = self.page.locator("#auth-lights-toggle")
        username = self.page.locator("#auth-username")
        pin = self.page.locator("#auth-pin")
        self.assertTrue(username.is_enabled())
        self.assertTrue(pin.is_enabled())

        lights.click()
        self.assertEqual(lights.get_attribute("aria-pressed"), "true")
        self.assertTrue(username.is_enabled())
        self.assertTrue(pin.is_enabled())

        def pull_cord():
            cord = lights.locator(".auth-lamp-cord-hit").bounding_box()
            self.assertIsNotNone(cord)
            cord_x = cord["x"] + cord["width"] / 2
            cord_y = cord["y"] + cord["height"] * 0.45
            self.page.mouse.move(cord_x, cord_y)
            self.page.mouse.down()
            self.page.mouse.move(cord_x, cord_y + 32, steps=4)
            stretched_path = lights.locator(".auth-lamp-cord").get_attribute("d")
            self.assertTrue(stretched_path.startswith("M194 58"))
            self.assertIn("143", stretched_path)
            self.page.mouse.up()

        pull_cord()
        self.assertEqual(lights.get_attribute("aria-pressed"), "false")
        self.assertEqual(
            lights.get_attribute("aria-label"),
            "Pull the cord to turn lights on",
        )
        self.assertTrue(username.is_disabled())
        self.assertTrue(pin.is_disabled())
        self.page.wait_for_function(
            """() => getComputedStyle(
              document.querySelector('#auth-username')
            ).backgroundColor === 'rgb(8, 11, 10)'"""
        )
        login_style = self.page.evaluate(
            """() => ({
              lights: document.querySelector('#auth-screen').dataset.lights,
              disabled: document.querySelector('#auth-username').disabled,
              selectorMatches: document.querySelector('#auth-username').matches(
                '#auth-screen[data-lights="off"] #auth-form input:disabled'
              ),
              background: getComputedStyle(
                document.querySelector('#auth-username')
              ).backgroundColor
            })"""
        )
        self.assertEqual(login_style["background"], "rgb(8, 11, 10)", login_style)

        self.page.reload()
        self.page.locator("#auth-screen:not(.hidden)").wait_for()
        lights = self.page.locator("#auth-lights-toggle")
        username = self.page.locator("#auth-username")
        pin = self.page.locator("#auth-pin")
        self.assertEqual(lights.get_attribute("aria-pressed"), "false")
        self.assertTrue(username.is_disabled())
        self.assertTrue(pin.is_disabled())

        pull_cord()
        self.assertEqual(lights.get_attribute("aria-pressed"), "true")
        self.assertTrue(username.is_enabled())
        self.assertTrue(pin.is_enabled())
        self.assertEqual(
            self.page.locator(".auth-lamp-bulb").evaluate(
                "element => getComputedStyle(element).transform"
            ),
            "none",
        )
        self.assertTrue(self.page.locator(".auth-lamp-bulb").is_visible())
        self.assertEqual(
            self.page.locator("#auth-screen").get_attribute("data-igniting"),
            "true",
        )
        self.page.wait_for_function(
            "!document.querySelector('#auth-screen').hasAttribute('data-igniting')",
            timeout=1500,
        )
        self.assertEqual(self.browser_errors, [])

    def test_assign_dashboard_shows_surveyor_workload(self):
        with server.connect_db() as db:
            db.execute(
                """
                INSERT INTO provided_houses(id, postcode, owner_name, ready_for_start)
                VALUES ('WORKLOAD-PENDING', 'T1', 'Pending Owner', 1),
                       ('WORKLOAD-COMPLETE', 'T2', 'Completed Owner', 1)
                """
            )
            server._insert_user(db, "workload-assign", "Cathy", "234567", "assign")
            busy_surveyor_id = server._insert_user(
                db, "workload-busy", "House Surveyor", "345678", "surveyor"
            )
            server._insert_user(
                db, "workload-available", "Available Surveyor", "456789", "surveyor"
            )
            db.execute(
                """
                INSERT INTO house_role_assignments
                    (house_id, role, user_id, assigned_by, assigned_at)
                VALUES ('WORKLOAD-PENDING', 'surveyor', ?, 'Cathy', '2026-10-29T10:00:00'),
                       ('WORKLOAD-COMPLETE', 'surveyor', ?, 'Cathy', '2026-10-29T10:00:00')
                """,
                (busy_surveyor_id, busy_surveyor_id),
            )
            db.execute(
                """
                INSERT INTO house_surveys(house_id, data, updated_by, updated_at)
                VALUES ('WORKLOAD-COMPLETE', '{"perimeter_m": 20}', 'House Surveyor', '2026-10-29T12:00:00')
                """
            )

        self.page.locator("#account-button").click()
        self.page.locator("#account-sign-out").click()
        self.page.locator("#auth-screen:not(.hidden)").wait_for()
        self.page.locator("#auth-username").fill("workload-assign")
        self.page.locator("#auth-pin").fill("234567")
        self.page.locator("#auth-submit").click()

        workload = self.page.locator("#assign-surveyor-workload")
        workload.wait_for(state="visible")
        self.assertFalse(workload.evaluate("(element) => element.open"))
        self.assertIn("busy", workload.locator("summary").inner_text().lower())
        workload.locator("summary").click()
        busy_surveyor = self.page.locator(
            "#assign-surveyor-workload-list .assign-surveyor-row"
        ).filter(has_text="House Surveyor")
        self.assertIn("Busy", busy_surveyor.inner_text())
        self.assertIn("1 pending · 1 completed", busy_surveyor.inner_text())
        self.assertIn("WORKLOAD-PENDING", busy_surveyor.inner_text())
        available_surveyor = self.page.locator(
            "#assign-surveyor-workload-list .assign-surveyor-row"
        ).filter(has_text="Available Surveyor")
        self.assertIn("Available", available_surveyor.inner_text())
        self.assertIn("0 pending · 0 completed", available_surveyor.inner_text())

        self.page.locator("#theme-toggle").click()
        self.assertEqual(
            workload.evaluate("(element) => getComputedStyle(element).backgroundColor"),
            "rgb(27, 39, 34)",
        )
        self.page.set_viewport_size({"width": 375, "height": 800})
        self.assertLessEqual(
            workload.evaluate("(element) => element.scrollWidth"),
            workload.evaluate("(element) => element.clientWidth"),
        )
        self.assertEqual(self.browser_errors, [])

    def test_assign_map_can_view_details_and_mark_selected_house_ready(self):
        with server.connect_db() as db:
            db.execute(
                """
                INSERT INTO provided_houses(id, postcode, address, owner_name)
                VALUES ('MAP-ASSIGN-PENDING', 'D03 TEST', 'Selected map address', 'Map Owner')
                """
            )
            db.execute(
                "CREATE TABLE seai_applications "
                "(application_id TEXT PRIMARY KEY, detail_data_json TEXT NOT NULL)"
            )
            db.execute(
                "INSERT INTO seai_applications(application_id, detail_data_json) "
                "VALUES (?, ?)",
                (
                    "MAP-ASSIGN-PENDING",
                    json.dumps(
                        {
                            "_demo_data": True,
                            "Application": [
                                {"Application ID": "MAP-ASSIGN-PENDING"}
                            ],
                            "Measures": [
                                {
                                    "Name": "Radiator",
                                    "Survey Quantity": "6",
                                    "Survey Qty including Variation": "7",
                                    "Inspection Quantity": "6",
                                    "Invoice Quantity": "6",
                                }
                            ],
                            "Variance": [
                                {
                                    "Variance ID": "DEMO-VAR-001-01",
                                    "Measure": "Radiator",
                                    "Value": "1",
                                    "Comment": "Synthetic demo note for UI test.",
                                }
                            ]
                        }
                    ),
                ),
            )
            server._insert_user(
                db, "assign-map-user", "Map Assign User", "234567", "assign"
            )

        self.page.locator("#account-button").click()
        self.page.locator("#account-sign-out").click()
        self.page.locator("#auth-screen:not(.hidden)").wait_for()
        self.page.locator("#auth-username").fill("assign-map-user")
        self.page.locator("#auth-pin").fill("234567")
        self.page.locator("#auth-submit").click()
        self.page.locator("#assign-houses-dashboard").wait_for(state="visible")
        self.page.locator("[data-assign-house-filter='not_started']").click()
        card_ready_button = self.page.locator(
            "#houses-list [data-house-id='MAP-ASSIGN-PENDING'].plan-ready-toggle"
        )
        card_ready_button.wait_for()
        self.page.evaluate("document.documentElement.dataset.theme = 'light'")
        card_light_colors = card_ready_button.evaluate(
            """button => ({
              background: getComputedStyle(button).backgroundColor,
              border: getComputedStyle(button).borderTopColor,
              color: getComputedStyle(button).color
            })"""
        )
        self.page.evaluate("document.documentElement.dataset.theme = 'dark'")
        card_dark_colors = card_ready_button.evaluate(
            """button => ({
              background: getComputedStyle(button).backgroundImage,
              border: getComputedStyle(button).borderTopColor,
              color: getComputedStyle(button).color
            })"""
        )
        self.page.evaluate("document.documentElement.dataset.theme = 'light'")
        self.page.locator("[data-assign-house-filter='seai']").click()
        self.page.locator("#house-map-view").wait_for(state="visible")
        map_house = self.page.locator(
            "#house-map-house-list [data-map-house-id='MAP-ASSIGN-PENDING']"
        )
        map_house.wait_for()
        map_house.click()
        selection = self.page.locator("#house-map-selection")
        details_button = selection.locator(".assign-seai-details")
        ready_button = selection.locator(
            '.plan-ready-toggle[data-ready="true"]'
        )
        self.assertTrue(details_button.is_visible())
        self.assertTrue(ready_button.is_visible())
        self.assertEqual(
            ready_button.evaluate(
                """button => ({
                  background: getComputedStyle(button).backgroundColor,
                  border: getComputedStyle(button).borderTopColor,
                  color: getComputedStyle(button).color
                })"""
            ),
            card_light_colors,
        )
        self.page.evaluate("document.documentElement.dataset.theme = 'dark'")
        self.page.wait_for_timeout(250)
        self.assertEqual(
            ready_button.evaluate(
                """button => ({
                  background: getComputedStyle(button).backgroundImage,
                  border: getComputedStyle(button).borderTopColor,
                  color: getComputedStyle(button).color
                })"""
            ),
            card_dark_colors,
        )
        self.page.evaluate("document.documentElement.dataset.theme = 'light'")

        details_button.click()
        self.page.locator("#assign-seai-house-detail").wait_for(state="visible")
        self.assertEqual(
            self.page.locator("#assign-seai-house-detail-title").inner_text(),
            "MAP-ASSIGN-PENDING",
        )
        self.page.wait_for_function(
            """() => document.querySelector('#assign-seai-house-record')
              ?.textContent.includes('MAP-ASSIGN-PENDING')"""
        )
        self.assertIn(
            "DEMO DATA - synthetic sample measures",
            self.page.locator("#assign-seai-house-record").inner_text(),
        )
        self.assertIn(
            "Synthetic sample application information",
            self.page.locator("#assign-seai-house-detail .page-subtitle").inner_text(),
        )
        sections = self.page.locator(
            "#assign-seai-house-record .survey-room-seai-section"
        )
        self.assertEqual(sections.count(), 3)
        self.assertIsNone(sections.nth(0).get_attribute("open"))
        measures_section = self.page.locator(
            "#assign-seai-house-record .survey-room-seai-measures"
        )
        variance_section = self.page.locator(
            "#assign-seai-house-record .survey-room-seai-variance"
        )
        self.assertIsNone(measures_section.get_attribute("open"))
        self.assertIsNone(variance_section.get_attribute("open"))
        measures_section.locator("summary").click()
        self.assertEqual(measures_section.get_attribute("open"), "")
        self.assertIn(
            "invoice quantity",
            self.page.locator("#assign-seai-house-record").inner_text().lower(),
        )
        variance_section.locator("summary").click()
        self.assertEqual(variance_section.get_attribute("open"), "")
        self.assertIn(
            "Variance ID",
            self.page.locator("#assign-seai-house-record").inner_text(),
        )
        self.assertIn(
            "DEMO-VAR-001-01",
            self.page.locator("#assign-seai-house-record").inner_text(),
        )
        self.page.locator("#assign-seai-house-back").click()
        self.page.locator("#house-map-view").wait_for(state="visible")
        self.assertIn("MAP-ASSIGN-PENDING", selection.inner_text())

        with self.page.expect_response(
            lambda response: response.url.endswith("/api/houses/plan")
            and response.request.method == "POST"
            and response.status == 200
        ):
            selection.locator(
                '.plan-ready-toggle[data-ready="true"]'
            ).click()
        self.page.locator("#assign-houses-dashboard").wait_for(state="visible")
        self.page.wait_for_function(
            """() => document.querySelector('#houses-list-heading')
              ?.dataset.status === 'ready'"""
        )
        self.assertEqual(
            self.page.locator("#houses-list-heading").get_attribute("data-status"),
            "ready",
        )
        ready_card = self.page.locator(
            "#houses-list article[data-house-id='MAP-ASSIGN-PENDING']"
        )
        ready_card.wait_for()
        self.assertIn("assign-ready-arrival", ready_card.get_attribute("class"))
        self.assertEqual(
            ready_card.evaluate("(card) => getComputedStyle(card).animationName"),
            "assign-ready-arrival",
        )
        self.assertTrue(ready_card.locator(".status-pill.ready").is_visible())
        self.assertFalse(
            ready_card.locator('.plan-ready-toggle[data-ready="true"]').count()
        )
        self.assertTrue(ready_card.locator(".assign-seai-details").is_visible())
        ready_card.locator(".assign-seai-details").click()
        self.page.locator("#assign-seai-house-detail").wait_for(state="visible")
        self.page.wait_for_function(
            """() => document.querySelector('#assign-seai-house-ready')
              ?.classList.contains('hidden')"""
        )
        self.assertEqual(
            self.page.locator("#assign-seai-house-status").inner_text(),
            "Ready for start",
        )
        self.assertTrue(self.page.locator("#assign-seai-house-ready").is_hidden())
        self.page.locator("#assign-seai-house-back").click()
        self.page.locator("#assign-houses-dashboard").wait_for(state="visible")
        self.assertEqual(self.browser_errors, [])

    def test_cathy_filters_ready_houses_by_survey_status(self):
        with server.connect_db() as db:
            db.execute(
                """
                INSERT INTO provided_houses(id, owner_name, ready_for_start)
                VALUES ('CATHY-NEEDS-SURVEY', 'Needs Survey Owner', 1),
                       ('CATHY-SURVEYED', 'Surveyed Owner', 1)
                """
            )
            db.execute(
                """
                INSERT INTO house_status_history(house_id, status, changed_by, changed_at)
                VALUES ('CATHY-NEEDS-SURVEY', 'ready', 'Cathy', '2026-11-04T09:00:00'),
                       ('CATHY-SURVEYED', 'ready', 'Cathy', '2026-11-04T09:00:00')
                """
            )
            db.execute(
                """
                INSERT INTO house_surveys(house_id, data, notes, updated_by, updated_at)
                VALUES (
                    'CATHY-SURVEYED',
                    '{"windows":[],"doors":[],"house_corners":[],"perimeter_m":42,"notes":""}',
                    '', 'Surveyor', '2026-11-04T10:00:00'
                )
                """
            )
            server._insert_user(
                db, "cathy-survey-filter", "Cathy", "234567", "assign"
            )

        self.page.locator("#account-button").click()
        self.page.locator("#account-sign-out").click()
        self.page.locator("#auth-screen:not(.hidden)").wait_for()
        self.page.locator("#auth-username").fill("cathy-survey-filter")
        self.page.locator("#auth-pin").fill("234567")
        self.page.locator("#auth-submit").click()
        self.page.locator("#assign-houses-dashboard").wait_for(state="visible")

        survey_filters = self.page.locator("#assign-survey-filters")
        self.assertTrue(survey_filters.is_hidden())
        self.page.locator("[data-assign-house-filter='ready']").click()
        survey_filters.wait_for(state="visible")
        self.page.wait_for_function(
            """() => document.querySelector('#assign-survey-all-count').textContent === '2'"""
        )
        self.assertEqual(
            [
                self.page.locator(f"#assign-survey-{key}-count").inner_text()
                for key in ("all", "needed", "completed")
            ],
            ["2", "1", "1"],
        )

        needed_filter = self.page.locator(
            "[data-assign-survey-filter='needed']"
        )
        needed_filter.click()
        self.assertEqual(needed_filter.get_attribute("aria-pressed"), "true")
        self.page.locator(
            "#houses-list article[data-house-id='CATHY-NEEDS-SURVEY']"
        ).wait_for()
        self.assertFalse(
            self.page.locator(
                "#houses-list article[data-house-id='CATHY-SURVEYED']"
            ).count()
        )

        surveyed_filter = self.page.locator(
            "[data-assign-survey-filter='completed']"
        )
        surveyed_filter.click()
        self.assertEqual(surveyed_filter.get_attribute("aria-pressed"), "true")
        self.page.locator(
            "#houses-list article[data-house-id='CATHY-SURVEYED']"
        ).wait_for()
        self.assertFalse(
            self.page.locator(
                "#houses-list article[data-house-id='CATHY-NEEDS-SURVEY']"
            ).count()
        )
        self.assertEqual(self.browser_errors, [])

    def test_assign_user_can_assign_surveyor_from_ready_house_map(self):
        with server.connect_db() as db:
            db.execute(
                """
                INSERT INTO provided_houses(id, postcode, owner_name, ready_for_start)
                VALUES ('MAP-READY-ASSIGN', 'D03 TEST', 'Map Owner', 1)
                """
            )
            db.execute(
                """
                INSERT INTO house_status_history(house_id, status, changed_by, changed_at)
                VALUES ('MAP-READY-ASSIGN', 'ready', 'Cathy', '2026-10-29T11:00:00')
                """
            )
            db.execute(
                """
                INSERT INTO house_surveys(house_id, data, notes, updated_by, updated_at)
                VALUES ('MAP-READY-ASSIGN', '{"perimeter_m": 42}', 'survey notes', 'Map Surveyor', '2026-10-29T12:00:00')
                """
            )
            surveyor_id = server._insert_user(
                db, "map-assignment-surveyor", "Map Surveyor", "345678", "surveyor"
            )
            alternate_surveyor_id = server._insert_user(
                db,
                "map-assignment-surveyor-two",
                "Alternate Surveyor",
                "456789",
                "surveyor",
            )
            server._insert_user(
                db, "map-assignment-cathy", "Cathy", "234567", "assign"
            )

        self.page.locator("#account-button").click()
        self.page.locator("#account-sign-out").click()
        self.page.locator("#auth-screen:not(.hidden)").wait_for()
        self.page.locator("#auth-username").fill("map-assignment-cathy")
        self.page.locator("#auth-pin").fill("234567")
        self.page.locator("#auth-submit").click()
        self.page.locator("#assign-houses-dashboard").wait_for(state="visible")
        self.page.locator("[data-assign-house-filter='seai']").click()
        self.page.locator("#house-map-view").wait_for(state="visible")
        self.page.locator(
            "#house-map-house-list [data-map-house-id='MAP-READY-ASSIGN']"
        ).click()

        selection = self.page.locator("#house-map-selection")
        activate_button = selection.locator(".assign-house-activate")
        self.assertTrue(activate_button.is_disabled())
        self.assertEqual(
            activate_button.evaluate(
                "button => getComputedStyle(button).backgroundColor"
            ),
            "rgb(237, 241, 238)",
        )
        assign_button = selection.locator(".assign-house-team")
        self.assertEqual(assign_button.inner_text(), "Assign Surveyor")
        self.assertIn("assign-surveyor-action", assign_button.get_attribute("class"))
        assign_button_color = assign_button.evaluate(
            "button => getComputedStyle(button).backgroundColor"
        )
        assign_button.click()
        self.page.locator("#house-assignment-dashboard").wait_for(state="visible")
        self.page.locator("#back-from-house-assignments").click()
        self.page.locator("#house-assignment-dashboard").wait_for(state="hidden")
        self.page.locator("#house-map-view").wait_for(state="visible")
        assign_button = selection.locator(".assign-house-team")
        assign_button.wait_for()
        assign_button.click()
        self.page.locator("#house-assignment-dashboard").wait_for(state="visible")
        surveyor_option = self.page.locator(
            f'#house-assignment-surveyor input[name="surveyor_user_id"][value="{surveyor_id}"]'
        )
        surveyor_option.check()
        self.page.locator("#save-house-assignments").click()
        self.page.locator("#house-assignment-dashboard").wait_for(state="hidden")
        self.page.locator("#house-map-view").wait_for(state="visible")
        self.page.wait_for_function(
            """() => document.querySelector(
              '#house-map-selection .house-map-selected-heading strong'
            )?.textContent === 'MAP-READY-ASSIGN'"""
        )
        change_button = self.page.locator("#house-map-selection .assign-house-team")
        self.assertEqual(change_button.inner_text(), "Change Surveyor")
        self.assertIn("change-surveyor-action", change_button.get_attribute("class"))
        change_button_color = change_button.evaluate(
            "button => getComputedStyle(button).backgroundColor"
        )
        self.assertNotEqual(change_button_color, assign_button_color)
        self.page.evaluate("document.documentElement.dataset.theme = 'dark'")
        self.page.wait_for_function(
            """() => getComputedStyle(document.querySelector(
              '#house-map-selection .assign-house-team'
            )).backgroundColor === 'rgb(53, 42, 67)'"""
        )
        dark_change_button_color = change_button.evaluate(
            "button => getComputedStyle(button).backgroundColor"
        )
        self.assertEqual(dark_change_button_color, "rgb(53, 42, 67)")
        self.page.evaluate("document.documentElement.dataset.theme = 'light'")
        activate_button = selection.locator(".assign-house-activate")
        self.assertFalse(activate_button.is_disabled())
        self.assertEqual(
            activate_button.evaluate(
                "button => getComputedStyle(button).backgroundColor"
            ),
            "rgb(23, 107, 74)",
        )
        self.page.locator("#house-map-selection .assign-house-team").click()
        self.page.locator("#house-assignment-dashboard").wait_for(state="visible")
        original_option = self.page.locator(
            f'#house-assignment-surveyor input[name="surveyor_user_id"][value="{surveyor_id}"]'
        )
        alternate_option = self.page.locator(
            f'#house-assignment-surveyor input[name="surveyor_user_id"][value="{alternate_surveyor_id}"]'
        )
        self.assertTrue(original_option.is_checked())
        alternate_option.check()
        self.page.locator("#save-house-assignments").click()
        self.page.locator("#house-assignment-dashboard").wait_for(state="hidden")
        self.page.locator("#house-map-view").wait_for(state="visible")
        with server.connect_db() as db:
            assignment = db.execute(
                """
                SELECT user_id FROM house_role_assignments
                WHERE house_id = 'MAP-READY-ASSIGN' AND role = 'surveyor'
                """
            ).fetchone()
        self.assertEqual(assignment["user_id"], alternate_surveyor_id)
        self.page.once("dialog", lambda dialog: dialog.accept())
        self.page.locator(
            "#house-map-selection .plan-ready-toggle[data-ready='false']"
        ).click()
        not_started_card = self.page.locator("#houses-list .house-card").filter(
            has_text="MAP-READY-ASSIGN"
        )
        not_started_card.wait_for()
        self.assertEqual(
            self.page.locator("#houses-list-heading").get_attribute("data-status"),
            "not_started",
        )
        self.assertTrue(
            not_started_card.locator(
                ".plan-ready-toggle[data-ready='true']"
            ).is_visible()
        )
        with server.connect_db() as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM house_surveys WHERE house_id = 'MAP-READY-ASSIGN'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM house_role_assignments WHERE house_id = 'MAP-READY-ASSIGN' AND role = 'surveyor'"
                ).fetchone()[0],
                0,
            )
        self.assertEqual(self.browser_errors, [])

    def test_assign_user_not_started_card_hides_surveyor_information(self):
        with server.connect_db() as db:
            db.execute(
                """
                INSERT INTO provided_houses(id, postcode, owner_name)
                VALUES ('TEAM-PREASSIGN-1', 'T2', 'Pending Owner')
                """
            )
            db.execute(
                """
                INSERT INTO provided_houses(id, postcode, owner_name, ready_for_start)
                VALUES ('TEAM-PREASSIGN-READY', 'T3', 'Ready Owner', 1)
                """
            )
            server._insert_user(
                db, "preassign-assign", "Preassign Assign", "234567", "assign"
            )

        self.page.locator("#account-button").click()
        self.page.locator("#account-sign-out").click()
        self.page.locator("#auth-screen:not(.hidden)").wait_for()
        self.page.locator("#auth-username").fill("preassign-assign")
        self.page.locator("#auth-pin").fill("234567")
        self.page.locator("#auth-submit").click()
        self.page.locator("#assign-houses-dashboard").wait_for(state="visible")
        self.page.locator("[data-assign-house-filter='not_started']").click()
        card = self.page.locator(
            "#houses-list article[data-house-id='TEAM-PREASSIGN-1']"
        )
        for width, expected_columns in (
            (1280, 2),
            (1024, 1),
            (768, 1),
            (390, 1),
        ):
            self.page.set_viewport_size({"width": width, "height": 900})
            self.assertEqual(
                self.page.locator("#houses-list").evaluate(
                    "(element) => getComputedStyle(element).gridTemplateColumns.split(' ').length"
                ),
                expected_columns,
                f"Unexpected Not-started card columns at {width}px",
            )
            self.assertLessEqual(
                card.evaluate("(element) => element.scrollWidth"),
                card.evaluate("(element) => element.clientWidth"),
                f"Not-started card overflows horizontally at {width}px",
            )
        self.assertFalse(card.locator(".house-assignment-summary").count())
        self.assertNotIn("Surveyor", card.inner_text())
        self.assertNotIn("Assigned by", card.inner_text())
        ready_button = card.locator(".plan-ready-toggle[data-ready='true']")
        self.assertEqual(card.locator(".house-card-actions button").count(), 2)
        self.assertTrue(card.locator(".assign-seai-details").is_visible())
        self.assertEqual(ready_button.inner_text(), "Mark ready for start")
        self.assertFalse(card.locator(".assign-house-team").count())
        self.page.locator("[data-assign-house-filter='ready']").click()
        ready_missing_surveyor = self.page.locator(
            "#houses-list article.needs-surveyor-assignment"
        ).first
        ready_missing_surveyor.wait_for()
        self.assertIn(
            "Surveyor assignment needed",
            ready_missing_surveyor.locator(".surveyor-assignment-alert").inner_text(),
        )
        self.assertTrue(
            ready_missing_surveyor.locator(".assign-surveyor-action").is_visible()
        )

    def test_admin_not_started_card_has_only_mark_ready_action(self):
        with server.connect_db() as db:
            db.execute(
                """
                INSERT INTO provided_houses(id, postcode, owner_name)
                VALUES ('ADMIN-PREASSIGN-1', 'T3', 'Admin Pending Owner')
                """
            )
        self.page.locator('[data-tab="overview"]').click()
        self.page.locator('[data-house-view="not_started"]').click()
        card = self.page.locator(
            "#houses-list article[data-house-id='ADMIN-PREASSIGN-1']"
        )
        self.assertFalse(card.locator(".house-assignment-summary").count())
        self.assertNotIn("Surveyor", card.inner_text())
        self.assertEqual(card.locator(".house-card-actions button").count(), 1)
        self.assertEqual(
            card.locator(".plan-ready-toggle").inner_text(),
            "Mark ready for start",
        )
        self.assertFalse(card.locator(".assign-house-team").count())
        self.assertNotIn("Assigned by", card.inner_text())

    def test_assign_user_selects_supervisor_from_account_cards(self):
        with server.connect_db() as db:
            db.execute(
                """
                INSERT INTO provided_houses(id, postcode, owner_name, ready_for_start)
                VALUES ('SUPERVISOR-PICKER-TEST', 'T1', 'Picker Owner', 1)
                """
            )
            surveyor_id = server._insert_user(
                db, "picker-surveyor", "Picker Surveyor", "345678", "surveyor"
            )
            supervisor_id = server._insert_user(
                db,
                "picker-supervisor",
                "Picker Supervisor",
                "456789",
                "supervisor",
                "External Wall Insulation",
            )
            server._insert_user(db, "picker-assign", "Cathy", "234567", "assign")
            db.execute(
                """
                INSERT INTO house_role_assignments
                    (house_id, role, user_id, assigned_by, assigned_at)
                VALUES ('SUPERVISOR-PICKER-TEST', 'surveyor', ?, 'Test Admin', '2026-10-29T10:00:00')
                """,
                (surveyor_id,),
            )
            db.execute(
                """
                INSERT INTO house_surveys(house_id, data, notes, updated_by, updated_at)
                VALUES (
                    'SUPERVISOR-PICKER-TEST',
                    '{"windows":[],"doors":[],"house_corners":[],"perimeter_m":42,"notes":""}',
                    '', 'Picker Surveyor', '2026-10-29T10:30:00'
                )
                """
            )

        self.page.locator("#account-button").click()
        self.page.locator("#account-sign-out").click()
        self.page.locator("#auth-screen:not(.hidden)").wait_for()
        self.page.locator("#auth-username").fill("picker-assign")
        self.page.locator("#auth-pin").fill("234567")
        self.page.locator("#auth-submit").click()
        self.page.locator("#assign-houses-dashboard").wait_for(state="visible")
        refresh_button = self.page.locator("#refresh-surveyor-houses")
        self.assertTrue(refresh_button.is_visible())
        self.assertEqual(refresh_button.get_attribute("aria-label"), "Refresh house dashboard")
        self.assertIn("ventilation-refresh-button", refresh_button.get_attribute("class"))
        self.assertTrue(refresh_button.locator(".ventilation-refresh-icon").is_visible())
        refresh_button.click()
        self.page.wait_for_function(
            "() => !document.querySelector('#refresh-surveyor-houses').hasAttribute('aria-busy')"
        )
        self.page.locator("[data-assign-house-filter='ready']").click()
        self.page.locator(
            "#houses-list [data-house-id='SUPERVISOR-PICKER-TEST'] .assign-house-activate"
        ).click()

        dialog = self.page.locator("#assign-supervisor-dialog")
        dialog.wait_for(state="visible")
        self.page.set_viewport_size({"width": 390, "height": 740})
        option = self.page.locator(
            f'#activate-house-supervisor-options input[value="{supervisor_id}"]'
        )
        self.assertTrue(option.is_visible())
        account_card = option.locator("..")
        self.assertEqual(
            account_card.locator(".supervisor-account-identity strong").inner_text(),
            "Picker Supervisor",
        )
        self.assertEqual(
            account_card.locator(".supervisor-account-identity small").inner_text(),
            "@picker-supervisor",
        )
        self.assertEqual(
            account_card.locator(".supervisor-account-avatar").inner_text(),
            "PS",
        )
        self.assertLessEqual(
            dialog.evaluate("(element) => element.scrollWidth"),
            dialog.evaluate("(element) => element.clientWidth"),
        )
        self.assertFalse(self.page.locator("#confirm-house-activation").is_enabled())
        option.check()
        self.assertTrue(account_card.evaluate("(element) => element.classList.contains('selected')"))
        self.assertEqual(
            account_card.locator(".supervisor-account-status").inner_text(),
            "Selected",
        )
        confirm = self.page.locator("#confirm-house-activation")
        self.assertTrue(confirm.is_enabled())
        self.assertGreaterEqual(confirm.bounding_box()["height"], 44)
        confirm.click()
        dialog.wait_for(state="hidden")
        with server.connect_db() as db:
            house = db.execute(
                "SELECT employee_name FROM houses WHERE id = 'SUPERVISOR-PICKER-TEST'"
            ).fetchone()
        self.assertEqual(house["employee_name"], "Picker Supervisor")
        self.assertEqual(self.browser_errors, [])

    def test_cathy_assigns_house_to_surveyor_and_surveyor_completes_it(self):
        with server.connect_db() as db:
            db.execute(
                """
                INSERT INTO provided_houses(id, postcode, owner_name, ready_for_start)
                VALUES ('TEAM-ASSIGN-1', 'T1', 'Team Owner', 1)
                """
            )
            db.execute(
                """
                INSERT INTO house_status_history(house_id, status, changed_by, changed_at)
                VALUES ('TEAM-ASSIGN-1', 'ready', 'Cathy', '2026-10-29T11:00:00')
                """
            )
            surveyor_id = server._insert_user(
                db, "test-surveyor-one", "Surveyor One", "345678", "surveyor"
            )
            supervisor_id = server._insert_user(
                db,
                "browser-supervisor",
                "Browser Supervisor",
                "456789",
                "supervisor",
                "External Wall Insulation",
            )
            server._insert_user(
                db, "cathy-assign", "Cathy", "234567", "assign"
            )

        self.page.locator("#account-button").click()
        self.page.locator("#account-sign-out").click()
        self.page.locator("#auth-screen:not(.hidden)").wait_for()
        self.page.locator("#auth-username").fill("cathy-assign")
        self.page.locator("#auth-pin").fill("234567")
        self.page.locator("#auth-submit").click()
        self.page.locator("#assign-houses-dashboard").wait_for(state="visible")
        self.page.locator("[data-assign-house-filter='ready']").click()
        card = self.page.locator(
            "#houses-list article[data-house-id='TEAM-ASSIGN-1']"
        )
        self.assertEqual(
            card.locator(".house-status-stack").inner_text(),
            "Ready for start\nNot surveyed yet",
        )
        activate = card.locator(".assign-house-activate")
        self.assertTrue(activate.is_disabled())
        self.assertIn("Assign a Surveyor first", activate.get_attribute("title"))
        self.assertEqual(card.locator(".assign-house-team").inner_text(), "Assign Surveyor")

        card.locator(".assign-house-team").click()
        self.page.locator("#house-assignment-dashboard").wait_for(state="visible")
        surveyor_option = self.page.locator(
            f'#house-assignment-surveyor input[name="surveyor_user_id"][value="{surveyor_id}"]'
        )
        surveyor_option.check()
        self.assertTrue(
            self.page.locator(
                "#house-assignment-surveyor"
            ).get_by_text("Surveyor One", exact=True).is_visible()
        )
        self.assertTrue(self.page.get_by_text("@test-surveyor-one", exact=True).is_visible())
        self.assertTrue(surveyor_option.is_checked())
        self.assertEqual(
            self.page.locator("#house-assignment-surveyor .selected").count(), 1
        )
        save = self.page.locator("#save-house-assignments")
        self.assertTrue(save.is_enabled())
        save.click()
        self.page.locator("#house-assignment-dashboard").wait_for(state="hidden")
        self.assertEqual(
            self.page.locator(
                "#assign-houses-dashboard [data-assign-house-filter='ready']"
            ).get_attribute("aria-pressed"),
            "true",
        )
        activate = self.page.locator(
            "#houses-list [data-house-id='TEAM-ASSIGN-1'] .assign-house-activate"
        )
        self.page.wait_for_function(
            """() => {
              const button = document.querySelector(
                "#houses-list [data-house-id='TEAM-ASSIGN-1'] .assign-house-activate"
              );
              return button && button.disabled
                && button.title.includes("Waiting for the assigned Surveyor");
            }"""
        )
        self.assertTrue(activate.is_disabled())
        self.assertIn("Waiting for the assigned Surveyor", activate.get_attribute("title"))
        assignment_audit = self.page.locator(
            "#houses-list [data-house-id='TEAM-ASSIGN-1'] .house-audit-ready .house-audit-summary"
        )
        assignment_audit.get_by_text("Surveyor One", exact=True).wait_for()
        audit_text = assignment_audit.inner_text().casefold()
        self.assertIn("marked ready to start", audit_text)
        self.assertIn("assigned surveyor", audit_text)
        self.assertIn("Surveyor One", assignment_audit.inner_text())
        assigned_surveyor_field = assignment_audit.locator(
            ".house-audit-summary-field"
        ).nth(1)
        self.assertTrue(
            assigned_surveyor_field.locator("small").inner_text().strip()
        )
        self.assertTrue(
            assigned_surveyor_field.locator("small").inner_text().startswith("Assigned ")
        )
        ready_field_box = assignment_audit.locator(
            ".house-audit-summary-field"
        ).first.bounding_box()
        assigned_field_box = assigned_surveyor_field.bounding_box()
        self.assertLess(abs(ready_field_box["y"] - assigned_field_box["y"]), 10)
        self.assertGreater(assigned_field_box["x"], ready_field_box["x"])
        self.page.evaluate("document.documentElement.dataset.theme = 'dark'")
        self.assertEqual(
            assignment_audit.evaluate(
                "(element) => getComputedStyle(element).gridTemplateColumns.split(' ').length"
            ),
            2,
        )
        self.page.set_viewport_size({"width": 1024, "height": 768})
        self.assertEqual(
            self.page.locator("#houses-list").evaluate(
                "(element) => getComputedStyle(element).gridTemplateColumns.split(' ').length"
            ),
            1,
        )
        self.assertEqual(
            assignment_audit.evaluate(
                "(element) => getComputedStyle(element).gridTemplateColumns.split(' ').length"
            ),
            2,
        )
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.assertEqual(
            assignment_audit.evaluate(
                "(element) => getComputedStyle(element).gridTemplateColumns.split(' ').length"
            ),
            1,
        )
        self.assertLessEqual(
            assignment_audit.evaluate("(element) => element.scrollWidth"),
            assignment_audit.evaluate("(element) => element.clientWidth"),
        )
        self.page.evaluate("document.documentElement.dataset.theme = 'light'")
        self.page.set_viewport_size({"width": 1024, "height": 768})
        self.assertEqual(
            self.page.locator("#houses-list").evaluate(
                "(element) => getComputedStyle(element).gridTemplateColumns.split(' ').length"
            ),
            1,
        )
        self.assertEqual(
            assignment_audit.evaluate(
                "(element) => getComputedStyle(element).gridTemplateColumns.split(' ').length"
            ),
            2,
        )
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.assertEqual(
            assignment_audit.evaluate(
                "(element) => getComputedStyle(element).gridTemplateColumns.split(' ').length"
            ),
            1,
        )
        self.assertLessEqual(
            assignment_audit.evaluate("(element) => element.scrollWidth"),
            assignment_audit.evaluate("(element) => element.clientWidth"),
        )
        self.assertEqual(
            self.page.locator(
                "#houses-list [data-house-id='TEAM-ASSIGN-1'] .house-status-stack"
            ).inner_text(),
            "Ready for start\nNot surveyed yet",
        )

        self.page.locator("#account-button").click()
        self.page.locator("#account-sign-out").click()
        self.page.locator("#auth-screen:not(.hidden)").wait_for()
        self.page.locator("#auth-username").fill("test-surveyor-one")
        self.page.locator("#auth-pin").fill("345678")
        self.page.locator("#auth-submit").click()
        self.page.get_by_role("heading", name="My survey dashboard").wait_for()
        surveyor_card = self.page.locator(
            "#houses-list article[data-house-id='TEAM-ASSIGN-1']"
        )
        surveyor_card.wait_for()
        surveyor_card.locator(".surveyor-house-action").click()
        self.page.locator("#surveyor-room:not(.hidden)").wait_for()
        work_plan = self.page.locator("#surveyor-work-plan-form")
        for trade in ("scaffolding", "esb", "plumber"):
            work_plan.locator(f"[name='{trade}_required']").select_option("false")
        work_plan.locator("button[type='submit']").click()
        self.page.wait_for_function(
            """() => document.querySelector('#toast').textContent.includes(
              'Pre-start work plan for TEAM-ASSIGN-1 saved'
            )"""
        )
        self.page.locator("#survey-perimeter").fill("42.5")
        self.page.locator("#house-survey-form button[type='submit']").click()
        self.page.locator("#surveyor-room").wait_for(state="hidden")
        with server.connect_db() as db:
            survey = db.execute(
                "SELECT updated_by, data FROM house_surveys WHERE house_id = ?",
                ("TEAM-ASSIGN-1",),
            ).fetchone()
        self.assertEqual(survey["updated_by"], "Surveyor One")
        self.assertEqual(json.loads(survey["data"])["perimeter_m"], 42.5)

        self.page.locator("#account-button").click()
        self.page.locator("#account-sign-out").click()
        self.page.locator("#auth-screen:not(.hidden)").wait_for()
        self.page.locator("#auth-username").fill("cathy-assign")
        self.page.locator("#auth-pin").fill("234567")
        self.page.locator("#auth-submit").click()
        self.page.locator("#assign-houses-dashboard").wait_for(state="visible")
        self.page.locator("[data-assign-house-filter='ready']").click()
        self.page.wait_for_function(
            """() => {
              const button = document.querySelector(
                "#houses-list [data-house-id='TEAM-ASSIGN-1'] .assign-house-activate"
              );
              return button && !button.disabled;
            }"""
        )
        self.assertTrue(
            self.page.locator(
                "#houses-list [data-house-id='TEAM-ASSIGN-1'] .status-pill.survey-complete"
            ).inner_text().startswith("Survey completed")
        )
        self.page.locator(
            "#houses-list [data-house-id='TEAM-ASSIGN-1'] .assign-house-activate"
        ).click()
        supervisor_dialog = self.page.locator("#assign-supervisor-dialog")
        supervisor_dialog.wait_for(state="visible")
        self.assertIn(
            "TEAM-ASSIGN-1",
            supervisor_dialog.locator("#assign-supervisor-house-label").inner_text(),
        )
        supervisor_option = self.page.locator(
            f'#activate-house-supervisor-options input[value="{supervisor_id}"]'
        )
        self.assertTrue(supervisor_option.is_visible())
        self.assertFalse(self.page.locator("#confirm-house-activation").is_enabled())
        supervisor_option.check()
        selected_supervisor = supervisor_option.locator("..")
        self.assertTrue(selected_supervisor.evaluate("(option) => option.classList.contains('selected')"))
        self.assertEqual(
            selected_supervisor.locator(".supervisor-account-status").inner_text(),
            "Selected",
        )
        self.assertTrue(self.page.locator("#confirm-house-activation").is_enabled())
        self.page.locator("#confirm-house-activation").click()
        self.page.locator(
            "#houses-list article[data-house-id='TEAM-ASSIGN-1']"
        ).wait_for(state="detached")
        with server.connect_db() as db:
            house = db.execute(
                "SELECT employee_name FROM houses WHERE id = 'TEAM-ASSIGN-1'"
            ).fetchone()
        self.assertEqual(house["employee_name"], "Browser Supervisor")
        self.assertEqual(
            self.page.evaluate(
                """async () => (await fetch('/api/movements')).status"""
            ),
            403,
        )
        self.assertNotIn(
            "materials",
            self.page.locator("#page-houses").inner_text().lower(),
        )
        self.assertEqual(self.browser_errors, [])

    def test_cross_account_survey_plumbing_and_ventilation_workflow(self):
        house_id = "ROLE-FLOW-1"
        with server.connect_db() as db:
            db.execute(
                """
                INSERT INTO provided_houses(id, postcode, owner_name, ready_for_start)
                VALUES (?, 'T1', 'Role Flow Owner', 1)
                """,
                (house_id,),
            )
            db.execute(
                """
                INSERT INTO house_status_history(house_id, status, changed_by, changed_at)
                VALUES (?, 'ready', 'Cathy', '2026-10-29T11:00:00')
                """,
                (house_id,),
            )
            cathy_id = server._insert_user(
                db, "flow-cathy", "Cathy Flow", "234567", "assign"
            )
            surveyor_id = server._insert_user(
                db, "flow-surveyor", "Surveyor Flow", "345678", "surveyor"
            )
            ewi_supervisor_id = server._insert_user(
                db,
                "flow-ewi-supervisor",
                "EWI Flow Supervisor",
                "456780",
                "supervisor",
                "External Wall Insulation",
            )
            plumbing_supervisor_id = server._insert_user(
                db,
                "flow-plumbing-supervisor",
                "Plumbing Flow Supervisor",
                "456781",
                "supervisor",
                "Plumbing",
            )
            server._insert_user(
                db,
                "flow-plumbing-coordinator",
                "Plumbing Flow Coordinator",
                "456782",
                "plumbing_coordinator",
            )
            server._insert_user(
                db, "flow-plumber", "Plumber Flow", "456783", "plumber"
            )
            server._insert_user(
                db,
                "flow-vent-coordinator",
                "Vent Flow Coordinator",
                "456784",
                "ventilation_coordinator",
            )
            vent_teamlead_id = server._insert_user(
                db,
                "flow-vent-teamlead",
                "Vent Teamlead Flow",
                "456785",
                "teamlead",
                team_name="Ventilation",
            )
        def sign_in(username, pin, display_name):
            if self.page.locator(".app-shell:not(.hidden)").is_visible():
                self.page.locator("#account-button").click()
                self.page.locator("#account-sign-out").click()
                self.page.locator("#auth-screen:not(.hidden)").wait_for()
            self.page.locator("#auth-username").fill(username)
            self.page.locator("#auth-pin").fill(pin)
            self.page.locator("#auth-submit").click()
            self.page.locator(".app-shell:not(.hidden)").wait_for()
            self.page.reload(wait_until="domcontentloaded")
            self.page.locator(".app-shell:not(.hidden)").wait_for()
            self.page.wait_for_function(
                "(name) => document.querySelector('#account-name').textContent === name",
                arg=display_name,
            )

        sign_in("flow-cathy", "234567", "Cathy Flow")
        self.page.locator("#assign-houses-dashboard").wait_for(state="visible")
        self.page.locator("[data-assign-house-filter='ready']").click()
        card = self.page.locator(
            f"#houses-list article[data-house-id='{house_id}']"
        )
        card.locator(".assign-house-team").click()
        self.page.locator("#house-assignment-dashboard").wait_for(state="visible")
        surveyor_option = self.page.locator(
            f"#house-assignment-surveyor input[name='surveyor_user_id'][value='{surveyor_id}']"
        )
        surveyor_option.check()
        self.page.locator("#save-house-assignments").click()
        self.page.locator("#house-assignment-dashboard").wait_for(state="hidden")
        with server.connect_db() as db:
            assignment = db.execute(
                "SELECT user_id FROM house_role_assignments "
                "WHERE house_id = ? AND role = 'surveyor'",
                (house_id,),
            ).fetchone()
        self.assertEqual(assignment["user_id"], surveyor_id)

        sign_in("flow-surveyor", "345678", "Surveyor Flow")
        surveyor_card = self.page.locator(
            f"#houses-list article[data-house-id='{house_id}']"
        )
        surveyor_card.wait_for()
        surveyor_card.locator(".surveyor-house-action").click()
        self.page.locator("#surveyor-room:not(.hidden)").wait_for()
        work_plan = self.page.locator("#surveyor-work-plan-form")
        work_plan.locator("[name='scaffolding_required']").select_option("false")
        work_plan.locator("[name='esb_required']").select_option("false")
        work_plan.locator("[name='plumber_required']").select_option("true")
        work_plan.locator("[name='plumber_notes']").fill("Replace the supply line")
        work_plan.locator("[name='vent_required']").select_option("true")
        work_plan.locator("[name='vent_notes']").fill("Check extractor routing")
        with self.page.expect_response(
            lambda response: response.url.endswith("/api/house-work-plan")
            and response.status == 200
        ):
            work_plan.locator("button[type='submit']").click()
        work_plan.locator("[name='vent_notes']").fill("Check extractor and airflow")
        with self.page.expect_response(
            lambda response: response.url.endswith("/api/house-work-plan")
            and response.status == 200
        ):
            work_plan.locator("button[type='submit']").click()
        self.page.locator("#survey-perimeter").fill("42.5")
        self.page.locator("#house-survey-form button[type='submit']").click()
        self.page.locator("#surveyor-room").wait_for(state="hidden")
        with server.connect_db() as db:
            survey = db.execute(
                "SELECT updated_by, data FROM house_surveys WHERE house_id = ?",
                (house_id,),
            ).fetchone()
            plan = db.execute(
                """
                SELECT plan_plumber_required, plan_plumber_notes,
                       plan_vent_required, plan_vent_notes
                FROM provided_houses WHERE id = ?
                """,
                (house_id,),
            ).fetchone()
        self.assertEqual(survey["updated_by"], "Surveyor Flow")
        self.assertEqual(json.loads(survey["data"])["perimeter_m"], 42.5)
        self.assertEqual(
            tuple(plan),
            (1, "Replace the supply line", 1, "Check extractor and airflow"),
        )

        sign_in("flow-vent-coordinator", "456784", "Vent Flow Coordinator")
        self.assertTrue(
            self.page.locator("#ventilation-coordinator-workload").is_visible(),
            self.page.locator("#ventilation-coordinator-workload").get_attribute(
                "class"
            ),
        )
        with self.page.expect_response(
            lambda response: response.url.endswith("/api/dashboard")
            and response.status == 200
        ):
            self.page.locator("#refresh-ventilation-coordinator-houses").click()
        vent_coordinator_card = self.page.locator(
            "#ventilation-coordinator-house-list .ventilation-house-card"
        ).filter(has_text=house_id)
        vent_coordinator_card.wait_for()
        vent_option = vent_coordinator_card.locator(
            f"input[name='teamlead_user_id'][value='{vent_teamlead_id}']"
        )
        vent_option.check()
        vent_coordinator_card.locator("button[type='submit']").click()
        self.page.wait_for_function(
            "() => document.querySelector('#toast').textContent.includes('ROLE-FLOW-1 assigned to Ventilation')"
        )
        with server.connect_db() as db:
            assignment = db.execute(
                "SELECT teamlead_user_id FROM house_vent_assignments WHERE house_id = ?",
                (house_id,),
            ).fetchone()
        self.assertEqual(assignment["teamlead_user_id"], vent_teamlead_id)

        sign_in("flow-plumber", "456783", "Plumber Flow")
        self.page.locator("#stat-ready").click()
        plumber_check = self.page.locator(
            f"#houses-list .work-plan-check[data-house-id='{house_id}'][data-trade='plumber']"
        )
        plumber_check.wait_for()
        plumber_check.click()
        self.page.wait_for_function(
            """() => document.querySelector(
              "#houses-list .work-plan-check[data-house-id='ROLE-FLOW-1'][data-trade='plumber']"
            )?.getAttribute("aria-pressed") === "true" """
        )
        plumber_check = self.page.locator(
            f"#houses-list .work-plan-check[data-house-id='{house_id}'][data-trade='plumber']"
        )
        plumber_check.click()
        self.page.wait_for_function(
            """() => document.querySelector(
              "#houses-list .work-plan-check[data-house-id='ROLE-FLOW-1'][data-trade='plumber']"
            )?.getAttribute("aria-pressed") === "false" """
        )
        plumber_check.click()
        self.page.wait_for_function(
            """() => document.querySelector(
              "#houses-list .work-plan-check[data-house-id='ROLE-FLOW-1'][data-trade='plumber']"
            )?.getAttribute("aria-pressed") === "true" """
        )
        with server.connect_db() as db:
            completion = db.execute(
                "SELECT plan_plumber_done_by FROM provided_houses WHERE id = ?",
                (house_id,),
            ).fetchone()
        self.assertEqual(completion["plan_plumber_done_by"], "Plumber Flow")

        sign_in("flow-vent-teamlead", "456785", "Vent Teamlead Flow")
        vent_card = self.page.locator(
            "#ventilation-house-list .ventilation-house-card"
        ).filter(has_text=house_id)
        vent_completion = vent_card.get_by_role(
            "button", name="Mark Vent work done"
        )
        vent_completion.wait_for()
        vent_completion.click()
        self.page.wait_for_function(
            """houseId => [...document.querySelectorAll(
              "#ventilation-house-list .ventilation-house-card"
            )].some(card => card.textContent.includes(houseId) &&
              card.classList.contains("is-complete"))""",
            arg=house_id,
        )
        with server.connect_db() as db:
            completion = db.execute(
                "SELECT plan_vent_done_by FROM provided_houses WHERE id = ?",
                (house_id,),
            ).fetchone()
        self.assertEqual(completion["plan_vent_done_by"], "Vent Teamlead Flow")

        sign_in("flow-cathy", "234567", "Cathy Flow")
        self.page.locator("[data-assign-house-filter='ready']").click()
        activation = self.page.locator(
            f"#houses-list [data-house-id='{house_id}'] .assign-house-activate"
        )
        activation.wait_for()
        self.assertTrue(activation.is_enabled())
        activation.click()
        supervisor_dialog = self.page.locator("#assign-supervisor-dialog")
        supervisor_dialog.wait_for(state="visible")
        ewi_option = self.page.locator(
            f"#activate-house-supervisor-options input[value='{ewi_supervisor_id}']"
        )
        ewi_option.check()
        self.page.locator("#confirm-house-activation").click()
        self.page.locator(
            f"#houses-list article[data-house-id='{house_id}']"
        ).wait_for(state="detached")
        with server.connect_db() as db:
            active_house = db.execute(
                "SELECT id, employee_name FROM houses WHERE id = ?",
                (house_id,),
            ).fetchone()
        self.assertIsNotNone(active_house)
        self.assertEqual(active_house["employee_name"], "EWI Flow Supervisor")

        sign_in("flow-plumbing-coordinator", "456782", "Plumbing Flow Coordinator")
        active_plumbing_card = self.page.locator(
            "#plumbing-house-list .plumbing-house-card"
        ).filter(has_text=house_id)
        active_plumbing_card.wait_for()
        plumbing_option = active_plumbing_card.locator(
            f"input[name='supervisor_user_id'][value='{plumbing_supervisor_id}']"
        )
        plumbing_option.check()
        active_plumbing_card.locator("button[type='submit']").click()
        self.page.wait_for_function(
            "() => document.querySelector('#toast').textContent.includes('ROLE-FLOW-1 assigned to Plumbing Flow Supervisor')"
        )
        with server.connect_db() as db:
            assignment = db.execute(
                "SELECT user_id FROM house_role_assignments "
                "WHERE house_id = ? AND role = 'supervisor'",
                (house_id,),
            ).fetchone()
        self.assertEqual(assignment["user_id"], plumbing_supervisor_id)
        self.assertIn("Plumbing Flow Supervisor", active_plumbing_card.inner_text())

        sign_in("flow-plumbing-supervisor", "456781", "Plumbing Flow Supervisor")
        active_plumbing_work = self.page.locator(
            f"#plumbing-supervisor-house-list .plumbing-completion-toggle[data-house-id='{house_id}']"
        )
        active_plumbing_work.wait_for()
        active_plumbing_work.click()
        self.page.wait_for_function(
            """() => document.querySelector(
              "#plumbing-supervisor-house-list .plumbing-completion-toggle[data-house-id='ROLE-FLOW-1']"
            )?.getAttribute("aria-pressed") === "true" """
        )
        active_plumbing_work = self.page.locator(
            f"#plumbing-supervisor-house-list .plumbing-completion-toggle[data-house-id='{house_id}']"
        )
        active_plumbing_work.click()
        self.page.wait_for_function(
            """() => document.querySelector(
              "#plumbing-supervisor-house-list .plumbing-completion-toggle[data-house-id='ROLE-FLOW-1']"
            )?.getAttribute("aria-pressed") === "false" """
        )
        active_plumbing_work.click()
        self.page.wait_for_function(
            """() => document.querySelector(
              "#plumbing-supervisor-house-list .plumbing-completion-toggle[data-house-id='ROLE-FLOW-1']"
            )?.getAttribute("aria-pressed") === "true" """
        )
        with server.connect_db() as db:
            checklist = db.execute(
                "SELECT plumber_status FROM house_checklists WHERE house_id = ?",
                (house_id,),
            ).fetchone()
        self.assertEqual(checklist["plumber_status"], "done")

        sign_in("flow-vent-teamlead", "456785", "Vent Teamlead Flow")
        self.page.locator("#ventilation-house-list").wait_for(state="visible")
        assigned_vent_house = self.page.locator(
            "#ventilation-house-list .ventilation-house-card"
        ).filter(has_text=house_id)
        assigned_vent_house.wait_for()
        self.assertIn("Ventilation complete", assigned_vent_house.inner_text())
        self.assertIn("Completed by Vent Teamlead Flow", assigned_vent_house.inner_text())

        sign_in("navigation-test-admin", "123456", "Navigation Test Admin")
        self.page.locator('[data-tab="houses"]').click()
        active_house_card = self.page.locator(
            "#houses-list .house-card"
        ).filter(has_text=house_id)
        active_house_card.wait_for()
        active_house_card.locator(".house-finish-toggle").click()
        self.page.wait_for_function(
            "() => document.querySelector('#toast').textContent.includes('marked finished')"
        )
        self.page.locator('[data-tab="overview"]').click()
        self.page.locator('[data-house-view="closed"]').click()
        closed_house_card = self.page.locator(
            "#houses-list .house-card"
        ).filter(has_text=house_id)
        closed_house_card.wait_for()
        closed_house_card.locator(".house-finish-toggle").click()
        self.page.wait_for_function(
            "() => document.querySelector('#toast').textContent.includes('reopened')"
        )
        self.page.locator('[data-tab="overview"]').click()
        self.page.locator('[data-house-view="active"]').click()
        self.page.locator("#houses-list .house-card").filter(
            has_text=house_id
        ).wait_for()
        with server.connect_db() as db:
            statuses = [
                row["status"]
                for row in db.execute(
                    "SELECT status FROM house_status_history WHERE house_id = ? "
                    "ORDER BY id",
                    (house_id,),
                )
            ]
        self.assertEqual(statuses, ["ready", "active", "closed", "active"])
        self.assertEqual(self.browser_errors, [])

    def test_navigation_tour_reaches_every_workspace_view_without_saving(self):
        self.page.locator("#start-navigation-tour").click()
        tour = self.page.locator("#navigation-tour")
        self.assertTrue(tour.is_visible())
        self.assertEqual(self.active_page(), "overview")
        expected = [
            ("Overview", "overview"),
            ("Inventory", "inventory"),
            ("Houses", "houses"),
            ("Teams", "houses"),
            ("House map", "houses"),
            ("Movements", "movements"),
        ]
        for index, (title, page_name) in enumerate(expected[:2]):
            self.assertEqual(
                self.page.locator("#navigation-tour-title").inner_text(),
                title,
            )
            self.assertEqual(self.active_page(), page_name)
            if index == 0:
                self.page.locator("#navigation-tour-next").click()
        self.page.locator("#navigation-tour-previous").click()
        self.assertEqual(self.page.locator("#navigation-tour-title").inner_text(), "Overview")
        self.assertEqual(self.active_page(), "overview")
        self.page.locator("#navigation-tour-next").click()
        for index, (title, page_name) in enumerate(expected[1:], start=1):
            self.assertEqual(
                self.page.locator("#navigation-tour-title").inner_text(),
                title,
            )
            self.assertEqual(self.active_page(), page_name)
            if title == "Teams":
                self.assertTrue(self.page.locator("#teams-section").is_visible())
            if title == "House map":
                self.assertTrue(self.page.locator("#house-map-view").is_visible())
            if index < len(expected) - 1:
                self.page.locator("#navigation-tour-next").click()
        self.page.locator("#navigation-tour-next").click()
        self.assertFalse(tour.is_visible())
        self.assertEqual(self.active_page(), "movements")
        self.page.locator("#start-navigation-tour").click()
        self.page.locator("#close-navigation-tour").click()
        self.assertFalse(tour.is_visible())
        with server.connect_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM provided_houses").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM houses").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM movements").fetchone()[0], 0)
        self.assertEqual(self.browser_errors, [])

    def test_dark_theme_keeps_inventory_overview_and_ready_cards_readable(self):
        logo = self.page.locator(".surveyor-brand img")
        logo.wait_for()
        self.page.wait_for_function(
            "() => document.querySelector('.surveyor-brand img').naturalWidth > 0"
        )
        self.assertEqual(
            logo.get_attribute("src"),
            "/brusna-curious-cat-logo.png",
        )
        self.assertIn("Curious Cat", logo.get_attribute("alt"))
        for branded_image in self.page.locator(
            "#auth-screen .auth-logo, .mobile-brand .brand-logo"
        ).all():
            self.assertEqual(
                branded_image.get_attribute("src"),
                "/brusna-curious-cat-logo.png",
            )
        self.assertEqual(
            logo.evaluate(
                """(image) => {
                    const canvas = document.createElement("canvas");
                    canvas.width = image.naturalWidth;
                    canvas.height = image.naturalHeight;
                    const context = canvas.getContext("2d");
                    context.drawImage(image, 0, 0);
                    return context.getImageData(0, 0, 1, 1).data[3];
                }"""
            ),
            0,
        )
        self.assertFalse(self.page.locator(".sidebar-tommy").count())
        with server.connect_db() as db:
            db.execute(
                "INSERT INTO materials(code, name, unit, opening_qty) "
                "VALUES ('DARK-TEST', 'Readable test material', 'units', 12)"
            )
            db.execute(
                "INSERT INTO provided_houses(id, postcode, owner_name, source, ready_for_start) "
                "VALUES ('DARK-READY-TEST', 'D01 TEST', 'Dark Theme Owner', 'government', 1)"
            )
            db.execute(
                "INSERT INTO provided_houses(id, postcode, owner_name, source) "
                "VALUES ('DARK-PENDING-TEST', 'D02 TEST', 'Pending Theme Owner', 'government')"
            )
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator("#theme-toggle").click()
        self.assertEqual(self.page.locator("html").get_attribute("data-theme"), "dark")

        self.page.locator('[data-tab="inventory"]').click()
        inventory_row = self.page.locator("#inventory-table tr").filter(
            has_text="Readable test material"
        )
        inventory_row.wait_for()
        self.assertEqual(
            inventory_row.evaluate("(row) => getComputedStyle(row).display"),
            "table-row",
        )
        self.assertEqual(
            inventory_row.locator(".material-name").evaluate(
                "(element) => getComputedStyle(element).color"
            ),
            "rgb(237, 246, 240)",
        )

        self.page.locator('[data-tab="overview"]').click()
        self.page.locator(".overview-dashboard-summary").wait_for()
        self.assertTrue(self.page.locator('[data-house-view="ready"]').is_visible())
        self.assertEqual(self.page.locator("html").get_attribute("data-theme"), "dark")
        self.assertEqual(self.active_page(), "overview")
        self.page.locator('[data-house-view="ready"]').click()
        self.assertEqual(self.active_page(), "houses")
        ready_card = self.page.locator(
            '.house-plan-ready[data-house-id="DARK-READY-TEST"]'
        )
        ready_card.wait_for()
        ready_step = ready_card.locator(
            ".house-workflow-steps li.current.workflow-stage-2 > span"
        )
        not_started_step = ready_card.locator(
            ".house-workflow-steps li.complete.workflow-stage-1 > span"
        )
        self.assertEqual(
            not_started_step.evaluate(
                "(element) => getComputedStyle(element).backgroundColor"
            ),
            "rgb(27, 40, 35)",
        )
        self.assertEqual(
            not_started_step.evaluate(
                "(element) => getComputedStyle(element).borderTopColor"
            ),
            "rgb(83, 104, 97)",
        )
        self.assertEqual(
            ready_step.evaluate(
                "(element) => getComputedStyle(element).backgroundColor"
            ),
            "rgb(36, 88, 136)",
        )
        self.assertEqual(
            ready_step.evaluate(
                "(element) => getComputedStyle(element).borderTopColor"
            ),
            "rgb(88, 150, 209)",
        )
        self.assertEqual(
            ready_card.locator(".house-body").evaluate(
                "(element) => getComputedStyle(element).overflowY"
            ),
            "visible",
        )
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.assertLessEqual(
            ready_card.locator(".house-body").evaluate(
                "(element) => element.scrollHeight - element.clientHeight"
            ),
            1,
        )
        self.page.locator('[data-tab="overview"]').click()
        self.page.locator('[data-house-view="not_started"]').click()
        pending_card = self.page.locator(
            '.house-plan-pending[data-house-id="DARK-PENDING-TEST"]'
        )
        pending_card.wait_for()
        self.assertEqual(
            pending_card.evaluate("(element) => getComputedStyle(element).backgroundImage"),
            "linear-gradient(145deg, rgb(32, 45, 43) 0%, rgb(26, 37, 38) 100%)",
        )
        self.assertEqual(
            pending_card.locator(".house-avatar").evaluate(
                "(element) => getComputedStyle(element).backgroundColor"
            ),
            "rgb(41, 58, 62)",
        )
        self.assertEqual(
            pending_card.locator(".status-pill.pending").evaluate(
                "(element) => getComputedStyle(element).backgroundColor"
            ),
            "rgb(40, 58, 64)",
        )
        self.assertEqual(
            pending_card.locator(".house-workflow").evaluate(
                "(element) => getComputedStyle(element).backgroundImage"
            ),
            "linear-gradient(145deg, rgb(36, 51, 47), rgb(32, 46, 44))",
        )
        self.assertEqual(
            pending_card.locator(".house-workflow-heading strong").evaluate(
                "(element) => getComputedStyle(element).color"
            ),
            "rgb(237, 245, 239)",
        )
        self.assertEqual(
            pending_card.locator(".house-workflow-steps li.current > span").evaluate(
                "(element) => getComputedStyle(element).backgroundColor"
            ),
            "rgb(53, 47, 33)",
        )
        self.assertEqual(
            pending_card.locator(".house-workflow-steps li.current > span").evaluate(
                "(element) => getComputedStyle(element).borderTopColor"
            ),
            "rgb(227, 177, 94)",
        )
        self.assertEqual(
            pending_card.locator(".house-workflow-hint").evaluate(
                "(element) => getComputedStyle(element).color"
            ),
            "rgb(175, 192, 187)",
        )
        self.assertEqual(
            pending_card.locator(".plan-ready-toggle").evaluate(
                "(element) => getComputedStyle(element).color"
            ),
            "rgb(242, 255, 246)",
        )
        self.page.locator("#house-detail").evaluate(
            "(element) => element.classList.remove('hidden')"
        )
        back_button = self.page.locator("#back-to-houses")
        back_button.wait_for()
        self.assertEqual(
            back_button.evaluate("(element) => getComputedStyle(element).color"),
            "rgb(220, 235, 225)",
        )
        self.assertEqual(
            back_button.evaluate("(element) => getComputedStyle(element).backgroundColor"),
            "rgb(37, 55, 47)",
        )
        self.page.locator('[data-tab="movements"]').click()
        movement_card = self.page.locator("#page-movements .card").first
        self.assertTrue(movement_card.is_visible())
        self.assertNotEqual(
            movement_card.evaluate(
                "(element) => getComputedStyle(element).backgroundColor"
            ),
            "rgb(255, 255, 255)",
        )
        self.assertEqual(self.browser_errors, [])

    def test_topbar_brand_logo_fits_its_brand_area(self):
        logo = self.page.locator(".surveyor-brand img")
        self.page.wait_for_function(
            "() => document.querySelector('.surveyor-brand img').naturalWidth > 0"
        )
        self.assertAlmostEqual(
            logo.evaluate(
                "(image) => image.getBoundingClientRect().width - image.parentElement.getBoundingClientRect().width"
            ),
            0,
            delta=1,
        )

    def test_dark_seai_insulation_measures_have_readable_scrollable_table(self):
        self.page.evaluate(
            """() => {
                document.documentElement.dataset.theme = "dark";
                const card = document.createElement("article");
                card.innerHTML = `
                    <details class="survey-room-seai-section survey-room-seai-measures" open>
                        <summary>Measures <span>1</span></summary>
                        <div class="table-wrap survey-room-seai-table">
                            <table>
                                <thead><tr><th>Name</th><th>Quantity</th></tr></thead>
                                <tbody><tr><td>External Wall Insulation</td><td>
                                    <div class="seai-variation-cell">
                                        <input class="seai-variation-input" value="104">
                                        <button class="button button-secondary seai-variation-edit">Edit</button>
                                    </div>
                                </td></tr></tbody>
                            </table>
                        </div>
                    </details>`;
                document.querySelector(".app-shell").append(card);
            }"""
        )
        section = self.page.locator(".survey-room-seai-measures")
        table_wrap = section.locator(".survey-room-seai-table")
        self.assertEqual(
            section.evaluate("(el) => getComputedStyle(el).backgroundColor"),
            "rgb(25, 38, 34)",
        )
        self.assertEqual(
            section.locator("summary").evaluate("(el) => getComputedStyle(el).color"),
            "rgb(226, 238, 230)",
        )
        self.assertEqual(
            table_wrap.evaluate("(el) => getComputedStyle(el).overflow"),
            "auto",
        )
        self.assertEqual(
            table_wrap.locator("thead").evaluate("(el) => getComputedStyle(el).position"),
            "sticky",
        )
        self.assertEqual(
            section.locator("tbody td").first.evaluate(
                "(el) => getComputedStyle(el).color"
            ),
            "rgb(220, 232, 225)",
        )
        self.assertEqual(
            section.locator(".seai-variation-input").evaluate(
                "(el) => getComputedStyle(el).backgroundColor"
            ),
            "rgb(23, 33, 30)",
        )

    def test_seai_original_and_annotated_pdfs_have_distinct_styles(self):
        self.page.evaluate(
            """() => {
              const list = document.createElement("div");
              list.innerHTML = `
                <section class="seai-document-category">
                  <div class="seai-document-entry is-original">
                    <span class="seai-document-name">
                      <span class="seai-document-status original">ORIGINAL PDF</span>
                      plan.pdf
                    </span>
                  </div>
                  <div class="seai-document-entry is-annotated">
                    <span class="seai-document-name">
                      <span class="seai-document-status annotated">ANNOTATED COPY</span>
                      plan-annotated.pdf
                    </span>
                  </div>
                </section>`;
              document.querySelector(".app-shell").append(list);
            }"""
        )
        original = self.page.locator(".seai-document-entry.is-original")
        annotated = self.page.locator(".seai-document-entry.is-annotated")
        self.assertEqual(original.locator(".seai-document-status").inner_text(), "ORIGINAL PDF")
        self.assertEqual(annotated.locator(".seai-document-status").inner_text(), "ANNOTATED COPY")
        light_colors = self.page.evaluate(
            """() => Object.fromEntries(["original", "annotated"].map(kind => {
              const row = document.querySelector(`.seai-document-entry.is-${kind}`);
              const badge = row.querySelector(".seai-document-status");
              return [kind, {
                background: getComputedStyle(row).backgroundColor,
                borderLeft: getComputedStyle(row).borderLeftColor,
                badge: getComputedStyle(badge).backgroundColor
              }];
            }))"""
        )
        self.assertEqual(light_colors["original"]["background"], "rgb(241, 243, 245)")
        self.assertEqual(light_colors["annotated"]["background"], "rgb(237, 245, 255)")
        self.assertNotEqual(light_colors["original"]["borderLeft"], light_colors["annotated"]["borderLeft"])
        self.assertEqual(light_colors["original"]["badge"], "rgb(71, 85, 105)")
        self.assertEqual(light_colors["annotated"]["badge"], "rgb(37, 99, 235)")
        self.page.evaluate('document.documentElement.dataset.theme = "dark"')
        dark_colors = self.page.evaluate(
            """() => Object.fromEntries(["original", "annotated"].map(kind => {
              const row = document.querySelector(`.seai-document-entry.is-${kind}`);
              return [kind, {
                background: getComputedStyle(row).backgroundColor,
                borderLeft: getComputedStyle(row).borderLeftColor
              }];
            }))"""
        )
        self.assertEqual(dark_colors["original"]["background"], "rgb(40, 50, 61)")
        self.assertEqual(dark_colors["annotated"]["background"], "rgb(29, 53, 82)")
        self.assertNotEqual(dark_colors["original"]["borderLeft"], dark_colors["annotated"]["borderLeft"])

    def test_dark_not_started_card_seai_insulation_summary_is_readable(self):
        self.page.evaluate(
            """() => {
                document.documentElement.dataset.theme = "dark";
                const list = document.createElement("section");
                list.id = "houses-list";
                list.className = "house-grid house-grid-compact";
                list.innerHTML = `
                    <article class="card house-card house-pending house-plan-pending">
                        <div class="house-insulation-summary">
                            <h3>SEAI insulation measures</h3>
                            <div class="house-insulation-header">
                                <span>Measure</span><span>Survey qty</span><span>Including variation</span>
                            </div>
                            <div class="house-insulation-row">
                                <strong>External Wall Insulation</strong>
                                <div role="cell"><span class="house-insulation-quantity">94 m²
                                    <small>External Wall Insulation: Greater than 85m2</small>
                                </span></div>
                                <div role="cell"><span class="house-insulation-quantity">104 m²
                                    <small>External Wall Insulation: Greater than 85m2</small>
                                </span></div>
                            </div>
                            <small>Insulation quantities are shown in m² as recorded in the SEAI application.</small>
                        </div>
                        <div class="house-card-actions">
                            <button class="text-button plan-ready-toggle" data-ready="true">Mark ready for start</button>
                        </div>
                    </article>`;
                document.querySelector(".app-shell").append(list);
            }"""
        )
        summary = self.page.locator(
            "#houses-list.house-grid-compact .house-insulation-summary"
        )
        self.assertEqual(
            summary.evaluate("(el) => getComputedStyle(el).backgroundColor"),
            "rgb(25, 38, 34)",
        )
        self.assertEqual(
            summary.locator("h3").evaluate("(el) => getComputedStyle(el).color"),
            "rgb(220, 235, 225)",
        )
        self.assertEqual(
            summary.locator(".house-insulation-header").evaluate(
                "(el) => getComputedStyle(el).color"
            ),
            "rgb(169, 189, 176)",
        )
        self.assertEqual(
            summary.locator(".house-insulation-quantity").first.evaluate(
                "(el) => getComputedStyle(el).color"
            ),
            "rgb(226, 238, 230)",
        )
        self.assertEqual(
            summary.locator(".house-insulation-quantity small").first.evaluate(
                "(el) => getComputedStyle(el).color"
            ),
            "rgb(169, 187, 176)",
        )
        actions = self.page.locator(
            "#houses-list.house-grid-compact .house-card-actions"
        )
        self.assertEqual(
            actions.evaluate("(el) => getComputedStyle(el).backgroundImage"),
            "linear-gradient(rgb(29, 43, 38), rgb(23, 35, 31))",
        )
        self.assertEqual(
            actions.evaluate("(el) => getComputedStyle(el).borderTopColor"),
            "rgb(57, 77, 67)",
        )
        primary_action = actions.locator(".plan-ready-toggle")
        self.assertEqual(
            primary_action.evaluate("(el) => getComputedStyle(el).backgroundImage"),
            "linear-gradient(135deg, rgb(40, 110, 74), rgb(32, 93, 64))",
        )
        self.assertEqual(
            primary_action.evaluate("(el) => getComputedStyle(el).color"),
            "rgb(242, 255, 246)",
        )
        self.assertEqual(actions.locator("button").count(), 1)

    def test_overview_shows_house_status_shortcuts(self):
        self.page.locator("#page-overview .overview-dashboard-summary").wait_for(
            state="visible"
        )
        for house_view in ("", "ready", "active", "closed", "not_started"):
            shortcut = self.page.locator(
                f'#page-overview [data-house-view="{house_view}"]'
            )
            self.assertTrue(shortcut.is_visible())
            self.assertTrue(shortcut.is_enabled())
        self.assertEqual(self.browser_errors, [])

    def test_teams_tab_can_switch_back_to_houses_after_overview_navigation(self):
        self.page.locator('[data-tab="houses"]').click()
        self.page.locator("#teams-tab").click()
        self.assertTrue(self.page.locator("#teams-section").is_visible())
        self.page.locator("#go-overview").click()
        self.assertEqual(self.active_page(), "overview")
        self.page.locator('[data-tab="houses"]').click()
        self.assertEqual(self.active_page(), "houses")
        self.assertTrue(self.page.locator("#houses-section").is_visible())
        self.assertFalse(self.page.locator("#teams-section").is_visible())
        self.page.locator("#houses-tab").click()
        self.assertTrue(self.page.locator("#houses-section").is_visible())
        self.assertFalse(self.page.locator("#teams-section").is_visible())
        self.assertEqual(
            self.page.locator("#houses-tab").get_attribute("aria-selected"),
            "true",
        )
        self.page.locator("#teams-tab").click()
        self.assertTrue(self.page.locator("#teams-section").is_visible())
        self.page.locator('[data-tab="overview"]').click()
        self.page.locator('[data-tab="houses"]').click()
        self.page.locator("#houses-tab").click()
        self.assertTrue(self.page.locator("#houses-section").is_visible())
        self.assertEqual(self.browser_errors, [])

    def test_admin_can_add_non_login_member_to_teamlead_roster(self):
        with server.connect_db() as db:
            with db:
                server._insert_user(
                    db,
                    "kamil-bulczak",
                    "Kamil Bulczak",
                    "123456",
                    "teamlead",
                )
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator('[data-tab="houses"]').click()
        self.page.locator("#teams-tab").click()
        self.page.locator("#team-roster-form").wait_for(state="visible")
        self.page.locator("#team-roster-teamlead").select_option(label="Kamil Bulczak")
        self.page.locator("#team-roster-form input[name='member_name']").fill(
            "Grzegorz Kaminski"
        )
        self.page.locator("#team-roster-form button[type='submit']").click()
        roster = self.page.locator("#team-roster-list .team-roster-card").filter(
            has=self.page.locator("h2", has_text="Kamil Bulczak")
        )
        roster.get_by_text("Grzegorz Kaminski").wait_for(state="visible")
        self.assertTrue(roster.get_by_text("Grzegorz Kaminski").is_visible())
        self.assertEqual(self.browser_errors, [])

    def test_admin_can_create_named_teamlead_and_add_roster_member(self):
        with server.connect_db() as db:
            karol_id = server._insert_user(
                db,
                "karol",
                "Karol",
                "123456",
                "supervisor",
                "External Wall Insulation",
            )
        self.page.locator("#account-button").click()
        self.page.locator("#account-manage-users").click()
        self.page.locator("#user-form [name='display_name']").fill("Krystian Stelmach")
        self.page.locator("#user-form [name='username']").fill("krystian.stelmach")
        self.page.locator("#user-form [name='role']").select_option("teamlead")
        self.assertEqual(
            self.page.locator("#new-user-manager").input_value(),
            str(karol_id),
        )
        self.page.locator("#user-form [name='team_name']").fill("Ventilation")
        self.assertEqual(
            self.page.locator("#new-user-manager").input_value(),
            "",
        )
        self.page.locator("#user-form [name='pin']").fill("123456")
        self.page.locator("#user-form [name='pin_confirm']").fill("123456")
        self.page.locator("#user-form button[type='submit']").click()
        self.page.locator(
            "#user-list .user-row-identity", has_text="Krystian Stelmach"
        ).wait_for()
        self.page.locator("#users-dialog .close-dialog").click()

        self.page.locator('[data-tab="houses"]').click()
        self.page.locator("#teams-tab").click()
        self.page.locator("#team-roster-teamlead").select_option(
            label="Ventilation · Krystian Stelmach"
        )
        self.page.locator("#team-roster-form input[name='member_name']").fill(
            "Nariman Ibrahimov"
        )
        self.page.locator("#team-roster-form button[type='submit']").click()
        roster = self.page.locator("#team-roster-list .team-roster-card").filter(
            has=self.page.locator("h2", has_text="Ventilation")
        )
        roster.get_by_text("Nariman Ibrahimov").wait_for(state="visible")
        self.assertIn("Teamlead: Krystian Stelmach", roster.inner_text())
        self.assertEqual(self.browser_errors, [])

    def test_admin_can_create_department_custom_role_and_assign_permissions(self):
        self.page.locator("#account-button").click()
        self.page.locator("#account-manage-users").click()
        self.page.locator("#department-form [name='name']").fill("Quality Assurance")
        self.page.locator("#department-form button[type='submit']").click()
        self.page.locator("#department-list").get_by_text(
            "Quality Assurance", exact=True
        ).wait_for()

        self.page.locator("#role-form [name='display_name']").fill("Quality Checker")
        self.page.locator("#role-form [name='role_key']").fill("quality_checker")
        self.page.locator("#role-form button[type='submit']").click()
        self.page.locator("#permission-role-select").select_option("quality_checker")
        self.page.locator(
            "#role-permission-list .role-permission-check[value='access.surveyor']"
        ).check()
        self.page.locator("#save-role-permissions").click()
        self.page.get_by_text("Role permissions saved.").wait_for()

        self.page.locator("#user-form [name='display_name']").fill("Quality Checker One")
        self.page.locator("#user-form [name='username']").fill("quality-checker-one")
        self.page.locator("#user-form [name='role']").select_option("quality_checker")
        self.page.locator("#new-user-departments").select_option(
            label="Quality Assurance"
        )
        self.assertEqual(
            self.page.locator("#new-user-manager option").all_text_contents(),
            ["No manager"],
        )
        self.page.locator("#user-form [name='pin']").fill("123456")
        self.page.locator("#user-form [name='pin_confirm']").fill("123456")
        self.page.locator("#user-form button[type='submit']").click()
        self.page.locator(
            "#user-list .user-row-identity", has_text="Quality Checker One"
        ).wait_for()
        with server.connect_db() as db:
            user = db.execute(
                "SELECT id, role FROM app_users WHERE username = ?",
                ("quality-checker-one",),
            ).fetchone()
            self.assertEqual(user["role"], "quality_checker")
            department = db.execute(
                """
                SELECT departments.name
                FROM user_departments memberships
                JOIN departments ON departments.id = memberships.department_id
                WHERE memberships.user_id = ?
                """,
                (user["id"],),
            ).fetchone()
            self.assertEqual(department["name"], "Quality Assurance")
            self.assertTrue(
                db.execute(
                    """
                    SELECT 1 FROM app_role_permissions
                    WHERE role_key = 'quality_checker'
                      AND permission_key = 'access.surveyor'
                      AND is_allowed = 1
                    """
                ).fetchone()
            )

        with server.connect_db() as db:
            admin_id = db.execute(
                "SELECT id FROM app_users WHERE role = 'admin' AND is_active = 1"
            ).fetchone()["id"]
        self.page.locator("#user-form [name='display_name']").fill("Cathy")
        self.page.locator("#user-form [name='username']").fill("cathy-assign")
        self.page.locator("#user-form [name='role']").select_option("assign")
        self.assertEqual(
            self.page.locator("#new-user-manager").input_value(),
            str(admin_id),
        )
        self.page.locator("#user-form [name='pin']").fill("234567")
        self.page.locator("#user-form [name='pin_confirm']").fill("234567")
        self.page.locator("#user-form button[type='submit']").click()
        self.page.locator(
            "#user-list .user-row-identity", has_text="Cathy"
        ).wait_for()
        with server.connect_db() as db:
            user = db.execute(
                "SELECT id, role FROM app_users WHERE username = ?",
                ("cathy-assign",),
            ).fetchone()
            reporting_line = db.execute(
                "SELECT manager_user_id FROM user_reporting_lines WHERE report_user_id = ?",
                (user["id"],),
            ).fetchone()
            self.assertEqual(user["role"], "assign")
            self.assertEqual(reporting_line["manager_user_id"], admin_id)
        self.assertEqual(self.browser_errors, [])

    def test_admin_can_edit_supervisor_specialty_from_account_management(self):
        with server.connect_db() as db:
            with db:
                supervisor_id = server._insert_user(
                    db,
                    "supervisor-specialty",
                    "Specialty Supervisor",
                    "234567",
                    "supervisor",
                )
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator("#account-button").click()
        self.page.locator("#account-manage-users").click()
        specialty_input = self.page.locator(
            f".supervisor-specialty-input[data-user-id='{supervisor_id}']"
        )
        specialty_input.fill("Plumbing")
        self.page.locator(
            f".supervisor-specialty-save[data-user-id='{supervisor_id}']"
        ).click()
        self.assertEqual(specialty_input.input_value(), "Plumbing")
        self.assertEqual(self.browser_errors, [])

    def test_teams_tab_shows_open_and_closed_assigned_house_counts(self):
        with server.connect_db() as db:
            for house_id, is_finished in (
                ("TEAM-OPEN-TEST", 0),
                ("TEAM-CLOSED-TEST", 1),
            ):
                server._upsert_house(
                    db, house_id, employee_name="Assigned Worker"
                )
                db.execute(
                    "UPDATE houses SET is_finished = ? WHERE id = ?",
                    (is_finished, house_id),
                )
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator('[data-tab="houses"]').click()
        self.page.locator("#teams-tab").click()
        team_card = self.page.locator("#teams-list .team-card").filter(
            has=self.page.locator("h2", has_text="Assigned Worker")
        )
        open_count = team_card.locator(".team-house-count-open")
        closed_count = team_card.locator(".team-house-count-closed")
        self.assertTrue(open_count.is_visible())
        self.assertTrue(closed_count.is_visible())
        self.assertEqual(open_count.locator("strong").inner_text(), "1")
        self.assertEqual(closed_count.locator("strong").inner_text(), "1")
        open_count.click()
        self.assertTrue(
            self.page.locator("#houses-list").get_by_text("TEAM-OPEN-TEST").is_visible()
        )
        self.page.locator("#teams-tab").click()
        closed_count = self.page.locator("#teams-list .team-card").filter(
            has=self.page.locator("h2", has_text="Assigned Worker")
        ).locator(".team-house-count-closed")
        closed_count.click()
        self.assertTrue(
            self.page.locator("#houses-list").get_by_text("TEAM-CLOSED-TEST").is_visible()
        )
        self.assertEqual(self.browser_errors, [])

    def test_teams_tab_merges_same_name_employee_and_team_cards(self):
        with server.connect_db() as db:
            with db:
                server._upsert_house(db, "PAIR-JACK", employee_name="Jack")
                server._upsert_house(db, "PAIR-DAVIT-EMP", employee_name="Davit")
                server._upsert_house(db, "PAIR-DAVIT-TEAM")
                db.execute(
                    "UPDATE houses SET is_finished = 1 "
                    "WHERE id IN ('PAIR-DAVIT-EMP', 'PAIR-DAVIT-TEAM')"
                )
                material_id = db.execute(
                    "INSERT INTO materials(name, unit, opening_qty) "
                    "VALUES ('Team grouping test material', 'units', 10)"
                ).lastrowid
                for house_id, team in (
                    ("PAIR-JACK", "Jack"),
                    ("PAIR-DAVIT-TEAM", "Davit"),
                ):
                    db.execute(
                        """
                        INSERT INTO movements(
                            happened_on, kind, material_id, qty, source, destination,
                            house_id, person, team, department, notes, recorded_at
                        ) VALUES (
                            '2026-10-22', 'delivery', ?, 1, 'warehouse', ?, ?,
                            'Test user', ?, '', '', '2026-10-22T10:00:00'
                        )
                        """,
                        (material_id, f"house:{house_id}", house_id, team),
                    )
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator('[data-tab="houses"]').click()
        self.page.locator("#teams-tab").click()

        cards = self.page.locator("#teams-list .team-card")
        jack_card = cards.filter(has=self.page.locator("h2", has_text="Jack"))
        davit_card = cards.filter(has=self.page.locator("h2", has_text="Davit"))
        self.assertEqual(jack_card.count(), 1)
        self.assertEqual(davit_card.count(), 1)
        self.assertEqual(cards.filter(has=self.page.locator("h2", has_text="Team Jack")).count(), 0)
        self.assertEqual(cards.filter(has=self.page.locator("h2", has_text="Team Davit")).count(), 0)
        self.assertEqual(jack_card.locator(".team-house-count-open strong").inner_text(), "1")
        self.assertEqual(davit_card.locator(".team-house-count-closed strong").inner_text(), "2")
        self.assertEqual(jack_card.locator(".team-avatar-icon").count(), 1)

        davit_card.locator(".team-house-count-closed").click()
        house_list = self.page.locator("#houses-list")
        self.assertTrue(house_list.get_by_text("PAIR-DAVIT-EMP").is_visible())
        self.assertTrue(house_list.get_by_text("PAIR-DAVIT-TEAM").is_visible())
        self.assertEqual(self.browser_errors, [])

    def test_active_house_card_shows_employee_and_audit_actors(self):
        with server.connect_db() as db:
            with db:
                server._upsert_house(
                    db,
                    "AUDIT-CARD-TEST",
                    employee_name="Assigned Worker",
                )
                db.execute(
                    """
                    INSERT INTO house_surveys(house_id, data, notes, updated_by, updated_at)
                    VALUES ('AUDIT-CARD-TEST', '{}', '', 'Surveyor Name', '2026-05-02T10:30:00')
                    """
                )
                db.execute(
                    """
                    UPDATE provided_houses
                    SET address = 'Audit Test Address', area_m2 = 100, perimeter_m = 40,
                        plan_vent_required = 1,
                        plan_vent_done_by = 'Vent Team Member',
                        plan_vent_done_at = '2026-11-03T10:00:00'
                    WHERE id = 'AUDIT-CARD-TEST'
                    """
                )
                server._record_house_status(
                    db, "AUDIT-CARD-TEST", "ready", "Ready Actor"
                )
                server._record_house_status(
                    db, "AUDIT-CARD-TEST", "active", "Active Actor"
                )
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator("#stat-houses").click()
        card = self.page.locator("#houses-list .house-card").filter(
            has=self.page.locator("h2", has_text="AUDIT-CARD-TEST")
        )
        self.assertIn("Assigned employee: Assigned Worker", card.inner_text())
        self.assertIn("Ready Actor", card.inner_text())
        self.assertIn("Active Actor", card.inner_text())
        self.assertIn("Survey updated by", card.inner_text())
        self.assertIn("Surveyor Name", card.inner_text())
        self.assertIn("Vent work", card.inner_text())
        self.assertIn("Complete", card.inner_text())
        self.assertIn("Vent Team Member", card.inner_text())
        self.assertEqual(card.locator(".house-property-details").count(), 0)
        self.assertNotIn("Audit Test Address", card.inner_text())
        stage_one = card.locator('.stage-btn[data-stage="1"]')
        self.assertEqual(
            stage_one.evaluate("(element) => getComputedStyle(element).backgroundColor"),
            "rgb(24, 135, 95)",
        )
        card.locator('.stage-btn[data-stage="2"]').click()
        stage_two = card.locator('.stage-btn[data-stage="2"]')
        self.page.wait_for_function(
            """() => [...document.querySelectorAll('#houses-list .stage-btn[data-stage="2"]')].some((button) => button.classList.contains("on"))"""
        )
        self.assertEqual(
            stage_two.evaluate("(element) => getComputedStyle(element).backgroundColor"),
            "rgb(184, 119, 50)",
        )
        self.assertEqual(self.browser_errors, [])

    def test_active_house_card_shows_vent_completion_from_shared_work_plan(self):
        with server.connect_db() as db:
            with db:
                server._upsert_house(db, "VENT-CATHY-CARD")
                db.execute(
                    """
                    UPDATE provided_houses
                    SET plan_vent_required = 1,
                        plan_vent_done_by = 'Vent Team Member',
                        plan_vent_done_at = '2026-11-03T10:00:00'
                    WHERE id = 'VENT-CATHY-CARD'
                    """
                )
                server._record_house_status(
                    db, "VENT-CATHY-CARD", "ready", "Ready Actor"
                )
                server._record_house_status(
                    db, "VENT-CATHY-CARD", "active", "Active Actor"
                )
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator("#stat-houses").click()
        card = self.page.locator("#houses-list .house-card").filter(
            has=self.page.locator("h2", has_text="VENT-CATHY-CARD")
        )
        self.assertIn("Vent work", card.inner_text())
        self.assertIn("Complete", card.inner_text())
        self.assertIn("Vent Team Member", card.inner_text())
        self.assertIn("Completed by Vent Team Member", card.inner_text())
        with server.connect_db() as db:
            db.execute(
                """
                UPDATE provided_houses
                SET plan_vent_done_by = '', plan_vent_done_at = ''
                WHERE id = 'VENT-CATHY-CARD'
                """
            )
        server._record_change("/api/house-work-plan/complete")
        self.page.evaluate("pollTrackerChanges()")
        self.page.wait_for_function(
            """() => [...document.querySelectorAll('#houses-list .house-card')]
              .some(card => card.textContent.includes('VENT-CATHY-CARD') &&
                card.textContent.includes('Outstanding') &&
                !card.textContent.includes('Vent Team Member'))"""
        )
        self.assertIn("Outstanding", card.inner_text())
        self.assertNotIn("Vent Team Member", card.inner_text())
        with server.connect_db() as db:
            db.execute(
                """
                UPDATE provided_houses
                SET plan_vent_done_by = 'Vent Team Member',
                    plan_vent_done_at = '2026-11-03T10:00:00'
                WHERE id = 'VENT-CATHY-CARD'
                """
            )
        server._record_change("/api/house-work-plan/complete")
        self.page.evaluate("pollTrackerChanges()")
        self.page.wait_for_function(
            """() => [...document.querySelectorAll('#houses-list .house-card')]
              .some(card => card.textContent.includes('VENT-CATHY-CARD') &&
                card.textContent.includes('Vent Team Member') &&
                card.textContent.includes('Complete'))"""
        )
        self.assertIn("Completed by Vent Team Member", card.inner_text())
        self.assertEqual(self.browser_errors, [])
        self.assertEqual(self.browser_errors, [])

    def test_house_stage_change_updates_card_dashboard_and_survives_reload(self):
        with server.connect_db() as db:
            server._upsert_house(db, "STAGE-UI-TEST")
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator('[data-tab="houses"]').click()
        card = self.page.locator("#houses-list .house-active").filter(
            has=self.page.locator("h2", has_text="STAGE-UI-TEST")
        )
        stage_two = card.locator('.stage-btn[data-stage="2"]')
        self.assertEqual(stage_two.get_attribute("aria-pressed"), "false")
        with self.page.expect_response(
            lambda response: response.url.endswith("/api/houses/stage")
            and response.status == 200
        ):
            stage_two.click()
        self.page.wait_for_function(
            """() => {
              const card = [...document.querySelectorAll('#houses-list .house-active')]
                .find((item) => item.querySelector('h2')?.textContent === 'STAGE-UI-TEST');
              return card?.querySelector('.stage-btn[data-stage="2"]')?.getAttribute('aria-pressed') === 'true';
            }"""
        )
        with server.connect_db() as db:
            self.assertEqual(
                db.execute(
                    "SELECT stage FROM houses WHERE id = 'STAGE-UI-TEST'"
                ).fetchone()[0],
                2,
            )
        self.page.locator('[data-tab="overview"]').click()
        self.assertEqual(self.page.locator("#stat-houses").inner_text(), "1")
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator('[data-tab="houses"]').click()
        card = self.page.locator("#houses-list .house-active").filter(
            has=self.page.locator("h2", has_text="STAGE-UI-TEST")
        )
        self.assertEqual(
            card.locator('.stage-btn[data-stage="2"]').get_attribute("aria-pressed"),
            "true",
        )
        self.assertEqual(self.browser_errors, [])

    def test_cancelled_movement_is_not_saved_and_submit_cannot_duplicate(self):
        with server.connect_db() as db:
            with db:
                server._upsert_house(db, "MOVEMENT-UI-TEST")
                material_id = db.execute(
                    "INSERT INTO materials(code, name, unit, opening_qty) "
                    "VALUES ('MOVEMENT-UI', 'Movement UI board', 'boards', 5)"
                ).lastrowid
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator('[data-tab="houses"]').click()
        card = self.page.locator("#houses-list .house-active").filter(
            has=self.page.locator("h2", has_text="MOVEMENT-UI-TEST")
        )
        card.locator(".house-record-movement").click()
        self.page.locator("#movement-material-search").fill("Movement UI board")
        self.page.locator("#add-material-line").click()
        self.page.locator("#movement-lines input[type='number']").fill("1")
        self.page.get_by_role("button", name="Cancel").click()
        with server.connect_db() as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM movements WHERE material_id = ?",
                    (material_id,),
                ).fetchone()[0],
                0,
            )

        card.locator(".house-record-movement").click()
        self.page.locator("#movement-material-search").fill("Movement UI board")
        self.page.locator("#add-material-line").click()
        self.page.locator("#movement-lines input[type='number']").fill("1")
        self.page.locator(
            '#movement-dialog .movement-fields[data-for="pickup"] [name="person"]'
        ).fill("Delivery Worker")
        self.page.locator(
            '#movement-dialog .movement-fields[data-for="pickup"] [name="team"]'
        ).fill("Movement Crew")
        save_button = self.page.locator(
            "#movement-dialog button[type='submit']"
        )
        entered_batch = threading.Event()
        release_batch = threading.Event()
        calls_lock = threading.Lock()
        batch_calls = 0
        record_batch = server._record_movement_batch

        def pause_batch(db, payload, allow_house_updates=True):
            nonlocal batch_calls
            with calls_lock:
                batch_calls += 1
            entered_batch.set()
            if not release_batch.wait(timeout=5):
                raise TimeoutError("Timed out waiting to release the UI movement.")
            return record_batch(db, payload, allow_house_updates)

        with patch.object(server, "_record_movement_batch", pause_batch):
            try:
                save_button.click()
                self.assertTrue(entered_batch.wait(timeout=5))
                self.assertTrue(save_button.is_disabled())
                with calls_lock:
                    self.assertEqual(batch_calls, 1)
            finally:
                release_batch.set()
            self.page.wait_for_function(
                "() => !document.querySelector('#movement-dialog').open"
            )

        with server.connect_db() as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM movements WHERE material_id = ?",
                    (material_id,),
                ).fetchone()[0],
                1,
            )
            balances = server.balances_for(db, material_id)
            self.assertEqual(balances["warehouse"], 4)
            self.assertEqual(balances["house:MOVEMENT-UI-TEST"], 1)
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator('[data-tab="houses"]').click()
        self.assertIn("Movement UI board", card.inner_text())
        self.assertEqual(self.browser_errors, [])

    def test_active_house_cards_keep_equal_height_and_scroll_long_content(self):
        self.page.set_viewport_size({"width": 1440, "height": 900})
        with server.connect_db() as db:
            with db:
                for house_id in ("CARD-SCROLL-TEST", "CARD-SHORT-TEST"):
                    server._upsert_house(
                        db,
                        house_id,
                        employee_name=(
                            "Long assigned worker name " * 40
                            if house_id == "CARD-SCROLL-TEST"
                            else ""
                        ),
                    )
                db.executemany(
                    "INSERT INTO house_snags(house_id, text) VALUES (?, ?)",
                    [
                        ("CARD-SCROLL-TEST", f"Long content snag {index}")
                        for index in range(30)
                    ],
                )
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator('[data-tab="houses"]').click()
        cards = self.page.locator("#houses-list.house-grid-active > .house-active")
        cards.first.wait_for()
        long_card = cards.filter(
            has=self.page.locator('.house-body[data-house-id="CARD-SCROLL-TEST"]')
        )
        short_card = cards.filter(
            has=self.page.locator('.house-body[data-house-id="CARD-SHORT-TEST"]')
        )
        self.assertEqual(cards.count(), 2)
        self.assertAlmostEqual(
            long_card.bounding_box()["height"],
            short_card.bounding_box()["height"],
            delta=1,
        )
        self.assertLess(long_card.bounding_box()["height"], 760)
        for house_card in (long_card, short_card):
            body_box = house_card.locator(".house-body").bounding_box()
            actions_box = house_card.locator(".house-card-actions").bounding_box()
            self.assertAlmostEqual(actions_box["y"] - body_box["y"] - body_box["height"], 7, delta=1)
        top = long_card.locator(".house-top")
        body = long_card.locator(".house-body")
        self.assertEqual(
            top.evaluate("(element) => getComputedStyle(element).overflowY"),
            "auto",
        )
        self.assertEqual(
            body.evaluate("(element) => getComputedStyle(element).overflowY"),
            "auto",
        )
        self.assertGreater(
            top.evaluate("(element) => element.scrollHeight"),
            top.evaluate("(element) => element.clientHeight"),
        )
        self.assertGreater(
            body.evaluate("(element) => element.scrollHeight"),
            body.evaluate("(element) => element.clientHeight"),
        )
        actions = long_card.locator(".house-card-actions").bounding_box()
        card_box = long_card.bounding_box()
        self.assertLess(actions["y"], card_box["y"] + card_box["height"])
        self.assertEqual(self.browser_errors, [])

    def test_house_list_defaults_to_active_and_status_titles_use_stage_colors(self):
        with server.connect_db() as db:
            with db:
                server._upsert_house(db, "FILTER-ACTIVE-TEST")
                server._upsert_house(db, "FILTER-CLOSED-TEST")
                db.execute(
                    "UPDATE houses SET is_finished = 1 WHERE id = 'FILTER-CLOSED-TEST'"
                )
                db.execute(
                    """
                    INSERT INTO provided_houses(id, source, ready_for_start)
                    VALUES ('FILTER-READY-TEST', 'government', 1)
                    """
                )
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator('[data-tab="houses"]').click()
        active_card = self.page.locator("#houses-list").get_by_text(
            "FILTER-ACTIVE-TEST"
        )
        self.assertTrue(active_card.is_visible())
        self.assertFalse(
            self.page.locator("#houses-list").get_by_text("FILTER-CLOSED-TEST").count()
        )
        active_label = self.page.locator("#house-team-filter-label")
        self.assertEqual(active_label.inner_text(), "Showing active houses")
        self.assertEqual(
            active_label.evaluate("(element) => getComputedStyle(element).color"),
            "rgb(23, 107, 74)",
        )

        self.page.locator('[data-tab="overview"]').click()
        self.page.locator('[data-house-view="closed"]').click()
        closed_label = self.page.locator("#house-team-filter-label")
        self.assertEqual(closed_label.inner_text(), "Showing closed houses")
        self.assertEqual(
            closed_label.evaluate("(element) => getComputedStyle(element).color"),
            "rgb(163, 63, 58)",
        )
        self.assertTrue(
            self.page.locator("#houses-list").get_by_text("FILTER-CLOSED-TEST").is_visible()
        )
        self.assertFalse(
            self.page.locator("#houses-list").get_by_text("FILTER-ACTIVE-TEST").count()
        )

        self.page.locator('[data-tab="overview"]').click()
        self.page.locator('[data-house-view="ready"]').click()
        ready_label = self.page.locator("#house-team-filter-label")
        self.assertEqual(ready_label.inner_text(), "Showing houses ready for start")
        self.assertEqual(
            ready_label.evaluate("(element) => getComputedStyle(element).color"),
            "rgb(36, 93, 146)",
        )
        self.assertEqual(self.browser_errors, [])

    def test_house_team_filter_is_readable_in_dark_mode(self):
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator("#theme-toggle").click()
        self.page.locator('[data-tab="houses"]').click()
        filter_panel = self.page.locator("#house-team-filter")
        filter_label = self.page.locator("#house-team-filter-label")
        filter_panel.wait_for(state="visible")
        self.assertEqual(
            filter_panel.evaluate("(element) => getComputedStyle(element).backgroundImage"),
            "linear-gradient(110deg, rgb(29, 43, 37), rgb(32, 46, 41))",
        )
        self.assertEqual(
            filter_panel.evaluate("(element) => getComputedStyle(element).borderTopColor"),
            "rgb(59, 85, 71)",
        )
        self.assertEqual(
            filter_label.evaluate("(element) => getComputedStyle(element).color"),
            "rgb(156, 224, 183)",
        )
        back_button = filter_panel.get_by_role("button", name="Back to houses & teams")
        self.assertEqual(
            back_button.evaluate("(element) => getComputedStyle(element).backgroundColor"),
            "rgb(37, 55, 47)",
        )
        self.assertEqual(self.browser_errors, [])

    def test_record_movement_prefills_assigned_employee_as_responsible_team(self):
        with server.connect_db() as db:
            server._upsert_house(
                db,
                "TEAM-PREFILL-TEST",
                employee_name="Assigned Movement Worker",
            )
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator('[data-tab="houses"]').click()
        card = self.page.locator("#houses-list .house-card").filter(
            has=self.page.locator("h2", has_text="TEAM-PREFILL-TEST")
        )
        card.get_by_role("button", name="＋ Record movement").click()
        self.assertEqual(
            self.page.locator('#movement-form [data-for="pickup"] [name="team"]').input_value(),
            "Assigned Movement Worker",
        )
        self.assertEqual(self.browser_errors, [])

    def test_employee_and_delivery_names_are_suggested_without_restricting_custom_values(self):
        crew_names = ["Davit", "Tedo", "Jack", "Slavek D", "Slawomir", "Kamil"]
        delivery_names = ["Gary Lowther", "Karol"]
        self.assertEqual(
            self.page.locator("#assigned-crew-options option").evaluate_all(
                "(options) => options.map((option) => option.value)"
            ),
            crew_names,
        )
        self.assertEqual(
            self.page.locator("#delivery-person-options option").evaluate_all(
                "(options) => options.map((option) => option.value)"
            ),
            delivery_names,
        )
        self.page.locator('[data-tab="movements"]').click()
        self.page.locator("#new-movement").click()
        assigned_employee = self.page.locator("#movement-employee")
        self.assertEqual(assigned_employee.get_attribute("list"), "assigned-crew-options")
        assigned_employee.fill("A different employee")
        self.assertEqual(assigned_employee.input_value(), "A different employee")
        self.page.locator('#movement-form [data-for="pickup"] [name="person"]').evaluate(
            "(input) => input.value = 'Gary Lowther'"
        )
        self.assertEqual(
            self.page.locator(
                '#movement-form [data-for="pickup"] [name="person"]'
            ).get_attribute("list"),
            "delivery-person-options",
        )
        self.assertEqual(self.browser_errors, [])

    def test_closing_house_moves_it_out_of_active_list(self):
        with server.connect_db() as db:
            server._upsert_house(db, "STATUS-MOVE-TEST")
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator('[data-tab="houses"]').click()
        card = self.page.locator("#houses-list .house-card").filter(
            has=self.page.locator("h2", has_text="STATUS-MOVE-TEST")
        )
        with self.page.expect_response(
            lambda response: response.url.endswith("/api/houses/status")
            and response.status == 200
        ):
            card.locator(".house-finish-toggle").click()
        self.assertEqual(
            self.page.locator("#houses-list").get_by_text("STATUS-MOVE-TEST").count(),
            0,
        )
        self.page.locator('[data-tab="overview"]').click()
        self.page.locator('[data-house-view="closed"]').click()
        self.assertTrue(
            self.page.locator("#houses-list").get_by_text("STATUS-MOVE-TEST").is_visible()
        )
        self.assertEqual(self.browser_errors, [])

    def test_ready_and_active_house_cards_open_map_with_house_selected(self):
        with server.connect_db() as db:
            db.execute(
                """
                INSERT INTO provided_houses(id, postcode, address, owner_name, source, ready_for_start)
                VALUES ('MAP-READY', 'D01 TEST', 'Ready address', 'Ready Owner', 'government', 1)
                """
            )
            db.execute(
                """
                INSERT INTO provided_houses(id, postcode, address, owner_name, source)
                VALUES ('MAP-ACTIVE', 'D02 TEST', 'Active address', 'Active Owner', 'government')
                """
            )
            db.execute(
                """
                INSERT INTO houses(id, postcode, owner_name, is_finished)
                VALUES ('MAP-ACTIVE', 'D02 TEST', 'Active Owner', 0)
                """
            )
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator('[data-house-view="ready"]').click()
        ready_card = self.page.locator('.house-card[data-house-id="MAP-READY"]')
        ready_card.wait_for()
        self.assertEqual(ready_card.locator(".house-property-details").count(), 0)
        ready_card.locator(".house-map-open").click()
        self.page.locator("#house-map-view").wait_for(state="visible")
        self.page.wait_for_function(
            """() => document.querySelector(
              '#house-map-selection .house-map-selected-heading strong'
            )?.textContent === 'MAP-READY'"""
        )
        self.assertEqual(
            self.page.locator("#house-map-selection .house-map-selected-heading strong").inner_text(),
            "MAP-READY",
        )
        self.assertTrue(
            self.page.locator('#house-map-house-list [data-map-house-id="MAP-READY"]').evaluate(
                "(item) => item.classList.contains('selected')"
            )
        )

        self.page.locator('[data-tab="overview"]').click()
        self.page.locator('[data-house-view="active"]').click()
        active_card = self.page.locator("#houses-list .house-active").filter(
            has=self.page.locator("h2").filter(has_text="MAP-ACTIVE")
        )
        active_card.locator(".house-map-open").click()
        self.page.locator("#house-map-view").wait_for(state="visible")
        self.page.wait_for_function(
            """() => document.querySelector(
              '#house-map-selection .house-map-selected-heading strong'
            )?.textContent === 'MAP-ACTIVE'"""
        )
        self.assertEqual(
            self.page.locator("#house-map-selection .house-map-selected-heading strong").inner_text(),
            "MAP-ACTIVE",
        )
        self.assertEqual(
            self.page.locator("#house-map-selection .house-map-open").count(),
            0,
        )
        self.assertTrue(
            self.page.locator('#house-map-house-list [data-map-house-id="MAP-ACTIVE"]').evaluate(
                "(item) => item.classList.contains('selected')"
            )
        )
        self.assertEqual(self.browser_errors, [])

    def test_map_shows_house_activity_without_recording_stage_changes(self):
        with server.connect_db() as db:
            with db:
                server._upsert_house(
                    db,
                    "MAP-AUDIT-TEST",
                    postcode="D03 TEST",
                    owner_name="Map Audit Owner",
                    employee_name="Assigned Map Worker",
                )
                server._record_house_status(
                    db, "MAP-AUDIT-TEST", "ready", "Ready Actor"
                )
                server._record_house_status(
                    db, "MAP-AUDIT-TEST", "active", "Activation Actor"
                )
                db.execute(
                    """
                    INSERT INTO house_surveys(house_id, data, notes, updated_by, updated_at)
                    VALUES ('MAP-AUDIT-TEST', '{}', '', 'Survey Actor', '2026-10-21T09:00:00')
                    """
                )
                material_id = db.execute(
                    "INSERT INTO materials(code, name, unit, opening_qty) "
                    "VALUES ('AUDIT-TEST', 'Audit board', 'packs', 5)"
                ).lastrowid
                second_material_id = db.execute(
                    "INSERT INTO materials(code, name, unit, opening_qty) "
                    "VALUES ('AUDIT-TEST-2', 'Audit fixings', 'boxes', 5)"
                ).lastrowid
                server._record_movement_batch(
                    db,
                    {
                        "kind": "delivery",
                        "date": "2026-10-20",
                        "person": "Delivery Actor",
                        "team": "Delivery Team",
                        "house_id": "MAP-AUDIT-TEST",
                        "items": [
                            {"material_id": material_id, "qty": 1},
                        ],
                    },
                )
                server._record_movement_batch(
                    db,
                    {
                        "kind": "delivery",
                        "date": "2026-10-22",
                        "person": "Latest Delivery Actor",
                        "team": "Delivery Team",
                        "house_id": "MAP-AUDIT-TEST",
                        "items": [
                            {"material_id": second_material_id, "qty": 2},
                        ],
                    },
                )
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator('[data-house-view=""]').click()
        self.page.locator("#house-map-view").wait_for(state="visible")
        empty_activity = self.page.locator("#house-map-audit .house-map-audit-empty")
        self.assertEqual(
            empty_activity.locator("strong").inner_text(),
            "Choose a house",
        )
        self.assertIn("marked it ready", empty_activity.inner_text())
        self.page.locator('[data-tab="overview"]').click()
        self.page.locator('[data-house-view="active"]').click()
        active_card = self.page.locator("#houses-list .house-active").filter(
            has=self.page.locator("h2", has_text="MAP-AUDIT-TEST")
        )
        active_card.locator(".house-map-open").click()
        self.page.locator("#house-map-view").wait_for(state="visible")
        self.page.wait_for_function(
            """() => document.querySelector(
              '#house-map-selection .house-map-selected-heading strong'
            )?.textContent === 'MAP-AUDIT-TEST'"""
        )
        self.assertIn("Assigned to Assigned Map Worker", self.page.locator(
            "#house-map-selection"
        ).inner_text())
        audit = self.page.locator("#house-map-audit")
        audit_text = " ".join(audit.inner_text().split())
        self.assertIn("Marked ready to start Ready Actor", audit_text)
        self.assertNotIn("Surveyor Survey Actor", audit_text)
        self.assertIn("Activated by Activation Actor", audit_text)
        self.assertIn("Activation Actor", audit.inner_text())
        self.assertIn("Delivery handoff Latest Delivery Actor → Assigned Map Worker", audit_text)
        self.assertIn("First delivery 20 Oct 2026", audit_text)
        self.assertIn("Last delivery 22 Oct 2026", audit_text)
        self.assertNotIn("Materials specified", audit.inner_text())
        self.assertEqual(
            self.page.locator("#house-map-activity-title").inner_text(),
            "Recent activity",
        )
        self.assertEqual(
            self.page.locator("#house-map-activity-count").inner_text(),
            "5",
        )
        side = self.page.locator(".house-map-side")
        self.assertEqual(
            side.evaluate("(element) => getComputedStyle(element).overflowY"),
            "auto",
        )
        self.assertTrue(
            self.page.locator(
                '#house-map-house-list [data-map-house-id="MAP-AUDIT-TEST"]'
            ).is_visible()
        )
        self.page.locator(
            '#house-map-selection .stage-btn[data-stage="2"]'
        ).click()
        self.page.locator(
            '#house-map-selection .stage-btn[data-stage="2"].on'
        ).wait_for()
        self.assertNotIn("Navigation Test Admin", audit.inner_text())
        self.assertEqual(
            self.page.locator("#house-map-activity-count").inner_text(),
            "5",
        )
        self.page.locator(
            '#house-map-selection .stage-btn[data-stage="1"]'
        ).click()
        self.page.locator(
            '#house-map-selection .stage-btn[data-stage="1"].on'
        ).wait_for()
        self.assertEqual(
            self.page.locator("#house-map-activity-count").inner_text(),
            "5",
        )
        with server.connect_db() as db:
            stage_events = db.execute(
                "SELECT status, changed_by, changed_at FROM house_status_history "
                "WHERE house_id = 'MAP-AUDIT-TEST' ORDER BY id"
            ).fetchall()
            stage = db.execute(
                "SELECT stage FROM houses WHERE id = 'MAP-AUDIT-TEST'"
            ).fetchone()["stage"]
        self.assertEqual(stage, 1)
        self.assertEqual(
            [(event["status"], event["changed_by"]) for event in stage_events],
            [
                ("ready", "Ready Actor"),
                ("active", "Activation Actor"),
            ],
        )
        self.assertEqual(self.browser_errors, [])

    def test_newly_ready_house_appears_first_in_ready_cards(self):
        with server.connect_db() as db:
            with db:
                db.execute(
                    "INSERT INTO provided_houses(id, source, ready_for_start) "
                    "VALUES ('QUEUE-OLDER', 'government', 1)"
                )
                db.execute(
                    "INSERT INTO provided_houses(id, source, ready_for_start) "
                    "VALUES ('QUEUE-NEWER', 'government', 0)"
                )
                db.execute(
                    """
                    INSERT INTO house_status_history(house_id,status,changed_by,changed_at)
                    VALUES ('QUEUE-OLDER','ready','Earlier actor','2020-01-01T10:00:00')
                    """
                )

        self.page.reload(wait_until="domcontentloaded")
        self.page.locator('[data-house-view="not_started"]').click()
        new_card = self.page.locator(
            '.house-plan-pending[data-house-id="QUEUE-NEWER"]'
        )
        new_card.locator(".plan-ready-toggle").click()
        self.page.locator('[data-tab="overview"]').click()
        self.page.locator('[data-house-view="ready"]').click()
        self.page.wait_for_function(
            """() => {
              const cards = [...document.querySelectorAll('#houses-list .house-plan-ready')];
              return cards.length === 2 && cards[0].dataset.houseId === 'QUEUE-NEWER';
            }"""
        )
        self.assertEqual(
            self.page.locator("#houses-list .house-plan-ready").evaluate_all(
                "(cards) => cards.map((card) => card.dataset.houseId)"
            ),
            ["QUEUE-NEWER", "QUEUE-OLDER"],
        )
        self.assertEqual(self.browser_errors, [])

    def test_fictional_house_cards_do_not_show_placeholder_badges(self):
        with server.connect_db() as db:
            db.execute(
                "INSERT INTO provided_houses "
                "(id, postcode, address, owner_name, source) "
                "VALUES (?, ?, ?, ?, 'fictional_sample')",
                (
                    "SAMPLE-BADGE-TEST",
                    "DEMO-001",
                    "1 Rowan Rise, Alderbrook Demo Village, Demo County",
                    "Maeve Brennan",
                ),
            )
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator('[data-house-view="not_started"]').click()
        card = self.page.locator(
            '.house-plan-pending[data-house-id="SAMPLE-BADGE-TEST"]'
        )
        card.wait_for()
        self.assertEqual(card.locator(".fictional-house-badge").count(), 0)
        self.assertEqual(self.browser_errors, [])

    def test_ready_house_activation_hides_search_but_generic_activation_keeps_it(self):
        with server.connect_db() as db:
            db.execute(
                """
                INSERT INTO provided_houses(id, postcode, address, owner_name, source, ready_for_start)
                VALUES ('ACTIVATE-READY', 'D01 TEST', 'Ready address', 'Ready Owner', 'government', 1)
                """
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS seai_applications "
                "(application_id TEXT PRIMARY KEY, detail_data_json TEXT NOT NULL)"
            )
            db.execute(
                "INSERT INTO seai_applications(application_id, detail_data_json) VALUES (?, ?)",
                (
                    "ACTIVATE-READY",
                    '{"Measures":[{"Name":"External Wall Insulation: Greater than 85m2",'
                    '"Survey Quantity":"94","Survey Qty including Variation":"104"}]}',
                ),
            )
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator('[data-house-view="ready"]').click()
        activate_button = self.page.locator(
            '.house-card[data-house-id="ACTIVATE-READY"] .seai-activate'
        )
        self.assertTrue(activate_button.is_disabled())
        self.assertEqual(
            activate_button.get_attribute("title"),
            "Complete a survey before activation.",
        )
        self.assertEqual(
            self.page.locator(
                '.house-card[data-house-id="ACTIVATE-READY"] .activation-requirement'
            ).count(),
            0,
        )
        self.page.locator(
            '.house-card[data-house-id="ACTIVATE-READY"] .house-survey-open'
        ).click()
        work_plan = self.page.locator("#surveyor-work-plan-form")
        for trade in ("scaffolding", "esb", "plumber"):
            work_plan.locator(f"[name='{trade}_required']").select_option("false")
        work_plan.locator("button[type='submit']").click()
        self.page.wait_for_function(
            """() => document.querySelector('#toast').textContent.includes(
              'Pre-start work plan for ACTIVATE-READY saved'
            )"""
        )
        self.page.locator("#survey-perimeter").fill("42.75")
        self.page.locator("#house-survey-form button[type='submit']").click()
        self.page.wait_for_function(
            "() => document.querySelector('#surveyor-room').classList.contains('hidden')"
        )
        activate_button = self.page.locator(
            '.house-card[data-house-id="ACTIVATE-READY"] .seai-activate'
        )
        self.assertFalse(activate_button.is_disabled())
        activate_button.click()
        self.assertTrue(self.page.locator("#house-dialog").is_visible())
        self.assertFalse(self.page.locator(".seai-search").is_visible())
        self.assertEqual(
            self.page.locator('#house-form input[name="id"]').input_value(),
            "ACTIVATE-READY",
        )
        self.assertIn("EWI Area: 104 m²", self.page.locator("#seai-info").inner_text())
        self.assertIn("Perimeter: 42.75 m", self.page.locator("#seai-info").inner_text())
        self.page.locator("#house-dialog").get_by_role("button", name="Cancel").click()

        self.page.locator("#open-house").click()
        self.assertTrue(self.page.locator("#house-dialog").is_visible())
        self.assertTrue(self.page.locator(".seai-search").is_visible())
        self.assertEqual(self.browser_errors, [])

    def test_active_house_card_actions_fit_without_a_bottom_scrollbar(self):
        with server.connect_db() as db:
            db.execute(
                """
                INSERT INTO provided_houses(id, postcode, owner_name, source)
                VALUES ('ACTIVE-ACTIONS-TEST', 'D01 TEST', 'Active Owner', 'government')
                """
            )
            db.execute(
                """
                INSERT INTO houses(id, postcode, owner_name, is_finished)
                VALUES ('ACTIVE-ACTIONS-TEST', 'D01 TEST', 'Active Owner', 0)
                """
            )
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator('[data-house-view="ready"]').click()
        self.page.locator("#houses-list.house-grid-ready").wait_for()
        self.page.locator('[data-tab="overview"]').click()
        self.page.locator('[data-house-view="active"]').click()
        self.assertNotIn(
            "house-grid-ready",
            self.page.locator("#houses-list").get_attribute("class"),
        )
        card = self.page.locator("#houses-list .house-active").filter(
            has=self.page.locator("h2").filter(has_text="ACTIVE-ACTIONS-TEST")
        )
        actions = card.locator(".house-card-actions")
        self.assertEqual(actions.evaluate("(element) => getComputedStyle(element).overflowY"), "visible")
        self.assertLessEqual(
            actions.evaluate("(element) => element.scrollHeight - element.clientHeight"),
            1,
        )
        self.assertEqual(
            actions.locator("button").first.inner_text(),
            "View SEAI house details\n→",
        )
        active_dot = card.locator(".house-workflow-steps li.current > span")
        self.assertEqual(
            active_dot.evaluate("(element) => getComputedStyle(element).backgroundColor"),
            "rgb(33, 134, 83)",
        )
        map_button = actions.get_by_role("button", name="View on map")
        self.assertEqual(
            map_button.evaluate("(element) => getComputedStyle(element).gridColumn"),
            "1 / -1",
        )
        self.assertTrue(map_button.locator("svg.house-map-icon").is_visible())
        self.assertEqual(map_button.locator("svg").get_attribute("aria-hidden"), "true")
        actions_box = actions.bounding_box()
        map_button_box = map_button.bounding_box()
        self.assertIsNotNone(actions_box)
        self.assertIsNotNone(map_button_box)
        self.assertAlmostEqual(map_button_box["width"], actions_box["width"] - 28, delta=3)
        self.assertTrue(actions.get_by_role("button", name="Mark house finished").is_visible())
        self.assertTrue(actions.get_by_role("button", name="View on map").is_visible())
        self.assertEqual(self.browser_errors, [])

    def test_active_house_card_has_readable_dark_mode_surfaces_and_actions(self):
        with server.connect_db() as db:
            db.execute(
                """
                INSERT INTO provided_houses(id, postcode, owner_name, source)
                VALUES ('ACTIVE-DARK-TEST', 'D01 TEST', 'Dark Active Owner', 'government')
                """
            )
            db.execute(
                """
                INSERT INTO houses(id, postcode, owner_name, is_finished)
                VALUES ('ACTIVE-DARK-TEST', 'D01 TEST', 'Dark Active Owner', 0)
                """
            )
        self.page.reload(wait_until="domcontentloaded")
        if self.page.locator("html").get_attribute("data-theme") != "dark":
            self.page.locator("#theme-toggle").click()
        self.page.locator('[data-tab="overview"]').click()
        self.page.locator('[data-house-view="active"]').click()
        card = self.page.locator(
            '#houses-list.house-grid-active > .house-active:has(h2:text("ACTIVE-DARK-TEST"))'
        )
        card.wait_for()
        actions = card.locator(".house-card-actions")
        workflow = card.locator(".house-workflow")
        movement_button = actions.get_by_role("button", name="＋ Record movement")
        active_step = card.locator(".house-workflow-steps li.current > span")
        self.assertEqual(
            card.evaluate("(element) => getComputedStyle(element).backgroundColor"),
            "rgb(28, 41, 36)",
        )
        self.assertEqual(
            card.evaluate("(element) => getComputedStyle(element).borderRadius"),
            "12px",
        )
        self.assertEqual(
            workflow.evaluate("(element) => getComputedStyle(element).backgroundColor"),
            "rgb(36, 53, 45)",
        )
        self.assertEqual(
            actions.evaluate("(element) => getComputedStyle(element).backgroundColor"),
            "rgb(25, 37, 33)",
        )
        self.assertEqual(
            movement_button.evaluate("(element) => getComputedStyle(element).backgroundColor"),
            "rgb(38, 115, 79)",
        )
        self.assertEqual(
            active_step.evaluate("(element) => getComputedStyle(element).backgroundColor"),
            "rgb(38, 115, 79)",
        )
        self.assertGreaterEqual(movement_button.bounding_box()["height"], 44)
        self.assertEqual(
            card.locator(".house-top").evaluate(
                "(element) => getComputedStyle(element).borderBottomColor"
            ),
            "rgb(59, 80, 69)",
        )
        self.assertEqual(self.page.locator("html").get_attribute("data-theme"), "dark")
        self.assertEqual(self.browser_errors, [])

    def test_ready_house_card_has_readable_dark_mode_surfaces_and_actions(self):
        with server.connect_db() as db:
            db.execute(
                """
                INSERT INTO provided_houses(id, postcode, owner_name, ready_for_start)
                VALUES ('READY-DARK-TEST', 'D01 TEST', 'Dark Ready Owner', 1)
                """
            )
            server._record_house_status(
                db, "READY-DARK-TEST", "ready", "Dark Ready Actor"
            )
        self.page.reload(wait_until="domcontentloaded")
        if self.page.locator("html").get_attribute("data-theme") != "dark":
            self.page.locator("#theme-toggle").click()
        self.page.locator('[data-tab="overview"]').click()
        self.page.locator('[data-house-view="ready"]').click()
        card = self.page.locator(
            '#houses-list.house-grid-ready > .house-plan-ready:has(h2:text("READY-DARK-TEST"))'
        )
        card.wait_for()
        workflow = card.locator(".house-workflow")
        actions = card.locator(".house-card-actions")
        audit_field = card.locator(".house-audit-summary-field").first
        ready_status = card.locator(".status-pill.ready")
        activate_button = actions.locator(".seai-activate")
        self.assertEqual(
            card.evaluate("(element) => getComputedStyle(element).backgroundColor"),
            "rgb(28, 41, 37)",
        )
        self.assertEqual(
            workflow.evaluate("(element) => getComputedStyle(element).backgroundColor"),
            "rgb(32, 47, 41)",
        )
        self.assertEqual(
            actions.evaluate("(element) => getComputedStyle(element).backgroundColor"),
            "rgb(23, 35, 31)",
        )
        self.assertEqual(
            actions.evaluate("(element) => getComputedStyle(element).borderBottomLeftRadius"),
            "14px",
        )
        self.assertEqual(
            actions.evaluate("(element) => getComputedStyle(element).borderBottomRightRadius"),
            "14px",
        )
        self.assertLess(card.bounding_box()["height"], 760)
        self.assertEqual(
            audit_field.evaluate("(element) => getComputedStyle(element).backgroundColor"),
            "rgb(36, 53, 43)",
        )
        self.assertEqual(
            ready_status.evaluate("(element) => getComputedStyle(element).backgroundColor"),
            "rgb(32, 59, 44)",
        )
        self.assertEqual(
            activate_button.evaluate("(element) => getComputedStyle(element).backgroundColor"),
            "rgb(37, 52, 44)",
        )
        self.assertTrue(activate_button.is_disabled())
        self.assertEqual(activate_button.inner_text(), "Survey required to activate")
        self.assertAlmostEqual(
            activate_button.bounding_box()["width"] / actions.bounding_box()["width"],
            1,
            delta=0.06,
        )
        self.assertLess(actions.bounding_box()["height"], 160)
        card.hover()
        self.page.wait_for_function(
            """() => getComputedStyle(document.querySelector(
              '#houses-list.house-grid-ready > .house-plan-ready'
            )).borderTopColor === 'rgb(76, 130, 96)'"""
        )
        self.page.set_viewport_size({"width": 357, "height": 480})
        self.page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        mobile_actions = card.locator(".house-card-actions").bounding_box()
        mobile_actions_bottom = mobile_actions["y"] + mobile_actions["height"]
        mobile_nav_top = self.page.locator(".main-nav").bounding_box()["y"]
        self.assertLessEqual(mobile_actions_bottom, mobile_nav_top)
        mobile_layout = card.evaluate(
            """element => ({
              headerDisplay: getComputedStyle(element.querySelector('.house-top')).display,
              auditColumns: getComputedStyle(element.querySelector('.house-audit-summary')).gridTemplateColumns.split(' ').length,
              statusColumn: getComputedStyle(element.querySelector('.house-status-stack')).gridColumn,
              tableHeaderDisplay: getComputedStyle(element.querySelector('.house-insulation-header')).display,
              measureColumns: getComputedStyle(element.querySelector('.house-insulation-row')).gridTemplateColumns.split(' ').length,
              measureLabelColumn: getComputedStyle(element.querySelector('.house-insulation-row > strong')).gridColumn,
              scrollWidth: element.scrollWidth,
              clientWidth: element.clientWidth
            })"""
        )
        self.assertEqual(mobile_layout["headerDisplay"], "grid")
        self.assertEqual(mobile_layout["auditColumns"], 1)
        self.assertEqual(mobile_layout["statusColumn"], "1 / -1")
        self.assertEqual(mobile_layout["tableHeaderDisplay"], "none")
        self.assertEqual(mobile_layout["measureColumns"], 2)
        self.assertEqual(mobile_layout["measureLabelColumn"], "1 / -1")
        self.assertLessEqual(mobile_layout["scrollWidth"], mobile_layout["clientWidth"])
        self.page.set_viewport_size({"width": 320, "height": 640})
        narrow_layout = card.evaluate(
            "element => ({cardWidth: element.clientWidth, scrollWidth: element.scrollWidth, pageWidth: document.body.scrollWidth})"
        )
        self.assertLessEqual(narrow_layout["scrollWidth"], narrow_layout["cardWidth"])
        self.assertLessEqual(narrow_layout["pageWidth"], 320)
        self.assertEqual(self.page.locator("html").get_attribute("data-theme"), "dark")
        self.assertEqual(self.browser_errors, [])

    def test_closed_house_cards_fit_their_content(self):
        with server.connect_db() as db:
            db.execute(
                """
                INSERT INTO provided_houses(id, postcode, owner_name, source)
                VALUES ('CLOSED-COMPACT-TEST', 'D01 TEST', 'Closed Owner', 'government')
                """
            )
            db.execute(
                """
                INSERT INTO houses(id, postcode, owner_name, is_finished)
                VALUES ('CLOSED-COMPACT-TEST', 'D01 TEST', 'Closed Owner', 1)
                """
            )
            db.execute(
                """
                INSERT INTO provided_houses(id, postcode, owner_name, source, notes)
                VALUES ('CLOSED-LONG-TEST', 'D02 TEST', 'Long Owner', 'government', ?)
                """,
                ("Long closed-house note. " * 120,),
            )
            db.execute(
                """
                INSERT INTO houses(id, postcode, owner_name, is_finished)
                VALUES ('CLOSED-LONG-TEST', 'D02 TEST', 'Long Owner', 1)
                """
            )
            server._record_house_status(
                db, "CLOSED-COMPACT-TEST", "closed", "Closing Actor"
            )
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator('[data-tab="overview"]').click()
        self.page.locator('[data-house-view="closed"]').click()
        cards = self.page.locator("#houses-list.house-grid-closed > .house-finished")
        card = cards.filter(
            has=self.page.locator("h2").filter(has_text="CLOSED-COMPACT-TEST")
        )
        long_card = cards.filter(
            has=self.page.locator("h2").filter(has_text="CLOSED-LONG-TEST")
        )
        card.wait_for()
        long_card.wait_for()
        self.assertEqual(cards.count(), 2)
        self.assertAlmostEqual(
            card.bounding_box()["height"],
            long_card.bounding_box()["height"],
            delta=1,
        )
        self.assertLess(card.bounding_box()["height"], 620)
        self.assertEqual(
            card.locator(".house-top").evaluate(
                "(element) => getComputedStyle(element).overflowY"
            ),
            "auto",
        )
        self.assertEqual(
            card.evaluate("(element) => getComputedStyle(element).backgroundColor"),
            "rgb(248, 232, 229)",
        )
        self.assertEqual(
            card.locator(".status-pill.finished").evaluate(
                "(element) => getComputedStyle(element).backgroundColor"
            ),
            "rgb(173, 71, 60)",
        )
        self.assertIn("Closed by", card.locator(".house-audit-summary").inner_text())
        self.assertIn("Closing Actor", card.locator(".house-audit-summary").inner_text())
        self.assertEqual(
            card.locator(".status-pill.finished").evaluate(
                "(element) => getComputedStyle(element, '::before').content"
            ),
            '"✓"',
        )
        self.assertEqual(
            long_card.locator(".house-body").evaluate(
                "(element) => getComputedStyle(element).overflowY"
            ),
            "auto",
        )
        self.assertGreater(
            long_card.locator(".house-body").evaluate(
                "(element) => element.scrollHeight"
            ),
            long_card.locator(".house-body").evaluate(
                "(element) => element.clientHeight"
            ),
        )
        actions = card.locator(".house-card-actions")
        self.assertEqual(actions.evaluate("(element) => getComputedStyle(element).overflowY"), "visible")
        self.assertLessEqual(
            actions.evaluate("(element) => element.scrollHeight - element.clientHeight"),
            1,
        )
        self.assertEqual(self.browser_errors, [])

    def test_ready_houses_hide_snags_and_snag_history_in_house_details(self):
        self.page.set_viewport_size({"width": 1000, "height": 520})
        with server.connect_db() as db:
            db.execute(
                "INSERT INTO provided_houses(id, postcode, source, ready_for_start) "
                "VALUES ('SNAG-READY-TEST', 'D01 TEST', 'government', 1)"
            )
            db.executemany(
                "INSERT INTO house_snags(house_id, text) VALUES (?, ?)",
                [
                    ("SNAG-READY-TEST", f"Snag item {index}")
                    for index in range(1, 13)
                ],
            )
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator(".app-shell:not(.hidden)").wait_for()
        self.page.locator('[data-house-view="ready"]').click()
        ready_card = self.page.locator('.house-plan-ready[data-house-id="SNAG-READY-TEST"]')
        ready_card.wait_for()
        self.assertEqual(ready_card.locator(".snag-row").count(), 0)
        self.assertEqual(ready_card.locator(".house-note-open").count(), 0)
        self.assertEqual(ready_card.get_by_role("button", name="Edit checklist").count(), 0)
        self.assertEqual(ready_card.get_by_role("button", name="View details & history").count(), 0)
        self.assertTrue(ready_card.locator(".house-card-actions").is_visible())
        self.page.evaluate("openHouseDetail('SNAG-READY-TEST')")
        self.page.wait_for_function(
            "() => document.querySelector('#house-detail-status').textContent === 'Ready for start'"
        )
        self.assertTrue(self.page.locator("#house-detail-snag-section").evaluate(
            "(section) => section.classList.contains('hidden')"
        ))
        self.assertEqual(self.page.locator("#house-detail-records-title").inner_text(), "Notes")
        self.assertEqual(self.browser_errors, [])

    def test_active_house_card_updates_checklist_status_inline(self):
        with server.connect_db() as db:
            db.execute(
                "INSERT INTO provided_houses(id, postcode, source) "
                "VALUES ('CHECKLIST-INLINE-TEST', 'D03 TEST', 'government')"
            )
            db.execute(
                "INSERT INTO houses(id, postcode) "
                "VALUES ('CHECKLIST-INLINE-TEST', 'D03 TEST')"
            )
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator(".app-shell:not(.hidden)").wait_for()
        self.page.locator('.nav-item[data-tab="houses"]').click()
        card = self.page.locator(
            '.house-active:has(.house-body[data-house-id="CHECKLIST-INLINE-TEST"])'
        )
        card.wait_for()
        toggle = card.locator(
            '.house-checklist-status[data-checklist-field="scaffolding_status"]'
        )
        input_position = toggle.evaluate(
            """(input) => {
              const rect = input.getBoundingClientRect();
              const toggle = input.closest('.checklist-toggle').getBoundingClientRect();
              return {
                inputTop: rect.top,
                inputLeft: rect.left,
                toggleTop: toggle.top,
                toggleLeft: toggle.left,
                toggleBottom: toggle.bottom,
                toggleRight: toggle.right
              };
            }"""
        )
        self.assertGreaterEqual(input_position["inputTop"], input_position["toggleTop"])
        self.assertLessEqual(input_position["inputTop"], input_position["toggleBottom"])
        self.assertGreaterEqual(input_position["inputLeft"], input_position["toggleLeft"])
        self.assertLessEqual(input_position["inputLeft"], input_position["toggleRight"])
        esb_toggle = card.locator(
            '.house-checklist-status[data-checklist-field="esb_unclipped"]'
        )
        plumber_toggle = card.locator(
            '.house-checklist-status[data-checklist-field="plumber_status"]'
        )
        self.page.evaluate(
            """() => {
              const card = document.querySelector(
                '.house-active:has(.house-body[data-house-id="CHECKLIST-INLINE-TEST"])'
              );
              window.scrollTo(0, card.getBoundingClientRect().top + window.scrollY - 100);
              const body = card.querySelector('.house-body');
              body.style.height = '120px';
              body.style.overflowY = 'auto';
              body.scrollTop = body.scrollHeight;
            }"""
        )
        before_scroll = self.page.evaluate("window.scrollY")
        body = card.locator(".house-body")
        before_body_scroll = body.evaluate("(element) => element.scrollTop")
        before_card_top = card.evaluate("(element) => element.getBoundingClientRect().top")
        toggle_box = card.locator(".checklist-toggle").first.bounding_box()
        self.page.mouse.click(
            toggle_box["x"] + toggle_box["width"] / 2,
            toggle_box["y"] + toggle_box["height"] / 2,
        )
        self.assertTrue(toggle.is_checked())
        self.assertAlmostEqual(self.page.evaluate("window.scrollY"), before_scroll, delta=2)
        self.assertAlmostEqual(
            body.evaluate("(element) => element.scrollTop"),
            before_body_scroll,
            delta=2,
        )
        self.assertAlmostEqual(
            card.evaluate("(element) => element.getBoundingClientRect().top"),
            before_card_top,
            delta=2,
        )
        self.page.wait_for_function(
            """() => {
              const input = document.querySelector(
                '.house-active:has(.house-body[data-house-id="CHECKLIST-INLINE-TEST"]) ' +
                '.house-checklist-status[data-checklist-field="scaffolding_status"]'
              );
              return input?.dataset.savedValue === "done" &&
                input.closest('.checklist-toggle')?.classList.contains('is-done');
            }"""
        )
        self.page.wait_for_function(
            """() => {
              const input = document.querySelector(
                '.house-active:has(.house-body[data-house-id="CHECKLIST-INLINE-TEST"]) ' +
                '.house-checklist-status[data-checklist-field="scaffolding_status"]'
              );
              return getComputedStyle(input.closest('.checklist-toggle')).backgroundColor ===
                'rgb(232, 246, 238)';
            }"""
        )
        self.assertFalse(esb_toggle.is_checked())
        self.assertFalse(plumber_toggle.is_checked())
        esb_toggle.check()
        self.page.wait_for_function(
            """() => document.querySelector(
              '.house-active:has(.house-body[data-house-id="CHECKLIST-INLINE-TEST"]) ' +
              '.house-checklist-status[data-checklist-field="esb_unclipped"]'
            )?.dataset.savedValue === "yes" """
        )
        self.assertTrue(toggle.is_checked())
        self.assertFalse(plumber_toggle.is_checked())
        plumber_toggle.check()
        self.page.wait_for_function(
            """() => document.querySelector(
              '.house-active:has(.house-body[data-house-id="CHECKLIST-INLINE-TEST"]) ' +
              '.house-checklist-status[data-checklist-field="plumber_status"]'
            )?.dataset.savedValue === "done" """
        )
        toggle.uncheck()
        self.page.wait_for_function(
            """() => document.querySelector(
              '.house-active:has(.house-body[data-house-id="CHECKLIST-INLINE-TEST"]) ' +
              '.house-checklist-status[data-checklist-field="scaffolding_status"]'
            )?.dataset.savedValue === "not_done" """
        )
        self.assertFalse(toggle.is_checked())
        self.assertTrue(esb_toggle.is_checked())
        self.assertTrue(plumber_toggle.is_checked())
        with server.connect_db() as db:
            checklist = db.execute(
                "SELECT scaffolding_status, esb_unclipped, plumber_status "
                "FROM house_checklists "
                "WHERE house_id = 'CHECKLIST-INLINE-TEST'"
            ).fetchone()
        self.assertEqual(
            tuple(checklist),
            ("not_done", "yes", "done"),
        )
        self.assertEqual(
            toggle.evaluate(
                """(input) => JSON.stringify({
                  className: input.closest('.checklist-toggle').className,
                  background: getComputedStyle(input.closest('.checklist-toggle')).backgroundColor,
                  indicator: getComputedStyle(input.closest('.checklist-toggle').querySelector('.checklist-toggle-indicator')).backgroundColor,
                  ruleMatches: input.closest('.checklist-toggle').matches('.house-card .house-checklist-card .checklist-toggle.is-done'),
                  label: input.closest('.checklist-toggle').querySelector('.checklist-toggle-label').textContent
                })"""
            ),
            '{"className":"checklist-toggle","background":"rgb(247, 249, 248)","indicator":"rgb(255, 255, 255)","ruleMatches":false,"label":"Not done yet"}',
        )
        self.assertEqual(self.browser_errors, [])

    def test_active_house_card_references_ready_stage_survey_and_work_checklist(self):
        survey = {
            "windows": [{"w": 1.2, "h": 1.4, "qty": 2}],
            "doors": [{"w": 0.9, "h": 2.0, "qty": 1}],
            "house_corners": [3.2, 4.1],
            "perimeter_m": 42.5,
            "notes": "Survey access through the side gate.",
        }
        with server.connect_db() as db:
            db.execute(
                """
                INSERT INTO provided_houses
                    (id, postcode, source, plan_scaffolding_required,
                     plan_scaffolding_notes, plan_esb_required, plan_plumber_required,
                     plan_plumber_notes, plan_vent_required, plan_vent_notes)
                VALUES ('ACTIVE-SURVEY-REFERENCE', 'D03 TEST', 'government',
                        1, 'Rear access only', 0, 1, 'Isolate old pipework',
                        1, 'Check attic duct route')
                """
            )
            db.execute(
                """
                INSERT INTO houses(id, postcode, stage)
                VALUES ('ACTIVE-SURVEY-REFERENCE', 'D03 TEST', 1)
                """
            )
            db.execute(
                """
                INSERT INTO house_status_history
                    (house_id, status, changed_by, changed_at)
                VALUES ('ACTIVE-SURVEY-REFERENCE', 'ready', 'Assign Operator',
                        '2026-11-08T09:00:00'),
                       ('ACTIVE-SURVEY-REFERENCE', 'active', 'Assign Operator',
                        '2026-11-08T10:00:00')
                """
            )
            db.execute(
                """
                INSERT INTO house_surveys
                    (house_id, data, notes, updated_by, updated_at)
                VALUES (?, ?, ?, 'Ready Stage Surveyor', '2026-11-08T09:30:00')
                """,
                (
                    "ACTIVE-SURVEY-REFERENCE",
                    json.dumps(survey),
                    survey["notes"],
                ),
            )
            db.execute(
                """
                INSERT INTO house_checklists
                    (house_id, scaffolding_status, esb_unclipped, plumber_status)
                VALUES ('ACTIVE-SURVEY-REFERENCE', 'not_done', 'not_esb', 'not_done')
                """
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS seai_applications "
                "(application_id TEXT PRIMARY KEY, detail_data_json TEXT NOT NULL)"
            )
            db.execute(
                "INSERT OR REPLACE INTO seai_applications "
                "(application_id, detail_data_json) VALUES (?, ?)",
                (
                    "ACTIVE-SURVEY-REFERENCE",
                    json.dumps(
                        {
                            "Application Details": {
                                "Application ID": "ACTIVE-SEAI-REFERENCE",
                                "Applicant Name": "Active SEAI Applicant",
                            },
                            "Measures": [
                                {
                                    "Name": "External Wall Insulation",
                                    "Survey Quantity": "42",
                                }
                            ],
                        }
                    ),
                ),
            )

        self.page.reload(wait_until="domcontentloaded")
        self.page.locator(".app-shell:not(.hidden)").wait_for()
        self.page.locator('.nav-item[data-tab="houses"]').click()
        card = self.page.locator(
            '.house-active:has(.house-body[data-house-id="ACTIVE-SURVEY-REFERENCE"])'
        )
        card.wait_for()
        reference = card.locator("details.house-survey-reference")
        reference.wait_for()
        reference.locator("summary").click()
        self.assertIn("Perimeter 42.5 m", reference.inner_text())
        self.assertIn("Window", reference.inner_text())
        self.assertIn("1.2 × 1.4 m", reference.inner_text())
        self.assertIn("Door", reference.inner_text())
        self.assertIn("House corner lengths", reference.inner_text())
        self.assertIn("Ready Stage Surveyor", reference.inner_text())
        self.assertIn("Survey access through the side gate.", reference.inner_text())
        checklist = card.locator(".house-checklist-card")
        self.assertIn("Ready-stage work checklist", checklist.text_content())
        self.assertIn("Rear access only", checklist.inner_text())
        self.assertIn("Isolate old pipework", checklist.inner_text())
        self.assertIn("Check attic duct route", checklist.inner_text())
        seai_details_button = card.locator(".assign-seai-details")
        self.assertEqual(
            " ".join(seai_details_button.inner_text().split()),
            "View SEAI house details →",
        )
        seai_details_button.click()
        self.page.locator("#assign-seai-house-detail").wait_for(state="visible")
        self.assertEqual(
            self.page.locator("#assign-seai-house-detail-title").inner_text(),
            "ACTIVE-SURVEY-REFERENCE",
        )
        self.page.wait_for_function(
            """() => document.querySelector('#assign-seai-house-record')
              ?.textContent.includes('Active SEAI Applicant')"""
        )
        self.page.locator("#assign-seai-house-back").click()
        card.wait_for(state="visible")
        self.assertEqual(self.browser_errors, [])

    def test_surveyor_work_plan_becomes_active_house_checklist(self):
        with server.connect_db() as db:
            with db:
                db.execute(
                    """
                    INSERT INTO provided_houses(
                        id, source, plan_scaffolding_required, plan_scaffolding_notes,
                        plan_esb_required, plan_esb_notes, plan_plumber_required,
                        plan_plumber_notes
                    ) VALUES (
                        'WORKPLAN-CHECKLIST-TEST', 'government', 1, 'Use rear access',
                        0, 'No ESB work required', 1, 'Coordinate with owner'
                    )
                    """
                )
                db.execute(
                    "INSERT INTO houses(id) VALUES ('WORKPLAN-CHECKLIST-TEST')"
                )
                db.execute(
                    """
                    INSERT INTO house_checklists(
                        house_id, scaffolding_status, esb_unclipped, plumber_status
                    ) VALUES (
                        'WORKPLAN-CHECKLIST-TEST', 'not_done', 'not_esb', 'not_done'
                    )
                    """
                )
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator('[data-tab="houses"]').click()
        card = self.page.locator("#houses-list .house-active").filter(
            has=self.page.locator("h2", has_text="WORKPLAN-CHECKLIST-TEST")
        )
        scaffolding = card.locator(
            '.house-checklist-status[data-checklist-field="scaffolding_status"]'
        )
        self.assertFalse(scaffolding.is_checked())
        self.assertEqual(
            scaffolding.locator("xpath=ancestor::label").locator(
                ".checklist-toggle-label"
            ).inner_text(),
            "Not done yet",
        )
        self.assertIn(
            "Required",
            card.locator(".checklist-card-row").filter(
                has_text="Scaffolding"
            ).inner_text(),
        )
        self.assertEqual(
            card.locator(".checklist-card-label").filter(
                has_text="Scaffolding"
            ).locator("small").first.inner_text(),
            "Use rear access",
        )
        self.assertIn(
            "Not required",
            card.locator(".checklist-card-row").filter(
                has_text="ESB work"
            ).inner_text(),
        )
        self.assertEqual(
            card.locator(".checklist-card-label").filter(
                has_text="Plumber work"
            ).locator("small").first.inner_text(),
            "Coordinate with owner",
        )
        scaffolding.check()
        self.page.wait_for_function(
            """() => [...document.querySelectorAll('.house-checklist-status[data-checklist-field="scaffolding_status"]')].some((input) => input.dataset.savedValue === "done")"""
        )
        self.assertIn("Done", card.inner_text())
        self.assertEqual(self.browser_errors, [])

    def test_active_house_snag_toggle_preserves_page_and_card_scroll(self):
        self.page.set_viewport_size({"width": 1000, "height": 520})
        with server.connect_db() as db:
            db.execute(
                "INSERT INTO provided_houses(id, postcode, source) "
                "VALUES ('SNAG-SCROLL-TEST', 'D02 TEST', 'government')"
            )
            db.execute(
                "INSERT INTO houses(id, postcode) "
                "VALUES ('SNAG-SCROLL-TEST', 'D02 TEST')"
            )
            db.executemany(
                "INSERT INTO house_snags(house_id, text) VALUES ('SNAG-SCROLL-TEST', ?)",
                [(f"Snag item {index}",) for index in range(1, 13)],
            )
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator(".app-shell:not(.hidden)").wait_for()
        self.page.locator('.nav-item[data-tab="houses"]').click()
        card = self.page.locator(
            '.house-active:has(.house-body[data-house-id="SNAG-SCROLL-TEST"])'
        )
        card.wait_for()
        snag = card.locator(".snag-row").last
        snag.scroll_into_view_if_needed()
        positions_before = self.page.evaluate(
            """() => ({
              page: window.scrollY,
              card: document.querySelector('.house-body[data-house-id="SNAG-SCROLL-TEST"]').scrollTop
            })"""
        )
        self.assertGreater(positions_before["card"], 0)
        snag.click()
        updated_snag = self.page.locator(
            '.house-body[data-house-id="SNAG-SCROLL-TEST"] .snag-row'
        ).last
        self.page.wait_for_function(
            """() => {
              const button = document.querySelector('.house-body[data-house-id="SNAG-SCROLL-TEST"] .snag-row:last-of-type');
              return button?.getAttribute('aria-pressed') === 'true';
            }"""
        )
        positions_after = self.page.evaluate(
            """() => ({
              page: window.scrollY,
              card: document.querySelector('.house-body[data-house-id="SNAG-SCROLL-TEST"]').scrollTop
            })"""
        )
        self.assertLessEqual(abs(positions_after["page"] - positions_before["page"]), 2)
        self.assertLessEqual(abs(positions_after["card"] - positions_before["card"]), 2)
        self.assertTrue(updated_snag.get_attribute("class").endswith("done"))
        updated_snag.scroll_into_view_if_needed()
        positions_before_undo = self.page.evaluate(
            """() => ({
              page: window.scrollY,
              card: document.querySelector('.house-body[data-house-id="SNAG-SCROLL-TEST"]').scrollTop
            })"""
        )
        updated_snag.click()
        self.page.wait_for_function(
            """() => {
              const button = document.querySelector('.house-body[data-house-id="SNAG-SCROLL-TEST"] .snag-row:last-of-type');
              return button?.getAttribute('aria-pressed') === 'false';
            }"""
        )
        final_positions = self.page.evaluate(
            """() => ({
              page: window.scrollY,
              card: document.querySelector('.house-body[data-house-id="SNAG-SCROLL-TEST"]').scrollTop
            })"""
        )
        self.assertLessEqual(abs(final_positions["page"] - positions_before_undo["page"]), 2)
        self.assertLessEqual(abs(final_positions["card"] - positions_before_undo["card"]), 2)
        self.assertEqual(self.browser_errors, [])

    def test_calculator_actions_are_colored_and_left_aligned(self):
        self.page.goto(f"{self.base_url}/calculator", wait_until="domcontentloaded")
        actions = self.page.locator(".action-bar .actions")
        print_button = actions.locator("button").nth(0)
        assign_button = actions.locator("#saveToHouse")
        self.assertEqual(
            actions.evaluate("(element) => getComputedStyle(element).justifyContent"),
            "flex-start",
        )
        self.assertEqual(
            actions.evaluate("(element) => getComputedStyle(element).flexBasis"),
            "100%",
        )
        self.assertLessEqual(
            print_button.evaluate(
                "(button) => button.getBoundingClientRect().left - button.closest('.actions').getBoundingClientRect().left"
            ),
            1,
        )
        self.assertEqual(
            print_button.evaluate(
                "(element) => getComputedStyle(element).backgroundColor"
            ),
            "rgb(39, 107, 169)",
        )
        self.assertEqual(
            assign_button.evaluate(
                "(element) => getComputedStyle(element).backgroundColor"
            ),
            "rgb(33, 134, 83)",
        )
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.assertEqual(
            actions.evaluate("(element) => getComputedStyle(element).justifyContent"),
            "flex-start",
        )
        self.assertEqual(
            actions.evaluate("(element) => getComputedStyle(element).flexDirection"),
            "row",
        )
        self.assertEqual(self.browser_errors, [])

    def test_active_house_grid_uses_page_scrolling(self):
        with server.connect_db() as db:
            with db:
                for index in range(8):
                    house_id = f"PAGE-SCROLL-{index}"
                    db.execute(
                        "INSERT INTO provided_houses(id, source) VALUES (?, 'tracker')",
                        (house_id,),
                    )
                    db.execute("INSERT INTO houses(id) VALUES (?)", (house_id,))
        self.page.set_viewport_size({"width": 1000, "height": 620})
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator(".app-shell:not(.hidden)").wait_for()
        self.page.locator('[data-house-view="active"]').click()
        house_list = self.page.locator("#houses-list")
        self.assertFalse(
            house_list.evaluate(
                "(element) => element.classList.contains('house-grid-scrollable')"
            )
        )
        self.assertEqual(
            house_list.evaluate("(element) => getComputedStyle(element).overflowY"),
            "visible",
        )
        self.assertGreater(
            self.page.evaluate(
                "() => document.scrollingElement.scrollHeight - window.innerHeight"
            ),
            0,
        )
        self.page.evaluate("() => window.scrollTo(0, document.scrollingElement.scrollHeight)")
        self.assertGreater(self.page.evaluate("() => window.scrollY"), 0)
        self.assertEqual(
            house_list.evaluate("(element) => element.scrollTop"),
            0,
        )
        self.assertEqual(self.browser_errors, [])

    def test_ready_house_opens_the_surveyor_room(self):
        with server.connect_db() as db:
            db.execute(
                "INSERT INTO provided_houses(id, postcode, address, owner_name, "
                "source, ready_for_start) VALUES (?, ?, ?, ?, 'government', 1)",
                (
                    "SURVEY-ROOM-TEST",
                    "D01 TEST",
                    "A deliberately long survey address intended to wrap cleanly across "
                    "the Ready card header without creating an inner scrollbar",
                    "A long owner name for testing ready card header wrapping",
                ),
            )
            db.execute(
                "UPDATE provided_houses SET plan_scaffolding_notes = ? "
                "WHERE id = 'SURVEY-ROOM-TEST'",
                ("Long pre-start detail that should remain visible without a clipped header",),
            )
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator(".app-shell:not(.hidden)").wait_for()
        self.assertFalse(self.page.locator("#surveyor-room").is_visible())
        self.page.locator('[data-house-view="ready"]').click()
        survey_button = self.page.locator(
            '.house-survey-open[data-house-id="SURVEY-ROOM-TEST"]'
        )
        survey_button.wait_for()
        ready_card = self.page.locator(
            '.house-plan-ready[data-house-id="SURVEY-ROOM-TEST"]'
        )
        self.assertEqual(ready_card.locator(".house-note-open").count(), 0)
        self.assertEqual(ready_card.locator(".house-checklist-open").count(), 0)
        action_buttons = ready_card.locator(".house-card-actions .text-button")
        button_heights = action_buttons.evaluate_all(
            "(buttons) => buttons.map((button) => button.getBoundingClientRect().height)"
        )
        self.assertGreaterEqual(len(button_heights), 3)
        self.assertEqual(len(set(button_heights)), 1)
        ready_header = ready_card.locator(".house-top")
        self.assertEqual(
            ready_header.evaluate("(element) => getComputedStyle(element).overflowY"),
            "visible",
        )

        self.assertLessEqual(
            ready_header.evaluate(
                "(element) => element.scrollHeight - element.clientHeight"
            ),
            1,
        )
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.assertEqual(
            ready_header.evaluate("(element) => getComputedStyle(element).overflowY"),
            "visible",
        )
        self.assertLessEqual(
            ready_header.evaluate(
                "(element) => element.scrollHeight - element.clientHeight"
            ),
            1,
        )
        self.page.set_viewport_size({"width": 1280, "height": 800})
        self.assertNotEqual(
            ready_card.locator(".seai-activate").evaluate(
                "(button) => getComputedStyle(button).gridColumn"
            ),
            "1 / -1",
        )
        self.assertEqual(survey_button.inner_text(), "Surveyor room")
        self.assertEqual(survey_button.get_attribute("aria-label"), "Open surveyor room"        )
        self.assertTrue(ready_card.locator(".seai-activate").is_disabled())
        self.assertEqual(ready_card.locator(".activation-requirement").count(), 0)
        self.assertLessEqual(
            ready_card.locator(".house-body").evaluate(
                "(element) => element.scrollHeight - element.clientHeight"
            ),
            1,
        )
        self.assertLessEqual(
            ready_card.evaluate(
                "(element) => element.scrollHeight - element.clientHeight"
            ),
            1,
        )
        self.assertNotIn(
            "house-grid-scrollable",
            self.page.locator("#houses-list").get_attribute("class"),
        )
        survey_button.click()
        self.assertTrue(self.page.locator("#surveyor-room").is_visible())
        status = self.page.locator("#survey-room-status")
        self.assertEqual(status.inner_text(), "Not surveyed")
        self.assertIn("survey-not-surveyed", status.get_attribute("class"))
        self.assertEqual(
            status.evaluate("(element) => getComputedStyle(element).backgroundColor"),
            "rgb(255, 244, 223)",
        )
        nav = self.page.locator(".survey-room-nav")
        self.assertEqual(nav.locator("a").count(), 5)
        self.assertEqual(nav.locator("a[href='#survey-room-documents']").count(), 0)
        nav.locator('a[href="#survey-room-work-plan"]').click()
        self.assertEqual(
            self.page.locator("#house-survey-title").inner_text(),
            "SURVEY-ROOM-TEST · Surveyor room",
        )
        work_plan = self.page.locator("#surveyor-work-plan-form")
        self.assertTrue(work_plan.is_visible())
        work_plan.locator('[name="scaffolding_required"]').select_option("true")
        work_plan.locator('[name="scaffolding_notes"]').fill("Rear access only")
        work_plan.locator('[name="esb_required"]').select_option("false")
        work_plan.locator('[name="plumber_required"]').select_option("true")
        work_plan.locator('[name="plumber_notes"]').fill("Coordinate with owner")
        with server.connect_db() as db:
            unsaved_plan = db.execute(
                "SELECT plan_scaffolding_required, plan_esb_required, "
                "plan_plumber_required, plan_scaffolding_notes, plan_plumber_notes "
                "FROM provided_houses "
                "WHERE id = 'SURVEY-ROOM-TEST'"
            ).fetchone()
        self.assertEqual(
            tuple(unsaved_plan),
            (
                None,
                None,
                None,
                "Long pre-start detail that should remain visible without a clipped header",
                "",
            ),
        )
        self.page.locator("#survey-perimeter").fill("42.75")
        self.page.locator("#house-survey-form button[type='submit']").click()
        self.page.wait_for_function(
            """() => document.querySelector('#toast').textContent.includes(
              'Pre-start work plan is not saved'
            )"""
        )
        self.assertTrue(self.page.locator("#surveyor-room").is_visible())
        self.assertIn(
            "Pre-start work plan is not saved",
            self.page.locator("#surveyor-work-plan-save-status").inner_text(),
        )
        with server.connect_db() as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM house_surveys WHERE house_id = 'SURVEY-ROOM-TEST'"
                ).fetchone()[0],
                0,
            )
        work_plan.locator('button[type="submit"]').click()
        self.page.wait_for_function(
            """() => document.querySelector('#surveyor-work-plan-save-status')
              ?.textContent === 'Work plan saved.'"""
        )
        with server.connect_db() as db:
            plan = db.execute(
                "SELECT plan_scaffolding_required, plan_scaffolding_notes, "
                "plan_esb_required, plan_plumber_required, plan_plumber_notes "
                "FROM provided_houses WHERE id = 'SURVEY-ROOM-TEST'"
            ).fetchone()
            draft = db.execute(
                "SELECT data FROM house_work_plan_drafts WHERE house_id = 'SURVEY-ROOM-TEST'"
            ).fetchone()
        self.assertEqual(
            tuple(plan),
            (
                1,
                "Rear access only",
                0,
                1,
                "Coordinate with owner",
            ),
        )
        self.assertIsNone(draft)
        self.page.locator("#house-survey-form button[type='submit']").click()
        self.page.wait_for_function(
            "() => document.querySelector('#surveyor-room').classList.contains('hidden')"
        )
        with server.connect_db() as db:
            published_plan = db.execute(
                "SELECT plan_scaffolding_required, plan_scaffolding_notes, "
                "plan_esb_required, plan_plumber_required, plan_plumber_notes "
                "FROM provided_houses WHERE id = 'SURVEY-ROOM-TEST'"
            ).fetchone()
            self.assertIsNone(
                db.execute(
                    "SELECT 1 FROM house_work_plan_drafts WHERE house_id = 'SURVEY-ROOM-TEST'"
                ).fetchone()
            )
        self.assertEqual(
            tuple(published_plan),
            (1, "Rear access only", 0, 1, "Coordinate with owner"),
        )
        self.assertFalse(
            self.page.locator(
                '.house-plan-ready[data-house-id="SURVEY-ROOM-TEST"] .seai-activate'
            ).is_disabled()
        )
        self.assertEqual(self.page.locator(".nav-item.active").get_attribute("data-tab"), "houses")
        self.assertTrue(
            self.page.locator("#houses-list").evaluate(
                "(list) => list.classList.contains('house-grid-ready')"
            )
        )
        self.page.locator(
            '.house-survey-open[data-house-id="SURVEY-ROOM-TEST"]'
        ).click()
        survey_status = self.page.locator("#survey-room-status")
        self.assertEqual(survey_status.inner_text(), "Survey recorded")
        self.assertIn("survey-complete", survey_status.get_attribute("class"))
        self.assertEqual(
            survey_status.evaluate("(element) => getComputedStyle(element).backgroundColor"),
            "rgb(229, 244, 238)",
        )
        self.assertEqual(
            self.page.locator("#survey-room-summary > div").count(),
            1,
        )
        self.assertEqual(
            self.page.locator(".survey-room-summary-card h2").inner_text(),
            "Assigned house information (reference copy)",
        )
        self.assertFalse(self.page.locator("#surveyor-house-notes-form").is_visible())
        self.assertEqual(self.page.locator("[data-edit-house-notes]").count(), 1)
        surveyor_house_notes = self.page.locator(
            "#survey-room-summary-house-notes"
        )
        self.assertEqual(surveyor_house_notes.inner_text(), "No house notes")
        self.assertEqual(self.page.locator("#survey-room-notes").count(), 0)
        self.assertEqual(
            self.page.locator(".survey-room-nav a[href='#survey-room-notes']").count(),
            0,
        )
        with server.connect_db() as db:
            saved_notes = db.execute(
                "SELECT notes FROM provided_houses WHERE id = 'SURVEY-ROOM-TEST'"
            ).fetchone()["notes"]
        self.assertEqual(saved_notes, "")
        self.page.locator("#back-from-surveyor-room").click()
        self.page.locator(
            '.house-survey-open[data-house-id="SURVEY-ROOM-TEST"]'
        ).click()
        self.assertTrue(self.page.locator("#survey-windows input").first.is_visible())
        self.assertEqual(self.page.locator("#surveyor-survey-notes-form").count(), 0)
        self.page.locator("#back-from-surveyor-room").click()
        self.assertTrue(self.page.locator("#houses-overview").is_visible())
        self.assertEqual(self.browser_errors, [])

    def test_surveyor_can_add_a_temporary_annotated_blank_page_to_share(self):
        house_id = "SURVEY-BLANK-PDF-TEST"
        with server.connect_db() as db:
            db.execute(
                "INSERT INTO provided_houses(id, postcode, address, owner_name, "
                "source, ready_for_start) VALUES (?, 'D01 TEST', '1 Example Road', "
                "'Example Owner', 'government', 1)",
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
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator(".app-shell:not(.hidden)").wait_for()
        self.page.locator('[data-house-view="ready"]').click()
        self.page.locator(
            f'.house-survey-open[data-house-id="{house_id}"]'
        ).click()
        self.page.locator("#survey-room-seai-library").wait_for(state="visible")
        self.assertTrue(
            self.page.locator("#survey-room-seai-library-title").is_visible()
        )
        self.page.locator(
            ".survey-room-nav a[data-ready-only][href='#survey-room-seai-details']"
        ).click()
        seai_pdfs_link = self.page.locator(
            ".survey-room-nav a[href='#survey-room-seai-library']"
        )
        self.assertTrue(seai_pdfs_link.is_visible())
        seai_pdfs_link.click()
        self.assertEqual(
            self.page.evaluate(
                "document.querySelector('#survey-room-seai-library').id"
            ),
            "survey-room-seai-library",
        )
        self.page.locator(
            "#survey-room-seai-documents [data-seai-create-blank]"
        ).wait_for()
        self.page.locator(
            "#survey-room-seai-documents [data-seai-create-blank]"
        ).click()
        self.page.locator("#pdf-annotator-dialog[open]").wait_for()
        self.page.wait_for_function(
            "document.querySelector('#pdf-annotator-image').naturalWidth > 0"
        )
        self.page.locator("#pdf-annotator-zoom-in").click()
        self.page.locator("#pdf-annotator-zoom-in").click()
        self.wait_for_pdf_canvas_layout()
        canvas = self.page.locator("#pdf-annotator-canvas")
        original_page_size = canvas.evaluate(
            "element => [element.clientWidth, element.clientHeight]"
        )
        canvas_box = canvas.bounding_box()
        self.page.mouse.move(canvas_box["x"] + 40, canvas_box["y"] + 45)
        self.page.mouse.down()
        self.page.mouse.move(canvas_box["x"] + 150, canvas_box["y"] + 120)
        self.page.mouse.up()
        self.page.locator("[data-pdf-tool='text']").click()
        self.page.locator("#pdf-annotator-text").fill(
            "Please check all window measurements and confirm the ventilation details "
            "before the survey is shared with the team."
        )
        self.page.mouse.click(
            canvas_box["x"] + canvas_box["width"] - 24,
            canvas_box["y"] + canvas_box["height"] - 30,
        )
        self.page.get_by_role(
            "button", name="Add blank page to selection"
        ).click()
        self.page.get_by_text("1 page selected").wait_for()
        share_button = self.page.locator(
            "#survey-room-seai-documents [data-seai-share-selected]"
        )
        previous_toast = self.page.locator("#toast").inner_text()
        share_button.click()
        self.page.wait_for_function(
            """previous => document.querySelector('#selected-pdf-preview-dialog').open ||
              document.querySelector('#toast').textContent.trim() !== previous""",
            arg=previous_toast,
        )
        self.assertTrue(
            self.page.locator("#selected-pdf-preview-dialog").evaluate(
                "dialog => dialog.open"
            ),
            self.page.locator("#toast").inner_text(),
        )
        self.page.locator("#selected-pdf-preview-dialog[open]").wait_for()
        preview = self.page.locator("#selected-pdf-preview")
        self.assertTrue(preview.get_attribute("src").startswith("blob:"))
        preview_pdf = self.page.evaluate(
            """async url => {
              const response = await fetch(url);
              const blob = await response.blob();
              return {
                type: blob.type,
                signature: new TextDecoder().decode(
                  await blob.slice(0, 5).arrayBuffer()
                )
              };
            }""",
            preview.get_attribute("src"),
        )
        self.assertEqual(preview_pdf, {"type": "application/pdf", "signature": "%PDF-"})
        previous_toast = self.page.locator("#toast").inner_text()
        self.page.locator("#selected-pdf-preview-continue").click()
        self.page.wait_for_function(
            """previous => !document.querySelector('#selected-pdf-preview-dialog').open ||
              document.querySelector('#toast').textContent.trim() !== previous""",
            arg=previous_toast,
        )
        self.assertFalse(
            self.page.locator("#selected-pdf-preview-dialog").evaluate(
                "dialog => dialog.open"
            ),
            self.page.locator("#toast").inner_text(),
        )
        self.assertIn(
            "saved to the house SEAI PDFs",
            self.page.locator("#toast").inner_text(),
        )
        self.assertFalse(self.page.locator("#house-document-share-dialog").evaluate(
            "dialog => dialog.open"
        ))
        saved_entry = self.page.locator(
            "#survey-room-saved-pdfs-list .saved-pdf-entry"
        ).filter(
            has_text=f"{house_id}-selected-seai-pages.pdf"
        )
        saved_entry.wait_for(state="visible")
        self.assertTrue(
            self.page.locator("#survey-room-saved-pdfs").evaluate(
                """section => section.parentElement.id === 'surveyor-room' &&
                  section.compareDocumentPosition(
                    document.querySelector('#survey-room-seai-library')
                  ) & Node.DOCUMENT_POSITION_FOLLOWING"""
            )
        )
        self.assertRegex(
            saved_entry.locator(".saved-pdf-view").get_attribute("href"),
            r"^/api/seai-documents/\d+/file$",
        )
        self.assertEqual(
            saved_entry.locator(".saved-pdf-delete[data-selected-seai-delete]").count(),
            1,
        )
        self.page.evaluate("document.documentElement.dataset.theme = 'dark'")
        self.assertEqual(
            saved_entry.locator(".saved-pdf-filename").evaluate(
                "(element) => getComputedStyle(element).color"
            ),
            "rgb(229, 239, 232)",
        )
        self.page.once("dialog", lambda dialog: dialog.accept())
        saved_entry.locator(".saved-pdf-delete").click()
        saved_entry.wait_for(state="detached")
        with server.connect_db() as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM seai_documents WHERE application_id = ?",
                    (house_id,),
                ).fetchone()[0],
                0,
            )
        self.assertEqual(self.browser_errors, []        )

    def test_surveyor_room_lists_and_opens_oldest_house_test_pdfs(self):
        self.page.set_viewport_size({"width": 1280, "height": 1000})
        self.create_seai_document_storage()
        house_id = "SURVEY-REAL-PDF-LIBRARY-TEST"
        pdf_directory = (
            server.STATIC_DIR
            / "oldest-house-test"
            / "synthetic"
            / "pdfs"
        )
        pdf_categories = server.SURVEY_ROOM_TEST_PDFS
        with server.connect_db() as db:
            db.execute(
                "INSERT INTO provided_houses(id, postcode, address, owner_name, "
                "source, ready_for_start) VALUES (?, 'D01 TEST', '1 Example Road', "
                "'Example Owner', 'government', 1)",
                (house_id,),
            )

        self.page.reload(wait_until="domcontentloaded")
        self.page.locator(".app-shell:not(.hidden)").wait_for()
        self.page.locator('[data-house-view="ready"]').click()
        self.page.locator(
            f'.house-survey-open[data-house-id="{house_id}"]'
        ).click()

        library = self.page.locator("#survey-room-seai-library")
        self.assertTrue(library.is_visible())
        library.locator(".seai-pdf-library-count").wait_for()
        self.assertEqual(
            library.locator(".seai-pdf-library-count").inner_text(),
            "6 files",
        )
        for category in ("Application", "Survey", "Variation"):
            section = library.locator(
                ".seai-document-category"
            ).filter(has_text=f"{category} PDFs")
            section.locator("summary").click()
        for filename in pdf_categories:
            entry = library.locator(".seai-document-entry").filter(
                has_text=filename
            )
            entry.wait_for(state="visible")
            self.assertTrue(entry.locator(".seai-document-status.test-sample").is_visible())
            pdf_link = entry.locator("a.seai-document-open")
            self.assertEqual(pdf_link.inner_text(), "View PDF")
            response = self.page.evaluate(
                """async (url) => {
                  const response = await fetch(url);
                  const blob = await response.blob();
                  const signature = new TextDecoder().decode(
                    await blob.slice(0, 5).arrayBuffer()
                  );
                  return {
                    status: response.status,
                    contentType: response.headers.get('content-type'),
                    signature,
                    size: blob.size,
                  };
                }""",
                pdf_link.get_attribute("href"),
            )
            self.assertEqual(response["status"], 200, filename)
            self.assertEqual(response["contentType"], "application/pdf", filename)
            self.assertEqual(response["signature"], "%PDF-", filename)
            self.assertEqual(response["size"], (pdf_directory / filename).stat().st_size)
            if filename == "application-letter.pdf":
                preview_button = entry.locator("[data-survey-room-pdf-preview]")
                preview_button.click()
                preview_dialog = self.page.locator(
                    "#survey-room-pdf-preview-dialog"
                )
                preview_dialog.wait_for(state="visible")
                iframe_url = urlsplit(
                    preview_dialog.locator("iframe").get_attribute("src")
                )
                document_url = urlsplit(pdf_link.get_attribute("href"))
                self.assertEqual(
                    (iframe_url.path, iframe_url.query),
                    (document_url.path, document_url.query),
                )
                self.assertEqual(
                    preview_dialog.locator("iframe").evaluate(
                        "frame => getComputedStyle(frame).flexGrow"
                    ),
                    "1",
                )
                preview_dialog.locator(".close-dialog").first.click()
                preview_dialog.wait_for(state="hidden")
        application_entry = library.locator(".seai-document-entry").filter(
            has_text="application-letter.pdf"
        )
        application_entry.locator("[data-test-pdf-name]").click()
        self.page.locator("#pdf-annotator-dialog[open]").wait_for()
        self.page.wait_for_function(
            "document.querySelector('#pdf-annotator-image').naturalWidth > 0"
        )
        self.page.locator("[data-pdf-tool='text']").click()
        self.page.locator("#pdf-annotator-text").fill(
            "Staged annotation for the final PDF"
        )
        self.wait_for_pdf_canvas_layout()
        canvas = self.page.locator("#pdf-annotator-canvas")
        canvas_box = canvas.bounding_box()
        self.page.mouse.click(
            canvas_box["x"] + canvas_box["width"] / 2,
            canvas_box["y"] + canvas_box["height"] / 2,
        )
        annotations_after_click = self.page.evaluate(
            """() => ({
              tool: state.pdfAnnotator.tool,
              input: document.querySelector('#pdf-annotator-text').value,
              annotations: state.pdfAnnotator.annotations.map(item => ({
                tool: item.tool,
                text: item.text,
                point: item.point
              })),
              canvas: (() => {
                const rect = document.querySelector('#pdf-annotator-canvas')
                  .getBoundingClientRect();
                return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
              })()
            })"""
        )
        self.assertTrue(
            any(
                item["text"] == "Staged annotation for the final PDF"
                for item in annotations_after_click["annotations"]
            ),
            annotations_after_click,
        )
        before_text_size = self.page.evaluate(
            """() => state.pdfAnnotator.annotations.find(
              item => item.tool === 'text' &&
                item.text === 'Staged annotation for the final PDF'
            ).font_size"""
        )
        self.page.evaluate(
            """() => {
              const slider = document.querySelector('#pdf-annotator-text-resize');
              slider.value = '40';
              slider.dispatchEvent(new Event('input', { bubbles: true }));
            }"""
        )
        self.assertEqual(
            self.page.locator("#pdf-annotator-text-resize-value").inner_text(),
            "40 px",
        )
        self.assertGreater(
            self.page.evaluate(
                """() => state.pdfAnnotator.annotations.find(
                  item => item.tool === 'text' &&
                    item.text === 'Staged annotation for the final PDF'
                ).font_size"""
            ),
            before_text_size,
        )
        self.page.get_by_role(
            "button", name="Add to selected pages"
        ).click()
        self.page.locator("#pdf-annotator-dialog").wait_for(state="hidden")
        self.page.get_by_text("1 page selected").wait_for()
        self.assertIn(
            "No saved PDFs yet",
            self.page.locator("#survey-room-saved-pdfs-list").inner_text(),
        )
        share_button = self.page.locator(
            "#survey-room-seai-documents [data-seai-share-selected]"
        )
        previous_toast = self.page.locator("#toast").inner_text()
        share_button.click()
        self.page.wait_for_function(
            """previous => document.querySelector('#selected-pdf-preview-dialog').open ||
              document.querySelector('#toast').textContent.trim() !== previous""",
            arg=previous_toast,
        )
        self.assertTrue(
            self.page.locator("#selected-pdf-preview-dialog").evaluate(
                "dialog => dialog.open"
            ),
            self.page.locator("#toast").inner_text(),
        )
        self.page.locator("#selected-pdf-preview-dialog[open]").wait_for()
        previous_toast = self.page.locator("#toast").inner_text()
        self.page.locator("#selected-pdf-preview-continue").click()
        self.page.wait_for_function(
            """previous => !document.querySelector('#selected-pdf-preview-dialog').open ||
              document.querySelector('#toast').textContent.trim() !== previous""",
            arg=previous_toast,
        )
        self.assertFalse(
            self.page.locator("#selected-pdf-preview-dialog").evaluate(
                "dialog => dialog.open"
            ),
            self.page.locator("#toast").inner_text(),
        )
        self.assertIn(
            "saved to the house SEAI PDFs",
            self.page.locator("#toast").inner_text(),
        )
        saved_pdf = self.page.locator(
            "#survey-room-saved-pdfs-list .saved-pdf-entry"
        ).filter(has_text=f"{house_id}-selected-seai-pages.pdf")
        saved_pdf.wait_for(state="visible")
        self.assertEqual(
            library.locator(".seai-document-entry.is-annotated").count(),
            0,
        )
        with server.connect_db() as db:
            saved_documents = db.execute(
                "SELECT filename, content FROM seai_documents WHERE application_id = ?",
                (house_id,),
            ).fetchall()
        self.assertEqual(
            [row["filename"] for row in saved_documents],
            [f"{house_id}-selected-seai-pages.pdf"],
        )
        saved_text = PdfReader(
            BytesIO(saved_documents[0]["content"])
        ).pages[0].extract_text()
        for expected_line in ("Staged", "annotation for", "the final PDF"):
            self.assertIn(expected_line, saved_text)
        self.assertEqual(self.browser_errors, [])

    def test_annotator_resizes_text_then_icon_and_icon_then_text(self):
        self.page.set_viewport_size({"width": 1280, "height": 1000})
        self.create_seai_document_storage()
        house_id = "SURVEY-PDF-MIXED-RESIZE-TEST"
        with server.connect_db() as db:
            db.execute(
                "INSERT INTO provided_houses(id, postcode, address, owner_name, "
                "source, ready_for_start) VALUES (?, 'D01 TEST', '1 Resize Road', "
                "'Resize Test', 'government', 1)",
                (house_id,),
            )

        self.page.reload(wait_until="domcontentloaded")
        self.page.locator(".app-shell:not(.hidden)").wait_for()
        self.page.locator('[data-house-view="ready"]').click()
        self.page.locator(
            f'.house-survey-open[data-house-id="{house_id}"]'
        ).click()
        library = self.page.locator("#survey-room-seai-library")
        library.locator(".seai-document-category summary").first.click()
        library.locator(".seai-document-entry").first.locator(
            "[data-test-pdf-name]"
        ).click()
        self.page.locator("#pdf-annotator-dialog[open]").wait_for()
        self.page.wait_for_function(
            "document.querySelector('#pdf-annotator-image').naturalWidth > 0"
        )
        canvas = self.page.locator("#pdf-annotator-canvas")

        def drag_text_resize_handle(text):
            self.wait_for_pdf_canvas_layout()
            handle = self.page.evaluate(
                """text => {
                  const annotator = state.pdfAnnotator;
                  const annotation = annotator.annotations.find(
                    item => item.tool === 'text' && item.text === text
                  );
                  const canvas = document.querySelector('#pdf-annotator-canvas');
                  const layout = pdfTextAnnotationLayout(
                    canvas.getContext('2d'),
                    annotation,
                    canvas.clientWidth,
                    canvas.clientHeight
                  );
                  return {
                    x: layout.x + layout.width - 2,
                    y: layout.y + layout.height - 2
                  };
                }""",
                text,
            )
            box = canvas.bounding_box()
            slider = self.page.locator("#pdf-annotator-text-resize")
            self.page.mouse.move(box["x"] + handle["x"], box["y"] + handle["y"])
            self.page.mouse.down()
            drag_state = self.page.evaluate(
                """({ x, y, text, oldBox, handle }) => ({
                  mode: state.pdfAnnotator.textDrag?.mode,
                  hit: document.elementFromPoint(x, y)?.id ||
                    document.elementFromPoint(x, y)?.className?.baseVal ||
                    document.elementFromPoint(x, y)?.className,
                  annotation: (() => {
                    const annotator = state.pdfAnnotator;
                    const annotation = annotator.annotations.find(
                      item => item.tool === 'text' && item.text === text
                    );
                    const canvas = document.querySelector('#pdf-annotator-canvas');
                    const layout = pdfTextAnnotationLayout(
                      canvas.getContext('2d'),
                      annotation,
                      canvas.clientWidth,
                      canvas.clientHeight
                    );
                    return { point: annotation.point, layout };
                  })(),
                  canvas: (() => {
                    const rect = document.querySelector('#pdf-annotator-canvas')
                      .getBoundingClientRect();
                    return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
                  })(),
                  point: { x, y },
                  previousBox: oldBox,
                  handle
                })""",
                {
                    "x": box["x"] + handle["x"],
                    "y": box["y"] + handle["y"],
                    "text": text,
                    "oldBox": box,
                    "handle": handle,
                },
            )
            self.assertEqual(drag_state["mode"], "resize", drag_state)
            before = int(slider.input_value())
            self.page.mouse.move(
                box["x"] + handle["x"],
                box["y"] + handle["y"] + 8,
                steps=4,
            )
            self.page.mouse.up()
            after = int(slider.input_value())
            self.assertGreater(
                after,
                before,
                f"First drag on the {text!r} text handle should resize",
            )
            self.page.wait_for_timeout(100)

        def place_text(text, x_ratio, y_ratio):
            self.page.locator("[data-pdf-tool='text']").click()
            self.page.locator("#pdf-annotator-text").fill(text)
            self.wait_for_pdf_canvas_layout()
            box = canvas.bounding_box()
            self.page.mouse.click(
                box["x"] + box["width"] * x_ratio,
                box["y"] + box["height"] * y_ratio,
            )
            text_count = self.page.evaluate(
                "text => state.pdfAnnotator.annotations.filter("
                "item => item.tool === 'text' && item.text === text"
                ").length",
                text,
            )
            placement_state = self.page.evaluate(
                """({ xRatio, yRatio }) => ({
                  tool: state.pdfAnnotator.tool,
                  input: document.querySelector('#pdf-annotator-text').value,
                  annotations: state.pdfAnnotator.annotations.map(item => ({
                    tool: item.tool,
                    text: item.text,
                    point: item.point
                  })),
                  canvas: (() => {
                    const canvas = document.querySelector('#pdf-annotator-canvas');
                    const rect = canvas.getBoundingClientRect();
                    return {
                      x: rect.x,
                      y: rect.y,
                      width: rect.width,
                      height: rect.height,
                      hit: document.elementFromPoint(
                        rect.x + rect.width * xRatio,
                        rect.y + rect.height * yRatio
                      )?.id
                    };
                  })(),
                  toast: document.querySelector('#toast').textContent
                })""",
                {"xRatio": x_ratio, "yRatio": y_ratio},
            )
            self.assertEqual(text_count, 1, placement_state)
            self.assertEqual(
                [
                    placement_state["canvas"]["width"],
                    placement_state["canvas"]["height"],
                ],
                original_page_size,
                "Showing text controls must not rescale the PDF page at 225% zoom",
            )
            self.assertTrue(
                self.page.locator("#pdf-annotator-text-resize").is_enabled()
            )
            drag_text_resize_handle(text)
            drag_text_resize_handle(text)

        def drag_and_resize_icon(x_ratio, y_ratio):
            box = canvas.bounding_box()
            self.page.locator(".pdf-annotator-icon-card").first.drag_to(
                canvas,
                target_position={
                    "x": box["width"] * x_ratio,
                    "y": box["height"] * y_ratio,
                },
            )
            self.page.wait_for_timeout(100)
            self.assertEqual(
                canvas.evaluate(
                    "element => [element.clientWidth, element.clientHeight]"
                ),
                original_page_size,
                "Showing icon controls must not rescale the PDF page at 225% zoom",
            )
            self.assertTrue(self.page.locator("#pdf-annotator-icon-edit").is_visible())
            self.assertEqual(
                self.page.locator("[data-pdf-tool='pen']").get_attribute("aria-pressed"),
                "true",
                "Dropping an icon should leave the annotator in icon-interaction mode",
            )

            def resize_selected_icon(dx, dy):
                handle = self.page.evaluate(
                    """() => {
                      const annotator = state.pdfAnnotator;
                      const annotation = annotator.annotations.find(
                        item => item.id === annotator.selectedIconId &&
                          item.tool === 'ventilation_icon'
                      );
                      const canvas = document.querySelector('#pdf-annotator-canvas');
                      const rect = canvas.getBoundingClientRect();
                      return {
                        x: rect.x + (annotation.point[0] + annotation.width / 2) * rect.width,
                        y: rect.y + (annotation.point[1] + annotation.height / 2) * rect.height,
                        width: annotation.width,
                        height: annotation.height
                      };
                    }"""
                )
                self.page.mouse.move(handle["x"], handle["y"])
                self.page.mouse.down()
                self.page.mouse.move(
                    handle["x"] + dx,
                    handle["y"] + dy,
                    steps=4,
                )
                self.page.mouse.up()
                resized = self.page.evaluate(
                    """() => {
                      const annotation = state.pdfAnnotator.annotations.find(
                        item => item.id === state.pdfAnnotator.selectedIconId &&
                          item.tool === 'ventilation_icon'
                      );
                      return { width: annotation.width, height: annotation.height };
                    }"""
                )
                self.assertGreater(resized["width"], handle["width"])
                self.assertGreater(resized["height"], handle["height"])

            resize_selected_icon(18, 14)
            resize_selected_icon(12, 10)

        place_text("Text first resize handle", 0.12, 0.25)
        drag_and_resize_icon(0.66, 0.35)
        self.assertTrue(
            self.page.locator("#pdf-annotator-text-resize").is_disabled(),
            "Dropping an icon should clear the previous text selection",
        )
        self.page.locator("[data-pdf-tool='text']").click()
        self.assertFalse(
            self.page.locator("#pdf-annotator-icon-edit").is_visible(),
            "Switching to text should clear the icon selection",
        )
        drag_text_resize_handle("Text first resize handle")
        drag_text_resize_handle("Text first resize handle")

        self.page.locator("[data-pdf-tool='pen']").click()
        drag_and_resize_icon(0.84, 0.35)
        place_text("Text after icon resize handle", 0.65, 0.05)
        self.assertEqual(self.browser_errors, [])

    def test_surveyor_room_does_not_show_empty_house_pdfs_block(self):
        house_id = "SURVEY-PDF-EMPTY-BLOCK-TEST"
        with server.connect_db() as db:
            db.execute(
                "INSERT INTO provided_houses(id, postcode, address, owner_name, "
                "source, ready_for_start) VALUES (?, 'D01 TEST', '1 Test Road', "
                "'Survey PDF Test', 'government', 1)",
                (house_id,),
            )
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator(".app-shell:not(.hidden)").wait_for()
        self.page.locator('[data-house-view="ready"]').click()
        self.page.locator(
            f'.house-survey-open[data-house-id="{house_id}"]'
        ).click()
        self.assertEqual(self.page.locator("#survey-room-house-pdfs").count(), 0)
        self.assertEqual(
            self.page.locator(".survey-room-nav a[href='#survey-room-house-pdfs']").count(),
            0,
        )
        self.assertNotIn(
            "No survey PDFs uploaded for this house yet",
            self.page.locator("#surveyor-room").inner_text(),
        )
        self.assertEqual(self.page.locator("#survey-room-seai-documents").count(), 1)
        self.assertEqual(self.browser_errors, [])

    def test_surveyor_logo_returns_from_room_to_assigned_house_dashboard(self):
        created = self.page.request.post(
            f"{self.base_url}/api/users",
            data={
                "username": "room-logo-surveyor",
                "display_name": "Room Logo Surveyor",
                "pin": "234567",
                "role": "surveyor",
            },
        )
        self.assertEqual(created.status, 201, created.text())
        user_id = created.json()["user"]["id"]
        house_id = "SURVEY-LOGO-DASHBOARD-TEST"
        with server.connect_db() as db:
            db.execute(
                "INSERT INTO provided_houses(id, postcode, address, owner_name, "
                "source, ready_for_start) VALUES (?, 'D01 TEST', '1 Example Road', "
                "'Example Owner', 'government', 1)",
                (house_id,),
            )
            db.execute(
                """
                INSERT INTO house_role_assignments
                    (house_id, role, user_id, assigned_by, assigned_at)
                VALUES (?, 'surveyor', ?, 'Test Admin', '2026-11-10T12:00:00')
                """,
                (house_id, user_id),
            )
        login = self.page.request.post(
            f"{self.base_url}/api/auth/login",
            data={"username": "room-logo-surveyor", "pin": "234567"},
        )
        self.assertEqual(login.status, 200, login.text())
        cookie = login.headers["set-cookie"].split(";", 1)[0]
        name, value = cookie.split("=", 1)
        self.page.context.add_cookies(
            [{"name": name, "value": value, "url": self.base_url}]
        )
        self.page.goto(self.base_url, wait_until="domcontentloaded")
        self.page.locator(".app-shell:not(.hidden)").wait_for()
        self.page.locator(
            f'.house-survey-open[data-house-id="{house_id}"]'
        ).click()
        self.assertTrue(self.page.locator("#surveyor-room").is_visible())
        self.page.locator(".surveyor-brand").click()
        self.page.wait_for_function(
            """() => document.querySelector('#surveyor-room').classList.contains('hidden') &&
              !document.querySelector('#houses-overview').classList.contains('hidden')"""
        )
        self.page.locator(
            f'.house-survey-open[data-house-id="{house_id}"]'
        ).wait_for(state="visible")
        self.assertEqual(
            self.page.locator(".nav-item.active").get_attribute("data-tab"),
            "houses",
        )
        self.assertTrue(
            self.page.locator(
                f'.house-survey-open[data-house-id="{house_id}"]'
            ).is_visible()
        )
        self.assertEqual(self.browser_errors, [])

    def test_ready_card_only_shows_ready_and_survey_audit(self):
        with server.connect_db() as db:
            with db:
                db.execute(
                    "INSERT INTO provided_houses(id, source) VALUES (?, 'government')",
                    ("AUDIT-NOT-STARTED-TEST",),
                )
                db.execute(
                    "INSERT INTO provided_houses(id, source, ready_for_start) "
                    "VALUES (?, 'government', 1)",
                    ("AUDIT-READY-TEST",),
                )
                for house_id in ("AUDIT-NOT-STARTED-TEST", "AUDIT-READY-TEST"):
                    server._record_house_status(
                        db, house_id, "ready", "Ready for start actor"
                    )
                    server._record_house_status(db, house_id, "active", "Active actor")

        self.page.reload(wait_until="domcontentloaded")
        self.page.locator(".app-shell:not(.hidden)").wait_for()
        self.page.locator('[data-house-view="not_started"]').click()
        not_started_card = self.page.locator(
            '.house-plan-pending[data-house-id="AUDIT-NOT-STARTED-TEST"]'
        )
        not_started_card.wait_for()
        self.assertEqual(not_started_card.locator(".house-audit").count(), 0)

        self.page.locator('[data-tab="overview"]').click()
        self.page.locator('[data-house-view="ready"]').click()
        ready_card = self.page.locator(
            '.house-plan-ready[data-house-id="AUDIT-READY-TEST"]'
        )
        ready_card.wait_for()
        audit = ready_card.locator(".house-audit")
        self.assertTrue(audit.is_visible())
        audit_text = audit.inner_text()
        self.assertIn("Marked ready to start", audit_text)
        self.assertIn("Ready for start actor", audit_text)
        self.assertNotIn("Active actor", audit_text)
        self.assertNotIn("Activated by", audit_text)
        self.assertNotIn("Not recorded", audit_text)
        self.assertEqual(self.browser_errors, [])

    def test_ewi_supervisor_uses_team_dashboard_layout(self):
        created = self.page.request.post(
            f"{self.base_url}/api/users",
            data={
                "username": "ewi-team-supervisor",
                "display_name": "EWI Team Supervisor",
                "pin": "456789",
                "role": "supervisor",
                "supervisor_specialty": "External Wall Insulation",
            },
        )
        self.assertEqual(created.status, 201, created.text())

        self.page.locator("#account-button").click()
        self.page.locator("#account-sign-out").click()
        self.page.locator("#auth-screen:not(.hidden)").wait_for()
        self.page.locator("#auth-username").fill("ewi-team-supervisor")
        self.page.locator("#auth-pin").fill("456789")
        self.page.locator("#auth-submit").click()

        self.page.get_by_role("heading", name="My EWI dashboard").wait_for()
        self.assertIn(
            "supervisor-workspace-mode",
            self.page.locator("body").get_attribute("class"),
        )
        self.assertTrue(self.page.locator("#supervisor-workload").is_visible())
        self.assertTrue(
            self.page.locator("#supervisor-dashboard-summary").is_hidden()
        )
        self.assertEqual(
            self.page.locator(".workspace-nav").evaluate(
                "element => getComputedStyle(element).display"
            ),
            "none",
        )
        self.assertIn(
            "EWI houses",
            self.page.locator("#supervisor-workload-intro").inner_text(),
        )
        if self.page.locator("html").get_attribute("data-theme") != "dark":
            self.page.locator("#theme-toggle").click()
        greeting = self.page.locator(
            "#supervisor-workload .plumbing-coordinator-greeting"
        )
        self.assertNotIn(
            "rgb(255, 255, 255)",
            greeting.evaluate("(element) => getComputedStyle(element).backgroundImage"),
        )
        self.page.wait_for_function(
            """() => getComputedStyle(
              document.querySelector("#refresh-supervisor-houses")
            ).backgroundColor === "rgb(32, 55, 42)" """
        )
        self.assertEqual(
            self.page.locator(
                "#supervisor-workload .ventilation-refresh-button"
            ).evaluate("(element) => getComputedStyle(element).backgroundColor"),
            "rgb(32, 55, 42)",
        )
        self.assertEqual(
            self.page.locator("#supervisor-house-list .supervisor-shared-seai").count(),
            0,
        )
        self.assertEqual(self.browser_errors, [])

    def test_supervisor_sees_only_assigned_houses_and_saves_delivery_assignments(self):
        with server.connect_db() as db:
            with db:
                supervisor_id = server._insert_user(
                    db, "supervisor-one", "Supervisor One", "456789", "supervisor"
                )
                other_supervisor_id = server._insert_user(
                    db, "supervisor-two", "Supervisor Two", "567890", "supervisor"
                )
                teamlead_id = server._insert_user(
                    db, "teamlead-one", "Team Lead One", "678901", "teamlead"
                )
                inactive_teamlead_id = server._insert_user(
                    db,
                    "teamlead-inactive",
                    "Inactive Team Lead",
                    "789012",
                    "teamlead",
                )
                ewi_department_id = db.execute(
                    """
                    SELECT id FROM departments
                    WHERE name = 'External Wall Insulation' COLLATE NOCASE
                    """
                ).fetchone()["id"]
                db.execute(
                    "UPDATE app_users SET supervisor_specialty = "
                    "'External Wall Insulation' WHERE id = ?",
                    (supervisor_id,),
                )
                server._set_user_organization(
                    db, supervisor_id, [ewi_department_id]
                )
                server._set_user_organization(
                    db,
                    teamlead_id,
                    [ewi_department_id],
                    supervisor_id,
                )
                db.execute(
                    "INSERT INTO team_roster_members (teamlead_user_id, member_name) "
                    "VALUES (?, 'Roster Worker One')",
                    (teamlead_id,),
                )
                for house_id, owner in (
                    ("SUPERVISOR-OWN", "Own House"),
                    ("SUPERVISOR-OWN-2", "Second Own House"),
                    ("SUPERVISOR-OTHER", "Other House"),
                ):
                    db.execute(
                        """
                        INSERT INTO provided_houses(id, owner_name, source)
                        VALUES (?, ?, 'government')
                        """,
                        (house_id, owner),
                    )
                    server._upsert_house(db, house_id, owner_name=owner)
                db.execute(
                    "CREATE TABLE IF NOT EXISTS seai_applications "
                    "(application_id TEXT PRIMARY KEY, detail_data_json TEXT NOT NULL)"
                )
                db.execute(
                    """
                    INSERT OR REPLACE INTO seai_applications
                        (application_id, detail_data_json)
                    VALUES (?, ?)
                    """,
                    (
                        "SUPERVISOR-OWN",
                        json.dumps(
                            {
                                "Application Details": {
                                    "Application ID": "SEAI-OWN-01",
                                    "Applicant Name": "Own House Applicant",
                                },
                                "Property Details": {"Eircode": "D01 TEST"},
                                "Measures": [
                                    {
                                        "Name": "External Wall Insulation",
                                        "Survey Quantity": "98",
                                        "Survey Qty including Variation": "108",
                                    }
                                ],
                            }
                        ),
                    ),
                )
                db.execute(
                    """
                    INSERT INTO house_documents
                        (house_id, filename, content, size_bytes, uploaded_by, uploaded_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "SUPERVISOR-OWN",
                        "surveyor-annotated.pdf",
                        b"%PDF-1.4\n",
                        9,
                        "Surveyor One",
                        "2026-10-30T10:00:00",
                    ),
                )
                db.execute(
                    """
                    INSERT INTO house_vent_documents
                        (house_id, filename, content, size_bytes, shared_by, shared_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "SUPERVISOR-OWN",
                        "vent-annotated.pdf",
                        b"%PDF-1.4\n",
                        9,
                        "Vent annotated by Vent One",
                        "2026-10-30T11:00:00",
                    ),
                )
                db.executemany(
                    """
                    INSERT INTO house_role_assignments
                        (house_id, role, user_id, assigned_by, assigned_at)
                    VALUES (?, 'supervisor', ?, 'Test Admin', '2026-10-29T10:00:00')
                    """,
                    (
                        ("SUPERVISOR-OWN", supervisor_id),
                        ("SUPERVISOR-OWN-2", supervisor_id),
                        ("SUPERVISOR-OTHER", other_supervisor_id),
                    ),
                )
                db.execute(
                    "UPDATE houses SET delivery_team = 'Team Lead One' "
                    "WHERE id = 'SUPERVISOR-OTHER'"
                )
                db.execute(
                    """
                    UPDATE houses
                    SET delivery_person = 'Gary Lowther',
                        delivery_team = 'Team Lead One'
                    WHERE id = 'SUPERVISOR-OWN-2'
                    """
                )
                db.execute(
                    """
                    UPDATE houses
                    SET delivery_team = 'Team Lead One'
                    WHERE id = 'SUPERVISOR-OWN'
                    """
                )
                db.execute(
                    "UPDATE houses SET is_finished = 1 WHERE id = 'SUPERVISOR-OWN-2'"
                )
                db.executemany(
                    """
                    INSERT INTO house_ewi_assignments
                        (house_id, teamlead_user_id, supervisor_user_id,
                         assigned_by, assigned_at, completed_by, completed_at)
                    VALUES (?, ?, ?, 'Supervisor One', '2026-10-29T10:00:00', ?, ?)
                    """,
                    (
                        (
                            "SUPERVISOR-OWN",
                            teamlead_id,
                            supervisor_id,
                            "",
                            "",
                        ),
                        (
                            "SUPERVISOR-OWN-2",
                            teamlead_id,
                            supervisor_id,
                            "Team Lead One",
                            "2026-10-30T10:00:00",
                        ),
                    ),
                )
                db.execute(
                    "UPDATE app_users SET is_active = 0 WHERE id = ?",
                    (inactive_teamlead_id,),
                )

        self.page.locator("#account-button").click()
        self.page.locator("#account-sign-out").click()
        self.page.locator("#auth-screen:not(.hidden)").wait_for()
        self.page.locator("#auth-username").fill("supervisor-one")
        self.page.locator("#auth-pin").fill("456789")
        self.page.locator("#auth-submit").click()
        self.page.get_by_role("heading", name="My EWI dashboard").wait_for()
        self.assertTrue(self.page.locator("#supervisor-workload").is_visible())
        self.assertEqual(
            self.page.locator("#supervisor-filter-all-count").inner_text(),
            "2",
        )
        self.assertEqual(
            self.page.locator("#supervisor-filter-needs-assignment-count").inner_text(),
            "0",
        )
        self.assertEqual(
            self.page.locator(
                '#supervisor-house-filters [data-supervisor-filter="active"]'
            ).evaluate(
                "element => element.textContent.replace(/\\s+/g, ' ').trim()"
            ),
            "Assigned 1",
        )
        self.assertEqual(
            self.page.locator(
                '#supervisor-house-filters [data-supervisor-filter="closed"]'
            ).evaluate(
                "element => element.textContent.replace(/\\s+/g, ' ').trim()"
            ),
            "Completed 1",
        )
        self.assertEqual(
            self.page.locator(
                '#supervisor-house-filters [data-supervisor-filter="needs_assignment"]'
            ).evaluate(
                "element => element.textContent.replace(/\\s+/g, ' ').trim()"
            ),
            "Needs assignment 0",
        )
        self.assertEqual(
            self.page.locator(
                "#supervisor-house-filters [data-supervisor-filter]"
            ).evaluate_all(
                "elements => elements.map(element => "
                "element.textContent.replace(/\\s+/g, ' ').trim())"
            ),
            ["All houses 2", "Assigned 1", "Needs assignment 0", "Completed 1"],
        )
        self.page.locator(
            '#supervisor-house-filters [data-supervisor-filter="closed"]'
        ).click()
        self.assertEqual(
            self.page.locator("#supervisor-house-list article[data-house-id]").count(),
            1,
        )
        self.assertEqual(
            self.page.locator("#supervisor-house-list article[data-house-id]").get_attribute(
                "data-house-id"
            ),
            "SUPERVISOR-OWN-2",
        )
        self.page.locator(
            '#supervisor-house-filters [data-supervisor-filter="active"]'
        ).click()
        self.assertEqual(
            self.page.locator("#supervisor-house-list article[data-house-id]").count(),
            1,
        )
        self.assertEqual(
            self.page.locator("#supervisor-house-list article[data-house-id]").get_attribute(
                "data-house-id"
            ),
            "SUPERVISOR-OWN",
        )
        self.page.locator(
            '#supervisor-house-filters [data-supervisor-filter="all"]'
        ).click()
        self.page.locator("#supervisor-team-rosters > summary").click()
        roster_card = self.page.locator(
            "#supervisor-team-roster-list .supervisor-team-roster-card"
        )
        self.assertEqual(roster_card.count(), 1)
        self.assertIn("Team Lead One", roster_card.inner_text())
        self.assertIn("Roster Worker One", roster_card.inner_text())
        self.assertEqual(
            roster_card.locator(".supervisor-team-workload-active-count").inner_text(),
            "1",
        )
        self.assertEqual(
            roster_card.locator(".supervisor-team-workload-closed-count").inner_text(),
            "1",
        )
        self.assertTrue(
            self.page.locator(
                '#supervisor-house-list [aria-label="EWI work assignment for SUPERVISOR-OWN"]'
            ).is_visible()
        )
        self.assertNotIn("Inactive Team Lead", self.page.locator(
            "#supervisor-team-roster-list"
        ).inner_text())

        ewi_assignment = self.page.locator(
            '#supervisor-house-list [aria-label="EWI work assignment for SUPERVISOR-OWN"]'
        )
        ewi_assignment.locator('select[name="teamlead_user_id"]').select_option("")
        ewi_assignment.get_by_role("button", name="Save EWI assignment").click()
        self.page.get_by_text(
            "EWI work assignment removed from SUPERVISOR-OWN."
        ).wait_for()
        self.assertEqual(
            self.page.locator("#supervisor-filter-needs-assignment-count").inner_text(),
            "1",
        )
        self.assertEqual(
            self.page.locator("#supervisor-filter-active-count").inner_text(),
            "0",
        )
        self.page.locator(
            '#supervisor-house-filters [data-supervisor-filter="needs_assignment"]'
        ).click()
        self.assertEqual(
            self.page.locator("#supervisor-house-list article[data-house-id]").count(),
            1,
        )
        self.assertEqual(
            self.page.locator("#supervisor-house-list article[data-house-id]").get_attribute(
                "data-house-id"
            ),
            "SUPERVISOR-OWN",
        )
        self.page.locator(
            '#supervisor-house-filters [data-supervisor-filter="all"]'
        ).click()

        own_house = self.page.locator(
            '#supervisor-house-list article[data-house-id="SUPERVISOR-OWN"]'
        )
        own_house.wait_for()
        self.assertEqual(
            self.page.locator(
                '#supervisor-house-list article[data-house-id="SUPERVISOR-OTHER"]'
            ).count(),
            0,
        )
        self.assertFalse(self.page.locator('.nav-item[data-tab="inventory"]').is_visible())
        self.assertIn("ewi-supervisor-house-card", own_house.get_attribute("class"))
        self.assertIn("108 m²", own_house.inner_text())
        self.assertTrue(
            own_house.get_by_role("button", name="View house details").is_visible()
        )
        self.assertTrue(
            own_house.get_by_role("button", name="Annotated PDFs").is_visible()
        )
        self.assertEqual(
            own_house.locator(
                ".supervisor-house-assignment:not(.supervisor-ewi-delivery-assignment)"
            ).count(),
            0,
        )
        own_house.get_by_role("button", name="Annotated PDFs").click()
        self.page.get_by_role(
            "heading", name="Surveyor PDFs shared with EWI"
        ).wait_for()
        self.assertEqual(
            self.page.locator("#vent-survey-pdfs-list .ewi-house-pdf-group").count(),
            3,
        )
        self.assertIn("surveyor-annotated.pdf", self.page.locator("#vent-survey-pdfs-list").inner_text())
        self.assertIn("vent-annotated.pdf", self.page.locator("#vent-survey-pdfs-list").inner_text())
        self.page.locator(
            "#vent-survey-pdfs-dialog .close-dialog"
        ).last.click()
        own_house.get_by_role("button", name="View house details").click()
        self.page.locator("#house-detail-title").wait_for()
        self.assertTrue(self.page.locator("#page-houses").is_visible())
        self.assertTrue(self.page.locator("#house-detail").is_visible())
        self.assertEqual(
            self.page.locator("#house-detail-title").inner_text(),
            "SUPERVISOR-OWN",
        )
        self.assertIn("108 m²", self.page.locator("#house-detail-facts").inner_text())
        self.page.locator("#back-to-houses").click()
        self.page.locator("#page-overview.active").wait_for()
        own_house.wait_for()
        self.assertIn("Own House Applicant", own_house.inner_text())
        self.assertIn("108 m²", own_house.inner_text())
        self.assertIn("D01 TEST", own_house.inner_text())
        delivery_assignment = own_house.locator(".supervisor-ewi-delivery-assignment")
        delivery_assignment.locator('select[name="delivery_person"]').select_option(
            label="Karol"
        )
        delivery_assignment.get_by_role(
            "button", name="Save delivery person"
        ).click()
        self.page.get_by_text("Delivery person saved for SUPERVISOR-OWN.").wait_for()
        with server.connect_db() as db:
            saved_delivery = db.execute(
                "SELECT delivery_person, delivery_team FROM houses WHERE id = ?",
                ("SUPERVISOR-OWN",),
            ).fetchone()
        self.assertEqual(saved_delivery["delivery_person"], "Karol")
        self.assertEqual(saved_delivery["delivery_team"], "Team Lead One")
        ewi_assignment = own_house.locator(".supervisor-ewi-assignment")
        ewi_assignment.locator('select[name="teamlead_user_id"]').select_option(
            str(teamlead_id)
        )
        ewi_assignment.get_by_role("button", name="Save EWI assignment").click()
        self.page.get_by_text(
            "SUPERVISOR-OWN assigned to Team Lead One for EWI work."
        ).wait_for()
        if self.page.locator("html").get_attribute("data-theme") != "dark":
            self.page.locator("#theme-toggle").click()
        self.page.wait_for_function(
            """() => getComputedStyle(document.querySelector(
              '#supervisor-house-list article[data-house-id="SUPERVISOR-OWN"] ' +
              '.supervisor-ewi-assignment button'
            )).backgroundColor === 'rgb(38, 58, 48)'"""
        )
        self.assertEqual(
            own_house.locator(".supervisor-ewi-assignment select").evaluate(
                "(element) => getComputedStyle(element).backgroundColor"
            ),
            "rgb(23, 35, 30)",
        )
        self.assertEqual(
            own_house.locator(".supervisor-ewi-assignment button").evaluate(
                "(element) => getComputedStyle(element).backgroundColor"
            ),
            "rgb(38, 58, 48)",
        )
        self.assertLessEqual(
            own_house.locator(".supervisor-ewi-assignment").evaluate(
                "(element) => element.getBoundingClientRect().right"
            ),
            own_house.evaluate("(element) => element.getBoundingClientRect().right"),
        )
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.assertLessEqual(
            own_house.evaluate("(element) => element.scrollWidth"),
            own_house.evaluate("(element) => element.clientWidth"),
        )
        self.assertLessEqual(
            own_house.locator("select").evaluate_all(
                "(elements) => Math.max(...elements.map((element) => element.scrollWidth - element.clientWidth))"
            ),
            0,
        )

        self.page.locator("#account-button").click()
        self.page.locator("#account-sign-out").click()
        self.page.locator("#auth-screen:not(.hidden)").wait_for()
        self.page.locator("#auth-username").fill("teamlead-one")
        self.page.locator("#auth-pin").fill("678901")
        self.page.locator("#auth-submit").click()
        self.page.get_by_role("heading", name="Your EWI work").wait_for()
        self.assertIn(
            "teamlead-workspace-mode",
            self.page.locator("body").get_attribute("class"),
        )
        self.assertTrue(self.page.locator("#teamlead-ewi-house-filters").is_visible())
        self.assertEqual(
            self.page.locator("#teamlead-ewi-all-count").inner_text(),
            "2",
        )
        self.page.locator("#teamlead-ewi-crew > summary").click()
        self.assertIn(
            "Roster Worker One",
            self.page.locator("#teamlead-ewi-crew-list").inner_text(),
        )
        self.assertEqual(self.page.locator("html").get_attribute("data-theme"), "dark")
        self.assertEqual(
            self.page.locator("#teamlead-ewi-open-count").inner_text(),
            "1",
        )
        self.page.locator("#teamlead-ewi-search").fill("SUPERVISOR-OWN")
        self.page.wait_for_function(
            "document.querySelectorAll('#teamlead-ewi-work-list article[data-house-id]').length === 2"
        )
        self.assertEqual(
            self.page.locator("#teamlead-ewi-work-list article[data-house-id]").count(),
            2,
        )
        self.page.locator("#teamlead-ewi-search").fill("")
        teamlead_work_card = self.page.locator(
            '#teamlead-ewi-work-list article[data-house-id="SUPERVISOR-OWN"]'
        )
        self.assertEqual(
            teamlead_work_card.evaluate(
                "(element) => getComputedStyle(element).backgroundColor"
            ),
            "rgb(32, 44, 41)",
        )
        self.assertEqual(
            teamlead_work_card.locator(".teamlead-ewi-complete-button").evaluate(
                "(element) => getComputedStyle(element).backgroundColor"
            ),
            "rgb(35, 133, 91)",
        )
        self.assertEqual(
            self.page.locator("#teamlead-ewi-completed-count").inner_text(),
            "1",
        )
        self.page.locator(
            '#teamlead-ewi-work-list article[data-house-id="SUPERVISOR-OWN"] '
            '[data-teamlead-ewi-complete]'
        ).click()
        self.page.get_by_text(
            "EWI work for SUPERVISOR-OWN marked complete."
        ).wait_for()
        self.assertEqual(
            self.page.locator("#teamlead-ewi-open-count").inner_text(),
            "0",
        )
        self.assertEqual(
            self.page.locator("#teamlead-ewi-completed-count").inner_text(),
            "2",
        )
        self.assertEqual(self.browser_errors, [])


if __name__ == "__main__":
    unittest.main()
