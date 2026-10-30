"""End-to-end browser test for the guided workspace navigation flow."""
from io import BytesIO
import http.client
import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

import server
from tests.support import UvicornTestServer
from pypdf import PdfReader
from reportlab.pdfgen import canvas

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
                (json.dumps({"windows": [], "doors": [], "house_corners": []}),),
            )

        self.page.locator("#account-button").click()
        self.page.locator("#account-sign-out").click()
        self.page.locator("#auth-screen:not(.hidden)").wait_for()
        self.page.locator("#auth-username").fill("dashboard-surveyor")
        self.page.locator("#auth-pin").fill("345678")
        self.page.locator("#auth-submit").click()

        self.page.get_by_role("heading", name="My survey dashboard").wait_for()
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
                (json.dumps({"windows": [{"w": 1.2, "h": 1.1, "qty": 2}], "doors": [], "house_corners": []}),),
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
                (json.dumps({"windows": [{"w": 1.6, "h": 1.5, "qty": 1}], "doors": [], "house_corners": []}),),
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
        self.assertFalse(self.page.locator(".surveyor-brand").evaluate(
            "(link) => link.classList.contains('has-new-information')"
        ))
        with server.connect_db() as db:
            db.execute(
                "UPDATE house_surveys SET data = ? WHERE house_id = 'DASHBOARD-2'",
                (json.dumps({"windows": [{"w": 1.4, "h": 1.3, "qty": 1}], "doors": [], "house_corners": []}),),
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
        insulation = card.locator(".house-insulation-summary")
        self.assertTrue(insulation.is_visible())
        self.assertIn("Sample insulation details", insulation.inner_text())
        self.assertIn("DEMO SAMPLE", insulation.inner_text())
        self.assertEqual(insulation.locator(".house-insulation-row").count(), 4)
        self.assertEqual(
            " ".join(card.locator(".surveyor-house-action").inner_text().split()),
            "Start survey →",
        )
        self.assertEqual(
            card.locator(".surveyor-house-action").get_attribute("aria-label"),
            "Open Surveyor room for DASHBOARD-1",
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
            "Review survey →",
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
        dark_styles = self.page.locator("#surveyor-room").evaluate(
            """root => Object.fromEntries([
              ['nav', '.survey-room-nav'],
              ['navLink', '.survey-room-nav a'],
              ['houseFact', '.survey-room-summary > div'],
              ['workPlanRow', '.work-plan-row'],
              ['upload', '.survey-document-upload'],
              ['measurementHint', '.survey-room-form > .helper-text']
            ].map(([key, selector]) => {
              const style = getComputedStyle(root.querySelector(selector));
              return [key, {background: style.backgroundColor, color: style.color}];
            }))"""
        )
        self.assertEqual(dark_styles["navLink"]["background"], "rgb(38, 53, 47)")
        self.assertEqual(dark_styles["houseFact"]["background"], "rgb(25, 38, 34)")
        self.assertEqual(dark_styles["workPlanRow"]["background"], "rgb(25, 38, 34)")
        self.assertEqual(dark_styles["upload"]["background"], "rgb(25, 38, 34)")
        self.assertEqual(dark_styles["measurementHint"]["background"], "rgb(25, 42, 32)")
        room_buttons = self.page.locator("#surveyor-room").evaluate(
            """root => Object.fromEntries([
              ['addRow', '.survey-add-row'],
              ['saveNotes', '#surveyor-house-notes-form .survey-room-notes-actions > .button'],
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
        self.assertGreaterEqual(room_buttons["saveNotes"]["height"], 44)
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
        self.assertEqual(len(room_layout["summaryColumns"].split()), 2)
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
                INSERT INTO provided_houses(id, owner_name, ready_for_start)
                VALUES ('READY-ASSIGN', 'Ready Owner', 1)
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
                INSERT INTO provided_houses(id, owner_name, ready_for_start)
                VALUES ('ACTIVE-ASSIGN', 'Active Owner', 0)
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
        self.assertEqual(admin_record_access, 403)
        self.page.locator("#account-button").click()
        self.page.locator("#account-sign-out").click()
        self.page.locator("#auth-screen:not(.hidden)").wait_for()
        self.page.locator("#auth-username").fill("assign-dashboard")
        self.page.locator("#auth-pin").fill("234567")
        self.page.locator("#auth-submit").click()

        dashboard = self.page.locator("#assign-houses-dashboard")
        dashboard.wait_for(state="visible")
        self.assertEqual(
            self.page.locator('[data-tab="houses"] .nav-label').inner_text(),
            "Dashboard",
        )
        self.assertTrue(self.page.locator("#houses-teams-tabs").is_hidden())
        self.assertTrue(self.page.locator("#teams-section").is_hidden())
        self.assertTrue(self.page.locator("#houses-section").is_visible())
        self.assertTrue(self.page.locator("#house-team-filter").is_hidden())
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
        assign_weather = self.page.locator("#assign-weather-panel")
        assign_weather.wait_for(state="visible")
        self.page.wait_for_function(
            "() => document.querySelector('#assign-weather-status')?.textContent.includes('1-day forecast')"
        )
        self.assertFalse(assign_weather.evaluate("(element) => element.open"))
        self.assertIn("Mayo & Sligo", assign_weather.locator("summary").inner_text())
        assign_weather.locator("summary").click()
        assign_weather_areas = self.page.locator("#assign-weather-areas .weather-area")
        self.assertEqual(assign_weather_areas.count(), 2)
        self.assertEqual(assign_weather_areas.nth(0).locator("h3").inner_text(), "County Mayo")
        self.assertEqual(assign_weather_areas.nth(1).locator("h3").inner_text(), "County Sligo")
        assign_weather.locator("#assign-weather-refresh").click()
        self.assertTrue(assign_weather.locator("#assign-weather-refresh").is_visible())
        self.page.set_viewport_size({"width": 375, "height": 800})
        self.assertLessEqual(
            assign_weather.evaluate("(element) => element.scrollWidth"),
            assign_weather.evaluate("(element) => element.clientWidth"),
        )
        self.page.set_viewport_size({"width": 1280, "height": 720})
        self.assertEqual(self.active_page(), "houses")
        self.assertEqual(
            self.page.locator("#houses-page-title").inner_text(),
            "House dashboard",
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
        self.assertEqual(active_card.locator(".stage-switch").count(), 0)
        self.assertEqual(active_card.locator(".house-note-open").count(), 0)
        self.assertEqual(active_card.locator(".house-snags-open").count(), 0)
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
        self.page.locator("#assign-seai-house-record .survey-room-seai-section").filter(
            has_text="Application",
        ).locator("summary").click()
        self.assertIn(
            "SEAI Owner",
            self.page.locator("#assign-seai-house-record").inner_text(),
        )
        self.assertIn(
            "External Wall Insulation",
            self.page.locator("#assign-seai-house-record").inner_text(),
        )
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
            "DEMO DATA",
            pending_card.locator(".house-insulation-demo-note").inner_text(),
        )
        self.assertIn("m²", pending_card.locator(".house-insulation-summary").inner_text())
        pending_action_rows = pending_card.evaluate(
            """card => {
              const details = card.querySelector('.assign-seai-details').getBoundingClientRect();
              const ready = card.querySelector('.plan-ready-toggle[data-ready="true"]').getBoundingClientRect();
              return {details: details.top, ready: ready.top};
            }"""
        )
        self.assertAlmostEqual(
            pending_action_rows["details"],
            pending_action_rows["ready"],
        )
        self.page.evaluate("document.documentElement.dataset.theme = 'dark'")
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
        self.page.evaluate("document.documentElement.dataset.theme = 'light'")
        pending_card.locator(".assign-seai-details").click()
        self.page.locator("#assign-seai-house-empty").wait_for(state="visible")
        ready_button = self.page.locator("#assign-seai-house-ready")
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
        self.assertIn("assign-ready-arrival", ready_card.get_attribute("class"))
        self.assertEqual(
            ready_card.evaluate("(card) => getComputedStyle(card).animationName"),
            "assign-ready-arrival",
        )
        self.assertNotIn(
            "Back to not started",
            ready_card.inner_text(),
        )
        self.assertEqual(ready_card.locator(".house-card-actions .text-button").count(), 3)
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
            ready_card.locator(".assign-house-team").inner_text(),
            "Assign Surveyor",
        )
        self.page.evaluate("window.__logoClickToken = 'assign-dashboard'")
        self.page.locator(".sidebar .brand").click()
        self.assertEqual(
            self.page.evaluate("window.__logoClickToken"),
            "assign-dashboard",
        )
        self.assertTrue(self.page.locator("#go-overview").is_hidden())
        self.page.set_viewport_size({"width": 357, "height": 480})
        mobile_nav_item = self.page.locator(".main-nav .nav-item:not(.hidden)")
        mobile_nav_item.wait_for()
        nav_layout = mobile_nav_item.evaluate(
            """item => ({
              direction: getComputedStyle(item).flexDirection,
              width: item.getBoundingClientRect().width,
              whiteSpace: getComputedStyle(item).whiteSpace,
              scrollWidth: item.scrollWidth,
              clientWidth: item.clientWidth,
              navWidth: item.parentElement.getBoundingClientRect().width,
              navTop: item.closest('.sidebar').getBoundingClientRect().top,
              viewportHeight: window.innerHeight
            })"""
        )
        self.assertEqual(nav_layout["direction"], "row")
        self.assertEqual(nav_layout["whiteSpace"], "nowrap")
        self.assertLessEqual(nav_layout["scrollWidth"], nav_layout["clientWidth"])
        self.assertGreater(nav_layout["width"], 240)
        self.assertLess(nav_layout["width"], nav_layout["navWidth"])
        self.assertGreaterEqual(
            nav_layout["navTop"],
            nav_layout["viewportHeight"] - 61,
        )
        self.assertEqual(self.browser_errors, [])

    def test_assign_dashboard_shows_surveyor_workload(self):
        with server.connect_db() as db:
            db.execute(
                """
                INSERT INTO provided_houses(id, postcode, owner_name)
                VALUES ('WORKLOAD-PENDING', 'T1', 'Pending Owner'),
                       ('WORKLOAD-COMPLETE', 'T2', 'Completed Owner')
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
                VALUES ('WORKLOAD-COMPLETE', '{}', 'House Surveyor', '2026-10-29T12:00:00')
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
                            "Application": [
                                {"Application ID": "MAP-ASSIGN-PENDING"}
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

    def test_assign_user_not_started_card_hides_surveyor_information(self):
        with server.connect_db() as db:
            db.execute(
                """
                INSERT INTO provided_houses(id, postcode, owner_name)
                VALUES ('TEAM-PREASSIGN-1', 'T2', 'Pending Owner')
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
        self.assertEqual(card.locator(".house-card-actions button").count(), 1)
        self.assertEqual(ready_button.inner_text(), "Mark ready for start")
        self.assertFalse(card.locator(".assign-house-team").count())

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
                db, "picker-supervisor", "Picker Supervisor", "456789", "supervisor"
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
                db, "browser-supervisor", "Browser Supervisor", "456789", "supervisor"
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
            self.page.get_by_text("Surveyor One", exact=True).is_visible()
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
        logo = self.page.locator(".sidebar .brand-logo")
        logo.wait_for()
        self.page.wait_for_function(
            "() => document.querySelector('.sidebar .brand-logo').naturalWidth > 0"
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
        overview_row = self.page.locator("#overview-stock tr").filter(
            has_text="Readable test material"
        )
        overview_row.wait_for()
        self.assertEqual(
            overview_row.locator(".material-name").evaluate(
                "(element) => getComputedStyle(element).color"
            ),
            "rgb(237, 246, 240)",
        )
        overview_row.hover()
        self.assertEqual(
            overview_row.locator("td").first.evaluate(
                "(element) => getComputedStyle(element).backgroundColor"
            ),
            "rgb(41, 58, 50)",
        )

        self.assertEqual(self.active_page(), "overview")
        self.page.locator('[data-house-view="ready"]').click()
        self.assertEqual(self.active_page(), "houses")
        ready_card = self.page.locator(
            '.house-plan-ready[data-house-id="DARK-READY-TEST"]'
        )
        ready_card.wait_for()
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
            "rgb(64, 93, 107)",
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

    def test_sidebar_brand_logo_is_slightly_larger_than_brand_area(self):
        logo = self.page.locator(".sidebar .brand-logo")
        self.page.wait_for_function(
            "() => document.querySelector('.sidebar .brand-logo').naturalWidth > 0"
        )
        self.assertAlmostEqual(
            logo.evaluate(
                "(image) => image.getBoundingClientRect().width - image.parentElement.getBoundingClientRect().width"
            ),
            10,
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

    def test_overview_shows_mayo_and_sligo_county_forecasts(self):
        status = self.page.locator("#weather-status")
        self.page.wait_for_function(
            "() => document.querySelector('#weather-status')?.textContent.includes('1-day forecast')"
        )
        areas = self.page.locator("#weather-areas .weather-area")
        self.assertEqual(areas.count(), 2)
        self.assertEqual(areas.nth(0).locator("h3").inner_text(), "County Mayo")
        self.assertEqual(areas.nth(1).locator("h3").inner_text(), "County Sligo")
        self.assertIn("Light Rain", areas.nth(0).inner_text())
        self.assertIn("High 11° · Low 5°", areas.nth(0).inner_text())
        self.assertIn("30 km/h", areas.nth(0).inner_text())
        self.assertIn(
            "Met Éireann",
            self.page.locator("#page-overview .weather-attribution").inner_text(),
        )
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
                    SET address = 'Audit Test Address', area_m2 = 100, perimeter_m = 40
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
        self.assertIn("Surveyor Name", card.inner_text())
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
        self.assertLess(long_card.bounding_box()["height"], 680)
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
        self.assertIsNone(activate_button.get_attribute("title"))
        self.assertEqual(
            self.page.locator(
                '.house-card[data-house-id="ACTIVATE-READY"] .activation-requirement'
            ).count(),
            0,
        )
        self.page.locator(
            '.house-card[data-house-id="ACTIVATE-READY"] .house-survey-open'
        ).click()
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
            "View on map",
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
        mobile_nav_top = self.page.locator(".sidebar").bounding_box()["y"]
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
        self.assertLess(ready_card.bounding_box()["height"], 400)
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
            ).locator("small").inner_text(),
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
            ).locator("small").inner_text(),
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
            action_buttons.filter(has_text="Activate house").evaluate(
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
        summary_status = self.page.locator(
            "#survey-room-summary .survey-room-summary-survey strong"
        )
        self.assertEqual(summary_status.inner_text(), "Not surveyed")
        self.assertIn("survey-not-surveyed", summary_status.get_attribute("class"))
        nav = self.page.locator(".survey-room-nav")
        self.assertEqual(nav.locator("a").count(), 6)
        nav.locator('a[href="#survey-room-work-plan"]').click()
        self.assertEqual(
            self.page.evaluate("window.location.hash"),
            "#survey-room-work-plan",
        )
        self.assertEqual(
            self.page.locator("#house-survey-title").inner_text(),
            "SURVEY-ROOM-TEST · Surveyor room",
        )
        work_plan = self.page.locator("#surveyor-work-plan-form")
        self.assertTrue(work_plan.is_visible())
        work_plan.locator('[name="scaffolding_required"]').select_option("true")
        self.page.get_by_text("Work plan saved automatically.").wait_for()
        with server.connect_db() as db:
            saved_requirement = db.execute(
                "SELECT plan_scaffolding_required FROM provided_houses "
                "WHERE id = 'SURVEY-ROOM-TEST'"
            ).fetchone()
        self.assertEqual(saved_requirement["plan_scaffolding_required"], 1)
        work_plan.locator('[name="scaffolding_notes"]').fill("Rear access only")
        work_plan.locator('[name="esb_required"]').select_option("false")
        work_plan.locator('[name="plumber_required"]').select_option("true")
        work_plan.locator('[name="plumber_notes"]').fill("Coordinate with owner")
        self.page.get_by_text("Work plan saved automatically.").wait_for()
        with server.connect_db() as db:
            saved_requirements = db.execute(
                "SELECT plan_scaffolding_required, plan_esb_required, "
                "plan_plumber_required FROM provided_houses "
                "WHERE id = 'SURVEY-ROOM-TEST'"
            ).fetchone()
        self.assertEqual(tuple(saved_requirements), (1, 0, 1))
        work_plan.locator('button[type="submit"]').click()
        self.page.get_by_text(
            "Pre-start work plan for SURVEY-ROOM-TEST saved."
        ).wait_for()
        with server.connect_db() as db:
            plan = db.execute(
                "SELECT plan_scaffolding_required, plan_scaffolding_notes, "
                "plan_esb_required, plan_plumber_required, plan_plumber_notes "
                "FROM provided_houses WHERE id = 'SURVEY-ROOM-TEST'"
            ).fetchone()
        self.assertEqual(
            tuple(plan),
            (1, "Rear access only", 0, 1, "Coordinate with owner"),
        )
        self.page.locator("#survey-perimeter").fill("42.75")
        self.page.locator("#house-survey-form button[type='submit']").click()
        self.page.wait_for_function(
            "() => document.querySelector('#surveyor-room').classList.contains('hidden')"
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
            self.page.locator(
                "#survey-room-summary .survey-room-summary-survey strong"
            ).inner_text(),
            "Survey recorded",
        )
        notes = self.page.locator("#surveyor-house-notes-form textarea")
        self.assertTrue(notes.is_visible())
        house_note_action = self.page.locator("[data-edit-house-notes]")
        self.assertEqual(house_note_action.inner_text(), "Add note")
        self.assertIn("note-action-add", house_note_action.get_attribute("class"))
        survey_note_action = self.page.locator("[data-edit-survey-notes]")
        self.assertEqual(survey_note_action.inner_text(), "Add note")
        self.assertIn("note-action-add", survey_note_action.get_attribute("class"))
        add_note_color = house_note_action.evaluate(
            "element => getComputedStyle(element).backgroundColor"
        )
        self.assertEqual(add_note_color, "rgb(49, 94, 170)")
        self.assertEqual(
            survey_note_action.evaluate(
                "element => getComputedStyle(element).backgroundColor"
            ),
            add_note_color,
        )
        notes.fill("Surveyor should call before access.")
        self.page.locator("#surveyor-house-notes-form button[type='submit']").click()
        self.page.get_by_text("House notes for SURVEY-ROOM-TEST saved.").wait_for()
        self.assertEqual(house_note_action.inner_text(), "Edit note")
        self.assertIn("note-action-edit", house_note_action.get_attribute("class"))
        self.page.wait_for_function(
            "() => getComputedStyle(document.querySelector('[data-edit-house-notes]')).backgroundColor === 'rgb(166, 91, 22)'"
        )
        edit_note_color = house_note_action.evaluate(
            "element => getComputedStyle(element).backgroundColor"
        )
        self.assertNotEqual(edit_note_color, add_note_color)
        self.page.wait_for_function(
            "() => document.querySelector('#surveyor-house-notes-form textarea').value === ''"
        )
        self.assertEqual(notes.input_value(), "")
        with server.connect_db() as db:
            saved_notes = db.execute(
                "SELECT notes FROM provided_houses WHERE id = 'SURVEY-ROOM-TEST'"
            ).fetchone()["notes"]
        self.assertEqual(saved_notes, "Surveyor should call before access.")
        self.page.locator("[data-edit-house-notes]").click()
        self.assertEqual(notes.input_value(), "Surveyor should call before access.")
        notes.fill("Updated access note.")
        self.page.locator("#surveyor-house-notes-form button[type='submit']").click()
        self.page.get_by_text("House notes for SURVEY-ROOM-TEST saved.").wait_for()
        self.page.wait_for_function(
            "() => document.querySelector('#surveyor-house-notes-form textarea').value === ''"
        )
        self.assertEqual(notes.input_value(), "")
        with server.connect_db() as db:
            saved_notes = db.execute(
                "SELECT notes FROM provided_houses WHERE id = 'SURVEY-ROOM-TEST'"
            ).fetchone()["notes"]
        self.assertEqual(saved_notes, "Updated access note.")
        self.page.locator("#back-from-surveyor-room").click()
        self.page.wait_for_function(
            "() => document.querySelector('[data-edit-house-notes]')?.classList.contains('note-action-edit')"
        )
        self.assertEqual(house_note_action.inner_text(), "Edit note")
        self.assertEqual(
            house_note_action.evaluate(
                "element => getComputedStyle(element).backgroundColor"
            ),
            "rgb(166, 91, 22)",
        )
        self.page.locator(
            '.house-survey-open[data-house-id="SURVEY-ROOM-TEST"]'
        ).click()
        self.assertTrue(self.page.locator("#survey-windows input").first.is_visible())
        pdf_buffer = BytesIO()
        pdf = canvas.Canvas(pdf_buffer)
        pdf.drawString(72, 720, "Browser survey plan")
        pdf.showPage()
        pdf.drawString(72, 720, "Second browser survey page")
        pdf.save()
        self.page.locator("#house-document-file").set_input_files(
            {
                "name": "survey-plan.pdf",
                "mimeType": "application/octet-stream",
                "buffer": pdf_buffer.getvalue(),
            }
        )
        self.page.locator("#house-document-form button[type='submit']").click()
        document_link = self.page.get_by_role("link", name="survey-plan.pdf")
        document_link.wait_for()
        response = self.page.request.get(
            f"{self.base_url}{document_link.get_attribute('href')}"
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertTrue(response.body().startswith(b"%PDF-"))
        self.page.set_viewport_size({"width": 768, "height": 1024})
        self.page.locator(".house-document-annotate").click()
        annotator = self.page.locator("#pdf-annotator-dialog")
        annotator.wait_for(state="visible")
        self.page.wait_for_function(
            "() => document.querySelector('#pdf-annotator-image').naturalWidth > 0"
        )
        self.assertEqual(
            self.page.locator("#pdf-annotator-page-count").inner_text(),
            "Page 1 of 2 · Scroll to change pages",
        )
        self.assertEqual(
            self.page.locator("#pdf-annotator-zoom-level").inner_text(),
            "100%",
        )
        self.page.locator("#pdf-annotator-zoom-in").click()
        self.assertEqual(
            self.page.locator("#pdf-annotator-zoom-level").inner_text(),
            "125%",
        )
        self.page.locator("#pdf-annotator-zoom-fit").click()
        self.assertEqual(
            self.page.locator("#pdf-annotator-zoom-level").inner_text(),
            "100%",
        )
        self.page.locator("#pdf-annotator-size").select_option("4")
        viewport = self.page.locator("#pdf-annotator-viewport")
        viewport.hover()
        self.page.mouse.wheel(0, 40)
        self.page.wait_for_function(
            "() => document.querySelector('#pdf-annotator-page-count').textContent === 'Page 2 of 2 · Scroll to change pages'"
        )
        self.page.mouse.wheel(0, -40)
        self.page.wait_for_function(
            "() => document.querySelector('#pdf-annotator-page-count').textContent === 'Page 1 of 2 · Scroll to change pages'"
        )
        annotation_canvas = self.page.locator("#pdf-annotator-canvas")
        canvas_bounds = annotation_canvas.bounding_box()
        self.page.mouse.move(canvas_bounds["x"] + 120, canvas_bounds["y"] + 140)
        self.page.mouse.down()
        self.page.mouse.move(canvas_bounds["x"] + 160, canvas_bounds["y"] + 150)
        self.page.mouse.up()
        self.assertFalse(self.page.locator("#pdf-annotator-save").is_disabled())
        self.page.locator("[data-pdf-tool='text']").click()
        self.page.locator("#pdf-annotator-text").fill("Survey check")
        annotation_canvas.click(position={"x": 160, "y": 200})
        self.page.locator("#pdf-annotator-undo").click()
        self.assertFalse(self.page.locator("#pdf-annotator-save").is_disabled())
        annotation_canvas.click(position={"x": 160, "y": 200})
        self.page.locator("#pdf-annotator-save").click()
        annotated_link = self.page.get_by_role(
            "link", name="survey-plan-annotated.pdf"
        )
        annotated_link.wait_for()
        annotated_response = self.page.request.get(
            f"{self.base_url}{annotated_link.get_attribute('href')}"
        )
        self.assertEqual(annotated_response.status, 200)
        self.assertIn(
            "Survey check",
            PdfReader(BytesIO(annotated_response.body())).pages[0].extract_text(),
        )
        with server.fitz.open(
            stream=annotated_response.body(), filetype="pdf"
        ) as annotated_pdf:
            self.assertGreater(
                max(path["width"] for path in annotated_pdf[0].get_drawings()),
                2.5,
            )
        self.assertTrue(document_link.is_visible())
        self.page.locator(
            '.house-document-share[data-document-name="survey-plan.pdf"]'
        ).click()
        self.page.get_by_text(
            "Only the Survey note is added to the shared PDF. Enter it in the Survey note field and click Save survey note first."
        ).wait_for()
        survey_note = self.page.locator("#surveyor-survey-notes-form textarea[name='notes']")
        self.page.wait_for_function(
            "() => document.activeElement === document.querySelector('#surveyor-survey-notes-form textarea[name=\"notes\"]')"
        )
        self.assertEqual(survey_note.input_value(), "")
        survey_note.fill("Survey details for sharing.")
        self.page.locator("#surveyor-survey-notes-form button[type='submit']").click()
        self.page.get_by_text("Survey note for SURVEY-ROOM-TEST saved.").wait_for()
        self.assertEqual(survey_note_action.inner_text(), "Edit note")
        self.assertIn("note-action-edit", survey_note_action.get_attribute("class"))
        self.page.wait_for_function(
            "() => document.querySelector('#surveyor-survey-notes-form textarea').value === ''"
        )
        self.assertEqual(survey_note.input_value(), "")
        self.page.locator("[data-edit-survey-notes]").click()
        self.assertEqual(survey_note.input_value(), "Survey details for sharing.")
        survey_note.fill("Updated survey note for sharing.")
        self.page.locator("#surveyor-survey-notes-form button[type='submit']").click()
        self.page.get_by_text("Survey note for SURVEY-ROOM-TEST saved.").wait_for()
        self.page.wait_for_function(
            "() => document.querySelector('#surveyor-survey-notes-form textarea').value === ''"
        )
        with server.connect_db() as db:
            saved_survey = db.execute(
                "SELECT data FROM house_surveys WHERE house_id = 'SURVEY-ROOM-TEST'"
            ).fetchone()["data"]
        self.assertEqual(
            json.loads(saved_survey)["notes"],
            "Updated survey note for sharing.",
        )
        self.page.locator(
            '.house-document-share[data-document-name="survey-plan.pdf"]'
        ).click()
        share_dialog = self.page.locator("#house-document-share-dialog")
        share_dialog.wait_for(state="visible")
        self.assertTrue(self.page.locator("#house-document-share-send").is_visible())
        self.assertTrue(self.page.locator("#house-document-share-email").is_visible())
        self.assertTrue(self.page.locator("#house-document-share-whatsapp").is_visible())
        self.assertIn(
            "survey note",
            self.page.locator("#house-document-share-description").inner_text().lower(),
        )
        with self.page.expect_download() as download_info:
            self.page.locator("#house-document-share-download").click()
        self.assertIn("with-survey-note.pdf", download_info.value.suggested_filename)
        self.page.locator("#back-from-surveyor-room").click()
        self.assertTrue(self.page.locator("#houses-overview").is_visible())
        self.assertEqual(self.browser_errors, [])

    def test_surveyor_room_can_delete_pdfs_and_read_filenames_in_dark_mode(self):
        house_id = "SURVEY-PDF-DELETE-TEST"
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
        self.page.locator("#survey-room-documents").wait_for(state="visible")

        pdf_buffer = BytesIO()
        pdf = canvas.Canvas(pdf_buffer)
        pdf.drawString(72, 720, "PDF deletion test")
        pdf.save()
        self.page.locator("#house-document-file").set_input_files(
            {
                "name": "survey-delete-test.pdf",
                "mimeType": "application/pdf",
                "buffer": pdf_buffer.getvalue(),
            }
        )
        self.page.locator("#house-document-form button[type='submit']").click()
        document_link = self.page.get_by_role("link", name="survey-delete-test.pdf")
        document_link.wait_for()
        self.page.evaluate(
            """() => {
              if (document.documentElement.dataset.theme !== "dark") {
                document.querySelector("#theme-toggle").click();
              }
            }"""
        )
        self.assertEqual(
            document_link.locator(".house-document-copy strong").evaluate(
                "element => getComputedStyle(element).color"
            ),
            "rgb(232, 240, 235)",
        )

        self.page.once("dialog", lambda dialog: dialog.accept())
        self.page.locator(
            '.house-document-delete[data-document-name="survey-delete-test.pdf"]'
        ).click()
        self.page.get_by_text("survey-delete-test.pdf deleted.").wait_for()
        self.page.locator(".house-documents-empty").wait_for()
        with server.connect_db() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM house_documents WHERE house_id = ?",
                (house_id,),
            ).fetchone()[0]
        self.assertEqual(count, 0)
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

    def test_supervisor_sees_only_assigned_houses_and_saves_delivery_assignments(self):
        with server.connect_db() as db:
            with db:
                supervisor_id = server._insert_user(
                    db, "supervisor-one", "Supervisor One", "456789", "supervisor"
                )
                other_supervisor_id = server._insert_user(
                    db, "supervisor-two", "Supervisor Two", "567890", "supervisor"
                )
                server._insert_user(
                    db, "teamlead-one", "Team Lead One", "678901", "teamlead"
                )
                inactive_teamlead_id = server._insert_user(
                    db,
                    "teamlead-inactive",
                    "Inactive Team Lead",
                    "789012",
                    "teamlead",
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
                    """
                    UPDATE houses
                    SET delivery_person = 'Gary Lowther',
                        delivery_team = 'Team Lead One'
                    WHERE id = 'SUPERVISOR-OWN-2'
                    """
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
        self.page.get_by_role("heading", name="My assigned houses").wait_for()

        own_house = self.page.locator(
            '#houses-list article[data-house-id="SUPERVISOR-OWN"]'
        )
        own_house.wait_for()
        self.assertEqual(
            self.page.locator(
                '#houses-list article[data-house-id="SUPERVISOR-OTHER"]'
            ).count(),
            0,
        )
        self.assertFalse(self.page.locator('.nav-item[data-tab="inventory"]').is_visible())
        self.assertEqual(
            own_house.locator('select[name="delivery_person"] option').evaluate_all(
                "(options) => options.map((option) => option.value).filter(Boolean)"
            ),
            ["Gary Lowther", "Karol", "Team Lead One"],
        )
        self.assertEqual(
            own_house.locator('select[name="delivery_team"] option').evaluate_all(
                "(options) => options.map((option) => option.value).filter(Boolean)"
            ),
            ["Team Lead One"],
        )
        own_house.locator('select[name="delivery_person"]').select_option("Karol")
        own_house.locator('select[name="delivery_team"]').select_option("Team Lead One")
        own_house.get_by_role("button", name="Save assignments").click()
        self.page.get_by_text(
            "Delivery assignments saved for SUPERVISOR-OWN."
        ).wait_for()
        self.assertEqual(
            own_house.locator(".supervisor-assignment-status").inner_text(),
            "Assigned",
        )
        self.assertEqual(
            own_house.get_by_role("button", name="Saved").count(),
            1,
        )
        self.assertEqual(
            self.page.locator("#supervisor-stat-unassigned").inner_text(),
            "0",
        )
        delivery_summary = self.page.locator("#supervisor-delivery-summary")
        karol_summary = delivery_summary.locator(
            ".supervisor-assignment-summary-row",
            has_text="Karol",
        )
        self.assertIn("1 house", karol_summary.inner_text())
        self.assertIn("SUPERVISOR-OWN", karol_summary.inner_text())
        gary_summary = delivery_summary.locator(
            ".supervisor-assignment-summary-row",
            has_text="Gary Lowther",
        )
        self.assertIn("1 house", gary_summary.inner_text())
        team_summary = self.page.locator("#supervisor-team-summary")
        teamlead_summary = team_summary.locator(
            ".supervisor-assignment-summary-row",
            has_text="Team Lead One",
        )
        self.assertIn("2 houses", teamlead_summary.inner_text())
        self.assertIn("SUPERVISOR-OWN-2", teamlead_summary.inner_text())
        self.assertEqual(
            team_summary.locator(".supervisor-assignment-summary-row").count(),
            1,
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

        with server.connect_db() as db:
            saved = db.execute(
                "SELECT delivery_person, delivery_team FROM houses "
                "WHERE id = 'SUPERVISOR-OWN'"
            ).fetchone()
        self.assertEqual(saved["delivery_person"], "Karol")
        self.assertEqual(saved["delivery_team"], "Team Lead One")
        self.assertEqual(self.browser_errors, [])


if __name__ == "__main__":
    unittest.main()
