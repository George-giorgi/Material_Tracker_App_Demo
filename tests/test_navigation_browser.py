"""End-to-end browser test for the guided workspace navigation flow."""
import tempfile
import threading
import unittest
from pathlib import Path

import server

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


if __name__ == "__main__":
    unittest.main()
