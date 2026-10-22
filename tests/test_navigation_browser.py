"""End-to-end browser test for the guided workspace navigation flow."""
from io import BytesIO
import json
import tempfile
import threading
import unittest
from pathlib import Path

import server
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
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.RequestHandler)
        self.server_thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.server_thread.start()
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch()
        self.page = self.browser.new_page()
        self.page.route(
            "https://tile.openstreetmap.org/**",
            lambda route: route.abort(),
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
        self.assertTrue(
            card.locator(".house-audit").evaluate(
                "(audit) => Boolean(audit.compareDocumentPosition(audit.parentElement.querySelector('.house-property-details')) & Node.DOCUMENT_POSITION_FOLLOWING)"
            )
        )
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
        self.assertIn("Surveyor Survey Actor", audit_text)
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
        self.assertEqual(nav.locator("a").count(), 4)
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
            "Page 1 of 2",
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
        self.page.locator("#pdf-annotator-next").click()
        self.page.wait_for_function(
            "() => document.querySelector('#pdf-annotator-page-count').textContent === 'Page 2 of 2'"
        )
        self.page.locator("#pdf-annotator-previous").click()
        self.page.wait_for_function(
            "() => document.querySelector('#pdf-annotator-page-count').textContent === 'Page 1 of 2'"
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


if __name__ == "__main__":
    unittest.main()
