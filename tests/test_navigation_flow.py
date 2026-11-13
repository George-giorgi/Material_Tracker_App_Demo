"""Tests for workspace navigation and the guided navigation test flow."""
import json
import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")


class NavigationMarkupParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.page_ids = set()
        self.nav_tabs = set()
        self.goto_targets = set()
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if "id" in attributes:
            self.ids.add(attributes["id"])
        if "page" in attributes.get("class", "").split():
            self.page_ids.add(attributes.get("data-page", ""))
        if "data-tab" in attributes:
            self.nav_tabs.add(attributes["data-tab"])
        if "data-goto" in attributes:
            self.goto_targets.add(attributes["data-goto"])


def extract_function(source, name):
    start = source.index(f"function {name}(")
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"Function {name} has unbalanced braces")


def extract_tour_steps(source):
    start = source.index("const navigationTourSteps = [")
    opening = source.index("[", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "[":
            depth += 1
        elif source[index] == "]":
            depth -= 1
            if depth == 0:
                return source[start:index + 1].removesuffix(";")
    raise AssertionError("navigationTourSteps has unbalanced brackets")


@unittest.skipUnless(shutil.which("node"), "node is required for navigation logic tests")
class NavigationFlowTests(unittest.TestCase):
    def test_assign_house_view_is_restored_per_account(self):
        helpers_start = APP_JS.index("const ASSIGN_HOUSE_VIEW_STORAGE_PREFIX")
        helpers_end = APP_JS.index("\nconst state = {", helpers_start)
        helpers = APP_JS[helpers_start:helpers_end]
        script = helpers + r"""
const values = new Map();
global.localStorage = {
  getItem(key) { return values.get(key) || null; },
  setItem(key, value) { values.set(key, value); },
};
const initial = readAssignHouseView({id: 14});
saveAssignHouseView({id: 14}, "ready", "needed");
const restored = readAssignHouseView({id: 14});
const otherAccount = readAssignHouseView({id: 15});
values.set("material-tracker-assign-house-view:14", "{invalid");
const invalidStoredValue = readAssignHouseView({id: 14});
console.log(JSON.stringify({initial, restored, otherAccount, invalidStoredValue}));
"""
        result = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "initial": {"status": "active", "survey": "all"},
                "restored": {"status": "ready", "survey": "needed"},
                "otherAccount": {"status": "active", "survey": "all"},
                "invalidStoredValue": {"status": "active", "survey": "all"},
            },
        )
        self.assertIn(
            "const assignHouseView = readAssignHouseView(user);",
            APP_JS,
        )

    def test_leaving_assign_seai_map_persists_list_view(self):
        start = APP_JS.index(
            '$("#house-map-list-view").addEventListener("click", () => {'
        )
        end = APP_JS.index(
            '$("#house-map-house-list").addEventListener("click",',
            start,
        )
        map_list_handler = APP_JS[start:end]
        self.assertIn('state.houseStatusFilter = "active";', map_list_handler)
        self.assertRegex(
            map_list_handler,
            r"saveAssignHouseView\(\s*state\.user,\s*state\.houseStatusFilter,\s*"
            r"state\.assignSurveyFilter,\s*\);",
        )

    def test_assign_home_icon_returns_to_and_persists_active_dashboard(self):
        start = APP_JS.index("function goToHomeView(event)")
        end = APP_JS.index("\nfunction nextPresentationHouseId", start)
        home_handler = APP_JS[start:end]
        self.assertIn(
            'if (hasAccessProfile(state.user, "assign")) {',
            home_handler,
        )
        self.assertIn('state.houseStatusFilter = "active";', home_handler)
        self.assertRegex(
            home_handler,
            r"saveAssignHouseView\(state\.user,\s*\"active\",\s*\"all\"\);",
        )
        self.assertIn('setTab("houses");', home_handler)

    def test_navigation_and_shortcut_targets_have_matching_pages(self):
        parser = NavigationMarkupParser()
        parser.feed(HTML)
        self.assertEqual(parser.nav_tabs, {"overview", "inventory", "houses", "movements"})
        self.assertEqual(parser.nav_tabs, parser.page_ids)
        self.assertTrue(parser.goto_targets)
        self.assertTrue(parser.goto_targets.issubset(parser.page_ids))

    def test_navigation_tour_covers_all_workspace_views(self):
        steps = extract_tour_steps(APP_JS)
        keys = re.findall(r'\bkey:\s*"([^"]+)"', steps)
        self.assertEqual(
            keys,
            ["overview", "inventory", "houses", "teams", "map", "movements"],
        )
        for element_id in (
            "start-navigation-tour",
            "navigation-tour",
            "navigation-tour-previous",
            "navigation-tour-next",
            "close-navigation-tour",
        ):
            self.assertIn(f'id="{element_id}"', HTML)
        self.assertIn(
            '$("#start-navigation-tour").addEventListener("click", startNavigationTour)',
            APP_JS,
        )

    def test_route_actions_and_primary_tabs_update_the_active_view(self):
        set_tab = extract_function(APP_JS, "setTab")
        open_step = extract_function(APP_JS, "openNavigationTourStep")
        steps = extract_tour_steps(APP_JS)
        setup = r"""
const state = {
  user: null,
  houseMapMode: false, housesTeamsTab: "houses", houseTeamFilter: "stale",
  houseStatusFilter: "stale", selectedMapHouseId: "old", houseMapCamera: {x: 1},
};
const hasAccessProfile = () => false;
const makeNode = (dataset = {}) => {
  const classes = new Set();
  const attributes = {};
  return {
    dataset, attributes, classes,
    classList: {
      toggle(name, force) {
        if (force === undefined ? !classes.has(name) : force) classes.add(name);
        else classes.delete(name);
      },
      add(name) { classes.add(name); },
      remove(name) { classes.delete(name); },
      contains(name) { return classes.has(name); },
    },
    setAttribute(name, value) { attributes[name] = value; },
    removeAttribute(name) { delete attributes[name]; },
    value: "stale",
  };
};
const tabs = ["overview", "inventory", "houses", "movements"].map(tab => makeNode({tab}));
const pages = ["overview", "inventory", "houses", "movements"].map(page => makeNode({page}));
const searchFields = new Map([
  ["#house-search", makeNode()],
  ["#team-search", makeNode()],
]);
const $ = selector => searchFields.get(selector);
const $$ = selector => selector === ".nav-item" ? tabs : selector === ".page" ? pages : [];
let movementRenders = 0;
let houseRenders = 0;
let overviewRenders = 0;
function renderMovementTable() { movementRenders += 1; }
function renderHouses() { houseRenders += 1; }
function showHousesOverview() {
  overviewRenders += 1;
  state.houseMapMode = false;
}
function refreshPendingChanges() { return Promise.resolve(); }
function showToast() {}
"""
        script = (
            setup + "\n" + steps + "\n" + set_tab + "\n" + open_step + r"""
const primaryTabs = ["overview", "inventory", "houses", "movements"];
const primary = primaryTabs.map(tab => {
  setTab(tab);
  return {
    tab,
    activeTab: tabs.find(item => item.dataset.tab === tab).classList.contains("active"),
    currentPage: pages.find(item => item.dataset.page === tab).classList.contains("active"),
    ariaCurrent: tabs.find(item => item.dataset.tab === tab).attributes["aria-current"] || null,
  };
});
const routeResults = {};
for (const key of ["overview", "inventory", "houses", "teams", "map", "movements"]) {
  openNavigationTourStep(key);
  routeResults[key] = {
    activeTab: tabs.find(item => item.classList.contains("active"))?.dataset.tab,
    activePage: pages.find(item => item.classList.contains("active"))?.dataset.page,
    houseMapMode: state.houseMapMode,
    housesTeamsTab: state.housesTeamsTab,
    filtersCleared: state.houseTeamFilter === null && state.houseStatusFilter === null,
    mapSelectionCleared: state.selectedMapHouseId === "" && state.houseMapCamera === null,
  };
}
console.log(JSON.stringify({primary, routeResults, movementRenders, houseRenders, overviewRenders}));
"""
        )
        result = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertTrue(all(item["activeTab"] and item["currentPage"] for item in output["primary"]))
        for item in output["primary"]:
            self.assertEqual(item["ariaCurrent"], "page")
        self.assertEqual(output["primary"][-1]["tab"], "movements")
        self.assertEqual(output["routeResults"]["overview"]["activePage"], "overview")
        self.assertEqual(output["routeResults"]["inventory"]["activePage"], "inventory")
        self.assertEqual(output["routeResults"]["houses"]["activePage"], "houses")
        self.assertEqual(output["routeResults"]["teams"]["housesTeamsTab"], "teams")
        self.assertFalse(output["routeResults"]["teams"]["houseMapMode"])
        self.assertEqual(output["routeResults"]["map"]["activePage"], "houses")
        self.assertTrue(output["routeResults"]["map"]["houseMapMode"])
        self.assertTrue(output["routeResults"]["map"]["mapSelectionCleared"])
        self.assertEqual(output["routeResults"]["movements"]["activePage"], "movements")
        self.assertTrue(
            all(
                output["routeResults"][key]["filtersCleared"]
                for key in ("houses", "teams", "map")
            )
        )
        self.assertGreater(output["movementRenders"], 0)
        self.assertGreater(output["houseRenders"], 0)
        self.assertGreater(output["overviewRenders"], 0)


if __name__ == "__main__":
    unittest.main()
