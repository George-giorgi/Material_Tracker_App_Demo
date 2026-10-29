const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const savedTheme = localStorage.getItem("material-tracker-theme");
if (savedTheme === "dark") document.documentElement.dataset.theme = "dark";

const state = {
  dashboard: null,
  movements: [],
  batchLines: [],
  selectedHouseId: "",
  viewingHouseId: "",
  houseDetailRequest: 0,
  houseChecklistRequest: 0,
  houseTeamFilter: null,
  houseStatusFilter: null,
  housesTeamsTab: "houses",
  houseMapMode: false,
  houseMapData: null,
  selectedMapHouseId: "",
  houseMapCamera: null,
  houseMapStageFilters: {
    not_started: true,
    ready: true,
    "active-1": true,
    "active-2": true,
    closed: true,
  },
  presentation: null,
  navigationTour: null,
  surveyRoomHouseId: "",
  surveyRoomReturn: "list",
  surveyRoomRequest: 0,
  weather: null,
  weatherError: "",
  weatherLoading: false,
  preparedHouseShare: null,
  seaiSharePages: new Map(),
  pdfAnnotator: null,
  user: null,
  setupRequired: false,
};
function setTheme(theme) {
  const isDark = theme === "dark";
  document.documentElement.dataset.theme = isDark ? "dark" : "light";
  localStorage.setItem("material-tracker-theme", isDark ? "dark" : "light");
  const button = $("#theme-toggle");
  button.setAttribute("aria-pressed", String(isDark));
  button.setAttribute("aria-label", `Switch to ${isDark ? "light" : "dark"} mode`);
  button.innerHTML = `<span aria-hidden="true">${isDark ? "☀" : "☾"}</span> ${isDark ? "Light" : "Dark"} mode`;
}

const movementLabels = {
  collection: "Picked up from warehouse for a team",
  pickup: "Picked up from warehouse",
  use: "Used on a house",
  delivery: "Delivered to a house",
  use: "Used on a house",
  return: "Returned to warehouse",
  transfer: "Moved to another site",
};
const presentationSteps = [
  {
    title: "A clear view of each job",
    narration:
      "We will follow a house from the pre-start survey through activation, material movements, completion, and its retained history.",
    instruction:
      "This isolated demo keeps its 50 provided-house records. House notes, snags, and movement history remain available from View details & history.",
    example: "Not started → ready for start → active → closed",
  },
  {
    title: "1. Review houses ready for start",
    narration:
      "The pre-start queue separates houses that still need preparation from houses ready for a survey. We will use an existing ready house so the 50-house pool stays intact.",
    instruction:
      "Open the Ready for start list. Choose one house, open Surveyor room or Review survey, and save a short sample survey. The guide will use that house for the next scenes.",
    example: "Keep the house ID visible; the remaining scenes follow that same job.",
    action: "ready-houses",
    actionLabel: "Show ready houses",
  },
  {
    title: "2. Activate the surveyed house",
    narration:
      "Once the survey is reviewed, the house can be activated. Its survey dimensions are then available to the automatic materials calculator.",
    instruction:
      "Return to Ready for start, click + Activate house on the selected job, review the details, and save. Then return to the guide.",
    example: "Activation moves one house from Ready for start to Active.",
    action: "ready-houses",
    actionLabel: "Return to the ready house",
  },
  {
    title: "3. Show the active stage",
    narration:
      "The active card shows its current work stage alongside materials, checklist, notes, and snags.",
    instruction:
      "Open Active houses and set the selected job to Stage 2. The stage buttons use blue for Stage 1 and green for Stage 2.",
    example: "Stage selection is saved on the house and can be changed as work progresses.",
    action: "active-houses",
    actionLabel: "Show active houses",
  },
  {
    title: "4. Record a warehouse pickup",
    narration:
      "A pickup records material taken from the warehouse and places it on the selected house, with a person and team responsible.",
    instruction:
      "Review the prefilled quantities and save the movement. Warehouse and house balances update together.",
    example: "Board 100mm · 12 units · Jordan Demo · Demo Retrofit Crew",
    action: "initial-pickup",
    actionLabel: "Open and prefill pickup",
  },
  {
    title: "5. Record an extra delivery",
    narration:
      "When the team needs more material later, mark the pickup as an extra delivery. The ledger numbers it after the earlier delivery.",
    instruction:
      "The extra-delivery box is preselected. Save the movement to see it numbered in the house history.",
    example: "Board 100mm · 4 units · Extra delivery",
    action: "extra-pickup",
    actionLabel: "Open and prefill extra delivery",
  },
  {
    title: "6. Return unused material",
    narration:
      "Returning unused material updates both the house balance and the warehouse balance, leaving a clear audit trail.",
    instruction:
      "Review the return details and save the movement.",
    example: "Return 2 boards · Recorded by Jordan Demo",
    action: "return",
    actionLabel: "Open and prefill return",
  },
  {
    title: "7. Transfer material to a team",
    narration:
      "When stock moves from a house to a crew, recording both locations keeps the quantity traceable.",
    instruction:
      "Review the prefilled transfer from the selected house to Demo Retrofit Crew, then save.",
    example: "Transfer 5 boards · House → Demo Retrofit Crew",
    action: "transfer",
    actionLabel: "Open and prefill transfer",
  },
  {
    title: "8. Review live stock balances",
    narration:
      "The inventory view reflects the pickup, extra delivery, return, and transfer.",
    instruction:
      "Review the Board 100mm warehouse and on-site balance.",
    example: "Each movement updates its source and destination.",
    action: "inventory",
    actionLabel: "Show updated inventory",
  },
  {
    title: "9. Mark the job finished",
    narration:
      "When work is complete, marking the house finished moves it from Active to Closed. Its history and remaining stock remain available.",
    instruction:
      "Open the selected active house and click Mark house finished. Resume the guide when the status changes.",
    example: "A closed house is no longer counted as active.",
    action: "finish-house",
    actionLabel: "Show the active house",
  },
  {
    title: "10. Find the house details and history",
    narration:
      "House details bring together notes, snags, and the movement ledger, including dates and responsibility details.",
    instruction:
      "Open View details & history on the selected house and review its notes, snag list, and recorded movements.",
    example: "House notes · Snag history · Material movement history",
    action: "active-houses",
    actionLabel: "Show the selected house",
  },
  {
    title: "Demo complete",
    narration:
      "This walkthrough covered the house workflow, survey, stages, material accountability, notes, snags, and retained history.",
    instruction:
      "The 50-house provided pool and its Not started count are preserved. Only the isolated demo database was used.",
    example: "For the matching spoken narration and recording order, open DEMO-PRESENTATION.md.",
  },
];
const navigationTourSteps = [
  {
    key: "overview",
    title: "Overview",
    instruction: "Check the headline stock and house counts, recent activity, and shortcuts into inventory, houses, and movements.",
    note: "Navigation check 1 of 6 · Overview",
  },
  {
    key: "inventory",
    title: "Inventory",
    instruction: "Inventory is open. Review material balances and locate the stocktake and movement actions.",
    note: "This tour only navigates; it does not change stock.",
  },
  {
    key: "houses",
    title: "Houses",
    instruction: "Houses & teams is open on the house list. Use search or status filters to find a house and open Surveyor room when it is ready.",
    note: "No house status or checklist is changed by this tour.",
  },
  {
    key: "teams",
    title: "Teams",
    instruction: "The Teams view is open. Review the crews and their house counts; select a count to inspect assigned houses.",
    note: "Returning to Houses restores the regular house list.",
  },
  {
    key: "map",
    title: "House map",
    instruction: "The From SEAI map view is open. Review the eligible-house list and select a marker or list entry to inspect it. Ready houses include the Surveyor room action.",
    note: "Demo pins are approximate; they are not exact Eircode locations.",
  },
  {
    key: "movements",
    title: "Movements",
    instruction: "The movement ledger is open. Try its search. Record movement opens a form, but nothing is saved unless you submit it.",
    note: "Navigation check 6 of 6 · Movements",
  },
];
let toastTimer;
let movementSearchRequest = 0;
let weatherRequestId = 0;

function escapeHtml(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (character) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[character],
  );
}

function formatQuantity(value, unit = "") {
  if (value === null || value === undefined) return "Needs count";
  const quantity = new Intl.NumberFormat(undefined, {
    maximumFractionDigits: 2,
  }).format(value);
  return unit ? `${quantity} ${unit}` : quantity;
}

function formatLocation(location) {
  if (!location) return "—";
  if (location === "warehouse") return "Warehouse";
  if (location === "consumed") return "Used";
  const [kind, ...parts] = location.split(":");
  const reference = parts.join(":");
  if (kind === "team" && reference.includes("|")) {
    const [department, team] = reference.split("|", 2).map(decodeURIComponent);
    return `Team ${team}${department ? ` · ${department}` : ""}`;
  }
  if (kind === "employee") return decodeURIComponent(reference);
  return `${kind === "house" ? "House" : "Team"} ${reference}`;
}

function groupedTeamLocations(locations, teamHouseCounts) {
  const groups = new Map();
  locations
    .filter((location) =>
      location.startsWith("team:") || location.startsWith("employee:"),
    )
    .forEach((location) => {
      const [kind, ...parts] = location.split(":");
      const reference = parts.join(":");
      const isEmployee = kind === "employee";
      let name;
      let department = "";
      if (isEmployee) {
        name = decodeURIComponent(reference);
      } else if (reference.includes("|")) {
        const [encodedDepartment, encodedName] = reference.split("|", 2);
        department = decodeURIComponent(encodedDepartment);
        name = decodeURIComponent(encodedName);
      } else {
        name = decodeURIComponent(reference);
      }
      const normalizedName = name.trim().toLocaleLowerCase();
      const key = department
        ? `department:${department.toLocaleLowerCase()}|${normalizedName}`
        : `name:${normalizedName}`;
      let group = groups.get(key);
      if (!group) {
        group = {
          locations: [],
          employeeName: "",
          teamName: "",
          department,
          openHouseIds: new Set(),
          closedHouseIds: new Set(),
        };
        groups.set(key, group);
      }
      group.locations.push(location);
      if (isEmployee) group.employeeName ||= name;
      else group.teamName ||= name;
      const counts = teamHouseCounts[location] || {};
      (counts.open_house_ids || []).forEach((id) => group.openHouseIds.add(id));
      (counts.closed_house_ids || []).forEach((id) => group.closedHouseIds.add(id));
    });
  return [...groups.values()]
    .map((group) => ({
      ...group,
      name: group.employeeName || `Team ${group.teamName}${group.department ? ` · ${group.department}` : ""}`,
      openHouseIds: [...group.openHouseIds],
      closedHouseIds: [...group.closedHouseIds],
    }))
    .sort((left, right) => left.name.localeCompare(right.name));
}

function formatAuditDateTime(value) {
  if (!value) return "Date/time not recorded";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : new Intl.DateTimeFormat("en-IE", {
        day: "2-digit",
        month: "short",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        hourCycle: "h23",
      }).format(parsed);
}

function formatAuditDate(value) {
  if (!value) return "Date not recorded";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : new Intl.DateTimeFormat("en-IE", {
        day: "2-digit",
        month: "short",
        year: "numeric",
      }).format(parsed);
}

function groupHouseDeliveries(activity) {
  const grouped = [];
  const deliveryByKey = new Map();
  activity.forEach((entry) => {
    if (entry.type !== "movement" ||
        !["delivery", "pickup"].includes(entry.kind)) {
      grouped.push(entry);
      return;
    }
    const key = entry.delivery_number
      ? `${entry.kind}:${entry.house_id || ""}:${entry.delivery_number}`
      : `${entry.kind}:${entry.house_id || ""}:${entry.person || ""}:${entry.team || ""}:${entry.happened_on || ""}:${entry.recorded_at || ""}`;
    let delivery = deliveryByKey.get(key);
    if (!delivery) {
      delivery = { ...entry, items: [] };
      deliveryByKey.set(key, delivery);
      grouped.push(delivery);
    }
    if (!delivery.house_employee && entry.house_employee) {
      delivery.house_employee = entry.house_employee;
    }
  });
  return grouped;
}

function houseAuditHtml(house, readyOnly = false) {
  const statusLabels = {
    not_started: "Not started",
    ready: "Ready for start",
    active: "Active",
    closed: "Closed",
  };
  const activity = [...(house.activity_history || [])];
  if (!activity.some((entry) => entry.type === "survey") &&
      (house.survey_updated_by || house.survey_updated_at)) {
    activity.push({
      type: "survey",
      changed_by: house.survey_updated_by,
      changed_at: house.survey_updated_at,
    });
  }
  if (!activity.length) {
    activity.push(...(house.status_history || []).filter((entry) =>
      Object.prototype.hasOwnProperty.call(statusLabels, entry.status),
    ));
    if (house.last_movement) {
      activity.push({ type: "movement", ...house.last_movement });
    }
  }
  const uniqueActivity = groupHouseDeliveries(activity);
  const latestOfType = (type, predicate = () => true) =>
    uniqueActivity
      .filter((entry) => (entry.type === type || (type === "status" && entry.status)) &&
        predicate(entry))
      .sort((left, right) => {
        const leftTime = left.changed_at || left.created_at || left.recorded_at || "";
        const rightTime = right.changed_at || right.created_at || right.recorded_at || "";
        return new Date(rightTime).getTime() - new Date(leftTime).getTime();
      })[0];
  const activation = latestOfType("status", (entry) => entry.status === "active");
  const ready = latestOfType("status", (entry) => entry.status === "ready");
  const closed = latestOfType("status", (entry) => entry.status === "closed");
  const survey = latestOfType("survey");
  const deliveries = uniqueActivity
    .filter((entry) => entry.type === "movement" &&
      ["delivery", "pickup"].includes(entry.kind))
    .sort((left, right) =>
      String(left.happened_on || left.recorded_at || "")
        .localeCompare(String(right.happened_on || right.recorded_at || "")),
    );
  const firstDelivery = deliveries[0];
  const lastDelivery = deliveries[deliveries.length - 1];
  const latestDelivery = deliveries[deliveries.length - 1];
  const auditField = (label, value, timestamp) =>
    `<div class="house-audit-summary-field"><strong>${escapeHtml(label)}</strong><span>${escapeHtml(value || "Not recorded")}</span>${timestamp ? `<small>${escapeHtml(formatAuditDateTime(timestamp))}</small>` : ""}</div>`;
  const hasAudit = uniqueActivity.some((entry) =>
    (entry.type === "status" &&
      (readyOnly
        ? entry.status === "ready"
        : ["ready", "active", "closed"].includes(entry.status))) ||
    entry.type === "survey" ||
    (!readyOnly && entry.type === "movement" &&
      ["delivery", "pickup"].includes(entry.kind)),
  );
  if (!hasAudit) return "";
  const highlights = `<div class="house-audit-summary">
    ${ready ? auditField("Marked ready to start", ready.changed_by, ready.changed_at) : ""}
    ${survey ? auditField("Surveyor", survey.changed_by, survey.changed_at) : ""}
    ${!readyOnly && activation ? auditField("Activated by", activation.changed_by, activation.changed_at) : ""}
    ${!readyOnly && closed ? auditField("Closed by", closed.changed_by, closed.changed_at) : ""}
    ${!readyOnly && latestDelivery ? auditField(
      "Delivery handoff",
      `${latestDelivery.person || "Name not recorded"} → ${latestDelivery.house_employee || "Assigned employee not recorded"}`,
      latestDelivery.recorded_at,
    ) : ""}
    ${!readyOnly && firstDelivery ? auditField("First delivery", formatAuditDate(firstDelivery.happened_on)) : ""}
    ${!readyOnly && lastDelivery ? auditField("Last delivery", formatAuditDate(lastDelivery.happened_on)) : ""}
  </div>`;
  return `<div class="house-audit${readyOnly ? " house-audit-ready" : ""}" aria-label="House activity">${highlights}</div>`;
}

function houseAuditCount(house) {
  const activity = [...(house.activity_history || [])];
  if (!activity.some((entry) => entry.type === "survey") &&
      (house.survey_updated_by || house.survey_updated_at)) {
    activity.push({ type: "survey" });
  }
  if (!activity.length) {
    activity.push(...(house.status_history || []));
    if (house.last_movement) {
      activity.push({ type: "movement", ...house.last_movement });
    }
  }
  return groupHouseDeliveries(activity).filter((entry) =>
    (entry.type === "status" &&
      ["not_started", "ready", "active", "closed"].includes(entry.status)) ||
      entry.type === "survey" ||
      (entry.type === "movement" && ["delivery", "pickup"].includes(entry.kind)),
  ).length;
}

function localDate() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
}

function showToast(message, isError = false) {
  const toast = $("#toast");
  const openDialog = isError ? $("dialog[open]") : null;
  if (openDialog) {
    openDialog.append(toast);
    toast.classList.add("in-dialog");
    toast.setAttribute("role", "alert");
    toast.setAttribute("aria-live", "assertive");
  } else {
    document.body.append(toast);
    toast.classList.remove("in-dialog");
    toast.setAttribute("role", "status");
    toast.setAttribute("aria-live", "polite");
  }
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 3800);
}

async function request(url, options = {}) {
  const response = await fetch(url, options);
  let result;
  try {
    result = await response.json();
  } catch {
    throw new Error(
      `The server returned an unreadable response (${response.status}).`,
    );
  }
  if (response.status === 401 && !url.startsWith("/api/auth/")) {
    state.user = null;
    showAuthScreen(false);
  }
  if (!response.ok)
    throw new Error(result.error || `Request failed (${response.status}).`);
  return result;
}

function showAuthScreen(setupRequired) {
  state.houseMapMode = false;
  state.houseMapData = null;
  state.selectedMapHouseId = "";
  state.houseMapCamera = null;
  state.setupRequired = setupRequired;
  $(".app-shell").classList.add("hidden");
  $("#auth-screen").classList.remove("hidden");
  $("#auth-error").classList.add("hidden");
  $("#auth-form").dataset.mode = setupRequired ? "setup" : "login";
  $("#auth-eyebrow").textContent = setupRequired
    ? "INITIAL ADMINISTRATOR SETUP"
    : "SECURE WORKSPACE";
  $("#auth-title").textContent = setupRequired
    ? "Set up your administrator account"
    : "Sign in";
  $("#auth-description").textContent = setupRequired
    ? "Create the first administrator account. Only this account can create employee logins."
    : "Sign in with your tracker account to continue.";
  $("#auth-display-name-field").classList.toggle("hidden", !setupRequired);
  $("#auth-pin-confirm-field").classList.toggle("hidden", !setupRequired);
  $("#auth-display-name").required = setupRequired;
  $("#auth-pin-confirm").required = setupRequired;
  $("#auth-pin-confirm").minLength = setupRequired ? 6 : 0;
  $("#auth-pin-confirm").maxLength = setupRequired ? 6 : 1024;
  if (setupRequired) {
    $("#auth-pin-confirm").setAttribute("pattern", "[0-9]{6}");
  } else {
    $("#auth-pin-confirm").removeAttribute("pattern");
  }
  $("#auth-pin").minLength = setupRequired ? 6 : 0;
  $("#auth-pin").maxLength = setupRequired ? 6 : 1024;
  if (setupRequired) {
    $("#auth-pin").setAttribute("pattern", "[0-9]{6}");
  } else {
    $("#auth-pin").removeAttribute("pattern");
  }
  $("#auth-pin").inputMode = "numeric";
  $("#auth-pin").autocomplete = setupRequired
    ? "new-password"
    : "current-password";
  $("#auth-submit").textContent = setupRequired
    ? "Create administrator account"
    : "Sign in";
  $("#auth-screen").classList.remove("hidden");
  $("#auth-username").focus();
}

function showWorkspace(user) {
  state.user = user;
  $("#auth-screen").classList.add("hidden");
  $(".app-shell").classList.remove("hidden");
  $("#account-name").textContent = user.display_name;
  const accountButton = $("#account-button");
  accountButton.textContent = user.display_name;
  accountButton.title = `Signed in as ${user.display_name} (${user.username})`;
  accountButton.setAttribute(
    "aria-label",
    `Account for ${user.display_name}, username ${user.username}`,
  );
  const roleNames = {
    admin: "Administrator",
    assign: "Assign",
    boss: "Boss",
    delivery: "Delivery",
    employee: "Employee",
    supervisor: "Supervisor",
    surveyor: "Surveyor",
    teamlead: "Teamlead",
    warehouse: "Warehouse",
  };
  $("#account-details").textContent = `${user.username} - ${roleNames[user.role] || "Employee"}`;
  const isSurveyor = user.role === "surveyor";
  const isAssign = user.role === "assign";
  $("#house-map-activity").classList.toggle("hidden", isAssign);
  document.body.classList.toggle("surveyor-mode", isSurveyor);
  $$(".nav-item[data-tab]").forEach((item) =>
    item.classList.toggle(
      "hidden",
      (isSurveyor || isAssign) && item.dataset.tab !== "houses",
    ),
  );
  $("#new-movement-top").classList.toggle("hidden", isSurveyor || isAssign);
  $("#start-presentation").classList.toggle("hidden", isSurveyor || user.role !== "admin");
  $("#start-navigation-tour").classList.toggle(
    "hidden",
    isSurveyor || isAssign,
  );
  $("#houses-teams-tabs").classList.toggle("hidden", isSurveyor);
  $("#teams-section").classList.toggle("hidden", isSurveyor);
  $("#open-house").classList.toggle("hidden", isSurveyor);
  if (isSurveyor) {
    $(".main-nav .nav-item[data-tab='houses']").click();
    $("#houses-list-heading").textContent = "Houses ready for survey";
    $(".page-subtitle", $("#page-houses")).textContent = "Review pre-start houses and record measurements for the calculator.";
    loadSurveyorHouses();
  }
  if (isAssign) {
    state.houseMapMode = false;
    state.houseStatusFilter = null;
    state.viewingHouseId = "";
    state.houseMapData = null;
    $("#houses-page-title").textContent = "House dashboard";
    $("#houses-page-subtitle").textContent =
      "Review house status and open the jobs that need your attention.";
    $("#houses-empty p").textContent = "Houses will appear here as jobs are assigned.";
    $(".main-nav .nav-item[data-tab='houses']").click();
  } else {
    $("#houses-page-title").textContent = "Houses & teams";
    $("#houses-page-subtitle").textContent =
      "Search houses by ID, owner, employee or postcode, and see materials on site.";
    $("#houses-empty p").textContent =
      "Houses will appear here when materials are delivered or returned.";
  }
  $("#account-manage-users").classList.toggle("hidden", user.role !== "admin");
  $("#house-map-import-card").classList.toggle("hidden", user.role !== "admin");
  $("#open-stocktake").classList.toggle("hidden", user.role !== "admin");
  $("#open-material").classList.toggle("hidden", user.role !== "admin");
  $("#open-house").classList.toggle("hidden", user.role !== "admin");
  $("#open-import-empty").classList.toggle("hidden", user.role !== "admin");
  $("#start-presentation").classList.toggle("hidden", user.role !== "admin");
  $("#go-overview").classList.add("hidden");
  updateMovementFields();
  if (user.role !== "admin") closePresentation();
  if (isSurveyor) closeNavigationTour();
}

async function checkAuthentication() {
  const result = await request("/api/auth/status");
  if (result.user) {
    showWorkspace(result.user);
    if (result.user.role !== "surveyor") await refresh();
  } else {
    showAuthScreen(result.setup_required);
  }
}

function setTab(tab) {
  if (state.user?.role === "assign" && tab !== "houses") tab = "houses";
  if (tab === "overview" && state.housesTeamsTab !== "houses") {
    state.housesTeamsTab = "houses";
    renderHouses();
  }
  $("#go-overview").classList.toggle("hidden", tab === "overview");
  if (state.user?.role === "assign") {
    $("#go-overview").classList.add("hidden");
  }
  $$(".nav-item").forEach((button) => {
    const active = button.dataset.tab === tab;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  $$(".page").forEach((page) =>
    page.classList.toggle("active", page.dataset.page === tab),
  );
  if (tab === "movements") renderMovementTable();
}

function goToHomeView(event) {
  if (event) event.preventDefault();
  if (!state.user) return;
  if (state.user?.role === "surveyor") {
    setTab("houses");
    loadSurveyorHouses();
    return;
  }
  setTab("overview");
  showHousesOverview();
}

function nextPresentationHouseId() {
  let number = 301;
  while (
    state.dashboard.house_list.some(
      (house) => house.id === `DEMO-SHOW-${number}`,
    )
  ) {
    number += 1;
  }
  return `DEMO-SHOW-${number}`;
}

function renderPresentationGuide() {
  if (!state.presentation) return;
  const { index, houseId } = state.presentation;
  const step = presentationSteps[index];
  $("#presentation-progress").textContent =
    `PRESENTATION · ${index + 1} / ${presentationSteps.length}`;
  $("#presentation-title").textContent = step.title;
  $("#presentation-narration").textContent = `Say: “${step.narration}”`;
  $("#presentation-instruction").textContent = step.instruction.replaceAll(
    "DEMO-SHOW-301",
    houseId,
  );
  $("#presentation-example").textContent = step.example.replaceAll(
    "DEMO-SHOW-301",
    houseId,
  );
  $("#presentation-previous").disabled = index === 0;
  $("#presentation-next").textContent =
    index === presentationSteps.length - 1 ? "Finish" : "Next";
  const actionButton = $("#presentation-do-action");
  actionButton.classList.toggle("hidden", !step.action);
  actionButton.textContent = step.actionLabel || "";
}

function startPresentation() {
  closeNavigationTour();
  state.presentation = { index: 0, houseId: nextPresentationHouseId() };
  $("#presentation-guide").classList.remove("hidden");
  $("#resume-presentation").classList.add("hidden");
  setTab("overview");
  renderPresentationGuide();
}

function closePresentation() {
  state.presentation = null;
  $("#presentation-guide").classList.add("hidden");
  $("#resume-presentation").classList.add("hidden");
}

function renderNavigationTour() {
  if (!state.navigationTour) return;
  const { index } = state.navigationTour;
  const step = navigationTourSteps[index];
  $("#navigation-tour-progress").textContent =
    `NAVIGATION TEST FLOW · ${index + 1} / ${navigationTourSteps.length}`;
  $("#navigation-tour-title").textContent = step.title;
  $("#navigation-tour-instruction").textContent = step.instruction;
  $("#navigation-tour-note").textContent = step.note;
  $("#navigation-tour-previous").disabled = index === 0;
  $("#navigation-tour-next").textContent =
    index === navigationTourSteps.length - 1 ? "Finish" : "Next";
}

function openNavigationTourStep(stepKey) {
  const step = navigationTourSteps.find((item) => item.key === stepKey);
  if (!step) throw new Error(`Unknown navigation tour step: ${stepKey}`);
  if (stepKey === "overview") {
    state.houseMapMode = false;
    setTab("overview");
    showHousesOverview();
    return;
  }
  if (stepKey === "inventory") {
    setTab("inventory");
    return;
  }
  if (stepKey === "movements") {
    setTab("movements");
    return;
  }
  state.houseMapMode = stepKey === "map";
  state.housesTeamsTab = stepKey === "teams" ? "teams" : "houses";
  state.houseTeamFilter = null;
  state.houseStatusFilter = null;
  state.selectedMapHouseId = "";
  state.houseMapCamera = null;
  $("#house-search").value = "";
  $("#team-search").value = "";
  if (stepKey === "map") {
    setTab("houses");
    renderHouses();
    return;
  }
  showHousesOverview();
  setTab("houses");
  renderHouses();
}

function startNavigationTour() {
  if (!state.user || state.user.role === "surveyor") return;
  closePresentation();
  state.navigationTour = { index: 0 };
  $("#navigation-tour").classList.remove("hidden");
  openNavigationTourStep(navigationTourSteps[0].key);
  renderNavigationTour();
}

function closeNavigationTour() {
  state.navigationTour = null;
  $("#navigation-tour").classList.add("hidden");
}

function moveNavigationTour(stepDelta) {
  if (!state.navigationTour) return;
  const nextIndex = state.navigationTour.index + stepDelta;
  if (nextIndex >= navigationTourSteps.length) {
    closeNavigationTour();
    return;
  }
  state.navigationTour.index = Math.max(0, nextIndex);
  const step = navigationTourSteps[state.navigationTour.index];
  openNavigationTourStep(step.key);
  renderNavigationTour();
}

function minimizePresentation() {
  $("#presentation-guide").classList.add("hidden");
  $("#resume-presentation").classList.remove("hidden");
}

function resumePresentation() {
  if (!state.presentation) return;
  $("#resume-presentation").classList.add("hidden");
  $("#presentation-guide").classList.remove("hidden");
}

function movePresentation(stepDelta) {
  if (!state.presentation) return;
  const nextIndex = state.presentation.index + stepDelta;
  if (nextIndex >= presentationSteps.length) {
    closePresentation();
    return;
  }
  state.presentation.index = Math.max(0, nextIndex);
  renderPresentationGuide();
}

function openPresentationAction() {
  if (!state.presentation) return;
  const { houseId } = state.presentation;
  const action = presentationSteps[state.presentation.index].action;
  if (["ready-houses", "active-houses", "closed-houses"].includes(action)) {
    const status = action === "ready-houses"
      ? "ready"
      : action === "active-houses"
        ? "active"
        : "closed";
    setTab("houses");
    state.housesTeamsTab = "houses";
    state.houseTeamFilter = null;
    state.houseStatusFilter = status;
    $("#team-search").value = "";
    $("#house-search").value = houseId.startsWith("DEMO-SHOW-") ? "" : houseId;
    renderHouses();
    minimizePresentation();
    return;
  }
  if (action === "house") {
    setTab("houses");
    openHouseDialog();
    const values = {
      id: houseId,
      postcode: "D01 DEMO",
      owner_name: "Riley Example",
      employee_name: "Jordan Demo",
    };
    Object.entries(values).forEach(([name, value]) => {
      $(`#house-form [name="${name}"]`).value = value;
    });
    return;
  }
  if (
    action === "initial-pickup" ||
    action === "extra-pickup" ||
    action === "return" ||
    action === "transfer"
  ) {
    const kind =
      action === "return"
        ? "return"
        : action === "transfer"
          ? "transfer"
          : "pickup";
    const board = state.dashboard.materials.find(
      (material) => material.name === "Board 100mm",
    );
    if (!board) {
      showToast("The demo catalog is missing Board 100mm.", true);
      return;
    }
    const houseDeliveryCount = state.movements.filter(
      (movement) => movement.house_id === houseId && movement.kind === "pickup",
    ).length;
    const isExtra = action === "extra-pickup" || houseDeliveryCount > 0;
    openMovementDialog();
    $("#movement-kind").value = kind;
    $("#movement-kind").dispatchEvent(new Event("change"));
    $("#movement-date").value = localDate();
    $("#movement-form [name='notes']").value = "Fictional presentation demo.";
    const activeFields = $(`.movement-fields[data-for="${kind}"]`);
    $("input[name='person']", activeFields).value = "Jordan Demo";
    $("input[name='team']", activeFields).value = "Demo Retrofit Crew";
    if (kind === "transfer") {
      const blocks = $$(".transfer-block", activeFields);
      if (blocks.length !== 2) {
        showToast("The transfer form is missing a source or destination field.", true);
        $("#movement-dialog").close();
        return;
      }
      const kinds = $$(".location-kind", activeFields);
      kinds[0].value = "house";
      kinds[1].value = "team";
      kinds.forEach((select) => select.dispatchEvent(new Event("change")));
      $("select[name='source_site']", activeFields).value = houseId;
      $("input[name='destination_reference']", activeFields).value = "Demo Retrofit Crew";
      $("input[name='destination_department']", activeFields).value = "Retrofit";
      $("#movement-form [name='notes']").value =
        "Fictional presentation transfer to Demo Retrofit Crew.";
    } else {
      $("#movement-house-id").value = houseId;
      updateHouseSelection();
      $("#extra-delivery").checked = kind === "pickup" && isExtra;
    }
    const quantity =
      action === "initial-pickup"
        ? "12"
        : action === "extra-pickup"
          ? "4"
          : action === "return"
            ? "2"
            : "5";
    state.batchLines = [{ material_id: board.id, qty: quantity }];
    renderMaterialLines();
    return;
  }
  if (action === "inventory") {
    setTab("inventory");
    $("#material-search").value = "Board 100mm";
    renderInventory();
    return;
  }
  if (action === "finish-house") {
    setTab("houses");
    state.housesTeamsTab = "houses";
    renderHouses();
    $(`.house-finish-toggle[data-house-id="${houseId}"]`)?.scrollIntoView({
      behavior: "smooth",
      block: "center",
    });
    minimizePresentation();
    return;
  }
  if (action === "houses") {
    setTab("houses");
    state.housesTeamsTab = "houses";
    $("#house-search").value = houseId;
    $("#team-search").value = "";
    state.houseTeamFilter = null;
    state.houseStatusFilter = null;
    renderHouses();
    return;
  }
  if (action === "history") {
    setTab("movements");
    $("#movement-search").value = houseId;
    searchMovementHistory();
  }
}

function movementIcon(kind) {
  if (kind === "return") return "↶";
  if (kind === "use") return "✓";
  if (kind === "transfer") return "⇄";
  if (kind === "collection") return "↓";
  return "↗";
}

function movementDisplayLabel(item) {
  if (isHouseToHouseTransfer(item)) {
    return "House-to-house transfer";
  }
  if (item.kind === "delivery") {
    return `Delivery #${item.delivery_number || 1}`;
  }
  if (item.kind === "pickup" && item.delivery_number) {
    return item.delivery_number > 1
      ? `Extra delivery #${item.delivery_number}`
      : "Initial warehouse pickup #1";
  }
  return movementLabels[item.kind] || item.kind;
}

function isHouseToHouseTransfer(item) {
  return (
    item.kind === "transfer" &&
    item.source?.startsWith("house:") &&
    item.destination?.startsWith("house:")
  );
}

function isExtraDelivery(item) {
  return item.kind === "pickup" && item.delivery_number > 1;
}

function movementStatusClass(item) {
  if (isHouseToHouseTransfer(item)) return "house-transfer";
  if (isExtraDelivery(item)) return "extra-delivery";
  if (item.kind === "return") return "warning";
  return "";
}

function weatherConditionIcon(condition) {
  const value = String(condition || "").toLowerCase();
  if (value.includes("thunder")) return "⛈️";
  if (value.includes("snow") || value.includes("sleet")) return "🌨️";
  if (value.includes("rain") || value.includes("shower")) return "🌧️";
  if (value.includes("fog") || value.includes("mist")) return "🌫️";
  if (value.includes("cloud")) return value.includes("scattered") ? "⛅" : "☁️";
  if (value.includes("sun") || value.includes("clear")) return "🌤️";
  return "🌦️";
}

function renderWeather() {
  const status = $("#weather-status");
  const refreshButton = $("#weather-refresh");
  const locations = $("#weather-areas");
  refreshButton.disabled = state.weatherLoading;
  refreshButton.setAttribute("aria-busy", String(state.weatherLoading));
  if (state.weatherError) {
    status.textContent = `Forecast unavailable: ${state.weatherError}`;
    status.classList.add("error");
  } else if (state.weatherLoading) {
    status.textContent = state.weather ? "Refreshing forecast…" : "Loading county forecast…";
    status.classList.remove("error");
  } else if (!state.weather) {
    status.textContent = "Forecast is not available yet.";
    status.classList.remove("error");
  } else {
    const issued = state.weather.issued
      ? new Date(state.weather.issued).toLocaleString(undefined, {
          weekday: "short",
          day: "numeric",
          month: "short",
          hour: "2-digit",
          minute: "2-digit",
        })
      : "recently";
    const dayCount = state.weather.areas[0]?.days.length || 0;
    status.textContent = `${dayCount}-day forecast · Issued ${issued}`;
    status.classList.remove("error");
  }

  if (!state.weather) {
    locations.replaceChildren();
    return;
  }
  locations.innerHTML = state.weather.areas
    .map(
      (area) => `
        <article class="weather-area">
          <h3>County ${escapeHtml(area.county)}</h3>
          <div class="weather-days" role="region" tabindex="0" aria-label="${escapeHtml(area.county)} county forecast">
            ${area.days
              .map((day) => {
                const date = new Date(`${day.date}T12:00:00`);
                const dateLabel = Number.isNaN(date.getTime())
                  ? day.date
                  : date.toLocaleDateString(undefined, {
                      weekday: "short",
                      day: "numeric",
                      month: "short",
                    });
                const temperature = (value) =>
                  value === null || value === undefined || !Number.isFinite(Number(value))
                    ? "—"
                    : `${Math.round(Number(value))}°`;
                const rain =
                  day.rainfall_mm === null || day.rainfall_mm === undefined
                    ? "Rain —"
                    : `Rain ${new Intl.NumberFormat(undefined, {
                        maximumFractionDigits: 1,
                      }).format(day.rainfall_mm)} mm`;
                const wind =
                  day.wind_speed_kmh === null || day.wind_speed_kmh === undefined
                    ? "Wind —"
                    : `Wind ${escapeHtml(day.wind_direction || "")} ${Math.round(Number(day.wind_speed_kmh))} km/h`;
                const condition = String(day.weather || "Forecast")
                  .replaceAll("_", " ")
                  .replace(/\b\w/g, (letter) => letter.toUpperCase());
                return `
                  <div class="weather-day">
                    <span class="weather-day-date">${escapeHtml(dateLabel)}</span>
                    <span class="weather-day-condition">${weatherConditionIcon(day.weather)} ${escapeHtml(condition)}</span>
                    <span class="weather-day-temps">High ${temperature(day.max_temp)} · Low ${temperature(day.min_temp)}</span>
                    <span class="weather-day-details">${rain}</span>
                    <span class="weather-day-details">${wind}</span>
                  </div>`;
              })
              .join("")}
          </div>
        </article>`,
    )
    .join("");
}

async function loadCountyWeather() {
  const requestId = ++weatherRequestId;
  state.weatherLoading = true;
  state.weatherError = "";
  renderWeather();
  try {
    state.weather = await request("/api/weather");
  } catch (error) {
    if (requestId !== weatherRequestId) return;
    state.weatherError = error.message;
  } finally {
    if (requestId === weatherRequestId) {
      state.weatherLoading = false;
      renderWeather();
    }
  }
}

function renderOverview() {
  const data = state.dashboard;
  const materials = data.materials;
  renderWeather();
  $("#stat-materials").textContent = materials.length;
  const hs = data.house_summary || {
    provided: 0, active: data.houses, closed: 0, not_started: 0,
  };
  $("#stat-provided").textContent = hs.provided;
  $("#stat-houses").textContent = hs.active;
  $("#stat-closed").textContent = hs.closed;
  $("#stat-not-started").textContent = hs.not_started;
  $("#stat-ready").textContent = hs.ready || 0;
  $("#stat-movements").textContent = data.movements.length;
  $("#stat-counts").textContent = materials.filter(
    (item) => item.needs_stocktake,
  ).length;
  $("#today-label").textContent = new Date().toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    year: "numeric",
  });

  $("#overview-stock").innerHTML = materials
    .slice(0, 8)
    .map(
      (item) => `
    <tr>
      <td><span class="material-name"><span class="material-dot"></span>${escapeHtml(item.name)}</span></td>
      <td class="code-cell">${escapeHtml(item.code || "—")}</td>
      <td class="${item.warehouse_qty === null ? "qty-unknown" : "qty-cell"}">${escapeHtml(formatQuantity(item.warehouse_qty, item.unit))}</td>
      <td><span class="status-pill ${item.needs_stocktake ? "warning" : ""}">${item.negative_locations?.length ? "Reconcile" : item.needs_stocktake ? "Count needed" : "Tracked"}</span></td>
    </tr>`,
    )
    .join("");
  $("#overview-empty").classList.toggle("hidden", materials.length > 0);
  $(".stock-card .table-wrap").classList.toggle(
    "hidden",
    materials.length === 0,
  );

  $("#overview-activity").innerHTML = data.movements
    .slice(0, 6)
    .map(
      (item) => `
    <div class="activity-item">
      <span class="activity-icon ${escapeHtml(item.kind)}">${movementIcon(item.kind)}</span>
      <div class="activity-copy"><strong>${escapeHtml(item.material)}</strong>${isExtraDelivery(item) ? `<span class="status-pill extra-delivery">Extra delivery #${item.delivery_number}</span>` : `<span class="${isHouseToHouseTransfer(item) ? "status-pill house-transfer" : ""}">${escapeHtml(movementDisplayLabel(item))}</span>`}<span>${escapeHtml(item.date)} · Recorded ${escapeHtml(formatAuditDateTime(item.recorded_at))}${item.department ? ` · ${escapeHtml(item.department)}` : ""}${item.team ? ` · ${escapeHtml(item.team)}` : ""}</span></div>
      <span class="activity-qty">${escapeHtml(formatQuantity(item.qty))}</span>
    </div>`,
    )
    .join("");
  if (!data.movements.length) {
    $("#overview-activity").innerHTML =
      `<div class="empty-state"><div class="empty-symbol">↔</div><strong>No activity yet</strong><p>Your movement history will appear here.</p></div>`;
  }
}

function locationTotal(item, type) {
  return Object.entries(item.balances)
    .filter(([key]) => key.startsWith(`${type}:`))
    .reduce((total, [, value]) => total + (value || 0), 0);
}

function renderInventory() {
  const query = $("#material-search").value.trim().toLocaleLowerCase();
  const canAdministerInventory = state.user?.role === "admin";
  const materials = state.dashboard.materials.filter((item) =>
    `${item.name} ${item.code || ""}`.toLocaleLowerCase().includes(query),
  );
  $("#inventory-table").innerHTML = materials
    .map(
      (item) => `
    <tr>
      <td data-label="Material"><span class="material-name"><span class="material-dot"></span>${escapeHtml(item.name)}</span></td>
      <td class="code-cell" data-label="Code"><span class="inventory-cell-label" aria-hidden="true">Code</span>${escapeHtml(item.code || "—")}</td>
      <td class="${item.warehouse_qty === null ? "qty-unknown" : "qty-cell"}" data-label="Warehouse"><span class="inventory-cell-label" aria-hidden="true">Warehouse</span>${escapeHtml(formatQuantity(item.warehouse_qty, item.unit))}</td>
      <td class="${locationTotal(item, "team") < 0 ? "qty-negative" : ""}" data-label="With teams"><span class="inventory-cell-label" aria-hidden="true">With teams</span>${escapeHtml(formatQuantity(locationTotal(item, "team"), item.unit))}</td>
      <td class="${locationTotal(item, "house") < 0 ? "qty-negative" : ""}" data-label="On houses"><span class="inventory-cell-label" aria-hidden="true">On houses</span>${escapeHtml(formatQuantity(locationTotal(item, "house"), item.unit))}</td>
      <td data-label="Unit"><span class="inventory-cell-label" aria-hidden="true">Unit</span>${escapeHtml(item.unit)}</td>
      <td data-label="Status"><span class="inventory-cell-label" aria-hidden="true">Status</span><span class="status-pill ${item.needs_stocktake ? "warning" : ""}">${item.negative_locations?.length ? "Reconcile" : item.needs_stocktake ? "Count needed" : "Tracked"}</span></td>
      <td data-label="Actions">${canAdministerInventory ? `<button type="button" class="text-button stock-edit" data-material-id="${item.id}">Edit warehouse stock</button>` : ""}</td>
    </tr>`,
    )
    .join("");
  const empty = state.dashboard.materials.length === 0;
  $("#inventory-empty").classList.toggle("hidden", !empty);
  $(".import-card").classList.toggle(
    "hidden",
    !empty || !canAdministerInventory,
  );
  $(".card:not(.import-card) .table-wrap").classList.toggle(
    "hidden",
    empty || materials.length === 0,
  );
  if (!empty && materials.length === 0) {
    $("#inventory-table").innerHTML =
      `<tr><td colspan="8">No materials match that search.</td></tr>`;
    $(".card:not(.import-card) .table-wrap").classList.remove("hidden");
  }
}

const STOCK_PREVIEW = 3;

const expandedStock = new Set();

let planCache = {};
const planStale = new Set();
const workPlanSaveStates = new WeakMap();
function checklistRequirementBadge(required, status) {
  if (required === 1 || required === true) {
    return '<span class="checklist-requirement required">Required</span>';
  }
  if (required === 0 || required === false || status === "not_necessary" || status === "not_esb") {
    return '<span class="checklist-requirement not-required">Not required</span>';
  }
  return '<span class="checklist-requirement requirement-unknown">Requirement not set</span>';
}

function activeChecklistToggle(houseId, field, label, status, required) {
  const requirement = checklistRequirementBadge(required, status);
  if (required === 0 || required === false || status === "not_necessary" || status === "not_esb") {
    return requirement;
  }
  const done = status === "done" || status === "yes";
  const notDoneStatus = field === "esb_unclipped" ? "no" : "not_done";
  return `<div class="checklist-toggle-wrap">${requirement}<label class="checklist-toggle${done ? " is-done" : ""}"><input type="checkbox" class="house-checklist-status" data-house-id="${escapeHtml(houseId)}" data-checklist-field="${field}" data-saved-value="${status || notDoneStatus}" aria-label="Mark ${escapeHtml(label)} done"${done ? " checked" : ""}><span class="checklist-toggle-indicator" aria-hidden="true">${done ? "✓" : ""}</span><span class="checklist-toggle-label">${done ? "Done" : "Not done yet"}</span></label></div>`;
}
function snagRowHtml(snag, houseId) {
  return `<button type="button" class="snag-row${snag.done ? " done" : ""}" data-snag-id="${snag.id}" data-house-id="${escapeHtml(houseId)}" data-done="${snag.done ? "false" : "true"}" aria-pressed="${snag.done}" title="${snag.done ? "Mark as not done" : "Mark as done"}"><span class="snag-box">${snag.done ? "✓" : ""}</span><span class="snag-text">${escapeHtml(snag.text)}</span></button>`;
}
function snagsHtml(house) {
  const snags = house.snags || [];
  return snags.length
    ? `<div class="snag-block"><div class="house-stock-title">Snags (${snags.filter((s) => !s.done).length} open)</div>${snags.map((s) => snagRowHtml(s, house.id)).join("")}</div>`
    : "";
}
function findAnyHouse(houseId) {
  return state.dashboard.house_list.find((item) => item.id === houseId) || findPlanHouse(houseId);
}
function renderSnagDialog() {
  const house = findAnyHouse($("#house-snag-form").dataset.houseId);
  const snags = house?.snags || [];
  $("#house-snag-list").innerHTML = snags.length
    ? snags
        .map((s) => `<div class="snag-item">${snagRowHtml(s, house.id)}<button type="button" class="snag-del" data-snag-id="${s.id}" data-house-id="${escapeHtml(house.id)}" aria-label="Delete snag" title="Delete snag">×</button></div>`)
        .join("")
    : `<div class="snag-empty">No snags yet.</div>`;
}
function openHouseSnags(houseId) {
  $("#house-snag-form").dataset.houseId = houseId;
  $("#house-snag-title").textContent = `${houseId} snags`;
  renderSnagDialog();
  $("#house-snag-form").reset();
  if (!$("#house-snag-dialog").open) $("#house-snag-dialog").showModal();
  $("#house-snag-form").elements.text.focus();
}
async function saveSnag(payload) {
  try {
    const result = await request("/api/houses/snags", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const house = findAnyHouse(result.id);
    if (house) house.snags = result.snags;
    renderHouses();
    if ($("#house-snag-dialog").open) renderSnagDialog();
    return true;
  } catch (error) {
    showToast(error.message, true);
    return false;
  }
}
function noteHtml(text) {
  return text ? `<div class="house-note"><div class="house-stock-title">Notes</div><p>${escapeHtml(text)}</p></div>` : "";
}
function houseNoteActionClass(notes) {
  return String(notes || "").trim() ? "note-action-edit" : "note-action-add";
}

function houseMapButtonHtml(houseId) {
  if (state.user?.role === "assign") return "";
  return `<button class="text-button house-map-open" type="button" data-house-id="${escapeHtml(houseId)}"><svg class="house-map-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="M20 10c0 5-8 12-8 12S4 15 4 10a8 8 0 1 1 16 0Z"></path><circle cx="12" cy="10" r="2.5"></circle></svg><span>View on map</span></button>`;
}

function findPlanHouse(houseId) {
  return Object.values(planCache).flat().find((item) => item.id === houseId) ||
    state.houseMapData?.find((item) => item.id === houseId);
}
let surveyHouses = [];
function surveyCountText(survey) {
  return survey ? "Survey recorded" : "Not surveyed";
}
function updateSurveyStatusDisplay(survey) {
  const isRecorded = Boolean(survey);
  const summary = $("#survey-room-summary .survey-room-summary-survey strong");
  if (summary) {
    summary.textContent = surveyCountText(survey);
    summary.className = isRecorded ? "survey-recorded" : "survey-not-surveyed";
  }
  const status = $("#survey-room-status");
  if (status) {
    status.classList.toggle("survey-complete", isRecorded);
    status.classList.toggle("survey-not-surveyed", !isRecorded);
    status.classList.remove("ready");
    status.textContent = surveyCountText(survey);
  }
}
function surveyStatusBadge(survey) {
  return survey ? '<span class="status-pill survey-complete">Survey recorded</span>' : "";
}
const SEAI_INSULATION_GROUPS = [
  ["External Wall Insulation", /external wall insulation/i],
  ["Internal Wall Insulation", /internal wall insulation/i],
  ["Attic (Loft) Insulation", /attic\s*\(loft\)\s*insulation/i],
  ["Cavity Wall Insulation", /cavity wall insulation/i],
];
function seaiInsulationSummaryHtml(details, houseId = "") {
  const measures = Array.isArray(details?.Measures) ? details.Measures : [];
  const demoQuantities = [94, 42, 78, 64];
  const demoMeasures = SEAI_INSULATION_GROUPS.map(([label], index) => {
    const seed = houseId
      ? houseId.charCodeAt(index % houseId.length)
      : index * 7;
    const surveyQuantity = demoQuantities[index] + (seed % 15) - 7;
    return {
      Name: label,
      "Survey Quantity": surveyQuantity,
      "Survey Qty including Variation":
        surveyQuantity + Math.max(3, Math.round(surveyQuantity * 0.08)),
      is_demo: true,
    };
  });
  if (!measures.length && !houseId) return "";
  const hasDemoMeasures = SEAI_INSULATION_GROUPS.some(([, matcher]) =>
    !measures.some((measure) => matcher.test(String(measure.Name || ""))),
  );
  return `<section class="house-insulation-summary" aria-label="SEAI insulation measures">
    <h3>${measures.length ? "SEAI insulation measures" : "Sample insulation details"}</h3>
    ${hasDemoMeasures ? '<p class="house-insulation-demo-note">DEMO DATA - sample quantities only; replace with verified SEAI information.</p>' : ""}
    <div class="house-insulation-table" role="table" aria-label="Recorded survey quantities">
      <div class="house-insulation-header" role="row"><span role="columnheader">Measure</span><span role="columnheader">Survey qty</span><span role="columnheader">Including variation</span></div>
      ${SEAI_INSULATION_GROUPS.map(([label, matcher], index) => {
        const matching = measures.filter((measure) => matcher.test(String(measure.Name || "")));
        const displayedMeasures = matching.length ? matching : [demoMeasures[index]];
        const value = (measure, key) => String(measure[key] ?? "").trim() || "—";
        const quantity = (measure, key) => {
          const raw = value(measure, key);
          if (raw === "—") return raw;
          const numeric = Number(raw);
          return Number.isFinite(numeric) ? `${raw} m²` : raw;
        };
        const measureLabel = (measure) =>
          `${measure.is_demo ? "DEMO SAMPLE - " : ""}${measure.Name || ""}`;
        return `<div class="house-insulation-row" role="row"><strong role="rowheader">${escapeHtml(label)}</strong><div role="cell">${displayedMeasures.map((measure) => `<span class="house-insulation-quantity">${escapeHtml(quantity(measure, "Survey Quantity"))}<small>${escapeHtml(measureLabel(measure))}</small></span>`).join("")}</div><div role="cell">${displayedMeasures.map((measure) => `<span class="house-insulation-quantity">${escapeHtml(quantity(measure, "Survey Qty including Variation"))}<small>${escapeHtml(measureLabel(measure))}</small></span>`).join("")}</div></div>`;
      }).join("")}
    </div>
    <small>${hasDemoMeasures ? "Sample figures are for layout/testing only and are not verified property details." : "Insulation quantities are shown in m² as recorded in the SEAI application."}</small>
  </section>`;
}
function seaiDetailValue(value) {
  if (value === null || value === undefined || value === "") return "Not recorded";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
function seaiDetailSectionHtml(title, value) {
  const rows = Array.isArray(value) ? value : [value];
  const fields = [...new Set(rows.flatMap((row) =>
    row && typeof row === "object" && !Array.isArray(row) ? Object.keys(row) : [],
  ))];
  const body = !rows.length
    ? '<p class="survey-room-seai-note">No records captured.</p>'
    : fields.length
    ? `<div class="table-wrap survey-room-seai-table"><table><thead><tr>${fields.map((field) => `<th scope="col">${escapeHtml(field)}</th>`).join("")}</tr></thead><tbody>${rows.map((row, measureIndex) => `<tr>${fields.map((field) => {
      if (title === "Measures" && field === "Survey Qty including Variation") {
        const quantity = String(row?.[field] ?? "");
        return `<td><div class="seai-variation-cell"><span class="seai-variation-value">${escapeHtml(quantity || "Not recorded")}</span><button class="button button-secondary seai-variation-edit" type="button" aria-label="Edit survey quantity including variation for ${escapeHtml(row?.Name || `measure ${measureIndex + 1}`)}">Edit</button><div class="seai-variation-editor" hidden><input class="seai-variation-input" type="number" min="0" max="1000000000" step="any" inputmode="decimal" aria-label="Survey quantity including variation for ${escapeHtml(row?.Name || `measure ${measureIndex + 1}`)}" value="${escapeHtml(quantity)}"><button class="button button-secondary seai-variation-save" type="button" data-seai-measure-index="${measureIndex}">Save</button></div></div></td>`;
      }
      return `<td>${escapeHtml(seaiDetailValue(row?.[field]))}</td>`;
    }).join("")}</tr>`).join("")}</tbody></table></div>`
    : `<dl class="survey-room-seai-fields">${rows.map((row, index) => `<div><dt>${escapeHtml(fields.length ? fields[index] : title)}</dt><dd>${escapeHtml(seaiDetailValue(row))}</dd></div>`).join("")}</dl>`;
  const isMeasures = title === "Measures";
  return `<details class="survey-room-seai-section${isMeasures ? " survey-room-seai-measures" : ""}"${isMeasures ? " open" : ""}><summary>${escapeHtml(title)} <span>${rows.length}</span></summary>${title === "Files" ? '<p class="survey-room-seai-note">PDFs in the Application, Survey, and Variation categories can be opened in the SEAI PDFs section below. Other attachment types are shown as metadata only.</p>' : ""}${body}</details>`;
}
function seaiApplicationRecordHtml(details) {
  if (!details || typeof details !== "object") return "";
  return Object.entries(details)
    .map(([title, value]) => seaiDetailSectionHtml(title, value))
    .join("");
}
async function saveSeaiMeasureVariation(button) {
  const index = Number(button.dataset.seaiMeasureIndex);
  const houseId = state.surveyRoomHouseId;
  const input = button.closest(".seai-variation-editor")?.querySelector(".seai-variation-input");
  if (!houseId || !Number.isInteger(index) || !input || !input.reportValidity()) return;
  button.disabled = true;
  try {
    const result = await request("/api/seai-measure-variation", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        house_id: houseId,
        measure_index: index,
        variation: input.value,
      }),
    });
    for (const house of [
      ...surveyHouses,
      ...Object.values(planCache).flat(),
      ...(state.houseMapData || []),
    ]) {
      if (house.id === houseId) house.seai_details = result.seai_details;
    }
    $("#survey-room-seai-content").innerHTML =
      seaiApplicationRecordHtml(result.seai_details);
    showToast(`SEAI variation quantity for ${houseId} saved.`);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
}
async function loadSurveyorHouses() {
  const list = $("#houses-list");
  list.innerHTML = `<p class="muted">Loading ready houses...</p>`;
  try {
    surveyHouses = (await request("/api/calculator/houses?status=ready")).houses;
    renderSurveyorHouses();
  } catch (error) {
    list.innerHTML = `<p class="muted">Could not load houses: ${escapeHtml(error.message)}</p>`;
  }
}
function renderSurveyorHouses() {
  const query = $("#house-search").value.trim().toLocaleLowerCase();
  const rows = surveyHouses.filter((h) => [h.id, h.owner_name, h.address, h.postcode].filter(Boolean).join(" ").toLocaleLowerCase().includes(query));
  $("#houses-list").classList.remove("house-grid-compact");
  $("#houses-list").classList.add("house-grid-ready");
  $("#houses-list").innerHTML = rows.length ? rows.map((h) => `<article class="card house-card house-ready house-plan-ready" data-house-id="${escapeHtml(h.id)}">
    <div class="house-top"><span class="house-avatar">&#8962;</span><div class="house-meta"><h2>${escapeHtml(h.id)}</h2><p>${escapeHtml([h.owner_name, h.address, h.postcode].filter(Boolean).join(" - ") || "No details")}</p></div><div class="house-status-stack"><span class="status-pill ready">Ready for start</span>${surveyStatusBadge(h.survey)}</div></div>
    <div class="house-body">${houseWorkflowHtml(2)}${seaiInsulationSummaryHtml(h.seai_details)}<div class="house-stock-title">Survey status</div><p>${escapeHtml(surveyCountText(h.survey))}${h.survey_updated_by ? ` - Updated by ${escapeHtml(h.survey_updated_by)}` : ""}</p></div>
    <div class="house-card-actions"><button class="text-button house-survey-open ${h.survey ? "survey-reviewed" : "survey-needed"}" type="button" data-house-id="${escapeHtml(h.id)}" aria-label="${h.survey ? "Review completed survey" : "Open surveyor room"}">${h.survey ? "Review survey" : "Surveyor room"}</button>${houseMapButtonHtml(h.id)}</div>
  </article>`).join("") : `<p class="muted">${query ? "No matching ready houses." : "No houses are ready for survey."}</p>`;
}
function surveyRow(list, value = {}) {
  const isLength = list.endsWith("corners");
  const row = document.createElement("div");
  row.className = isLength ? "survey-row survey-length-row" : "survey-row survey-dimension-row";
  if (isLength) {
    row.innerHTML = `<label class="field">Length (m)<input type="number" min="0.01" max="100" step="0.01"></label><button class="icon-button survey-remove" type="button" aria-label="Remove measurement">&times;</button>`;
    row.querySelector("input").value = value.length ?? "";
  } else {
    const quantity = Number(value.qty ?? 1);
    const options = Array.from({ length: Math.max(20, quantity) }, (_, index) => `<option value="${index + 1}"${index + 1 === quantity ? " selected" : ""}>${index + 1}</option>`).join("");
    row.innerHTML = `<label class="field">Width (m)<input type="number" min="0.01" max="100" step="0.01"></label><label class="field">Height (m)<input type="number" min="0.01" max="100" step="0.01"></label><label class="field">Quantity<select>${options}</select></label><button class="icon-button survey-remove" type="button" aria-label="Remove measurement">&times;</button>`;
    row.querySelectorAll("input")[0].value = value.w ?? "";
    row.querySelectorAll("input")[1].value = value.h ?? "";
    row.querySelector("select").value = String(quantity);
  }
  $("#survey-" + list).append(row);
}

function setNoteAction(button, hasNote) {
  button.textContent = hasNote ? "Edit note" : "Add note";
  button.classList.toggle("note-action-edit", hasNote);
  button.classList.toggle("note-action-add", !hasNote);
}

function openHouseSurvey(houseId) {
  const house = surveyHouses.find((item) => item.id === houseId) || findPlanHouse(houseId);
  if (!house) return;
  if (!["admin", "surveyor"].includes(state.user?.role)) {
    showToast("Surveyor access is required to open this room.", true);
    return;
  }
  state.surveyRoomReturn = state.houseMapMode ? "map" : "list";
  state.surveyRoomHouseId = houseId;
  state.seaiSharePages.clear();
  state.surveyRoomRequest += 1;
  const form = $("#house-survey-form");
  form.dataset.houseId = houseId;
  const workPlanForm = $("#surveyor-work-plan-form");
  workPlanForm.dataset.houseId = houseId;
  const workPlanStatus = $("#surveyor-work-plan-save-status");
  workPlanStatus.textContent =
    "Requirement choices and work details save automatically. You can also save manually.";
  workPlanStatus.classList.remove("save-error");
  const isReady = house.status === "ready";
  const isNotStarted = house.status === "not_started";
  workPlanForm.classList.toggle(
    "hidden",
    !isReady || !["admin", "surveyor"].includes(state.user?.role),
  );
  $("#survey-room-work-plan").classList.toggle("hidden", !isReady);
  for (const trade of ["scaffolding", "esb", "plumber"]) {
    const required = house[`plan_${trade}_required`];
    workPlanForm.elements[`${trade}_required`].value =
      required === null || required === undefined ? "" : String(Boolean(required));
    workPlanForm.elements[`${trade}_notes`].value =
      house[`plan_${trade}_notes`] || "";
  }
  const houseNotesForm = $("#surveyor-house-notes-form");
  houseNotesForm.dataset.houseId = houseId;
  houseNotesForm.elements.notes.value = house.notes || "";
  houseNotesForm.classList.toggle("hidden", !isReady);
  const survey = house.survey || {};
  const surveyNotesForm = $("#surveyor-survey-notes-form");
  surveyNotesForm.dataset.houseId = houseId;
  surveyNotesForm.elements.notes.value = "";
  surveyNotesForm.classList.toggle("hidden", !isReady);
  const hasHouseNotes = Boolean(house.notes?.trim());
  const hasSurveyNote = Boolean(survey.notes?.trim());
  const houseNoteActionClass = hasHouseNotes ? "note-action-edit" : "note-action-add";
  const surveyNoteActionClass = hasSurveyNote ? "note-action-edit" : "note-action-add";
  for (const key of ["windows", "doors", "house_corners"]) {
    const target = $("#survey-" + key);
    target.replaceChildren();
    (survey[key] || []).forEach((item) => surveyRow(key, key.endsWith("corners") ? { length: item } : item));
    if (!target.children.length) surveyRow(key);
  }
  $("#survey-perimeter").value = survey.perimeter_m ?? "";
  $("#house-survey-title").textContent = isNotStarted
    ? `${houseId} · PDFs`
    : `${houseId} · Surveyor room`;
  $("#surveyor-room .eyebrow").textContent = isNotStarted
    ? "HOUSE PDFS · VIEW ONLY"
    : "PRE-START HOUSE SURVEY";
  $("#survey-room-status").classList.toggle("hidden", isNotStarted);
  $("#survey-meta").textContent = house.survey_updated_by ? `Last saved by ${house.survey_updated_by}${house.survey_updated_at ? ` - ${formatAuditDateTime(house.survey_updated_at)}` : ""}` : "Not surveyed yet";
  updateSurveyStatusDisplay(house.survey);
  $("#survey-room-subtitle").textContent = [house.owner_name, house.address, house.postcode].filter(Boolean).join(" · ") || "No house details recorded";
  $("#survey-room-summary").innerHTML = `
    <div><span>House ID</span><strong>${escapeHtml(house.id)}</strong></div>
    <div><span>Owner</span><strong>${escapeHtml(house.owner_name || "Not recorded")}</strong></div>
    <div><span>Eircode / postcode</span><strong>${escapeHtml(house.postcode || "Not recorded")}</strong></div>
    <div><span>Address</span><strong>${escapeHtml(house.address || "Not recorded")}</strong></div>
    <div class="survey-room-summary-survey"><span>Survey status</span><strong class="${house.survey ? "survey-recorded" : "survey-not-surveyed"}">${escapeHtml(surveyCountText(house.survey))}</strong></div>
    <div class="survey-room-house-notes"><div class="survey-room-house-notes-heading"><span>House notes</span>${isReady ? `<button class="text-button ${houseNoteActionClass}" type="button" data-edit-house-notes>${hasHouseNotes ? "Edit note" : "Add note"}</button>` : ""}</div><strong id="survey-room-summary-house-notes">${escapeHtml(house.notes || "No house notes")}</strong></div>
    <div class="survey-room-house-notes survey-room-survey-notes"><div class="survey-room-house-notes-heading"><span>Survey note</span>${isReady ? `<button class="text-button ${surveyNoteActionClass}" type="button" data-edit-survey-notes>${hasSurveyNote ? "Edit note" : "Add note"}</button>` : ""}</div><strong id="survey-room-summary-survey-notes">${escapeHtml(survey.notes || "No survey note")}</strong></div>`;
  $(".survey-room-summary-card").classList.toggle("hidden", isNotStarted);
  $("#survey-room-seai-content").innerHTML = seaiApplicationRecordHtml(house.seai_details);
  $("#survey-room-seai-documents").innerHTML = "";
  const seaiDetailsCard = $("#survey-room-seai-details");
  $(".card-heading h2", seaiDetailsCard).textContent = isNotStarted
    ? "SEAI PDFs"
    : "SEAI application record";
  $(".card-heading p", seaiDetailsCard).textContent = isNotStarted
    ? "View-only PDFs. Editing, annotations, and sharing are disabled while the house is Not started."
    : "Captured application, property, contractor, measures, tests, variance, notes, and attachment metadata.";
  $("#survey-room-seai-content").classList.toggle("hidden", isNotStarted);
  seaiDetailsCard.classList.toggle(
    "hidden",
    !isNotStarted && (!house.seai_details || !Object.keys(house.seai_details).length),
  );
  $("#survey-room-measurements").classList.toggle("hidden", !isReady);
  $("#house-document-file").value = "";
  $("#house-document-file-name").textContent = "No file selected";
  $("#house-documents-list").innerHTML = '<p class="muted">Loading survey documents…</p>';
  $("#house-document-form").classList.toggle(
    "hidden",
    !isReady || !["admin", "surveyor"].includes(state.user?.role),
  );
  $$(".survey-room-nav a").forEach((link) => {
    const target = $(link.getAttribute("href"));
    const stageHidden =
      (link.hasAttribute("data-ready-only") && isNotStarted) ||
      (link.hasAttribute("data-not-started-only") && !isNotStarted);
    link.classList.toggle(
      "hidden",
      stageHidden || Boolean(target?.classList.contains("hidden")),
    );
  });
  $("#houses-overview").classList.add("hidden");
  $("#house-map-view").classList.add("hidden");
  $("#house-detail").classList.add("hidden");
  $("#surveyor-room").classList.remove("hidden");
  setTab("houses");
  if (["admin", "surveyor"].includes(state.user?.role)) {
    loadSeaiDocuments(houseId, state.surveyRoomRequest, isReady);
  } else {
    $("#survey-room-seai-documents").innerHTML =
      '<p class="muted">SEAI PDFs are available to administrators and surveyors.</p>';
  }
  $(".card-heading h2", $("#survey-room-documents")).textContent = isNotStarted
    ? "House PDFs"
    : "Survey documents";
  $(".card-heading p", $("#survey-room-documents")).textContent = isNotStarted
    ? "View-only PDFs. Uploading, annotations, and sharing are disabled while the house is Not started."
    : "PDF plans and documents shared with the surveyor. Share adds the saved survey note on a separate final page.";
  loadHouseDocuments(houseId, state.surveyRoomRequest, isReady);
}
async function submitSurveyorHouseNotes(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = $("button[type='submit']", form);
  button.disabled = true;
  try {
    const result = await request("/api/house-notes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        house_id: form.dataset.houseId,
        notes: form.elements.notes.value,
      }),
    });
    const houseId = form.dataset.houseId;
    const house = [
      ...surveyHouses,
      ...Object.values(planCache).flat(),
      ...(state.houseMapData || []),
    ].find((item) => item.id === houseId);
    if (house) house.notes = result.notes;
    const hasNotes = Boolean(result.notes?.trim());
    $("#survey-room-summary-house-notes").textContent =
      hasNotes ? result.notes : "No house notes";
    setNoteAction($("[data-edit-house-notes]"), hasNotes);
    form.elements.notes.value = "";
    planStale.add("ready");
    showToast(`House notes for ${houseId} saved.`);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
}
async function submitHouseWorkPlan(event) {
  event.preventDefault();
  await saveHouseWorkPlan(event.currentTarget, true);
}
async function saveHouseWorkPlan(form, manual = false) {
  const button = $("button[type='submit']", form);
  const status = $("#surveyor-work-plan-save-status");
  const values = new FormData(form);
  const payload = { house_id: form.dataset.houseId };
  for (const trade of ["scaffolding", "esb", "plumber"]) {
    const required = values.get(`${trade}_required`);
    payload[`${trade}_required`] = required === "" ? null : required === "true";
    payload[`${trade}_notes`] = values.get(`${trade}_notes`);
  }
  const saveState = workPlanSaveStates.get(form) || { revision: 0, pending: Promise.resolve() };
  const revision = ++saveState.revision;
  const previousSave = saveState.pending;
  const save = previousSave
    .catch(() => {})
    .then(() =>
      request("/api/house-work-plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }),
    );
  saveState.pending = save;
  workPlanSaveStates.set(form, saveState);
  if (manual) button.disabled = true;
  status.textContent = "Saving work plan…";
  status.classList.remove("save-error");
  try {
    await save;
    const houseId = form.dataset.houseId;
    const matchingHouses = [
      ...surveyHouses,
      ...Object.values(planCache).flat(),
      ...(state.houseMapData || []),
    ].filter((house) => house.id === houseId);
    for (const house of matchingHouses) {
      for (const trade of ["scaffolding", "esb", "plumber"]) {
        house[`plan_${trade}_required`] = payload[`${trade}_required`];
        house[`plan_${trade}_notes`] = payload[`${trade}_notes`];
      }
    }
    planStale.add("ready");
    if (revision === saveState.revision) {
      status.textContent = "Work plan saved automatically.";
      if (manual) showToast(`Pre-start work plan for ${houseId} saved.`);
    }
  } catch (error) {
    if (revision === saveState.revision) {
      status.textContent = `Could not save work plan: ${error.message}`;
      status.classList.add("save-error");
    }
    showToast(`Work plan was not saved: ${error.message}`, true);
  } finally {
    if (manual) button.disabled = false;
  }
}
async function submitSurveyorSurveyNotes(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = $("button[type='submit']", form);
  button.disabled = true;
  try {
    const result = await request("/api/survey-notes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        house_id: form.dataset.houseId,
        notes: form.elements.notes.value,
      }),
    });
    const houseId = form.dataset.houseId;
    const matchingHouses = [
      ...surveyHouses,
      ...Object.values(planCache).flat(),
      ...(state.houseMapData || []),
      ...(state.dashboard?.house_list || []),
    ].filter((item) => item.id === houseId);
    matchingHouses.forEach((house) => {
      house.survey = { ...(house.survey || {}), ...result.survey };
      house.survey_updated_by = result.updated_by;
      house.survey_updated_at = result.updated_at;
    });
    const hasNotes = Boolean(result.notes?.trim());
    $("#survey-room-summary-survey-notes").textContent =
      hasNotes ? result.notes : "No survey note";
    setNoteAction($("[data-edit-survey-notes]"), hasNotes);
    $("#survey-meta").textContent =
      `Survey note saved by ${result.updated_by} - ${formatAuditDateTime(result.updated_at)}`;
    updateSurveyStatusDisplay(result.survey);
    form.elements.notes.value = "";
    showToast(`Survey note for ${houseId} saved.`);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
}
async function loadHouseDocuments(houseId, requestId, canModify = true) {
  const list = $("#house-documents-list");
  try {
    const result = await request(
      `/api/house-documents?house_id=${encodeURIComponent(houseId)}`,
    );
    if (requestId !== state.surveyRoomRequest) return;
    list.innerHTML = result.documents.length
      ? result.documents.map((document) => `
        <div class="house-document-entry">
          <a class="house-document-link" href="/api/house-documents/${document.id}" target="_blank" rel="noreferrer">
            <span class="house-document-icon" aria-hidden="true">PDF</span>
            <span class="house-document-copy"><strong>${escapeHtml(document.filename)}</strong><small>Uploaded by ${escapeHtml(document.uploaded_by)} · ${escapeHtml(formatAuditDateTime(document.uploaded_at))} · ${(document.size_bytes / 1_000_000).toFixed(1)} MB</small></span>
            <span class="house-document-open" aria-hidden="true">↗</span>
          </a>
          ${canModify ? `<button class="button button-secondary house-document-annotate" type="button" data-document-id="${document.id}" data-document-name="${escapeHtml(document.filename)}">Annotate</button>
          <button class="button button-secondary house-document-share" type="button" data-document-id="${document.id}" data-document-name="${escapeHtml(document.filename)}">Share PDF</button>` : ""}
        </div>`).join("")
      : '<p class="muted house-documents-empty">No survey PDFs uploaded for this house yet.</p>';
  } catch (error) {
    if (requestId === state.surveyRoomRequest) {
      list.innerHTML = `<p class="house-documents-error">Could not load survey documents: ${escapeHtml(error.message)}</p>`;
    }
  }
}
async function loadSeaiDocuments(houseId, requestId, canModify = true) {
  const list = $("#survey-room-seai-documents");
  list.innerHTML = '<p class="muted">Loading SEAI PDFs…</p>';
  try {
    const result = await request(
      `/api/seai-documents?house_id=${encodeURIComponent(houseId)}`,
    );
    if (requestId !== state.surveyRoomRequest) return;
    const categories = ["Application", "Survey", "Variation"];
    const extraCategories = [...new Set(
      result.documents.map((document) => document.category),
    )].filter((category) => !categories.includes(category));
    const allCategories = [...categories, ...extraCategories];
    const toolbar = canModify ? `<div class="seai-share-toolbar">
      <p>Select pages from any PDFs. When a saved annotated copy exists, its marked-up page is used. The saved Survey note is appended to the combined PDF.</p>
      <div><span class="seai-share-selection-count" data-seai-share-count aria-live="polite">0 pages selected</span>
      <button class="button button-primary" type="button" data-seai-share-selected disabled>Share selected pages</button>
      <button class="button button-secondary" type="button" data-seai-share-clear disabled>Clear</button></div>
    </div>` : "";
    list.innerHTML = result.documents.length
      ? `${toolbar}<h3>SEAI PDFs</h3>${allCategories.map((category) => {
        const documents = result.documents.filter((document) => document.category === category);
        if (!documents.length) return "";
        return `<section class="seai-document-category"><h4>${escapeHtml(category)}</h4>${documents.map((document) => `
          <div class="seai-document-entry">
            <span class="house-document-icon" aria-hidden="true">PDF</span>
            <span class="seai-document-name"><span class="seai-document-status ${document.is_annotated ? "annotated" : "original"}">${document.is_annotated ? "Annotated copy" : "Original"}</span>${escapeHtml(document.filename)}<small>${(document.size_bytes / 1_000_000).toFixed(1)} MB</small></span>
            ${canModify && document.is_annotated ? `<button class="button button-secondary seai-document-share" type="button" data-seai-document-share="${Number(document.id)}" data-document-name="${escapeHtml(document.filename)}">Share annotated PDF</button>` : ""}
            ${canModify
    ? `<button class="button button-secondary seai-document-view" type="button" data-document-id="${Number(document.id)}" data-document-name="${escapeHtml(document.filename)}" data-document-category="${escapeHtml(category)}">Open &amp; annotate</button>`
    : `<a class="button button-secondary" href="/api/seai-documents/${Number(document.id)}/file" target="_blank" rel="noreferrer">View PDF</a>`}
          </div>`).join("")}</section>`;
      }).join("")}`
      : `${toolbar}<p class="muted seai-documents-empty">No SEAI PDF attachments are available for this house.</p>`;
    updateSeaiShareToolbar();
  } catch (error) {
    if (requestId === state.surveyRoomRequest) {
      list.innerHTML = `<p class="house-documents-error">Could not load SEAI PDFs: ${escapeHtml(error.message)}</p>`;
    }
  }
}
function seaiSharePageKey(documentId, page) {
  return `${documentId}:${page}`;
}
function updateSeaiShareToolbar() {
  const count = state.seaiSharePages.size;
  const countLabel = $("[data-seai-share-count]");
  const shareButton = $("[data-seai-share-selected]");
  const clearButton = $("[data-seai-share-clear]");
  if (countLabel) countLabel.textContent = `${count} ${count === 1 ? "page" : "pages"} selected`;
  if (shareButton) shareButton.disabled = count === 0;
  if (clearButton) clearButton.disabled = count === 0;
  const selection = $("#pdf-annotator-page-selection");
  const annotator = state.pdfAnnotator;
  if (selection && annotator?.documentType === "seai") {
    selection.checked = state.seaiSharePages.has(
      seaiSharePageKey(annotator.documentId, annotator.page),
    );
  }
}
function updateSeaiPageSelection(isSelected) {
  const annotator = state.pdfAnnotator;
  if (!annotator || annotator.documentType !== "seai") return;
  const key = seaiSharePageKey(annotator.documentId, annotator.page);
  if (isSelected) {
    state.seaiSharePages.set(key, {
      document_id: Number(annotator.documentId),
      page: annotator.page,
      filename: annotator.filename,
      category: annotator.category,
    });
  } else {
    state.seaiSharePages.delete(key);
  }
  updateSeaiShareToolbar();
}
async function prepareSeaiDocumentShare(button) {
  const categoryOrder = new Map(
    ["Application", "Survey", "Variation"].map((category, index) => [category, index]),
  );
  const pages = [...state.seaiSharePages.values()].sort((first, second) =>
    (categoryOrder.get(first.category) ?? 3) -
      (categoryOrder.get(second.category) ?? 3) ||
    first.category.localeCompare(second.category) ||
    first.filename.localeCompare(second.filename, undefined, { numeric: true }) ||
    first.page - second.page,
  );
  if (!pages.length || button.disabled) return;
  button.disabled = true;
  try {
    const response = await fetch("/api/seai-documents/share", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        house_id: state.surveyRoomHouseId,
        pages: pages.map(({ document_id, page }) => ({ document_id, page })),
      }),
    });
    if (!response.ok) {
      let result = {};
      try {
        result = await response.json();
      } catch {
        throw new Error(`Could not prepare the combined PDF (${response.status}).`);
      }
      if (response.status === 409 && result.error?.includes("survey note")) {
        const surveyNote = $("#surveyor-survey-notes-form textarea[name='notes']");
        showToast(
          "Save a Survey note first; it will be appended to the combined PDF.",
          true,
        );
        surveyNote.scrollIntoView({ behavior: "smooth", block: "center" });
        surveyNote.focus();
        return;
      }
      throw new Error(result.error || `Could not prepare the combined PDF (${response.status}).`);
    }
    const file = new File(
      [await response.blob()],
      `${state.surveyRoomHouseId}-selected-seai-pages-with-survey-note.pdf`,
      { type: "application/pdf" },
    );
    const filenames = [...new Set(pages.map((page) => page.filename))];
    const filename = `${pages.length} selected pages from ${filenames.length} SEAI PDFs`;
    state.preparedHouseShare = {
      file,
      houseId: state.surveyRoomHouseId,
      filename,
      description: "Selected SEAI pages followed by the saved Survey note.",
    };
    $("#house-document-share-title").textContent =
      `${state.surveyRoomHouseId} · ${filename}`;
    $("#house-document-share-dialog").showModal();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
}
function clearSeaiShareSelection() {
  state.seaiSharePages.clear();
  updateSeaiShareToolbar();
}
async function prepareSeaiDocumentFileShare(button) {
  const documentId = button.dataset.seaiDocumentShare;
  const filename = button.dataset.documentName;
  button.disabled = true;
  try {
    const response = await fetch(`/api/seai-documents/${documentId}/share`, {
      method: "POST",
      cache: "no-store",
    });
    if (!response.ok) {
      let result = {};
      try {
        result = await response.json();
      } catch {
        throw new Error(`Could not prepare the annotated PDF (${response.status}).`);
      }
      if (response.status === 409 && result.error?.includes("survey note")) {
        const surveyNote = $("#surveyor-survey-notes-form textarea[name='notes']");
        showToast(
          "Save a Survey note first; it will be appended to the annotated PDF.",
          true,
        );
        surveyNote.scrollIntoView({ behavior: "smooth", block: "center" });
        surveyNote.focus();
        return;
      }
      throw new Error(result.error || `Could not prepare the annotated PDF (${response.status}).`);
    }
    const file = new File(
      [await response.blob()],
      `${state.surveyRoomHouseId}-${filename.replace(/\.pdf$/i, "")}-with-survey-note.pdf`,
      { type: "application/pdf" },
    );
    state.preparedHouseShare = {
      file,
      houseId: state.surveyRoomHouseId,
      filename,
      description: `Annotated SEAI PDF ${filename} with the saved Survey note appended as a final page.`,
    };
    $("#house-document-share-title").textContent =
      `${state.surveyRoomHouseId} · ${filename} · Annotated copy`;
    $("#house-document-share-dialog").showModal();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
}
function openSeaiDocument(button) {
  openPdfAnnotator(button, "seai");
}
function closeHouseDocumentAnnotator() {
  const annotator = state.pdfAnnotator;
  if (annotator?.previewUrl) URL.revokeObjectURL(annotator.previewUrl);
  state.pdfAnnotator = null;
  $("#pdf-annotator-dialog").close();
  $("#pdf-annotator-image").removeAttribute("src");
  $("#pdf-annotator-canvas").width = 0;
  $("#pdf-annotator-canvas").height = 0;
}
function updatePdfPageZoom(preserveCenter = true) {
  const annotator = state.pdfAnnotator;
  const image = $("#pdf-annotator-image");
  const viewport = $("#pdf-annotator-viewport");
  const surface = $("#pdf-page-surface");
  if (!annotator || !image.naturalWidth || !image.naturalHeight) return;
  const oldWidth = surface.clientWidth;
  const oldHeight = surface.clientHeight;
  const centerX = oldWidth
    ? (viewport.scrollLeft + viewport.clientWidth / 2) / oldWidth
    : 0.5;
  const centerY = oldHeight
    ? (viewport.scrollTop + viewport.clientHeight / 2) / oldHeight
    : 0.5;
  const availableWidth = Math.max(1, viewport.clientWidth - 28);
  const availableHeight = Math.max(1, viewport.clientHeight - 28);
  annotator.fitScale = Math.min(
    availableWidth / image.naturalWidth,
    availableHeight / image.naturalHeight,
  );
  annotator.fitWidth = image.naturalWidth * annotator.fitScale;
  const width = Math.round(annotator.fitWidth * annotator.zoom);
  const height = Math.round(image.naturalHeight * annotator.fitScale * annotator.zoom);
  surface.style.width = `${width}px`;
  surface.style.height = `${height}px`;
  image.style.width = `${width}px`;
  image.style.height = `${height}px`;
  $("#pdf-annotator-zoom-level").textContent =
    `${Math.round(annotator.zoom * 100)}%`;
  $("#pdf-annotator-zoom-out").disabled = annotator.zoom <= 0.5;
  $("#pdf-annotator-zoom-in").disabled = annotator.zoom >= 3;
  drawPdfAnnotations();
  if (preserveCenter) {
    requestAnimationFrame(() => {
      viewport.scrollLeft = centerX * surface.clientWidth - viewport.clientWidth / 2;
      viewport.scrollTop = centerY * surface.clientHeight - viewport.clientHeight / 2;
    });
  }
}
function drawPdfAnnotations() {
  const annotator = state.pdfAnnotator;
  const canvas = $("#pdf-annotator-canvas");
  const image = $("#pdf-annotator-image");
  updateSeaiShareToolbar();
  if (!annotator || !image.naturalWidth || !image.naturalHeight) return;
  const width = image.clientWidth;
  const height = image.clientHeight;
  if (!width || !height) return;
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  canvas.style.width = `${width}px`;
  canvas.style.height = `${height}px`;
  const backingWidth = Math.round(width * ratio);
  const backingHeight = Math.round(height * ratio);
  if (canvas.width !== backingWidth || canvas.height !== backingHeight) {
    canvas.width = backingWidth;
    canvas.height = backingHeight;
  }
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  annotator.annotations
    .filter((annotation) => annotation.page === annotator.page)
    .forEach((annotation) => {
      context.save();
      context.strokeStyle = annotation.color;
      context.fillStyle = annotation.color;
      context.lineCap = "round";
      context.lineJoin = "round";
      if (annotation.tool === "text") {
        context.textBaseline = "top";
        context.font = `600 ${Math.max(14, annotation.font_size * width)}px system-ui, sans-serif`;
        context.fillText(
          annotation.text,
          annotation.point[0] * width,
          annotation.point[1] * height,
        );
      } else {
        context.globalAlpha = annotation.tool === "highlighter" ? 0.38 : 1;
        context.lineWidth = annotation.width * width;
        context.beginPath();
        annotation.points.forEach(([x, y], index) => {
          if (index === 0) context.moveTo(x * width, y * height);
          else context.lineTo(x * width, y * height);
        });
        if (annotation.points.length === 1) {
          context.arc(
            annotation.points[0][0] * width,
            annotation.points[0][1] * height,
            Math.max(1, context.lineWidth / 2),
            0,
            Math.PI * 2,
          );
          context.fill();
        } else {
          context.stroke();
        }
      }
      context.restore();
    });
  $("#pdf-annotator-undo").disabled = annotator.annotations.length === 0;
  $("#pdf-annotator-save").disabled =
    annotator.annotations.length === 0 || annotator.saving;
  $("#pdf-annotator-page-count").textContent =
    `Page ${annotator.page} of ${annotator.pageCount || 1} · Scroll to change pages`;
}
function drawPdfStrokeSegment(annotation, start, end) {
  const canvas = $("#pdf-annotator-canvas");
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (!width || !height) return;
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  const context = canvas.getContext("2d");
  context.save();
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.strokeStyle = annotation.color;
  context.globalAlpha = annotation.tool === "highlighter" ? 0.38 : 1;
  context.lineCap = "round";
  context.lineJoin = "round";
  context.lineWidth = annotation.width * width;
  context.beginPath();
  context.moveTo(start[0] * width, start[1] * height);
  context.lineTo(end[0] * width, end[1] * height);
  context.stroke();
  context.restore();
}
async function loadHouseDocumentPage(pageNumber) {
  const annotator = state.pdfAnnotator;
  if (!annotator) return;
  if (pageNumber < 1 || pageNumber > annotator.pageCount || annotator.loading) return;
  annotator.page = pageNumber;
  annotator.loading = true;
  $("#pdf-annotator-error").classList.add("hidden");
  $("#pdf-annotator-page-count").textContent = `Loading page ${pageNumber}…`;
  drawPdfAnnotations();
  try {
    const documentPath = annotator.documentType === "seai"
      ? "seai-documents"
      : "house-documents";
    const response = await fetch(
      `/api/${documentPath}/${annotator.documentId}/preview?page=${pageNumber}`,
      { cache: "no-store" },
    );
    if (!response.ok) {
      let result = {};
      try {
        result = await response.json();
      } catch {
        throw new Error(`Could not open this PDF page (${response.status}).`);
      }
      throw new Error(result.error || `Could not open this PDF page (${response.status}).`);
    }
    const pageCount = Number(response.headers.get("X-PDF-Page-Count"));
    if (!Number.isInteger(pageCount) || pageCount < 1) {
      throw new Error("The PDF page count could not be read.");
    }
    const previewUrl = URL.createObjectURL(await response.blob());
    const image = $("#pdf-annotator-image");
    const previousUrl = annotator.previewUrl;
    annotator.previewUrl = previewUrl;
    image.onload = () => {
      if (previousUrl) URL.revokeObjectURL(previousUrl);
      annotator.loading = false;
      annotator.wheelLocked = false;
      $("#pdf-annotator-viewport").scrollTop = 0;
      updatePdfPageZoom(false);
    };
    image.onerror = () => {
      URL.revokeObjectURL(previewUrl);
      annotator.previewUrl = "";
      annotator.loading = false;
      annotator.wheelLocked = false;
      $("#pdf-annotator-error").textContent = "This PDF page could not be displayed.";
      $("#pdf-annotator-error").classList.remove("hidden");
      drawPdfAnnotations();
    };
    annotator.pageCount = pageCount;
    image.src = previewUrl;
  } catch (error) {
    annotator.loading = false;
    annotator.wheelLocked = false;
    $("#pdf-annotator-error").textContent = error.message;
    $("#pdf-annotator-error").classList.remove("hidden");
    drawPdfAnnotations();
  }
}
function scrollPdfPage(event) {
  const annotator = state.pdfAnnotator;
  if (!annotator || !event.deltaY) return;
  const viewport = $("#pdf-annotator-viewport");
  const hasVerticalOverflow = viewport.scrollHeight > viewport.clientHeight + 2;
  const atScrollBoundary = event.deltaY > 0
    ? viewport.scrollTop + viewport.clientHeight >= viewport.scrollHeight - 2
    : viewport.scrollTop <= 2;
  if (annotator.zoom > 1.02 && hasVerticalOverflow && !atScrollBoundary) return;

  const direction = Math.sign(event.deltaY);
  const nextPage = annotator.page + direction;
  if (annotator.loading || annotator.wheelLocked ||
      nextPage < 1 || nextPage > annotator.pageCount) return;

  event.preventDefault();
  if (Math.sign(annotator.wheelDelta) !== direction) annotator.wheelDelta = 0;
  annotator.wheelDelta += event.deltaY;
  if (Math.abs(annotator.wheelDelta) < 35) return;

  annotator.wheelDelta = 0;
  annotator.wheelLocked = true;
  loadHouseDocumentPage(nextPage);
}
function openHouseDocumentAnnotator(button) {
  openPdfAnnotator(button, "house");
}
function openPdfAnnotator(button, documentType) {
  state.pdfAnnotator = {
    documentId: button.dataset.documentId,
    filename: button.dataset.documentName,
    category: button.dataset.documentCategory || "",
    documentType,
    houseId: state.surveyRoomHouseId,
    page: 1,
    pageCount: 1,
    tool: "pen",
    penSize: 2.5,
    zoom: 1,
    fitScale: 1,
    fitWidth: 1,
    annotations: [],
    previewUrl: "",
    loading: false,
    saving: false,
    drawing: null,
    wheelDelta: 0,
    wheelLocked: false,
  };
  $("#pdf-annotator-title").textContent = button.dataset.documentName;
  $("#pdf-annotator-page-selection-label").classList.toggle(
    "hidden",
    documentType !== "seai",
  );
  $("#pdf-annotator-page-selection").checked = false;
  $(".pdf-annotator-title .eyebrow").textContent =
    documentType === "seai" ? "SEAI PDF · ORIGINAL PRESERVED" : "SURVEY PDF";
  $("#pdf-annotator-save").textContent =
    "Save annotated copy";
  $("#pdf-annotator-help").textContent = documentType === "seai"
    ? "The original SEAI PDF stays unchanged. Your annotations will be saved as a separate PDF in this category."
    : "Draw with a finger, Apple Pencil, mouse, or trackpad. Choose a size and zoom in for detail; use Undo to remove the last mark.";
  $("#pdf-annotator-color").value = "#d93025";
  $("#pdf-annotator-size").value = "2.5";
  $("#pdf-annotator-zoom-level").textContent = "100%";
  $("#pdf-annotator-text").value = "";
  $("#pdf-annotator-text-panel").classList.add("hidden");
  $$(".pdf-tool-button").forEach((toolButton) => {
    const active = toolButton.dataset.pdfTool === "pen";
    toolButton.classList.toggle("active", active);
    toolButton.setAttribute("aria-pressed", String(active));
  });
  $("#pdf-annotator-dialog").showModal();
  loadHouseDocumentPage(1);
}
async function saveHouseDocumentAnnotations() {
  const annotator = state.pdfAnnotator;
  if (!annotator || !annotator.annotations.length || annotator.saving) return;
  annotator.saving = true;
  drawPdfAnnotations();
  try {
    const isSeaiDocument = annotator.documentType === "seai";
    const documentPath = isSeaiDocument ? "seai-documents" : "house-documents";
    const result = await request(
      `/api/${documentPath}/${annotator.documentId}/annotations`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ annotations: annotator.annotations }),
      },
    );
    closeHouseDocumentAnnotator();
    if (isSeaiDocument) {
      await loadSeaiDocuments(state.surveyRoomHouseId, state.surveyRoomRequest);
      showToast(`Annotated copy saved as ${result.filename}. The original is unchanged.`);
    } else {
      await loadHouseDocuments(result.house_id, state.surveyRoomRequest);
      showToast("Annotated PDF saved as a separate copy.");
    }
  } catch (error) {
    annotator.saving = false;
    drawPdfAnnotations();
    showToast(error.message, true);
  }
}
async function prepareHouseDocumentShare(button) {
  const documentId = button.dataset.documentId;
  const filename = button.dataset.documentName;
  const houseId = state.surveyRoomHouseId;
  button.disabled = true;
  try {
    const response = await fetch(`/api/house-documents/${documentId}/share`, {
      cache: "no-store",
    });
    if (!response.ok) {
      let result = {};
      try {
        result = await response.json();
      } catch {
        throw new Error(`Could not prepare the PDF (${response.status}).`);
      }
      if (response.status === 409 && result.error?.includes("survey note")) {
        const surveyNote = $("#surveyor-survey-notes-form textarea[name='notes']");
        showToast(
          "Only the Survey note is added to the shared PDF. Enter it in the Survey note field and click Save survey note first.",
          true,
        );
        surveyNote.scrollIntoView({ behavior: "smooth", block: "center" });
        surveyNote.focus();
        return;
      }
      throw new Error(result.error || `Could not prepare the PDF (${response.status}).`);
    }
    const blob = await response.blob();
    const shareFilename =
      `${houseId}-${filename.replace(/\.pdf$/i, "")}-with-survey-note.pdf`;
    const file = new File([blob], shareFilename, { type: "application/pdf" });
    state.preparedHouseShare = { file, houseId, filename };
    $("#house-document-share-title").textContent = `${houseId} · ${filename}`;
    $("#house-document-share-dialog").showModal();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
}
function downloadPreparedHouseShare() {
  const prepared = state.preparedHouseShare;
  if (!prepared) return;
  const url = URL.createObjectURL(prepared.file);
  const link = document.createElement("a");
  link.href = url;
  link.download = prepared.file.name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function sharePreparedHouseFile(target = "share") {
  const prepared = state.preparedHouseShare;
  if (!prepared) return;
  let canShareFile = false;
  try {
    canShareFile =
      typeof navigator.share === "function" &&
      typeof navigator.canShare === "function" &&
      navigator.canShare({ files: [prepared.file] });
  } catch {
    canShareFile = false;
  }
  if (canShareFile) {
    try {
      await navigator.share({
        files: [prepared.file],
        title: `${target === "share" ? "" : `${target} · `}House ${prepared.houseId} survey PDF`,
        text: prepared.description ||
          `Survey PDF for house ${prepared.houseId}. The saved survey note is appended on a separate page.`,
      });
      $("#house-document-share-dialog").close();
      showToast("Combined PDF shared with the selected app.");
      return;
    } catch (error) {
      if (error.name === "AbortError") {
        showToast("Sharing was cancelled.");
        return;
      }
      downloadPreparedHouseShare();
      $("#house-document-share-dialog").close();
      showToast("Your browser could not open its share sheet. The combined PDF was downloaded; attach it to your email or WhatsApp message.");
      return;
    }
  }
  downloadPreparedHouseShare();
  $("#house-document-share-dialog").close();
  showToast("Combined PDF downloaded. Attach it to your email or WhatsApp message.");
}
function sendPreparedHouseShare() {
  return sharePreparedHouseFile();
}
async function emailPreparedHouseShare() {
  const prepared = state.preparedHouseShare;
  if (!prepared) return;
  let canShareFile = false;
  try {
    canShareFile =
      typeof navigator.share === "function" &&
      typeof navigator.canShare === "function" &&
      navigator.canShare({ files: [prepared.file] });
  } catch {
    canShareFile = false;
  }
  if (canShareFile) return sharePreparedHouseFile("email");
  downloadPreparedHouseShare();
  const subject = `House ${prepared.houseId} survey PDF`;
  const body = `${prepared.description || "The combined PDF has been downloaded."} Attach it to this email before sending.`;
  $("#house-document-share-dialog").close();
  window.location.href = `mailto:?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
  showToast("Combined PDF downloaded. Attach it to the email before sending.");
}
async function whatsappPreparedHouseShare() {
  const prepared = state.preparedHouseShare;
  if (!prepared) return;
  let canShareFile = false;
  try {
    canShareFile =
      typeof navigator.share === "function" &&
      typeof navigator.canShare === "function" &&
      navigator.canShare({ files: [prepared.file] });
  } catch {
    canShareFile = false;
  }
  if (canShareFile) return sharePreparedHouseFile("whatsapp");
  downloadPreparedHouseShare();
  const message = [
    `Survey PDF for house ${prepared.houseId}. ${prepared.description || ""}`,
    "The combined PDF was downloaded separately. Attach it in WhatsApp before sending.",
  ].join(" ");
  window.open(
    `https://wa.me/?text=${encodeURIComponent(message)}`,
    "_blank",
    "noopener,noreferrer",
  );
  $("#house-document-share-dialog").close();
  showToast("Combined PDF downloaded. Attach it in WhatsApp before sending.");
}
async function submitHouseDocument(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const file = $("#house-document-file").files[0];
  if (!file) {
    showToast("Choose a PDF file to upload.", true);
    return;
  }
  if (!file.name.toLocaleLowerCase().endsWith(".pdf")) {
    showToast("House documents must be PDF files.", true);
    return;
  }
  if (file.size > 8_000_000) {
    showToast("PDF files must be 8 MB or smaller.", true);
    return;
  }
  const button = $("button[type='submit']", form);
  button.disabled = true;
  const payload = new FormData();
  payload.append("house_id", state.surveyRoomHouseId);
  payload.append("file", file);
  try {
    await request("/api/house-documents", { method: "POST", body: payload });
    $("#house-document-file").value = "";
    $("#house-document-file-name").textContent = "No file selected";
    await loadHouseDocuments(state.surveyRoomHouseId, state.surveyRoomRequest);
    showToast("Survey PDF uploaded and shared with the surveyor.");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
}
function closeSurveyorRoom() {
  $("#surveyor-room").classList.add("hidden");
  state.surveyRoomHouseId = "";
  state.surveyRoomRequest += 1;
  if (state.surveyRoomReturn === "map") {
    state.houseMapMode = true;
    $("#houses-overview").classList.add("hidden");
    $("#house-map-view").classList.remove("hidden");
    renderHouses();
    return;
  }
  state.houseMapMode = false;
  showHousesOverview();
  state.houseStatusFilter = "ready";
  state.houseTeamFilter = null;
  state.housesTeamsTab = "houses";
  if (state.user?.role === "surveyor") {
    setTab("houses");
    loadSurveyorHouses();
  } else {
    renderHouses();
  }
}
async function submitHouseSurvey(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = $("button[type='submit']", form);
  const readRows = (id) => [...$("#survey-" + id).querySelectorAll(".survey-row")];
  const dims = (id) => readRows(id).filter((row) => [...row.querySelectorAll("input")].every((input) => input.value.trim())).map((row) => ({ w: Number(row.querySelectorAll("input")[0].value), h: Number(row.querySelectorAll("input")[1].value), qty: Number(row.querySelector("select").value) }));
  const lengths = (id) => readRows(id).map((row) => Number(row.querySelector("input").value)).filter(Boolean);
  for (const id of ["windows", "doors"]) {
    const partial = readRows(id).find((row) => {
      const values = [...row.querySelectorAll("input")].map((input) => input.value.trim());
      return values.some(Boolean) && !values.every(Boolean);
    });
    if (partial) { showToast("Complete all dimensions and quantity in each window or door row.", true); partial.querySelector("input").focus(); return; }
  }
  const currentHouse = [
    ...surveyHouses,
    ...Object.values(planCache).flat(),
    ...(state.houseMapData || []),
    ...(state.dashboard?.house_list || []),
  ].find((house) => house.id === form.dataset.houseId);
  const survey = {
    windows: dims("windows"),
    doors: dims("doors"),
    house_corners: lengths("house_corners"),
    perimeter_m: $("#survey-perimeter").value.trim()
      ? Number($("#survey-perimeter").value)
      : null,
    notes: currentHouse?.survey?.notes || "",
  };
  button.disabled = true;
  try {
    await request("/api/house-survey", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ house_id: form.dataset.houseId, survey }) });
    const updatedAt = new Date().toISOString();
    $("#survey-meta").textContent = `Last saved by ${state.user.display_name} - ${formatAuditDateTime(updatedAt)}`;
    updateSurveyStatusDisplay(survey);
    const currentHouse = [...surveyHouses, ...Object.values(planCache).flat()].find((house) => house.id === form.dataset.houseId);
    if (currentHouse) {
      currentHouse.survey = survey;
      currentHouse.survey_updated_by = state.user.display_name;
      currentHouse.survey_updated_at = updatedAt;
    }
    planStale.add("ready");
    showToast(`Survey for ${form.dataset.houseId} saved.`);
    state.houseStatusFilter = "ready";
    state.houseTeamFilter = null;
    state.housesTeamsTab = "houses";
    $("#house-search").value = "";
    state.surveyRoomReturn = "list";
    closeSurveyorRoom();
  } catch (error) { showToast(error.message, true); }
  finally { button.disabled = false; }
}
function houseWorkflowHtml(currentStep, showHint = false) {
  const stages = ["Not started", "Ready for start", "Active", "Closed"];
  return `<div class="house-workflow" role="group" aria-label="House workflow progress">
    <div class="house-workflow-heading"><strong>House workflow</strong><span>Step ${currentStep} of ${stages.length}</span></div>
    <ol class="house-workflow-steps" aria-label="House stages">
      ${stages.map((stage, index) => {
        const step = index + 1;
        const stateClass = step < currentStep ? "complete" : step === currentStep ? "current" : "";
        return `<li class="${stateClass} workflow-stage-${step}"${step === currentStep ? ' aria-current="step"' : ""}><span>${step}</span><small>${stage}</small></li>`;
      }).join("")}
    </ol>
    ${showHint ? '<p class="house-workflow-hint">Mark ready when pre-start checks can begin. Stage 1/2 work stages appear after activation.</p>' : ""}
  </div>`;
}
async function renderNotStartedHouses(query, mode = "not_started") {
  const list = $("#houses-list");
  list.classList.remove("house-grid-scrollable");
  const cacheKey = mode;
  const loadPlan = async () => {
    planCache[cacheKey] = (await request(`/api/calculator/houses?status=${mode}`)).houses;
    planStale.delete(cacheKey);
  };
  if (!planCache[cacheKey]) {
    list.innerHTML = `<p class="muted">Loading SEAI houses…</p>`;
    delete list.dataset.planHtml;
    try {
      await loadPlan();
    } catch (error) {
      showToast(error.message, true);
      return;
    }
    if (state.houseStatusFilter !== mode) return;
  } else if (planStale.has(cacheKey)) {
    // Keep the current cards on screen while a background refresh runs.
    planStale.delete(cacheKey);
    const before = JSON.stringify(planCache[cacheKey]);
    loadPlan()
      .then(() => {
        if (state.houseStatusFilter === mode && JSON.stringify(planCache[cacheKey]) !== before) {
          renderNotStartedHouses($("#house-search").value.trim().toLocaleLowerCase(), mode);
        }
      })
      .catch(() => {});
  }
  const isReady = mode === "ready";
  const canManageHouses = state.user?.role === "admin";
  const canSurveyHouses = ["admin", "surveyor"].includes(state.user?.role);
  list.classList.toggle("house-grid-ready", isReady);
  const rows = planCache[cacheKey].filter((h) =>
    [h.id, h.owner_name, h.address, h.postcode].filter(Boolean).join(" ").toLocaleLowerCase().includes(query),
  );
  const html = rows.length
    ? rows
        .map((h) => {
          const id = escapeHtml(h.id);
          const workflowStep = isReady ? 2 : 1;
            const isMapTestHouse = h.source === "map_test";
            const locationDetails = [
              isMapTestHouse ? "" : h.owner_name,
              isMapTestHouse ? "" : h.address,
              h.postcode,
            ]
              .filter(Boolean)
              .join(" · ");
            const propertyDetails = [
              Number.isFinite(Number(h.area_m2)) && h.area_m2 !== null
                ? `${formatQuantity(h.area_m2)} m²`
                : "",
              Number.isFinite(Number(h.perimeter_m)) && h.perimeter_m !== null
                ? `${formatQuantity(h.perimeter_m)} m perimeter`
                : "",
            ].filter(Boolean);
            return `<article class="card house-card ${isReady ? "house-ready house-plan-ready" : "house-pending house-plan-pending"}" data-house-id="${id}">
          <div class="house-top"><span class="house-avatar">⌂</span><div class="house-meta"><h2>${id}</h2><p>${escapeHtml(locationDetails || "No address recorded")}</p>${h.employee_name ? `<p class="house-assigned-employee"><strong>Assigned employee:</strong> ${escapeHtml(h.employee_name)}</p>` : ""}${propertyDetails.length ? `<div class="house-property-details">${propertyDetails.map((detail) => `<span>${escapeHtml(detail)}</span>`).join("")}</div>` : ""}${isReady ? houseAuditHtml(h, true) : ""}</div><div class="house-status-stack"><span class="status-pill ${isReady ? "ready" : "pending"}">${isReady ? "Ready for start" : "Not started"}</span>${isReady ? surveyStatusBadge(h.survey) : ""}</div></div>
          <div class="house-body" data-house-id="${id}"${!isReady ? ' tabindex="0" role="region" aria-label="House status"' : ""}>
            ${houseWorkflowHtml(workflowStep, !isReady)}
            ${seaiInsulationSummaryHtml(h.seai_details, h.id)}
          </div>
        <div class="house-card-actions">
          ${!isReady ? `<button class="text-button plan-ready-toggle" type="button" data-house-id="${id}" data-ready="true">Mark ready for start</button>${canSurveyHouses ? `<button class="text-button house-survey-open survey-needed" type="button" data-house-id="${id}" aria-label="Open Surveyor room for ${id}">View Surveyor room</button>` : ""}` : `
            <button class="text-button seai-activate" type="button" data-house-id="${id}"${h.survey ? "" : " disabled"}>&#43; Activate house</button>
            ${canSurveyHouses ? `<button class="text-button house-survey-open ${h.survey ? "survey-reviewed" : "survey-needed"}" type="button" data-house-id="${id}" aria-label="${h.survey ? "Review completed survey" : "Open surveyor room"}">${h.survey ? "Review survey" : "Surveyor room"}</button>` : ""}
            <button class="text-button plan-ready-toggle" type="button" data-house-id="${id}" data-ready="false">Back to not started</button>
            ${houseMapButtonHtml(h.id)}
            `}
        </div>
      </article>`;
        })
        .join("")
    : `<p class="muted">${isReady ? "No houses are ready for start yet." : "No SEAI houses waiting to start."}</p>`;
  list.classList.toggle("house-grid-compact", !isReady);
  if (list.dataset.planHtml !== html) {
    const listScroll = list.scrollTop;
    const pageScroll = window.scrollY;
    const houseBodyScroll = new Map(
      [...list.querySelectorAll(".house-body[data-house-id]")].map((body) => [
        body.dataset.houseId,
        body.scrollTop,
      ]),
    );
    list.innerHTML = html;
    list.dataset.planHtml = html;
    list.querySelectorAll(".house-body[data-house-id]").forEach((body) => {
      const scrollTop = houseBodyScroll.get(body.dataset.houseId);
      if (scrollTop !== undefined) body.scrollTop = scrollTop;
    });
    list.scrollTo({ top: listScroll, behavior: "instant" });
    if (window.scrollY !== pageScroll) {
      window.scrollTo({ top: pageScroll, behavior: "instant" });
    }
  }
  list.classList.toggle("house-grid-scrollable", rows.length > 6 && !isReady);
}

async function renderAssignSeaiHouses(query) {
  const list = $("#houses-list");
  const mode = "all";
  if (!planCache[mode] || planStale.has(mode)) {
    try {
      planCache[mode] = (
        await request("/api/calculator/houses?status=all")
      ).houses;
      planStale.delete(mode);
    } catch (error) {
      showToast(error.message, true);
      return;
    }
    if (state.houseStatusFilter !== "seai") return;
  }
  const rows = planCache[mode].filter((house) =>
    [house.id, house.owner_name, house.address, house.postcode]
      .filter(Boolean)
      .join(" ")
      .toLocaleLowerCase()
      .includes(query),
  );
  list.innerHTML = rows.length
    ? rows.map((house) => {
        const statusLabels = {
          active: "Active",
          closed: "Closed",
          not_started: "Not started",
          ready: "Ready for start",
        };
        const status = statusLabels[house.status] || "Not started";
        const statusClass = house.status === "ready"
          ? "ready"
          : house.status === "active"
            ? "active"
            : house.status === "closed"
              ? "finished"
              : "pending";
        return `<article class="card house-card house-plan-pending" data-house-id="${escapeHtml(house.id)}">
          <div class="house-top"><span class="house-avatar">⌂</span><div class="house-meta"><h2>${escapeHtml(house.id)}</h2><p>${escapeHtml([house.owner_name, house.address, house.postcode].filter(Boolean).join(" · ") || "No address recorded")}</p></div><span class="status-pill ${statusClass}">${status}</span></div>
        </article>`;
      }).join("")
    : `<p class="muted">${query ? "No matching houses." : "No houses from SEAI are available."}</p>`;
  list.classList.toggle("house-grid-scrollable", rows.length > 6);
}

function openHouseNote(houseId) {
  const house =
    state.dashboard.house_list.find((item) => item.id === houseId) || findPlanHouse(houseId);
  const form = $("#house-note-form");
  form.dataset.houseId = houseId;
  form.elements.notes.value = house?.notes || "";
  $("#house-note-title").textContent = `${houseId} notes`;
  $("#house-note-dialog").showModal();
  form.elements.notes.focus();
}
async function updateHouseChecklistStatus(event) {
  const checkbox = event.target.closest(".house-checklist-status");
  if (!checkbox || checkbox.type !== "checkbox" || checkbox.disabled) return;
  const { houseId, checklistField } = checkbox.dataset;
  const checked = checkbox.checked;
  const value = checklistField === "esb_unclipped"
    ? checked ? "yes" : "no"
    : checked ? "done" : "not_done";
  checkbox.disabled = true;
  try {
    await request("/api/house-checklist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        house_id: houseId,
        [checklistField]: value,
      }),
    });
    const house = state.dashboard.house_list.find((item) => item.id === houseId);
    if (house) house[checklistField] = value;
    checkbox.dataset.savedValue = value;
    const toggle = checkbox.closest(".checklist-toggle");
    toggle.classList.toggle("is-done", checked);
    $(".checklist-toggle-indicator", toggle).textContent = checked ? "✓" : "";
    $(".checklist-toggle-label", toggle).textContent = checked ? "Done" : "Not done yet";
    showToast(`${checkbox.getAttribute("aria-label")} updated.`);
  } catch (error) {
    checkbox.checked = checkbox.dataset.savedValue === "done" ||
      checkbox.dataset.savedValue === "yes";
    showToast(error.message, true);
  } finally {
    checkbox.disabled = false;
  }
}
async function submitHouseNote(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = $("button[type='submit']", form);
  button.disabled = true;
  try {
    await request("/api/houses/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: form.dataset.houseId, notes: form.elements.notes.value }),
    });
    $("#house-note-dialog").close();
    showToast(`Note for ${form.dataset.houseId} saved.`);
    await refresh();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function toggleHouseReady(houseId, ready) {
  try {
    await request("/api/houses/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: houseId, ready }),
    });
    showToast(
      ready
        ? `${houseId} is ready for start.`
        : `${houseId} moved back to Not started; survey records cleared, variation quantities and original PDFs kept.`,
    );
    await refresh();
  } catch (error) {
    showToast(error.message, true);
  }
}

const HOUSE_MAP_TILE_SIZE = 256;
const HOUSE_MAP_DEFAULT_CENTER = { latitude: 53.4, longitude: -8, zoom: 6 };
const ASSIGN_DEMO_MAP_CENTERS = [
  [53.3498, -6.2603],
  [51.8985, -8.4756],
  [53.2707, -9.0568],
  [52.6638, -8.6267],
  [52.2593, -7.1101],
  [54.2766, -8.4761],
  [54.95, -7.734],
  [54.0, -6.405],
  [53.4239, -7.9407],
  [52.27, -9.70],
  [52.6541, -7.2448],
  [52.8436, -8.9864],
  [53.8508, -9.2977],
  [53.7179, -6.3561],
  [53.9908, -7.3601],
  [52.059, -9.508],
];

function hasUsableHouseCoordinates(house) {
  return Number.isFinite(Number(house.latitude)) &&
    Number.isFinite(Number(house.longitude)) &&
    house.latitude !== null &&
    house.longitude !== null;
}

function addAssignDemoMapLocations(houses) {
  const sortedHouses = [...houses].sort((left, right) =>
    left.id.localeCompare(right.id),
  );
  return sortedHouses.map((house, index) => {
    if (hasUsableHouseCoordinates(house)) return house;
    let hash = 2166136261;
    for (const character of house.id) {
      hash = Math.imul(hash ^ character.charCodeAt(0), 16777619) >>> 0;
    }
    const [centerLatitude, centerLongitude] =
      ASSIGN_DEMO_MAP_CENTERS[index % ASSIGN_DEMO_MAP_CENTERS.length];
    return {
      ...house,
      latitude:
        centerLatitude + (((hash & 0xffff) / 0xffff) - 0.5) * 0.035,
      longitude:
        centerLongitude + ((((hash >>> 16) & 0xffff) / 0xffff) - 0.5) * 0.05,
      source: "map_test",
    };
  });
}

function googleMapsEircodeUrl(house) {
  const query = String(house.postcode || "").trim();
  if (!query) return "";
  return `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(query)}`;
}

function projectMapPoint(latitude, longitude, zoom) {
  const scale = HOUSE_MAP_TILE_SIZE * 2 ** zoom;
  const safeLatitude = Math.max(-85.0511, Math.min(85.0511, latitude));
  const sinLatitude = Math.sin((safeLatitude * Math.PI) / 180);
  return {
    x: ((longitude + 180) / 360) * scale,
    y: (0.5 - Math.log((1 + sinLatitude) / (1 - sinLatitude)) / (4 * Math.PI)) * scale,
  };
}

function mapHouseStatusLabel(house) {
  if (house.status === "active") return `Active · Stage ${house.stage || 1}`;
  return {
    not_started: "Not started",
    ready: "Ready for start",
    closed: "Closed",
  }[house.status] || house.status;
}

function mapHouseStatusClass(house) {
  if (house.status === "active") return "active";
  return house.status || "not_started";
}

function mapHouseStageFilter(house) {
  if (house.status === "active") {
    return `active-${Number(house.stage) === 2 ? 2 : 1}`;
  }
  return ["not_started", "ready", "closed"].includes(house.status)
    ? house.status
    : "";
}

function isHouseMapStageVisible(house) {
  return Boolean(state.houseMapStageFilters[mapHouseStageFilter(house)]);
}

function renderHouseMapStageFilters() {
  $$("#house-map-legend [data-map-stage-filter]").forEach((button) => {
    button.setAttribute(
      "aria-pressed",
      String(Boolean(state.houseMapStageFilters[button.dataset.mapStageFilter])),
    );
  });
}

function fitHouseMap() {
  const houses = (state.houseMapData || []).filter(
    hasUsableHouseCoordinates,
  );
  if (!houses.length) {
    const center = projectMapPoint(
      HOUSE_MAP_DEFAULT_CENTER.latitude,
      HOUSE_MAP_DEFAULT_CENTER.longitude,
      HOUSE_MAP_DEFAULT_CENTER.zoom,
    );
    state.houseMapCamera = { ...center, zoom: HOUSE_MAP_DEFAULT_CENTER.zoom };
    renderHouseMapCanvas();
    return;
  }
  const canvas = $("#house-map-canvas");
  const width = canvas.clientWidth || 720;
  const height = canvas.clientHeight || 520;
  const bounds = houses.map((house) =>
    projectMapPoint(Number(house.latitude), Number(house.longitude), 15),
  );
  const xs = bounds.map((point) => point.x);
  const ys = bounds.map((point) => point.y);
  const spanX = Math.max(1, Math.max(...xs) - Math.min(...xs));
  const spanY = Math.max(1, Math.max(...ys) - Math.min(...ys));
  const zoom = Math.max(
    4,
    Math.min(15, Math.floor(Math.log2(Math.min((width * 0.78) / spanX, (height * 0.78) / spanY))) + 15),
  );
  const center = projectMapPoint(
    houses.reduce((sum, house) => sum + Number(house.latitude), 0) / houses.length,
    houses.reduce((sum, house) => sum + Number(house.longitude), 0) / houses.length,
    zoom,
  );
  state.houseMapCamera = { ...center, zoom };
  renderHouseMapCanvas();
}

function zoomHouseMap(change) {
  if (!state.houseMapCamera) {
    fitHouseMap();
    return;
  }
  const oldZoom = state.houseMapCamera.zoom;
  const zoom = Math.max(4, Math.min(18, oldZoom + change));
  if (zoom === oldZoom) return;
  const factor = 2 ** (zoom - oldZoom);
  state.houseMapCamera.x *= factor;
  state.houseMapCamera.y *= factor;
  state.houseMapCamera.zoom = zoom;
  renderHouseMapCanvas();
}

function renderHouseMapCanvas() {
  const canvas = $("#house-map-canvas");
  const tiles = $("#house-map-tiles");
  const markers = $("#house-map-markers");
  if (!canvas || !tiles || !markers) return;
  const width = canvas.clientWidth || 720;
  const height = canvas.clientHeight || 520;
  tiles.replaceChildren();
  markers.replaceChildren();
  const mappedHouses = (state.houseMapData || []).filter(
    (house) => hasUsableHouseCoordinates(house) && isHouseMapStageVisible(house),
  );
  if (!state.houseMapCamera) {
    const center = projectMapPoint(
      HOUSE_MAP_DEFAULT_CENTER.latitude,
      HOUSE_MAP_DEFAULT_CENTER.longitude,
      HOUSE_MAP_DEFAULT_CENTER.zoom,
    );
    state.houseMapCamera = { ...center, zoom: HOUSE_MAP_DEFAULT_CENTER.zoom };
  }
  if (!mappedHouses.length) {
    const empty = document.createElement("div");
    empty.className = "house-map-empty";
    const allHouses = state.houseMapData || [];
    const total = allHouses.filter(isHouseMapStageVisible).length;
    empty.innerHTML = total
      ? `<strong>${total} eligible house${total === 1 ? "" : "s"}</strong><span>The map has no saved coordinates for these houses. Select a house from the list to open its Eircode location in Google Maps.</span>`
      : allHouses.length
        ? "<strong>No stages selected</strong><span>Turn on a stage filter to show house pins.</span>"
        : "<strong>No active or ready houses</strong><span>Houses will appear here when marked Ready for start or Active.</span>";
    tiles.append(empty);
  }
  if (mappedHouses.some((house) => house.source === "map_test")) {
    const notice = document.createElement("div");
    notice.className = "house-map-empty house-map-approximate";
    notice.innerHTML = "<strong>Approximate demo pins</strong><span>Sample locations are spread around towns across Ireland to demonstrate the map. They are not the houses' exact Eircode locations.</span>";
    tiles.append(notice);
  }
  const { x: centerX, y: centerY, zoom } = state.houseMapCamera;
  const tileCount = 2 ** zoom;
  const firstTileX = Math.floor((centerX - width / 2) / HOUSE_MAP_TILE_SIZE);
  const lastTileX = Math.floor((centerX + width / 2) / HOUSE_MAP_TILE_SIZE);
  const firstTileY = Math.max(0, Math.floor((centerY - height / 2) / HOUSE_MAP_TILE_SIZE));
  const lastTileY = Math.min(tileCount - 1, Math.floor((centerY + height / 2) / HOUSE_MAP_TILE_SIZE));
  for (let tileY = firstTileY; tileY <= lastTileY; tileY += 1) {
    for (let tileX = firstTileX; tileX <= lastTileX; tileX += 1) {
      const image = document.createElement("img");
      const wrappedX = ((tileX % tileCount) + tileCount) % tileCount;
      image.className = "house-map-tile";
      image.alt = "";
      image.draggable = false;
      image.src = `https://tile.openstreetmap.org/${zoom}/${wrappedX}/${tileY}.png`;
      image.style.left = `${tileX * HOUSE_MAP_TILE_SIZE - centerX + width / 2}px`;
      image.style.top = `${tileY * HOUSE_MAP_TILE_SIZE - centerY + height / 2}px`;
      tiles.append(image);
    }
  }
  mappedHouses.forEach((house) => {
    const point = projectMapPoint(
      Number(house.latitude),
      Number(house.longitude),
      zoom,
    );
    const marker = document.createElement("button");
    const statusClass = mapHouseStatusClass(house);
    const activeStage = house.status === "active" ? ` · Stage ${house.stage || 1}` : "";
    const isApproximate = house.source === "map_test";
    const locationLabel = isApproximate ? " · approximate demo location, not exact Eircode location" : "";
    marker.type = "button";
    marker.className = `house-map-marker map-status-${statusClass}${isApproximate ? " house-map-marker-approximate" : ""}${house.id === state.selectedMapHouseId ? " selected" : ""}`;
    marker.dataset.mapHouseId = house.id;
    if (isApproximate) marker.dataset.locationAccuracy = "approximate";
    if (house.status === "active") marker.dataset.stage = String(house.stage || 1);
    marker.style.left = `${point.x - centerX + width / 2}px`;
    marker.style.top = `${point.y - centerY + height / 2}px`;
    marker.textContent = house.status === "active"
      ? String(house.stage || 1)
      : house.status === "not_started" ? "N" : "R";
    marker.setAttribute(
      "aria-label",
      `${house.id}, ${mapHouseStatusLabel(house)}${activeStage}${locationLabel}`,
    );
    marker.title = `${house.id} · ${mapHouseStatusLabel(house)}${activeStage}${locationLabel}`;
    markers.append(marker);
  });
}

function mapHouseActions(house) {
  const id = escapeHtml(house.id);
  const admin = state.user?.role === "admin";
  if (state.user?.role === "assign") return "";
  if (house.status === "not_started") {
    return `<button class="text-button plan-ready-toggle" type="button" data-house-id="${id}" data-ready="true">Mark ready for start</button><button class="text-button house-note-open ${houseNoteActionClass(house.notes)}" type="button" data-house-id="${id}">${house.notes?.trim() ? "Edit note" : "Add note"}</button>${["admin", "surveyor"].includes(state.user?.role) ? `<button class="text-button house-survey-open survey-needed" type="button" data-house-id="${id}">View Surveyor room</button>` : ""}`;
  }
  if (house.status === "ready") {
    return `<button class="text-button seai-activate" type="button" data-house-id="${id}"${house.survey ? "" : " disabled"}>＋ Activate house</button>${["admin", "surveyor"].includes(state.user?.role) ? `<button class="text-button house-survey-open ${house.survey ? "survey-reviewed" : "survey-needed"}" type="button" data-house-id="${id}" aria-label="${house.survey ? "Review completed survey" : "Open surveyor room"}">${house.survey ? "Review survey" : "Surveyor room"}</button>` : ""}<button class="text-button plan-ready-toggle" type="button" data-house-id="${id}" data-ready="false">Back to not started</button>`;
  }
  if (house.status === "closed") {
    return `<button class="text-button house-finish-toggle" type="button" data-house-id="${id}" data-finished="true">Reopen house</button>${admin ? `<button class="text-button house-open" type="button" data-house-id="${id}">View details &amp; history</button>` : ""}`;
  }
  return `<button class="text-button house-record-movement" type="button" data-house-id="${id}">＋ Record movement</button><span class="stage-switch" role="group" aria-label="Stage"><span class="map-stage-caption">WORK STAGE</span>${[1, 2].map((stage) => `<button class="stage-btn${(Number(house.stage) || 1) === stage ? " on" : ""}" type="button" data-house-id="${id}" data-stage="${stage}" aria-pressed="${(Number(house.stage) || 1) === stage}">Stage ${stage}</button>`).join("")}</span><button class="text-button house-open" type="button" data-house-id="${id}">View details &amp; history</button>${admin ? `<button class="text-button house-finish-toggle" type="button" data-house-id="${id}" data-finished="false">Mark house finished</button>` : ""}`;
}

function selectMapHouse(houseId) {
  const house = state.houseMapData?.find((item) => item.id === houseId);
  if (!house) return;
  state.selectedMapHouseId = houseId;
  const tracked = state.dashboard.house_list.find((item) => item.id === houseId);
  const displayHouse = { ...tracked, ...house };
  const address = [house.address, house.postcode].filter(Boolean).join(" · ");
  const measures = state.user?.role === "assign" ? "" : [
    house.area_m2 === null ? "" : `${formatQuantity(house.area_m2)} m²`,
    house.perimeter_m === null ? "" : `${formatQuantity(house.perimeter_m)} m perimeter`,
  ].filter(Boolean).join(" · ");
  const details = [
    house.owner_name,
    address,
    house.employee_name ? `Assigned to ${house.employee_name}` : "",
    measures,
  ].filter(Boolean).join(" · ");
  const mapsUrl = !hasUsableHouseCoordinates(house)
    ? googleMapsEircodeUrl(house)
    : "";
  $("#house-map-selection").innerHTML = `
    <div class="house-map-selected-heading"><div><strong>${escapeHtml(house.id)}</strong><span class="status-pill ${house.status === "active" ? "active" : house.status === "ready" ? "ready" : house.status === "closed" ? "finished" : "pending"}">${escapeHtml(mapHouseStatusLabel(house))}</span></div><p>${escapeHtml(details || "No location details recorded")}</p></div>
    <div class="house-card-actions">${mapHouseActions(displayHouse)}${mapsUrl ? `<a class="text-button house-map-eircode-link" href="${escapeHtml(mapsUrl)}" target="_blank" rel="noopener noreferrer">Open Eircode pin in Google Maps</a>` : ""}</div>`;
  const activityCount = houseAuditCount(displayHouse);
  $("#house-map-activity-count").textContent = String(activityCount);
  $("#house-map-activity-count").setAttribute(
    "aria-label",
    `${activityCount} recorded ${activityCount === 1 ? "update" : "updates"}`,
  );
  $("#house-map-audit").innerHTML = houseAuditHtml(displayHouse) ||
    '<div class="house-map-audit-empty"><span class="house-map-audit-empty-icon" aria-hidden="true">&#9711;</span><strong>No updates yet</strong><p>Ready, survey, material, activation, and close actions will appear here as they are recorded.</p></div>';
  $("#house-map-house-list").querySelectorAll("[data-map-house-id]").forEach((button) =>
    button.classList.toggle("selected", button.dataset.mapHouseId === houseId),
  );
  renderHouseMapCanvas();
}

function openHouseMap(houseId) {
  showHousesOverview();
  state.houseTeamFilter = null;
  state.houseStatusFilter = null;
  state.housesTeamsTab = "houses";
  state.houseMapMode = true;
  state.selectedMapHouseId = houseId;
  state.houseMapCamera = null;
  setTab("houses");
  renderHouses();
}

function renderHouseMapList() {
  const houses = (state.houseMapData || []).filter(isHouseMapStageVisible);
  const list = $("#house-map-house-list");
  $("#house-map-list-count").textContent = String(houses.length);
  list.innerHTML = houses.map((house) => `
    <button class="house-map-list-item${house.id === state.selectedMapHouseId ? " selected" : ""}" type="button" data-map-house-id="${escapeHtml(house.id)}">
      <span><strong>${escapeHtml(house.id)}</strong><small>${escapeHtml([house.address, house.postcode].filter(Boolean).join(" · ") || "Location details missing")}</small></span>
      <span class="house-map-list-status map-list-status-${mapHouseStatusClass(house)}"${house.status === "active" ? ` data-stage="${house.stage || 1}"` : ""}>${escapeHtml(mapHouseStatusLabel(house))}</span>
    </button>`).join("");
}

function renderHouseMapSummary() {
  const houses = state.houseMapData || [];
  const visibleHouses = houses.filter(isHouseMapStageVisible);
  const mappedCount = visibleHouses.filter(hasUsableHouseCoordinates).length;
  const readyCount = visibleHouses.filter((house) => house.status === "ready").length;
  const activeCount = visibleHouses.filter((house) => house.status === "active").length;
  const houseCountLabel = state.user?.role === "assign" ? "provided" : "eligible";
  $("#house-map-summary").innerHTML = `
    <span><strong>${visibleHouses.length}</strong> shown of ${houses.length} ${houseCountLabel}</span>
    <span><strong>${mappedCount}</strong> mapped</span>
    <span class="map-summary-ready"><strong>${readyCount}</strong> ready</span>
    <span class="map-summary-active"><strong>${activeCount}</strong> active</span>`;
}

async function loadHouseMap() {
  try {
    const status = state.user?.role === "assign" ? "all" : "map";
    const result = await request(`/api/calculator/houses?status=${status}`);
    if (!state.houseMapMode) return;
    state.houseMapData = state.user?.role === "assign"
      ? addAssignDemoMapLocations(result.houses)
      : result.houses;
    const houses = state.houseMapData;
    $(".house-map-list-heading h3").textContent =
      state.user?.role === "assign" ? "Provided houses" : "Eligible houses";
    renderHouseMapSummary();
    renderHouseMapList();
    renderHouseMapStageFilters();
    if (!state.houseMapCamera) fitHouseMap();
    else renderHouseMapCanvas();
    if (state.selectedMapHouseId) selectMapHouse(state.selectedMapHouseId);
    else {
      $("#house-map-selection").innerHTML = "<strong>Select a house</strong><p>Choose a marker or an address from the list below.</p>";
      $("#house-map-activity-count").textContent = "—";
      $("#house-map-activity-count").removeAttribute("aria-label");
      $("#house-map-audit").innerHTML = '<div class="house-map-audit-empty"><span class="house-map-audit-empty-icon" aria-hidden="true">&#8982;</span><strong>Choose a house</strong><p>Select a house to see who marked it ready, completed the survey, specified materials, activated or closed it, or recorded a material movement.</p></div>';
    }
  } catch (error) {
    showToast(error.message, true);
  }
}

function renderHouses() {
  if (state.houseMapMode) {
    $("#houses-overview").classList.add("hidden");
    $("#house-map-view").classList.remove("hidden");
    loadHouseMap();
    return;
  }
  const data = state.dashboard;
  const canManageHouses = state.user?.role === "admin";
  const canEditHouseChecklist = ["admin", "employee"].includes(state.user?.role);
  const canViewMaterials = state.user?.role !== "assign";
  const isAssign = state.user?.role === "assign";
  $("#assign-houses-dashboard").classList.toggle("hidden", !isAssign);
  if (isAssign) {
    const summary = data.house_summary || {};
    $("#assign-stat-seai").textContent = summary.provided || 0;
    $("#assign-stat-active").textContent = summary.active || 0;
    $("#assign-stat-closed").textContent = summary.closed || 0;
    $("#assign-stat-ready").textContent = summary.ready || 0;
    $("#assign-stat-not-started").textContent = summary.not_started || 0;
    $$("#assign-houses-dashboard [data-assign-house-filter]").forEach((button) => {
      const selectedFilter = state.houseStatusFilter || "active";
      button.setAttribute(
        "aria-pressed",
        String(button.dataset.assignHouseFilter === selectedFilter),
      );
    });
  }
  const query = $("#house-search").value.trim().toLocaleLowerCase();
  const teamFilter = state.houseTeamFilter;
  const statusFilter = state.houseStatusFilter;
  const filteredHouseIds = teamFilter
    ? [
        ...new Set(
          (teamFilter.locations || [teamFilter.location]).flatMap((location) =>
            data.team_house_counts?.[location]?.[
              teamFilter.status === "closed" ? "closed_house_ids" : "open_house_ids"
            ] || [],
          ),
        ),
      ]
    : null;
  const houseTeams = new Map();
  Object.entries(data.team_house_counts || {}).forEach(([location, counts]) => {
    if (!location.startsWith("team:")) return;
    [...(counts.open_house_ids || []), ...(counts.closed_house_ids || [])].forEach(
      (houseId) => {
        const names = houseTeams.get(houseId) || [];
        names.push(formatLocation(location));
        houseTeams.set(houseId, names);
      },
    );
  });
  if (isAssign && statusFilter === "seai") {
    $("#houses-list").classList.remove(
      "house-grid-active",
      "house-grid-closed",
      "house-grid-ready",
      "house-grid-compact",
    );
    $("#houses-list-heading").textContent = "Houses from SEAI";
    $("#house-team-filter").classList.remove("hidden");
    $("#house-team-filter-label").textContent = "Showing all houses from SEAI";
    $("#houses-empty").classList.add("hidden");
    $("#houses-no-results").classList.add("hidden");
    renderAssignSeaiHouses(query);
    return;
  }
  if ((statusFilter === "not_started" || statusFilter === "ready") && !teamFilter) {
    const ready = statusFilter === "ready";
    $("#houses-list").classList.remove("house-grid-active", "house-grid-closed");
    $("#houses-list-heading").textContent = ready ? "Houses ready for start" : "SEAI houses not started";
    $("#house-team-filter").classList.remove("hidden");
    const filterLabel = $("#house-team-filter-label");
    filterLabel.className = `house-team-filter-label filter-status-${statusFilter}`;
    filterLabel.textContent = ready
      ? "Showing houses ready for start"
      : "Showing SEAI houses not started yet";
    $("#houses-empty").classList.add("hidden");
    $("#houses-no-results").classList.add("hidden");
    renderNotStartedHouses(query, statusFilter);
    return;
  }
  const visibleStatus = statusFilter ||
    (teamFilter?.status === "closed" ? "closed" : "active");
  $("#houses-list").classList.remove("house-grid-ready", "house-grid-compact");
  $("#houses-list").classList.toggle("house-grid-active", visibleStatus === "active");
  $("#houses-list").classList.toggle("house-grid-closed", visibleStatus === "closed");
  const matchingHouses = data.house_list.filter((house) =>
    (!filteredHouseIds || filteredHouseIds.includes(house.id)) &&
    (!teamFilter || Boolean(house.is_finished) === (teamFilter.status === "closed")) &&
    (visibleStatus === "closed" ? Boolean(house.is_finished) : visibleStatus === "active" ? !house.is_finished : true) &&
    [house.id, house.owner_name, house.employee_name, house.postcode]
      .filter(Boolean)
      .join(" ")
      .toLocaleLowerCase()
      .includes(query),
  );
  const houseCards = matchingHouses.map((house) => {
    const checklistStatus = (value, labels) =>
      value === null || value === undefined
        ? "Not set"
        : labels[value];
    const checklistTone = (value) => {
      if (value === "done" || value === "yes") return "positive";
      if (value === "not_done" || value === "no") return "negative";
      if (value === "not_necessary" || value === "not_esb") {
        return "unnecessary";
      }
      return "unset";
    };
    const checklist = [
      [
        "Scaffolding",
        house.scaffolding_status,
        checklistStatus(house.scaffolding_status, {
          done: "Done",
          not_done: "Not done yet",
          not_necessary: "Not necessary",
        }),
        "scaffolding_status",
        house.plan_scaffolding_notes,
        house.plan_scaffolding_required,
      ],
      [
        "ESB work",
        house.esb_unclipped,
        checklistStatus(house.esb_unclipped, {
          yes: "Done",
          no: "Not done yet",
          not_esb: "Not necessary",
        }),
        "esb_unclipped",
        house.plan_esb_notes,
        house.plan_esb_required,
      ],
      [
        "Plumber work",
        house.plumber_status,
        checklistStatus(house.plumber_status, {
          done: "Done",
          not_done: "Not done yet",
          not_necessary: "Not necessary",
        }),
        "plumber_status",
        house.plan_plumber_notes,
        house.plan_plumber_required,
      ],
    ];
    const held = canViewMaterials ? data.materials.flatMap((material) => {
      const quantity = material.balances[`house:${house.id}`] || 0;
      return Math.abs(quantity) > 1e-9
        ? [{ name: material.name, qty: quantity, unit: material.unit }]
        : [];
    }) : [];
    const hasNegative = held.some((item) => item.qty < 0);
    const assignedTeams = houseTeams.get(house.id) || [];
    const propertyDetails = [
      house.address,
      Number.isFinite(Number(house.area_m2)) && house.area_m2 !== null
        ? `${formatQuantity(house.area_m2)} m²`
        : "",
      Number.isFinite(Number(house.perimeter_m)) && house.perimeter_m !== null
        ? `${formatQuantity(house.perimeter_m)} m perimeter`
        : "",
    ].filter(Boolean);
    return `
      <article class="card house-card ${house.is_finished ? "house-finished" : "house-active"}">
        <div class="house-top"><span class="house-avatar">⌂</span><div class="house-meta"><h2>${escapeHtml(house.id)}</h2><p>${escapeHtml([house.owner_name, house.postcode].filter(Boolean).join(" · ") || "Site details not recorded")}</p>${house.employee_name ? `<p class="house-assigned-employee"><strong>Assigned employee:</strong> ${escapeHtml(house.employee_name)}</p>` : ""}${assignedTeams.length ? `<p class="house-team-label">Team${assignedTeams.length === 1 ? "" : "s"}: ${assignedTeams.map(escapeHtml).join(", ")}</p>` : ""}${canViewMaterials ? houseAuditHtml(house) : ""}${propertyDetails.length ? `<div class="house-property-details">${propertyDetails.map((detail) => `<span>${escapeHtml(detail)}</span>`).join("")}</div>` : ""}</div><span class="status-pill ${house.is_finished ? "finished" : "active"}">${house.is_finished ? "Closed" : "Active"}</span>${canViewMaterials && hasNegative ? `<span class="status-pill warning">Reconcile</span>` : ""}</div>
        <div class="house-body" data-house-id="${escapeHtml(house.id)}">${houseWorkflowHtml(house.is_finished ? 4 : 3)}${canViewMaterials ? `<div class="house-stock"><div class="house-stock-title">Materials on site</div>
          ${held.length ? `<div class="stock-rows${expandedStock.has(house.id) || house.is_finished ? " expanded" : ""}">${held.map((item, index) => `<div class="balance-row${index >= STOCK_PREVIEW ? " stock-extra" : ""}"><span>${escapeHtml(item.name)}</span><span class="${item.qty < 0 ? "qty-negative" : ""}">${escapeHtml(formatQuantity(item.qty, item.unit))}</span></div>`).join("")}</div>${!house.is_finished && held.length > STOCK_PREVIEW ? `<button class="stock-more-btn" type="button" data-house-id="${escapeHtml(house.id)}" data-extra="${held.length - STOCK_PREVIEW}">${expandedStock.has(house.id) ? "Show less" : `… Show more (${held.length - STOCK_PREVIEW})`}</button>` : ""}` : `<div class="balance-row"><span>No balance recorded</span><span>—</span></div>`}
        </div>` : ""}
        ${noteHtml(house.notes)}${house.is_finished ? "" : snagsHtml(house)}
        <div class="house-checklist-card">
          <div class="house-stock-title">House checklist</div>
          ${checklist.map(([label, status, value, field, notes, required]) => `<div class="checklist-card-row"><span class="checklist-card-label">${escapeHtml(label)}${notes ? `<small>${escapeHtml(notes)}</small>` : ""}</span>${canEditHouseChecklist && !house.is_finished ? activeChecklistToggle(house.id, field, label, status, required) : `<span class="checklist-static-status">${checklistRequirementBadge(required, status)}<strong class="checklist-value checklist-value-${checklistTone(status)}">${escapeHtml(value)}</strong></span>`}</div>`).join("")}
        </div>
        </div>
        <div class="house-card-actions">
          ${house.is_finished ? `
            ${canManageHouses ? `<button class="text-button house-finish-toggle" type="button" data-house-id="${escapeHtml(house.id)}" data-finished="true">Reopen house</button>` : ""}
            ${canManageHouses ? `<button class="text-button house-open" type="button" data-house-id="${escapeHtml(house.id)}">View details &amp; history <span>→</span></button>` : ""}
          ` : `
          ${houseMapButtonHtml(house.id)}
          ${canViewMaterials ? `<button class="text-button house-record-movement" type="button" data-house-id="${escapeHtml(house.id)}">＋ Record movement</button>` : ""}
          <button class="text-button house-note-open ${houseNoteActionClass(house.notes)}" type="button" data-house-id="${escapeHtml(house.id)}">${house.notes?.trim() ? "Edit note" : "Add note"}</button>
          <span class="stage-switch" role="group" aria-label="Stage">${[1, 2].map((n) => `<button class="stage-btn${(Number(house.stage) || 1) === n ? " on" : ""}" type="button" data-house-id="${escapeHtml(house.id)}" data-stage="${n}" aria-pressed="${(Number(house.stage) || 1) === n}"${house.is_finished ? " disabled" : ""}>Stage ${n}</button>`).join("")}</span>
          <button class="text-button house-snags-open" type="button" data-house-id="${escapeHtml(house.id)}">Snags</button>
          ${canManageHouses ? `
            <button class="text-button house-checklist-open" type="button" data-house-id="${escapeHtml(house.id)}">Edit checklist</button>
            <button class="text-button house-open" type="button" data-house-id="${escapeHtml(house.id)}">View details &amp; history <span>→</span></button>
            <button class="text-button house-finish-toggle" type="button" data-house-id="${escapeHtml(house.id)}" data-finished="false">Mark house finished</button>` : ""}
          `}
        </div>
      </article>`;
  });
  const teams = groupedTeamLocations(
    [...new Set(data.locations)],
    data.team_house_counts || {},
  );
  const teamQuery = $("#team-search").value.trim().toLocaleLowerCase();
  const matchingTeams = teams.filter((location) =>
    location.name.toLocaleLowerCase().includes(teamQuery),
  );
  const teamCards = matchingTeams.map((team) => {
    const name = team.name;
    const countsAvailable = Boolean(data.team_house_counts);
    const houseCounts = {
      open: team.openHouseIds.length,
      closed: team.closedHouseIds.length,
    };
    const teamLocations = escapeHtml(JSON.stringify(team.locations));
    return `
      <article class="team-card">
        <div class="house-top"><span class="house-avatar"><svg class="team-avatar-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="9" cy="8" r="3"></circle><path d="M3.5 19v-1.2a5.5 5.5 0 0 1 11 0V19z"></path><path d="M16 5.3a3 3 0 0 1 0 5.8"></path><path d="M17 14a4.5 4.5 0 0 1 3.5 4.4V19"></path></svg></span><div class="house-meta"><h2>${escapeHtml(name)}</h2></div></div>
        <div class="team-house-counts">
          <button class="team-house-count team-house-count-open" type="button" data-team-locations="${teamLocations}" data-house-status="open" ${!countsAvailable || !houseCounts.open ? "disabled" : ""} aria-label="Show ${houseCounts.open} open houses for ${escapeHtml(name)}">
            <strong>${houseCounts.open}</strong><span>Open houses</span>
          </button>
          <button class="team-house-count team-house-count-closed" type="button" data-team-locations="${teamLocations}" data-house-status="closed" ${!countsAvailable || !houseCounts.closed ? "disabled" : ""} aria-label="Show ${houseCounts.closed} closed houses for ${escapeHtml(name)}">
            <strong>${houseCounts.closed}</strong><span>Closed houses</span>
          </button>
        </div>
      </article>`;
  });
  $("#houses-section").classList.toggle(
    "hidden",
    state.housesTeamsTab !== "houses",
  );
  $("#teams-section").classList.toggle(
    "hidden",
    state.housesTeamsTab !== "teams",
  );
  $("#houses-tab").classList.toggle(
    "active",
    state.housesTeamsTab === "houses",
  );
  $("#houses-tab").setAttribute(
    "aria-selected",
    String(state.housesTeamsTab === "houses"),
  );
  $("#houses-tab").tabIndex = state.housesTeamsTab === "houses" ? 0 : -1;
  $("#teams-tab").classList.toggle(
    "active",
    state.housesTeamsTab === "teams",
  );
  $("#teams-tab").setAttribute(
    "aria-selected",
    String(state.housesTeamsTab === "teams"),
  );
  $("#teams-tab").tabIndex = state.housesTeamsTab === "teams" ? 0 : -1;
  $("#teams-list").innerHTML = teamCards.join("");
  $("#teams-list").classList.toggle(
    "team-grid-scrollable",
    matchingTeams.length > 6,
  );
  $("#teams-no-results").classList.toggle(
    "hidden",
    !teamQuery || matchingTeams.length > 0,
  );
  const housesList = $("#houses-list");
  delete housesList.dataset.planHtml;
  const listScroll = housesList.scrollTop;
  const pageScroll = window.scrollY;
  const houseBodyScroll = new Map(
    [...housesList.querySelectorAll(".house-body[data-house-id]")].map((body) => [
      body.dataset.houseId,
      body.scrollTop,
    ]),
  );
  housesList.innerHTML = houseCards.join("");
  housesList.classList.remove("house-grid-scrollable");
  housesList.querySelectorAll(".house-body[data-house-id]").forEach((body) => {
    const scrollTop = houseBodyScroll.get(body.dataset.houseId);
    if (scrollTop !== undefined) body.scrollTop = scrollTop;
  });
  // Keep the view exactly where it was when a card is re-rendered in place.
  housesList.scrollTo({ top: listScroll, behavior: "instant" });
  if (window.scrollY !== pageScroll) window.scrollTo({ top: pageScroll, behavior: "instant" });
  $("#houses-list-heading").textContent = visibleStatus === "closed"
      ? "Closed houses"
      : visibleStatus === "active"
        ? "Active houses"
        : "Houses";
  $("#house-team-filter").classList.toggle("hidden", !teamFilter && !visibleStatus);
  const filterLabel = $("#house-team-filter-label");
  filterLabel.className = `house-team-filter-label filter-status-${visibleStatus}`;
  if (teamFilter) {
    const teamName = formatLocation(teamFilter.location);
    filterLabel.textContent =
      `Showing ${teamFilter.status === "closed" ? "closed" : "active"} houses for ${teamName}`;
  } else {
    filterLabel.textContent = `Showing ${visibleStatus} houses`;
  }
  $("#houses-empty").classList.toggle(
    "hidden",
    data.house_list.length > 0 || teams.length > 0,
  );
  $("#houses-no-results").classList.toggle(
    "hidden",
    (!query && !teamFilter && !statusFilter) || matchingHouses.length > 0,
  );
}

async function openHouseChecklist(houseId) {
  if (state.user?.role !== "admin") return;
  const requestId = ++state.houseChecklistRequest;
  const form = $("#house-checklist-form");
  form.reset();
  form.dataset.houseId = houseId;
  $("#house-checklist-title").textContent = `${houseId} checklist`;
  const button = $("button[type='submit']", form);
  button.disabled = true;
  $("#house-checklist-dialog").showModal();
  try {
    const checklist = await request(
      `/api/house-checklist/${encodeURIComponent(houseId)}`,
    );
    if (requestId !== state.houseChecklistRequest) return;
    for (const [name, value] of Object.entries({
      scaffolding_status: checklist.scaffolding_status,
      esb_unclipped:
        checklist.esb_unclipped,
      plumber_status: checklist.plumber_status,
    })) {
      if (value !== null) {
        $(
          `input[name="${name}"][value="${value}"]`,
          form,
        ).checked = true;
      }
    }
  } catch (error) {
    if (requestId !== state.houseChecklistRequest) return;
    $("#house-checklist-dialog").close();
    showToast(error.message, true);
  } finally {
    if (requestId === state.houseChecklistRequest) button.disabled = false;
  }
}

async function submitHouseChecklist(event) {
  event.preventDefault();
  if (state.user?.role !== "admin") {
    showToast("Administrator access is required to edit house checklists.", true);
    return;
  }
  const form = event.currentTarget;
  const button = $("button[type='submit']", form);
  const values = new FormData(form);
  button.disabled = true;
  try {
    await request("/api/house-checklist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        house_id: form.dataset.houseId,
        scaffolding_status: values.get("scaffolding_status"),
        esb_unclipped: values.get("esb_unclipped"),
        plumber_status: values.get("plumber_status"),
      }),
    });
    $("#house-checklist-dialog").close();
    showToast(`Checklist for ${form.dataset.houseId} saved.`);
    try {
      await refresh();
    } catch (error) {
      showToast(
        `Checklist was saved, but the house cards could not refresh: ${error.message}`,
        true,
      );
    }
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function renderHouseDetail(result) {
  const { house, movements } = result;
  const statusLabel = {
    not_started: "Not started",
    ready: "Ready for start",
    active: "Active",
    closed: "Closed",
  }[house.status] || (house.is_finished ? "Closed" : "Active");
  $("#house-detail-title").textContent = house.id;
  const detailStatus = $("#house-detail-status");
  detailStatus.textContent = statusLabel;
  detailStatus.className = `status-pill house-detail-status ${
    house.status === "ready" ? "ready"
      : house.status === "active" ? "active"
        : house.status === "closed" || house.is_finished ? "finished"
          : "pending"
  }`;
  $("#house-detail-subtitle").textContent =
    [house.owner_name, house.employee_name, house.postcode, house.address]
      .filter(Boolean)
      .join(" · ") || "House details have not been recorded.";
  const externalWallMeasure = house.seai_details?.Measures?.find((measure) =>
    /external wall insulation/i.test(String(measure.Name || "")),
  );
  const ewiArea = String(
    [
      externalWallMeasure?.["Survey Qty including Variation"],
      externalWallMeasure?.["Survey Quantity"],
    ].map((value) => String(value ?? "").trim()).find(Boolean) || "",
  );
  const surveyPerimeter = house.survey_perimeter_m ?? house.perimeter_m;
  const statusClass = {
    not_started: "pending",
    ready: "ready",
    active: "active",
    closed: "finished",
  }[house.status] || "pending";
  $("#house-detail-facts").innerHTML = [
    ["Owner", house.owner_name],
    ["Assigned employee", house.employee_name],
    ["Postcode / Eircode", house.postcode],
    ["Address", house.address],
    ["EWI area", ewiArea ? `${ewiArea} m²` : ""],
    ["Perimeter", surveyPerimeter == null ? "" : `${formatQuantity(surveyPerimeter)} m`],
    ["Status", statusLabel],
  ]
    .map(
      ([label, value]) => {
        const displayValue = label === "Status"
          ? `<span class="status-pill ${statusClass}">${escapeHtml(statusLabel)}</span>`
          : `<strong>${escapeHtml(value || "Not recorded")}</strong>`;
        return `
        <article class="card house-detail-fact${label === "Status" ? " house-detail-fact-status" : ""}">
          <div class="house-stock-title">${escapeHtml(label)}</div>
          ${displayValue}
        </article>`;
      },
    )
    .join("");

  const checklistItems = [
    [
      "Scaffolding",
      house.scaffolding_status,
      { done: "Done", not_done: "Not done", not_necessary: "Not necessary" },
    ],
    [
      "ESB work",
      house.esb_unclipped,
      { yes: "Done", no: "Not done", not_esb: "Not necessary" },
    ],
    [
      "Plumber work",
      house.plumber_status,
      { done: "Done", not_done: "Not done", not_necessary: "Not necessary" },
    ],
  ];
  const checklistHasValues = checklistItems.some(([, value]) => value !== null && value !== undefined);
  const recordedChecklistCount = checklistItems.filter(
    ([, value]) => value !== null && value !== undefined,
  ).length;
  $("#house-detail-checklist-summary").textContent =
    `${recordedChecklistCount} of ${checklistItems.length} recorded`;
  $("#house-detail-checklist").innerHTML = checklistHasValues
    ? checklistItems
        .map(([label, value, labels]) => {
          const tone = value === "done" || value === "yes"
            ? "positive"
            : value === "not_done" || value === "no"
              ? "negative"
              : value === "not_necessary" || value === "not_esb"
                ? "unnecessary"
                : "unset";
          return `<div class="checklist-card-row"><span>${escapeHtml(label)}</span><strong class="checklist-value checklist-value-${tone}">${escapeHtml(labels[value] || "Not set")}</strong></div>`;
        })
        .join("")
    : `<p class="muted">No pre-start checklist has been recorded.</p>`;
  const preStart = house.status === "ready" || house.status === "not_started";
  $("#house-detail-snag-section").classList.toggle("hidden", preStart);
  $("#house-detail-records-title").textContent = preStart
    ? "Notes"
    : "Notes & snag history";
  $('[data-house-detail-target="house-detail-records-card"]').textContent =
    preStart ? "Notes" : "Notes & snags";

  const noteRows = [
    ["House note", house.notes],
    ["Survey note", house.survey_notes],
  ].filter(([, text]) => text);
  $("#house-detail-notes").innerHTML = noteRows.length
    ? noteRows
        .map(
          ([label, text]) => `
            <article class="house-detail-note">
              <strong>${escapeHtml(label)}</strong>
              <p>${escapeHtml(text)}</p>
            </article>`,
        )
        .join("")
    : `<p class="muted">No notes recorded.</p>`;
  const snags = house.snags || [];
  $("#house-detail-snags").innerHTML = snags.length
    ? snags
        .map((snag) => {
          const createdAt = Number(snag.created_at);
          const date = Number.isFinite(createdAt) && createdAt > 0
            ? new Date(createdAt * 1000).toLocaleDateString()
            : "Date not recorded";
          return `
            <article class="house-detail-snag${snag.done ? " done" : ""}">
              <span class="house-detail-snag-status">${snag.done ? "Done" : "Open"}</span>
              <p>${escapeHtml(snag.text)}</p>
              <small>Added ${escapeHtml(date)}</small>
            </article>`;
        })
        .join("")
    : `<p class="muted">No snags recorded.</p>`;

  const used = new Map();
  movements
    .filter((item) => item.kind === "use")
    .forEach((item) => {
      const total = used.get(item.material_id) || {
        name: item.material,
        qty: 0,
        unit:
          state.dashboard.materials.find(
            (material) => material.id === item.material_id,
          )?.unit || "",
      };
      total.qty += item.qty;
      used.set(item.material_id, total);
    });
  const usedMaterials = [...used.values()].sort((a, b) =>
    a.name.localeCompare(b.name),
  );
  $("#house-detail-usage-card").classList.toggle(
    "hidden",
    !house.is_finished,
  );
  $("#house-detail-usage").innerHTML = usedMaterials.length
    ? usedMaterials
        .map(
          (item) => `
            <div class="balance-row">
              <span>${escapeHtml(item.name)}</span>
              <span>${escapeHtml(formatQuantity(item.qty, item.unit))}</span>
            </div>`,
        )
        .join("")
    : `<div class="empty-state"><strong>No materials were left to close out</strong><p>No material usage or remaining stock was recorded for this house.</p></div>`;

  const held = state.dashboard.materials.flatMap((material) => {
    const quantity = material.balances[`house:${house.id}`] || 0;
    return Math.abs(quantity) > 1e-9
      ? [{ name: material.name, qty: quantity, unit: material.unit }]
      : [];
  });
  $("#house-detail-stock").innerHTML = held.length
    ? held
        .map(
          (item) => `
            <div class="balance-row">
              <span>${escapeHtml(item.name)}</span>
              <span class="${item.qty < 0 ? "qty-negative" : ""}">${escapeHtml(formatQuantity(item.qty, item.unit))}</span>
            </div>`,
        )
        .join("")
    : `<div class="empty-state"><strong>No material balance recorded</strong><p>Current material quantities for this house will appear here.</p></div>`;

  $("#house-movement-history").innerHTML = movements
    .map(
      (item) => `
        <tr>
          <td>${escapeHtml(item.date)}<small class="movement-recorded-at">Recorded ${escapeHtml(formatAuditDateTime(item.recorded_at))}</small></td>
          <td><span class="status-pill ${movementStatusClass(item)}">${escapeHtml(movementDisplayLabel(item))}</span></td>
          <td>${escapeHtml(item.material)}</td>
          <td class="qty-cell">${escapeHtml(formatQuantity(item.qty))}</td>
          <td>${escapeHtml(formatLocation(item.source))}</td>
          <td>${escapeHtml(formatLocation(item.destination))}</td>
          <td>${escapeHtml([item.person, item.department, item.team].filter(Boolean).join(" · ") || "—")}</td>
          <td>${escapeHtml(item.notes || "—")}</td>
        </tr>`,
    )
    .join("");
  $("#house-history-empty").classList.toggle("hidden", movements.length > 0);
  $(".house-history-card .table-wrap").classList.toggle(
    "hidden",
    movements.length === 0,
  );
}

async function loadHouseDetail(houseId) {
  const requestId = ++state.houseDetailRequest;
  try {
    const result = await request(`/api/houses/${encodeURIComponent(houseId)}`);
    if (
      requestId !== state.houseDetailRequest ||
      state.viewingHouseId !== houseId
    ) {
      return;
    }
    renderHouseDetail(result);
  } catch (error) {
    if (requestId !== state.houseDetailRequest) return;
    showToast(error.message, true);
    showHousesOverview();
  }
}

function openHouseDetail(houseId) {
  state.houseMapMode = false;
  $("#house-map-view").classList.add("hidden");
  state.viewingHouseId = houseId;
  $("#houses-overview").classList.add("hidden");
  $("#house-detail").classList.remove("hidden");
  $("#house-detail-title").textContent = houseId;
  $("#house-detail-subtitle").textContent = "Loading house record…";
  $("#house-detail-facts").innerHTML = "";
  $("#house-detail-checklist").innerHTML = "";
  $("#house-detail-checklist-summary").textContent = "";
  $("#house-detail-status").textContent = "";
  $("#house-detail-stock").innerHTML = "";
  $("#house-detail-notes").innerHTML = "";
  $("#house-detail-snags").innerHTML = "";
  $("#house-movement-history").innerHTML = "";
  $("#house-detail-usage-card").classList.add("hidden");
  $("#house-history-empty").classList.add("hidden");
  $(".house-history-card .table-wrap").classList.remove("hidden");
  loadHouseDetail(houseId);
}

function showHousesOverview() {
  state.houseMapMode = false;
  state.viewingHouseId = "";
  state.houseDetailRequest += 1;
  $("#surveyor-room").classList.add("hidden");
  $("#house-map-view").classList.add("hidden");
  $("#house-detail").classList.add("hidden");
  $("#houses-overview").classList.remove("hidden");
}

function renderMovementTable() {
  const query = $("#movement-search").value.trim().toLocaleLowerCase();
  const movements = state.movements.filter((item) =>
    [
      item.date,
      item.material,
      movementDisplayLabel(item),
      item.source,
      item.destination,
      item.house_id,
      item.house_owner,
      item.house_employee,
      item.house_postcode,
      item.person,
      item.department,
      item.team,
      item.notes,
    ]
      .join(" ")
      .toLocaleLowerCase()
      .includes(query),
  );
  $("#movement-table").innerHTML = movements
    .map(
      (item) => `
    <tr>
      <td>${escapeHtml(item.date)}<small class="movement-recorded-at">Recorded ${escapeHtml(formatAuditDateTime(item.recorded_at))}</small></td>
      <td><span class="status-pill ${movementStatusClass(item)}">${escapeHtml(movementDisplayLabel(item))}</span></td>
      <td>${escapeHtml(item.material)}</td>
      <td class="qty-cell">${escapeHtml(formatQuantity(item.qty))}</td>
      <td>${escapeHtml(formatLocation(item.source))}</td>
      <td>${escapeHtml(formatLocation(item.destination))}</td>
      <td>${escapeHtml([item.house_id, item.house_owner, item.house_employee].filter(Boolean).join(" · ") || "—")}</td>
      <td>${escapeHtml([item.person, item.department, item.team].filter(Boolean).join(" · ") || "—")}</td>
    </tr>`,
    )
    .join("");
  const empty = state.movements.length === 0;
  const hasQuery = Boolean(query);
  $("#movements-empty").classList.toggle("hidden", !empty || hasQuery);
  $(".page[data-page='movements'] .table-wrap").classList.toggle(
    "hidden",
    empty && !hasQuery,
  );
  if (hasQuery && movements.length === 0) {
    $("#movement-table").innerHTML =
      `<tr><td colspan="8">No movements match that search.</td></tr>`;
    $(".page[data-page='movements'] .table-wrap").classList.remove("hidden");
  }
}

function refreshMovementOptions(preserveMovementLines = false) {
  const materials = state.dashboard.materials;
  const houses = state.dashboard.house_list;
  $$(".site-reference").forEach((select) => {
    const selectedSite = select.value;
    select.innerHTML = `<option value="">Choose a site</option>${houses
      .map(
        (house) =>
          `<option value="${escapeHtml(house.id)}">${escapeHtml([house.id, house.owner_name, house.employee_name].filter(Boolean).join(" · "))}</option>`,
      )
      .join("")}`;
    if (houses.some((house) => house.id === selectedSite))
      select.value = selectedSite;
  });
  const stocktakeMaterial = $("#stocktake-material");
  const selectedStocktakeMaterial = stocktakeMaterial.value;
  stocktakeMaterial.innerHTML =
    materials
      .map(
        (item) =>
          `<option value="${item.id}">${escapeHtml(item.name)}${item.code ? ` · ${escapeHtml(item.code)}` : ""}</option>`,
      )
      .join("") || `<option value="">Add or import a material first</option>`;
  if (materials.some((item) => String(item.id) === selectedStocktakeMaterial)) {
    stocktakeMaterial.value = selectedStocktakeMaterial;
  }
  const stocktakeLocation = $("#stocktake-location");
  const selectedStocktakeLocation = stocktakeLocation.value;
  const locations = state.dashboard.locations.filter(
    (location) => location !== "consumed",
  );
  stocktakeLocation.innerHTML = locations
    .map(
      (location) =>
        `<option value="${escapeHtml(location)}">${escapeHtml(formatLocation(location))}</option>`,
    )
    .join("");
  if (locations.includes(selectedStocktakeLocation)) {
    stocktakeLocation.value = selectedStocktakeLocation;
  }
  if (preserveMovementLines) {
    updateSelectedMaterialBalances();
  } else {
    renderMaterialLines();
  }
  renderHouseLiveStock();
}

function openStocktake(materialId = null) {
  const form = $("#stocktake-form");
  form.reset();
  const title = $("#stocktake-title");
  const intro = $("#stocktake-intro");
  const material = state.dashboard.materials.find(
    (item) => item.id === materialId,
  );
  if (material) {
    $("#stocktake-material").value = String(material.id);
    $("#stocktake-location").value = "warehouse";
    $("#stocktake-quantity").value = material.warehouse_qty ?? "";
    title.textContent = `Edit ${material.name} warehouse stock`;
    intro.textContent =
      "Enter the physical quantity. This saves an audited count and keeps the movement history.";
  } else {
    title.textContent = "Count stock";
    intro.textContent =
      "Save a physical count as a checkpoint. Future movements will be calculated from this count.";
  }
  $("#stocktake-dialog").showModal();
}

function openHouseDialog(house = null) {
  $("#house-form").reset();
  $("#seai-results").classList.add("hidden");
  $("#seai-info").classList.add("hidden");
  $(".seai-search").classList.toggle("hidden", Boolean(house));
  if (house) fillSeaiHouse(house);
  $("#house-dialog").showModal();
  if (!house) $("#seai-search").focus();
}

let seaiTimer = 0;
async function searchSeaiHouses() {
  const box = $("#seai-results");
  const q = $("#seai-search").value.trim();
  try {
    const { houses } = await request(`/api/calculator/houses?status=ready&q=${encodeURIComponent(q)}`);
    box.innerHTML = houses.length
      ? houses
          .map(
            (h, i) =>
              `<button type="button" data-index="${i}"><span><strong>${escapeHtml(h.id)}</strong> ${escapeHtml(h.owner_name || "")}<br><small>${escapeHtml([h.address, h.postcode].filter(Boolean).join(" · "))}</small></span><small>${escapeHtml(h.status.replace("_", " "))}</small></button>`,
          )
          .join("")
      : '<div class="seai-none">No matching houses ready for start</div>';
    box._houses = houses;
    box.classList.remove("hidden");
  } catch (error) {
    showToast(error.message, true);
  }
}

function pickSeaiHouse(event) {
  const button = event.target.closest("button[data-index]");
  if (!button) return;
  fillSeaiHouse($("#seai-results")._houses[Number(button.dataset.index)]);
}

function fillSeaiHouse(h) {
  const form = $("#house-form");
  form.elements.id.value = h.id;
  form.elements.postcode.value = h.postcode || "";
  form.elements.owner_name.value = h.owner_name || "";
  form.elements.perimeter_m.value = h.survey?.perimeter_m ?? "";
  if (h.employee_name) form.elements.employee_name.value = h.employee_name;
  const externalWallMeasure = h.seai_details?.Measures?.find((measure) =>
    /external wall insulation/i.test(String(measure.Name || "")),
  );
  const ewiArea = String(
    externalWallMeasure?.["Survey Qty including Variation"] ?? "",
  ).trim();
  const surveyPerimeter = h.survey?.perimeter_m;
  $("#seai-info").innerHTML = [
    ["Address", h.address || "—"],
    ["EWI Area", ewiArea ? `${ewiArea} m²` : "—"],
    [
      "Perimeter",
      surveyPerimeter == null || surveyPerimeter === ""
        ? "—"
        : `${formatQuantity(surveyPerimeter)} m`,
    ],
  ]
    .map(([k, v]) => `<span>${k}: <b>${escapeHtml(String(v))}</b></span>`)
    .join("");
  $("#seai-info").classList.remove("hidden");
  $("#seai-results").classList.add("hidden");
  $("#seai-search").value = "";
}

function renderAll(preserveMovementLines = false) {
  renderOverview();
  renderInventory();
  renderHouses();
  renderMovementTable();
  refreshMovementOptions(preserveMovementLines);
}

async function refresh(preserveMovementLines = false) {
  Object.keys(planCache).forEach((mode) => planStale.add(mode));
  const search = $("#movement-search")?.value.trim() || "";
  const isAssign = state.user?.role === "assign";
  const [dashboard, ledger] = await Promise.all([
    request("/api/dashboard"),
    isAssign
      ? Promise.resolve({ movements: [] })
      : request(
          `/api/movements${search ? `?q=${encodeURIComponent(search)}` : ""}`,
        ),
  ]);
  state.dashboard = dashboard;
  state.movements = ledger.movements;
  renderAll(preserveMovementLines);
  loadCountyWeather();
  if (state.viewingHouseId) {
    await loadHouseDetail(state.viewingHouseId);
  }
}

async function searchMovementHistory() {
  const requestId = ++movementSearchRequest;
  const query = $("#movement-search").value.trim();
  try {
    const result = await request(
      `/api/movements${query ? `?q=${encodeURIComponent(query)}` : ""}`,
    );
    if (requestId !== movementSearchRequest) return;
    state.movements = result.movements;
    renderMovementTable();
  } catch (error) {
    if (requestId === movementSearchRequest) {
      showToast(`Movement search failed: ${error.message}`, true);
    }
  }
}

function openMovementDialog() {
  Object.keys(requestCache).forEach((key) => delete requestCache[key]);
  $("#movement-form").reset();
  state.batchLines = [];
  state.selectedHouseId = "";
  renderMaterialLines();
  renderHouseLiveStock();
  $("#movement-date").value = localDate();
  $("#movement-kind").dispatchEvent(new Event("change"));
  $("#extra-delivery").checked = false;
  $("#movement-dialog").showModal();
}

function updateMovementFields() {
  const kind = $("#movement-kind").value;
  $$(".movement-fields").forEach((fields) => {
    fields.classList.toggle("hidden", fields.dataset.for !== kind);
  });
  $("#movement-house-id").required = ["pickup", "return"].includes(kind);
  const isTransfer = kind === "transfer";
  [
    "#primary-house-search",
    "#primary-postcode-field",
    "#primary-owner-field",
    "#primary-employee-field",
    "#house-live-stock",
  ].forEach((selector) => $(selector).classList.toggle("hidden", isTransfer));
  $("#house-hint").textContent = ["pickup", "return"].includes(kind)
    ? "Required for this movement. Search matches house, owner and assigned employee."
    : "Optional for this movement. Search matches house, owner and assigned employee.";
  if (state.user?.role === "employee") {
    $("#house-hint").textContent =
      "Employees can record movements only for existing houses.";
  }
  $$(".location-kind").forEach((select) =>
    select.dispatchEvent(new Event("change")),
  );
  renderHouseRequest();
}

function updateTransferKind(select) {
  const block = select.closest(".transfer-block");
  const kind = select.value;
  const transferActive = $("#movement-kind").value === "transfer";
  const teamReference = $(".transfer-team-reference", block);
  const houseReference = $(".transfer-house-reference", block);
  teamReference.classList.toggle("hidden", kind !== "team");
  houseReference.classList.toggle("hidden", kind !== "house");
  $("input", teamReference).required = transferActive && kind === "team";
  $("select", houseReference).required = transferActive && kind === "house";
  const department = $(".transfer-department", block);
  if (department) department.classList.toggle("hidden", kind !== "team");
}

function formDataForMovement() {
  const kind = $("#movement-kind").value;
  const activeFields = $(`.movement-fields[data-for="${kind}"]`);
  const data = {
    kind,
    date: $("#movement-date").value,
    department: $("#movement-form [name='department']").value,
    house_id: $("#movement-house-id").value.trim(),
    postcode: $("#movement-postcode").value.trim(),
    owner_name: $("#movement-owner").value.trim(),
    employee_name: $("#movement-employee").value.trim(),
    notes: $("#movement-form [name='notes']").value,
    items: state.batchLines.map((line) => ({
      material_id: line.material_id,
      qty: line.qty,
    })),
  };
  if (activeFields) {
    $$("input, select, textarea", activeFields).forEach((field) => {
      if (field.name) data[field.name] = field.value;
    });
  }
  if (kind === "pickup") {
    data.is_extra_delivery = $("#extra-delivery").checked;
  }
  if (kind === "transfer") {
    data.house_id = "";
    activeFields.querySelectorAll(".transfer-block").forEach((block) => {
      const locationKind = $(".location-kind", block).value;
      if (locationKind === "house") {
        const siteSelect = $(".site-reference", block);
        const referenceName =
          block.querySelector(".location-kind").name === "source_kind"
            ? "source_reference"
            : "destination_reference";
        data[referenceName] = siteSelect.value;
      }
    });
    delete data.source_site;
    delete data.destination_site;
  }
  return data;
}

async function submitMovement(event) {
  event.preventDefault();
  if (!state.batchLines.length) {
    showToast("Add at least one material to this movement.", true);
    $("#movement-material-search").focus();
    return;
  }
  if (state.batchLines.some((line) => !line.qty || Number(line.qty) <= 0)) {
    showToast(
      "Enter a quantity greater than zero for every selected material.",
      true,
    );
    return;
  }
  const button = $("button[type='submit']", event.currentTarget);
  button.disabled = true;
  try {
    const result = await request("/api/movements/batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(formDataForMovement()),
    });
    $("#movement-dialog").close();
    showToast(
      result.delivery_number
        ? `Delivery #${result.delivery_number} recorded.`
        : `${result.ids.length} material${result.ids.length === 1 ? "" : "s"} recorded.`,
    );
    try {
      await refresh();
    } catch (error) {
      showToast(
        `Movement was saved, but the tracker could not refresh: ${error.message}`,
        true,
      );
    }
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function submitMaterial(event) {
  event.preventDefault();
  const formElement = event.currentTarget;
  const button = $("button[type='submit']", formElement);
  button.disabled = true;
  const form = new FormData(formElement);
  try {
    await request("/api/materials", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.fromEntries(form)),
    });
    $("#material-dialog").close();
    formElement.reset();
    showToast("Material saved.");
    try {
      await refresh();
    } catch (error) {
      showToast(
        `Material was saved, but the inventory could not refresh: ${error.message}`,
        true,
      );
    }
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function validateHouseActivation(formElement) {
  const employeeField = formElement.elements.employee_name;
  const perimeterField = formElement.elements.perimeter_m;
  employeeField.value = employeeField.value.trim();
  employeeField.setCustomValidity(
    employeeField.value ? "" : "Assign an employee before opening the calculator.",
  );
  const perimeter = Number(perimeterField.value);
  perimeterField.setCustomValidity(
    Number.isFinite(perimeter) && perimeter > 0 && perimeter <= 10000
      ? ""
      : "Enter a surveyed perimeter greater than 0 and no more than 10,000 metres.",
  );
  return formElement.reportValidity();
}

async function submitHouse(event) {
  event.preventDefault();
  const formElement = event.currentTarget;
  if (!validateHouseActivation(formElement)) return;
  const button = $("button[type='submit']", formElement);
  button.disabled = true;
  const form = new FormData(formElement);
  try {
    await request("/api/houses", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.fromEntries(form)),
    });
    const savedId = String(form.get("id") || "").trim();
    $("#house-dialog").close();
    formElement.reset();
    window.location.href = `/calculator?house=${encodeURIComponent(savedId)}`;
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

const pendingHouseStatus = new Set();

async function toggleHouseFinished(event) {
  const mapButton = event.target.closest(".house-map-open");
  if (mapButton) {
    openHouseMap(mapButton.dataset.houseId);
    return;
  }
  const moreButton = event.target.closest(".stock-more-btn");
  if (moreButton) {
    const id = moreButton.dataset.houseId;
    const open = !expandedStock.has(id);
    if (open) expandedStock.add(id);
    else expandedStock.delete(id);
    const list = moreButton.previousElementSibling;
    list.classList.toggle("expanded", open);
    moreButton.textContent = open ? "Show less" : `… Show more (${moreButton.dataset.extra})`;
    if (!open) moreButton.closest(".house-body").scrollTop = 0;
    return;
  }
  const activateButton = event.target.closest(".seai-activate");
  if (activateButton) {
    if (state.presentation) {
      state.presentation.houseId = activateButton.dataset.houseId;
      renderPresentationGuide();
    }
    const h = findPlanHouse(activateButton.dataset.houseId);
    if (h) {
      openHouseDialog(h);
    }
    return;
  }
  const surveyButton = event.target.closest(".house-survey-open");
  if (surveyButton) {
    if (state.presentation) {
      state.presentation.houseId = surveyButton.dataset.houseId;
      renderPresentationGuide();
    }
    openHouseSurvey(surveyButton.dataset.houseId);
    return;
  }
  const readyButton = event.target.closest(".plan-ready-toggle");
  if (readyButton) {
    toggleHouseReady(readyButton.dataset.houseId, readyButton.dataset.ready === "true");
    return;
  }
  const snagOpen = event.target.closest(".house-snags-open");
  if (snagOpen) {
    openHouseSnags(snagOpen.dataset.houseId);
    return;
  }
  const snagDelete = event.target.closest(".snag-del");
  if (snagDelete) {
    saveSnag({ snag_id: Number(snagDelete.dataset.snagId), delete: true });
    return;
  }
  const snagToggle = event.target.closest(".snag-row");
  if (snagToggle) {
    saveSnag({ snag_id: Number(snagToggle.dataset.snagId), done: snagToggle.dataset.done === "true" });
    return;
  }
  const stageButton = event.target.closest(".stage-btn");
  if (stageButton) {
    const house = findAnyHouse(stageButton.dataset.houseId);
    const next = Number(stageButton.dataset.stage);
    if (house && (Number(house.stage) || 1) === next) return;
    request("/api/houses/stage", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: stageButton.dataset.houseId, stage: next }),
    })
      .then((result) => {
        if (house) {
          house.stage = next;
        }
        renderHouses();
      })
      .catch((error) => showToast(error.message, true));
    return;
  }
  const noteButton = event.target.closest(".house-note-open");
  if (noteButton) {
    openHouseNote(noteButton.dataset.houseId);
    return;
  }
  const recordButton = event.target.closest(".house-record-movement");
  if (recordButton) {
    openMovementDialog();
    $("#movement-kind").value = "pickup";
    $("#movement-kind").dispatchEvent(new Event("change"));
    $("#movement-house-id").value = recordButton.dataset.houseId;
    updateHouseSelection();
    return;
  }
  const detailButton = event.target.closest(".house-open");
  if (detailButton) {
    openHouseDetail(detailButton.dataset.houseId);
    return;
  }
  const button = event.target.closest(".house-finish-toggle");
  if (!button) return;
  const houseId = button.dataset.houseId;
  const finished =
    button.dataset.action === "closeout" || button.dataset.finished !== "true";
  if (pendingHouseStatus.has(houseId)) return;
  pendingHouseStatus.add(houseId);
  const house = state.dashboard.house_list.find((item) => item.id === houseId);
  const previous = house ? house.is_finished : null;
  const previousSummary = state.dashboard.house_summary
    ? { ...state.dashboard.house_summary }
    : null;
  // Show the new status immediately; the server call and refresh happen in the background.
  if (house && previous !== Number(finished)) {
    house.is_finished = Number(finished);
    const summary = state.dashboard.house_summary;
    if (summary) {
      summary.active += finished ? -1 : 1;
      summary.closed += finished ? 1 : -1;
    }
    state.dashboard.houses = state.dashboard.house_list.filter(
      (item) => !item.is_finished,
    ).length;
    renderOverview();
    renderHouses();
  }
  try {
    const result = await request("/api/houses/status", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: houseId, finished }),
    });
    showToast(
      finished
        ? `${houseId} marked finished; ${result.materials_closed_out} remaining material balance(s) recorded as used.`
        : result.materials_restored
          ? `${houseId} reopened; ${result.materials_restored} material balance(s) restored.`
          : `${houseId} reopened.`,
    );
    pendingHouseStatus.delete(houseId);
    if (pendingHouseStatus.size === 0) {
      try {
        await refresh();
      } catch (error) {
        showToast(
          `House status was saved, but the tracker could not refresh: ${error.message}`,
          true,
        );
      }
    }
  } catch (error) {
    pendingHouseStatus.delete(houseId);
    if (house) {
      house.is_finished = previous;
      if (previousSummary) state.dashboard.house_summary = previousSummary;
      state.dashboard.houses = state.dashboard.house_list.filter(
        (item) => !item.is_finished,
      ).length;
      renderOverview();
      renderHouses();
    }
    showToast(error.message, true);
  }
}

async function submitStocktake(event) {
  event.preventDefault();
  const formElement = event.currentTarget;
  const button = $("button[type='submit']", formElement);
  button.disabled = true;
  const form = new FormData(formElement);
  const payload = Object.fromEntries(form);
  payload.material_id = Number(payload.material_id);
  try {
    await request("/api/stocktakes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    $("#stocktake-dialog").close();
    formElement.reset();
    await refresh();
    showToast("Stocktake saved.");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function submitImport(event) {
  event.preventDefault();
  const file = $("#workbook-file").files[0];
  if (!file) {
    showToast("Choose an Excel workbook to import.", true);
    return;
  }
  const button = $("button[type='submit']", event.currentTarget);
  button.disabled = true;
  const formData = new FormData();
  formData.append("workbook", file);
  try {
    const result = await request("/api/import", {
      method: "POST",
      body: formData,
    });
    await refresh();
    const warning = result.warnings.length
      ? ` ${result.warnings.length} stock or data warning(s) need attention.`
      : "";
    showToast(
      `Imported ${result.material_count} materials and ${result.movement_count} movements.${warning}`,
    );
    if (result.warnings.length) {
      window.alert(
        `Workbook imported with items needing review:\n\n${result.warnings.join("\n")}`,
      );
    }
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function submitHouseLocations(event) {
  event.preventDefault();
  const file = $("#house-locations-file").files[0];
  if (!file) {
    showToast("Choose the approved SEAI house-location CSV.", true);
    return;
  }
  const button = $("button[type='submit']", event.currentTarget);
  button.disabled = true;
  const formData = new FormData();
  formData.append("file", file);
  try {
    const result = await request("/api/houses/import", {
      method: "POST",
      body: formData,
    });
    state.houseMapCamera = null;
    await refresh();
    showToast(
      `Imported ${result.imported} houses and updated ${result.updated}. ${result.total} provided houses are available.`,
    );
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function submitAuthentication(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const setup = form.dataset.mode === "setup";
  const values = new FormData(form);
  const pin = values.get("pin");
  if (setup && pin !== values.get("pin_confirm")) {
    $("#auth-error").textContent = "The PINs do not match.";
    $("#auth-error").classList.remove("hidden");
    return;
  }
  const payload = {
    username: values.get("username"),
    pin,
  };
  if (setup) payload.display_name = values.get("display_name");
  const button = $("#auth-submit");
  button.disabled = true;
  $("#auth-error").classList.add("hidden");
  try {
    const result = await request(
      setup ? "/api/auth/setup" : "/api/auth/login",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    );
    form.reset();
    showWorkspace(result.user);
    if (result.user.role === "surveyor") return;
    try {
      await refresh();
    } catch (error) {
      showToast(
        `Signed in, but the tracker could not load its data: ${error.message}`,
        true,
      );
      return;
    }
    showToast(
      setup ? "Administrator account created." : `Welcome, ${result.user.display_name}.`,
    );
  } catch (error) {
    $("#auth-error").textContent = error.message;
    $("#auth-error").classList.remove("hidden");
  } finally {
    button.disabled = false;
  }
}

async function loadUsers() {
  const result = await request("/api/users");
  const roleNames = {
    admin: "Admin",
    assign: "Assign",
    boss: "Boss",
    surveyor: "Surveyor",
    warehouse: "Warehouse",
    delivery: "Delivery",
    supervisor: "Supervisor",
    teamlead: "Teamlead",
    employee: "Employee (legacy)",
  };
  $("#user-list").innerHTML = result.users
    .map(
      (user) => `
        <div class="user-row">
          <span><strong>${escapeHtml(user.display_name)}</strong><small>${escapeHtml(user.username)}</small></span>
          <span>${escapeHtml(roleNames[user.role] || user.role)}${user.is_active ? "" : " (Disabled)"}</span>
        </div>`,
    )
    .join("");
}

async function submitEmployee(event) {
  event.preventDefault();
  const formElement = event.currentTarget;
  const values = Object.fromEntries(new FormData(formElement));
  if (values.pin !== values.pin_confirm) {
    showToast("The PINs do not match.", true);
    return;
  }
  delete values.pin_confirm;
  const button = $("button[type='submit']", formElement);
  button.disabled = true;
  try {
    await request("/api/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(values),
    });
    formElement.reset();
    await loadUsers();
    showToast(`${values.role[0].toUpperCase()}${values.role.slice(1)} account created.`);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function submitPinChange(event) {
  event.preventDefault();
  const formElement = event.currentTarget;
  const button = $("button[type='submit']", formElement);
  button.disabled = true;
  try {
    const values = Object.fromEntries(new FormData(formElement));
    if (values.new_pin !== values.new_pin_confirm) {
      showToast("The new PINs do not match.", true);
      return;
    }
    delete values.new_pin_confirm;
    await request("/api/auth/pin", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(values),
    });
    $("#pin-dialog").close();
    $("#account-dialog").close();
    formElement.reset();
    showToast("PIN updated. Other sessions have been signed out.");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function signOut() {
  try {
    await request("/api/auth/logout", { method: "POST" });
    state.user = null;
    state.dashboard = null;
    state.movements = [];
    $("#account-dialog").close();
    showAuthScreen(false);
    $("#auth-form").reset();
    $("#auth-error").classList.add("hidden");
    $("#auth-pin").focus();
  } catch (error) {
    showToast(error.message, true);
  }
}

function bindEvents() {
  $("#theme-toggle").addEventListener("click", () =>
    setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"),
  );
  setTheme(document.documentElement.dataset.theme === "dark" ? "dark" : "light");
  $$("dialog").forEach((dialog) =>
    dialog.addEventListener("close", () => {
      const toast = $("#toast");
      if (toast.parentElement === dialog) {
        document.body.append(toast);
        toast.classList.remove("in-dialog");
        toast.setAttribute("role", "status");
        toast.setAttribute("aria-live", "polite");
      }
    }),
  );
  $("#start-presentation").addEventListener("click", startPresentation);
  $("#start-navigation-tour").addEventListener("click", startNavigationTour);
  $("#close-navigation-tour").addEventListener("click", closeNavigationTour);
  $("#navigation-tour-previous").addEventListener("click", () =>
    moveNavigationTour(-1),
  );
  $("#navigation-tour-next").addEventListener("click", () =>
    moveNavigationTour(1),
  );
  $("#account-button").addEventListener("click", () =>
    $("#account-dialog").showModal(),
  );
  $("#account-manage-users").addEventListener("click", async () => {
    $("#account-dialog").close();
    try {
      await loadUsers();
      $("#users-dialog").showModal();
    } catch (error) {
      showToast(error.message, true);
    }
  });
  $("#account-change-pin").addEventListener("click", () => {
    $("#account-dialog").close();
    $("#pin-dialog").showModal();
  });
  $("#account-sign-out").addEventListener("click", signOut);
  $("#auth-form").addEventListener("submit", submitAuthentication);
  $("#user-form").addEventListener("submit", submitEmployee);
  $("#pin-form").addEventListener("submit", submitPinChange);
  $("#close-presentation").addEventListener("click", closePresentation);
  $("#minimize-presentation").addEventListener("click", minimizePresentation);
  $("#resume-presentation").addEventListener("click", resumePresentation);
  $("#presentation-previous").addEventListener("click", () =>
    movePresentation(-1),
  );
  $("#presentation-next").addEventListener("click", () => movePresentation(1));
  $("#presentation-do-action").addEventListener(
    "click",
    openPresentationAction,
  );
  $$(".brand, .mobile-brand").forEach((link) =>
    link.addEventListener("click", goToHomeView),
  );
  $$(".nav-item[data-tab]").forEach((button) =>
    button.addEventListener("click", () => setTab(button.dataset.tab)),
  );
  $("#go-overview").addEventListener("click", goToHomeView);
  $("#weather-refresh").addEventListener("click", loadCountyWeather);
  $$("[data-house-view]").forEach((tile) =>
    tile.addEventListener("click", () => {
      state.houseTeamFilter = null;
      state.houseStatusFilter = tile.dataset.houseView || null;
      state.housesTeamsTab = "houses";
      $("#house-search").value = "";
      if (tile.dataset.houseView === "") {
        state.houseMapMode = true;
        state.selectedMapHouseId = "";
        state.houseMapCamera = null;
        setTab("houses");
        renderHouses();
        return;
      }
      state.houseMapMode = false;
      showHousesOverview();
      setTab("houses");
      renderHouses();
    }),
  );
  $$("#assign-houses-dashboard [data-assign-house-filter]").forEach((tile) =>
    tile.addEventListener("click", () => {
      state.houseTeamFilter = null;
      state.houseStatusFilter = tile.dataset.assignHouseFilter;
      state.housesTeamsTab = "houses";
      $("#house-search").value = "";
      if (state.houseStatusFilter === "seai") {
        state.houseMapMode = true;
        state.selectedMapHouseId = "";
        state.houseMapCamera = null;
        renderHouses();
        return;
      }
      state.houseMapMode = false;
      renderHouses();
    }),
  );
  $$("[data-goto]").forEach((button) =>
    button.addEventListener("click", () => setTab(button.dataset.goto)),
  );
  ["#new-movement-top", "#new-movement", "#empty-record-movement"].forEach(
    (selector) => {
      $(selector)?.addEventListener("click", openMovementDialog);
    },
  );
  $("#open-material").addEventListener("click", () =>
    $("#material-dialog").showModal(),
  );
  $("#open-house").addEventListener("click", () => openHouseDialog());
  $("#back-to-houses").addEventListener("click", showHousesOverview);
  $$(".house-detail-nav [data-house-detail-target]").forEach((button) => {
    button.addEventListener("click", () => {
      document.getElementById(button.dataset.houseDetailTarget)?.scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
    });
  });
  $("#open-stocktake").addEventListener("click", () => openStocktake());
  $("#inventory-table").addEventListener("click", (event) => {
    const button = event.target.closest(".stock-edit");
    if (button) openStocktake(Number(button.dataset.materialId));
  });
  $("#open-import-empty").addEventListener("click", () => {
    $("#workbook-file").focus();
    $(".import-card").scrollIntoView({ behavior: "smooth", block: "center" });
  });
  $$(".close-dialog").forEach((button) =>
    button.addEventListener("click", () => button.closest("dialog").close()),
  );
  $("#movement-kind").addEventListener("change", updateMovementFields);
  $("#movement-material-search").addEventListener(
    "input",
    renderMaterialSuggestions,
  );
  $("#movement-material-search").addEventListener(
    "focus",
    renderMaterialSuggestions,
  );
  $("#movement-material-search").addEventListener(
    "keydown",
    handleMaterialSearchKey,
  );
  $("#add-material-line").addEventListener("click", addMaterialFromSearch);
  $("#material-suggestions").addEventListener(
    "click",
    selectMaterialSuggestion,
  );
  $("#movement-lines").addEventListener("input", updateBatchQuantity);
  $("#movement-lines").addEventListener("click", removeBatchMaterial);
  $("#movement-house-id").addEventListener("change", updateHouseSelection);
  $("#movement-house-id").addEventListener("input", updateHouseSelection);
  $("#movement-house-id").addEventListener("focus", renderHouseSuggestions);
  $("#movement-house-id").addEventListener("keydown", handleHouseSearchKey);
  $("#house-suggestions").addEventListener("click", selectHouseSuggestion);
  document.addEventListener("click", (event) => {
    if (!event.target.closest("#movement-house-id, #house-suggestions")) {
      $("#house-suggestions").classList.add("hidden");
    }
    if (
      !event.target.closest(
        "#movement-material-search, #material-suggestions, #add-material-line",
      )
    ) {
      $("#material-suggestions").classList.add("hidden");
    }
  });
  $$(".location-kind").forEach((select) =>
    select.addEventListener("change", () => {
      updateTransferKind(select);
      renderMaterialLines();
    }),
  );
  $$(".site-reference").forEach((select) =>
    select.addEventListener("change", renderMaterialLines),
  );
  $$(".transfer-team-reference input, .transfer-department input").forEach(
    (input) => {
      input.addEventListener("input", renderMaterialLines);
    },
  );
  $("#movement-form").addEventListener("submit", submitMovement);
  $("#movement-form").addEventListener(
    "invalid",
    (event) => {
      const field = event.target;
      const controlLabel = Array.from(field.labels?.[0]?.childNodes || [])
        .find(
          (node) => node.nodeType === Node.TEXT_NODE && node.textContent.trim(),
        )
        ?.textContent.trim();
      const label =
        controlLabel ||
        field.labels?.[0]?.querySelector(".sr-only")?.textContent.trim() ||
        field.name ||
        "Movement details";
      showToast(`${label}: ${field.validationMessage}`, true);
    },
    true,
  );
  $("#material-form").addEventListener("submit", submitMaterial);
  $("#house-form").addEventListener("submit", submitHouse);
  $("#house-form").elements.employee_name.addEventListener("input", (event) => {
    event.currentTarget.setCustomValidity("");
  });
  $("#house-form").elements.perimeter_m.addEventListener("input", (event) => {
    event.currentTarget.setCustomValidity("");
  });
  $("#house-request").addEventListener("change", toggleRequestItem);
  $("#seai-search").addEventListener("input", () => {
    clearTimeout(seaiTimer);
    seaiTimer = setTimeout(searchSeaiHouses, 200);
  });
  $("#seai-search").addEventListener("focus", searchSeaiHouses);
  $("#seai-results").addEventListener("click", pickSeaiHouse);
  $("#houses-list").addEventListener("click", toggleHouseFinished);
  $("#house-map-list-view").addEventListener("click", () => {
    if (state.user?.role === "assign" && state.houseStatusFilter === "seai") {
      state.houseStatusFilter = "active";
    }
    state.houseMapMode = false;
    state.selectedMapHouseId = "";
    state.houseMapCamera = null;
    showHousesOverview();
    renderHouses();
  });
  $("#house-map-house-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-map-house-id]");
    if (button) selectMapHouse(button.dataset.mapHouseId);
  });
  $("#house-map-legend").addEventListener("click", (event) => {
    const button = event.target.closest("[data-map-stage-filter]");
    if (!button) return;
    const filter = button.dataset.mapStageFilter;
    state.houseMapStageFilters[filter] =
      !state.houseMapStageFilters[filter];
    if (
      state.selectedMapHouseId &&
      !isHouseMapStageVisible(
        state.houseMapData?.find((house) => house.id === state.selectedMapHouseId) || {},
      )
    ) {
      state.selectedMapHouseId = "";
      $("#house-map-selection").innerHTML =
        "<strong>Select a house</strong><p>Choose a marker or an address from the list below.</p>";
      $("#house-map-activity-count").textContent = "—";
      $("#house-map-activity-count").removeAttribute("aria-label");
      $("#house-map-audit").innerHTML =
        '<div class="house-map-audit-empty"><span class="house-map-audit-empty-icon" aria-hidden="true">&#8982;</span><strong>Choose a house</strong><p>Select a house to see its recorded updates.</p></div>';
    }
    renderHouseMapStageFilters();
    renderHouseMapSummary();
    renderHouseMapList();
    renderHouseMapCanvas();
  });
  $("#house-map-markers").addEventListener("click", (event) => {
    const marker = event.target.closest("[data-map-house-id]");
    if (marker) selectMapHouse(marker.dataset.mapHouseId);
  });
  $("#house-map-selection").addEventListener("click", toggleHouseFinished);
  $("#house-map-selection").addEventListener("click", (event) => {
    const button = event.target.closest(".house-checklist-open");
    if (button) openHouseChecklist(button.dataset.houseId);
  });
  $("#house-map-zoom-in").addEventListener("click", () => zoomHouseMap(1));
  $("#house-map-zoom-out").addEventListener("click", () => zoomHouseMap(-1));
  $("#house-map-fit").addEventListener("click", fitHouseMap);
  $("#house-locations-import").addEventListener("submit", submitHouseLocations);
  $("#house-locations-file").addEventListener("change", (event) => {
    $("#house-locations-file-name").textContent =
      event.currentTarget.files[0]?.name || "No file selected";
  });
  $("#back-from-surveyor-room").addEventListener("click", closeSurveyorRoom);
  $("#house-document-form").addEventListener("submit", submitHouseDocument);
  $("#surveyor-house-notes-form").addEventListener("submit", submitSurveyorHouseNotes);
  $("#house-documents-list").addEventListener("click", (event) => {
    const annotateButton = event.target.closest(".house-document-annotate");
    if (annotateButton) {
      openHouseDocumentAnnotator(annotateButton);
      return;
    }
    const shareButton = event.target.closest(".house-document-share");
    if (shareButton) prepareHouseDocumentShare(shareButton);
  });
  $("#survey-room-seai-documents").addEventListener("click", (event) => {
    const documentShareButton = event.target.closest("[data-seai-document-share]");
    if (documentShareButton) {
      prepareSeaiDocumentFileShare(documentShareButton);
      return;
    }
    const shareButton = event.target.closest("[data-seai-share-selected]");
    if (shareButton) {
      prepareSeaiDocumentShare(shareButton);
      return;
    }
    if (event.target.closest("[data-seai-share-clear]")) {
      clearSeaiShareSelection();
    }
  });
  $("#pdf-annotator-page-selection").addEventListener("change", (event) => {
    updateSeaiPageSelection(event.currentTarget.checked);
  });
  $("#house-document-share-send").addEventListener("click", sendPreparedHouseShare);
  $("#house-document-share-email").addEventListener("click", emailPreparedHouseShare);
  $("#house-document-share-whatsapp").addEventListener("click", whatsappPreparedHouseShare);
  $("#house-document-share-download").addEventListener("click", () => {
    downloadPreparedHouseShare();
    $("#house-document-share-dialog").close();
    showToast("Combined PDF downloaded.");
  });
  $("#pdf-annotator-close").addEventListener("click", closeHouseDocumentAnnotator);
  $("#pdf-annotator-dialog").addEventListener("cancel", () => {
    const annotator = state.pdfAnnotator;
    if (annotator?.previewUrl) URL.revokeObjectURL(annotator.previewUrl);
    state.pdfAnnotator = null;
  });
  $$(".pdf-tool-button").forEach((button) =>
    button.addEventListener("click", () => {
      const annotator = state.pdfAnnotator;
      if (!annotator) return;
      annotator.tool = button.dataset.pdfTool;
      $$(".pdf-tool-button").forEach((toolButton) => {
        const active = toolButton === button;
        toolButton.classList.toggle("active", active);
        toolButton.setAttribute("aria-pressed", String(active));
      });
      const textTool = annotator.tool === "text";
      $("#pdf-annotator-text-panel").classList.toggle("hidden", !textTool);
      $("#pdf-annotator-help").textContent = textTool
        ? "Type a short note above, then tap the page to place it."
        : "Draw with a finger, Apple Pencil, mouse, or trackpad. Choose a size and zoom in for detail; use Undo to remove the last mark.";
      if (textTool) $("#pdf-annotator-text").focus();
    }),
  );
  $("#pdf-annotator-undo").addEventListener("click", () => {
    if (!state.pdfAnnotator?.annotations.length) return;
    state.pdfAnnotator.annotations.pop();
    drawPdfAnnotations();
  });
  $("#pdf-annotator-viewport").addEventListener("wheel", scrollPdfPage, {
    passive: false,
  });
  $("#pdf-annotator-save").addEventListener("click", saveHouseDocumentAnnotations);
  $("#pdf-annotator-color").addEventListener("change", drawPdfAnnotations);
  $("#pdf-annotator-size").addEventListener("change", (event) => {
    if (state.pdfAnnotator) {
      state.pdfAnnotator.penSize = Number(event.currentTarget.value);
    }
  });
  $("#pdf-annotator-zoom-in").addEventListener("click", () => {
    const annotator = state.pdfAnnotator;
    if (!annotator) return;
    annotator.zoom = Math.min(3, Math.round((annotator.zoom + 0.25) * 100) / 100);
    updatePdfPageZoom();
  });
  $("#pdf-annotator-zoom-out").addEventListener("click", () => {
    const annotator = state.pdfAnnotator;
    if (!annotator) return;
    annotator.zoom = Math.max(0.5, Math.round((annotator.zoom - 0.25) * 100) / 100);
    updatePdfPageZoom();
  });
  $("#pdf-annotator-zoom-fit").addEventListener("click", () => {
    if (!state.pdfAnnotator) return;
    state.pdfAnnotator.zoom = 1;
    updatePdfPageZoom(false);
    const viewport = $("#pdf-annotator-viewport");
    viewport.scrollTo({ left: 0, top: 0, behavior: "instant" });
  });
  const pdfCanvas = $("#pdf-annotator-canvas");
  const pdfCanvasPoint = (event) => {
    const rect = pdfCanvas.getBoundingClientRect();
    return [
      Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)),
      Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)),
    ];
  };
  pdfCanvas.addEventListener("pointerdown", (event) => {
    const annotator = state.pdfAnnotator;
    if (!annotator || annotator.loading || !pdfCanvas.clientWidth) return;
    event.preventDefault();
    const point = pdfCanvasPoint(event);
    const color = $("#pdf-annotator-color").value;
    if (annotator.tool === "text") {
      const text = $("#pdf-annotator-text").value.trim();
      if (!text) {
        $("#pdf-annotator-text").focus();
        showToast("Type a note before tapping the PDF.", true);
        return;
      }
      annotator.annotations.push({
        page: annotator.page,
        tool: "text",
        color,
        point,
        text,
        font_size: 16 / annotator.fitWidth,
      });
      drawPdfAnnotations();
      return;
    }
    const width = (annotator.tool === "highlighter"
      ? Math.max(8, annotator.penSize * 3)
      : annotator.penSize) / annotator.fitWidth;
    const annotation = {
      page: annotator.page,
      tool: annotator.tool,
      color,
      width,
      points: [point],
    };
    annotator.annotations.push(annotation);
    annotator.drawing = { pointerId: event.pointerId, annotation, lastPoint: point };
    pdfCanvas.setPointerCapture(event.pointerId);
    drawPdfAnnotations();
  });
  pdfCanvas.addEventListener("pointermove", (event) => {
    const annotator = state.pdfAnnotator;
    const drawing = annotator?.drawing;
    if (!drawing || drawing.pointerId !== event.pointerId) return;
    event.preventDefault();
    const points = drawing.annotation.points;
    const coalesced = typeof event.getCoalescedEvents === "function"
      ? event.getCoalescedEvents()
      : [event];
    for (const pointerEvent of coalesced) {
      const point = pdfCanvasPoint(pointerEvent);
      const dx = (point[0] - drawing.lastPoint[0]) * pdfCanvas.clientWidth;
      const dy = (point[1] - drawing.lastPoint[1]) * pdfCanvas.clientHeight;
      if (Math.hypot(dx, dy) < 0.75) continue;
      if (points.length >= 1000) break;
      points.push(point);
      drawPdfStrokeSegment(drawing.annotation, drawing.lastPoint, point);
      drawing.lastPoint = point;
    }
  });
  const finishPdfStroke = (event) => {
    const annotator = state.pdfAnnotator;
    if (annotator?.drawing?.pointerId === event.pointerId) {
      annotator.drawing = null;
      drawPdfAnnotations();
    }
  };
  pdfCanvas.addEventListener("pointerup", finishPdfStroke);
  pdfCanvas.addEventListener("pointercancel", finishPdfStroke);
  new ResizeObserver(() => updatePdfPageZoom()).observe($("#pdf-annotator-viewport"));
  $("#house-document-file").addEventListener("change", (event) => {
    $("#house-document-file-name").textContent =
      event.currentTarget.files[0]?.name || "No file selected";
  });
  const mapCanvas = $("#house-map-canvas");
  let mapDrag = null;
  mapCanvas.addEventListener("pointerdown", (event) => {
    if (!state.houseMapCamera || event.target.closest("button, a")) return;
    mapDrag = {
      pointerId: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      cameraX: state.houseMapCamera.x,
      cameraY: state.houseMapCamera.y,
    };
    mapCanvas.setPointerCapture(event.pointerId);
  });
  mapCanvas.addEventListener("pointermove", (event) => {
    if (!mapDrag || mapDrag.pointerId !== event.pointerId) return;
    const scale = HOUSE_MAP_TILE_SIZE * 2 ** state.houseMapCamera.zoom;
    state.houseMapCamera.x = mapDrag.cameraX - (event.clientX - mapDrag.x);
    state.houseMapCamera.y = mapDrag.cameraY - (event.clientY - mapDrag.y);
    state.houseMapCamera.y = Math.max(0, Math.min(scale, state.houseMapCamera.y));
    renderHouseMapCanvas();
  });
  mapCanvas.addEventListener("pointerup", () => {
    mapDrag = null;
  });
  mapCanvas.addEventListener("pointercancel", () => {
    mapDrag = null;
  });
  window.addEventListener("resize", () => {
    if (state.houseMapMode && state.houseMapCamera) renderHouseMapCanvas();
  });
  $("#teams-list").addEventListener("click", (event) => {
    const button = event.target.closest(".team-house-count");
    if (!button || button.disabled) return;
    const locations = JSON.parse(button.dataset.teamLocations);
    const selection = {
      locations,
      status: button.dataset.houseStatus,
    };
    state.houseTeamFilter =
      JSON.stringify(state.houseTeamFilter?.locations || [state.houseTeamFilter?.location]) ===
        JSON.stringify(selection.locations) &&
      state.houseTeamFilter?.status === selection.status
        ? null
        : selection;
    state.houseStatusFilter = null;
    state.housesTeamsTab = "houses";
    $("#house-search").value = "";
    $("#houses-list").scrollTop = 0;
    renderHouses();
    $("#houses-list").scrollIntoView({ behavior: "smooth", block: "start" });
  });
  $("#houses-list").addEventListener("click", (event) => {
    const button = event.target.closest(".house-checklist-open");
    if (button) openHouseChecklist(button.dataset.houseId);
  });
  $("#houses-list").addEventListener("change", updateHouseChecklistStatus);
  $("#house-checklist-form").addEventListener("submit", submitHouseChecklist);
  $("#house-note-form").addEventListener("submit", submitHouseNote);
  $("#house-survey-form").addEventListener("submit", submitHouseSurvey);
  $("#surveyor-work-plan-form").addEventListener("submit", submitHouseWorkPlan);
  $("#surveyor-work-plan-form").addEventListener("change", (event) => {
    if (
      event.target.matches(
        "select[name$='_required'], textarea[name$='_notes']",
      )
    ) {
      saveHouseWorkPlan(event.currentTarget);
    }
  });
  $("#surveyor-survey-notes-form").addEventListener("submit", submitSurveyorSurveyNotes);
  $("#surveyor-room").addEventListener("click", (event) => {
    const seaiDocument = event.target.closest(".seai-document-view");
    if (seaiDocument) {
      openSeaiDocument(seaiDocument);
      return;
    }
    const editVariation = event.target.closest(".seai-variation-edit");
    if (editVariation) {
      const cell = editVariation.closest(".seai-variation-cell");
      const editor = cell.querySelector(".seai-variation-editor");
      editVariation.hidden = true;
      cell.querySelector(".seai-variation-value").hidden = true;
      editor.hidden = false;
      const input = editor.querySelector(".seai-variation-input");
      input.focus();
      input.select();
      return;
    }
    const saveVariation = event.target.closest(".seai-variation-save");
    if (saveVariation) {
      saveSeaiMeasureVariation(saveVariation);
      return;
    }
    const editNotes = event.target.closest("[data-edit-house-notes]");
    if (editNotes) {
      const notes = $("#survey-room-summary-house-notes").textContent;
      const form = $("#surveyor-house-notes-form");
      form.elements.notes.value = notes === "No house notes" ? "" : notes;
      form.elements.notes.scrollIntoView({ behavior: "smooth", block: "center" });
      form.elements.notes.focus();
      return;
    }
    const editSurveyNotes = event.target.closest("[data-edit-survey-notes]");
    if (editSurveyNotes) {
      const notes = $("#survey-room-summary-survey-notes").textContent;
      const form = $("#surveyor-survey-notes-form");
      form.elements.notes.value = notes === "No survey note" ? "" : notes;
      form.elements.notes.scrollIntoView({ behavior: "smooth", block: "center" });
      form.elements.notes.focus();
      return;
    }
    const add = event.target.closest(".survey-add-row");
    if (add) surveyRow(add.dataset.surveyList);
    const remove = event.target.closest(".survey-remove");
    if (remove) remove.closest(".survey-row").remove();
  });
  $("#house-snag-list").addEventListener("click", (event) => {
    const remove = event.target.closest(".snag-del");
    if (remove) {
      saveSnag({ snag_id: Number(remove.dataset.snagId), delete: true });
      return;
    }
    const toggle = event.target.closest(".snag-row");
    if (toggle) {
      saveSnag({ snag_id: Number(toggle.dataset.snagId), done: toggle.dataset.done === "true" });
    }
  });
  $("#house-snag-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    if (await saveSnag({ id: form.dataset.houseId, text: form.elements.text.value })) {
      form.reset();
      form.elements.text.focus();
    }
  });
  $("#stocktake-form").addEventListener("submit", submitStocktake);
  $("#import-form").addEventListener("submit", submitImport);
  $("#material-search").addEventListener("input", renderInventory);
  $("#house-search").addEventListener("input", () => state.user?.role === "surveyor" ? renderSurveyorHouses() : renderHouses());
  $("#team-search").addEventListener("input", renderHouses);
  $("#clear-house-team-filter").addEventListener("click", () => {
    state.houseTeamFilter = null;
    state.houseStatusFilter = null;
    state.housesTeamsTab = "houses";
    renderHouses();
  });
  $("#houses-tab").addEventListener("click", () => {
    state.housesTeamsTab = "houses";
    renderHouses();
  });
  $("#teams-tab").addEventListener("click", () => {
    state.housesTeamsTab = "teams";
    state.houseTeamFilter = null;
    state.houseStatusFilter = null;
    renderHouses();
  });
  $("#houses-teams-tabs").addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const nextTab =
      event.key === "Home" || event.key === "ArrowLeft"
        ? "houses"
        : "teams";
    state.housesTeamsTab = nextTab;
    if (nextTab === "teams") state.houseTeamFilter = null;
    renderHouses();
    $(`#${nextTab}-tab`).focus();
  });
  $("#movement-search").addEventListener("input", searchMovementHistory);
  $("#workbook-file").addEventListener("change", (event) => {
    $("#file-name").textContent =
      event.target.files[0]?.name || "No file selected";
  });
}

function renderMaterialSuggestions() {
  const query = $("#movement-material-search").value.trim().toLocaleLowerCase();
  const matches = state.dashboard.materials
    .filter((item) =>
      `${item.name} ${item.code || ""}`.toLocaleLowerCase().includes(query),
    )
    .slice(0, 10);
  const list = $("#material-suggestions");
  list.innerHTML = matches
    .map(
      (item) => `
    <button class="suggestion-option" type="button" role="option" data-material-id="${item.id}">
      <span><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.code || "No code")}</small></span>
      <span class="suggestion-stock">${escapeHtml(formatQuantity(item.warehouse_qty, item.unit))}</span>
    </button>`,
    )
    .join("");
  list.classList.toggle("hidden", !matches.length);
}

function addMaterialFromSearch() {
  const query = $("#movement-material-search").value.trim().toLocaleLowerCase();
  const matches = state.dashboard.materials.filter((item) =>
    `${item.name} ${item.code || ""}`.toLocaleLowerCase().includes(query),
  );
  if (matches.length === 1) {
    addBatchMaterial(matches[0].id);
  } else {
    renderMaterialSuggestions();
    $("#material-suggestions").classList.remove("hidden");
  }
}

function selectMaterialSuggestion(event) {
  const button = event.target.closest("[data-material-id]");
  if (button) addBatchMaterial(Number(button.dataset.materialId));
}

function handleMaterialSearchKey(event) {
  if (event.key === "Enter") {
    event.preventDefault();
    const first = $("[data-material-id]", $("#material-suggestions"));
    if (first && !$("#material-suggestions").classList.contains("hidden")) {
      addBatchMaterial(Number(first.dataset.materialId));
    } else {
      addMaterialFromSearch();
    }
  } else if (event.key === "Escape") {
    $("#material-suggestions").classList.add("hidden");
  }
}

function addBatchMaterial(materialId) {
  if (state.batchLines.some((line) => line.material_id === materialId)) {
    showToast("That material is already in this movement.", true);
    return;
  }
  if (state.batchLines.length >= 200) {
    showToast("A movement can include at most 200 materials.", true);
    return;
  }
  state.batchLines.push({ material_id: materialId, qty: "" });
  $("#movement-material-search").value = "";
  $("#material-suggestions").classList.add("hidden");
  renderMaterialLines();
  $(".line-quantity input:last-of-type", $("#movement-lines"))?.focus();
}

function renderMaterialLines() {
  if (!state.dashboard || !$("#movement-lines")) return;
  const houseId = $("#movement-house-id").value.trim();
  const lines = state.batchLines
    .map((line) => {
      const material = state.dashboard.materials.find(
        (item) => item.id === line.material_id,
      );
      if (!material) return "";
      const balances = movementBalanceDetails(material, houseId);
      return `
      <div class="selected-material-row" data-material-id="${material.id}">
        <div class="selected-material-name"><strong>${escapeHtml(material.name)}</strong><small>${escapeHtml(material.code || material.unit)}</small></div>
        <div class="selected-balance"><small class="first-balance-label">${escapeHtml(balances.firstLabel)}</small><strong class="warehouse-balance">${escapeHtml(formatQuantity(balances.firstQty, material.unit))}</strong></div>
        <div class="selected-balance"><small class="second-balance-label">${escapeHtml(balances.secondLabel)}</small><strong class="house-balance">${escapeHtml(formatQuantity(balances.secondQty, material.unit))}</strong></div>
        <label class="line-quantity"><span class="sr-only">Quantity of ${escapeHtml(material.name)}</span><input type="number" min="0.001" step="any" required value="${escapeHtml(line.qty)}" placeholder="Qty"><small>${escapeHtml(material.unit)}</small></label>
        <button class="remove-line" type="button" data-remove-material="${material.id}" aria-label="Remove ${escapeHtml(material.name)}">×</button>
      </div>`;
    })
    .join("");
  renderHouseRequest();
  $("#movement-lines").innerHTML =
    lines ||
    `<div class="no-material-lines">No materials added yet. Search above to start a materials list.</div>`;
  $("#selected-material-count").textContent =
    `${state.batchLines.length} material${state.batchLines.length === 1 ? "" : "s"} selected`;
}

function movementBalanceDetails(material, houseId) {
  if ($("#movement-kind").value !== "transfer") {
    return {
      firstLabel: "Warehouse",
      firstQty: material.warehouse_qty,
      secondLabel: "On site",
      secondQty: material.balances[`house:${houseId}`] ?? 0,
    };
  }
  const blocks = $$(
    ".transfer-block",
    $(".movement-fields[data-for='transfer']"),
  );
  const readLocation = (block) => {
    const kind = $(".location-kind", block).value;
    if (kind === "house") {
      const id = $(".site-reference", block).value;
      const house = state.dashboard.house_list.find((item) => item.id === id);
      return {
        label: house ? `Site ${house.id}` : "Site balance",
        key: id ? `house:${id}` : "",
      };
    }
    const team = $("input[name$='_reference']", block).value.trim();
    const department = $("input[name$='_department']", block)?.value.trim();
    const key = team
      ? `team:${department ? `${encodeURIComponent(department)}|${encodeURIComponent(team)}` : team}`
      : "";
    return { label: team ? `Team ${team}` : "Team balance", key };
  };
  const source = readLocation(blocks[0]);
  const destination = readLocation(blocks[1]);
  return {
    firstLabel: source.label,
    firstQty: source.key ? (material.balances[source.key] ?? 0) : 0,
    secondLabel: destination.label,
    secondQty: destination.key ? (material.balances[destination.key] ?? 0) : 0,
  };
}

function updateSelectedMaterialBalances() {
  const houseId = $("#movement-house-id").value.trim();
  $$(".selected-material-row").forEach((row) => {
    const material = state.dashboard.materials.find(
      (item) => item.id === Number(row.dataset.materialId),
    );
    if (!material) return;
    const balances = movementBalanceDetails(material, houseId);
    $(".first-balance-label", row).textContent = balances.firstLabel;
    $(".warehouse-balance", row).textContent = formatQuantity(
      balances.firstQty,
      material.unit,
    );
    $(".second-balance-label", row).textContent = balances.secondLabel;
    $(".house-balance", row).textContent = formatQuantity(
      balances.secondQty,
      material.unit,
    );
  });
}

function updateBatchQuantity(event) {
  const input = event.target.closest(".line-quantity input");
  if (!input) return;
  const materialId = Number(
    input.closest("[data-material-id]").dataset.materialId,
  );
  const line = state.batchLines.find((item) => item.material_id === materialId);
  if (line) line.qty = input.value;
}

function removeBatchMaterial(event) {
  const button = event.target.closest("[data-remove-material]");
  if (!button) return;
  const id = Number(button.dataset.removeMaterial);
  state.batchLines = state.batchLines.filter((line) => line.material_id !== id);
  renderMaterialLines();
}

function renderHouseSuggestions() {
  const query = $("#movement-house-id").value.trim().toLocaleLowerCase();
  const list = $("#house-suggestions");
  if (!query) {
    list.classList.add("hidden");
    return;
  }
  if (state.selectedHouseId.toLocaleLowerCase() === query) {
    list.classList.add("hidden");
    return;
  }
  const matches = state.dashboard.house_list.filter((house) =>
    [house.id, house.owner_name, house.employee_name, house.postcode]
      .filter(Boolean)
      .some((value) => value.toLocaleLowerCase().includes(query)),
  );
  list.innerHTML = matches.length
    ? matches
        .map(
          (house) => `
    <button class="suggestion-option" type="button" role="option" data-house-id="${escapeHtml(house.id)}">
      <span><strong>${escapeHtml(house.id)}</strong><small>${escapeHtml([house.owner_name, house.employee_name].filter(Boolean).join(" · ") || "No owner or employee recorded")}</small></span>
      <span class="suggestion-stock">${escapeHtml(house.postcode || "")}</span>
    </button>`,
        )
        .join("")
    : `<div class="suggestion-option">No houses match “${escapeHtml(query)}”.</div>`;
  list.classList.remove("hidden");
}

const requestCache = {};
const formatNumber = (value) => String(Math.round(Number(value) * 1000) / 1000);
const materialKey = (name) =>
  String(name || "")
    .toLowerCase()
    .replace(/aluminium/g, "aluminum")
    .replace(/45\s*°\s*connector/g, "45 deg connector")
    .replace(/downpipe bracket (long|short)/g, "downpipe brackets $1")
    .replace(/\s+/g, " ")
    .trim();

function findTrackerMaterial(name) {
  let key = materialKey(name);
  const caping = key.match(/^caping 100mm (\w+)$/);
  if (caping) key = `caping ${caping[1]} 100mm`;
  return state.dashboard.materials.find((m) => materialKey(m.name) === key);
}

// Older saved requests picked up the calculator's stock tag text in the material name.
const cleanMaterialName = (name) =>
  String(name || "").replace(/\s*(\d+(\.\d+)?\s+in stock.*|not tracked|count needed)$/i, "").trim();

function requestEntries(request) {
  const data = request.data || {};
  const entries = [];
  (data.items || []).forEach((item) =>
    entries.push({ label: item.label, detail: item.required, material: item.material, qty: Number(item.qty) }),
  );
  (data.manual || []).forEach((item) =>
    entries.push({
      label: cleanMaterialName(item.material),
      detail: `${item.qty} ${item.unit || ""}`.trim(),
      material: cleanMaterialName(item.material),
      qty: parseFloat(item.qty),
    }),
  );
  return entries.filter((entry) => entry.qty > 0);
}

function renderHouseRequest() {
  const box = $("#house-request");
  if (!box || !state.dashboard) return;
  const houseId = $("#movement-house-id").value.trim();
  const kind = $("#movement-kind").value;
  const assigned = requestCache[houseId];
  if (kind === "pickup") {
    const deliveryNumber = assigned?.next_delivery_number || 1;
    $("#extra-delivery").checked = deliveryNumber > 1;
    $("#extra-delivery").disabled = true;
    $("#extra-delivery-hint").textContent = deliveryNumber > 1
      ? `Delivery #${deliveryNumber} for this house; marked automatically.`
      : "The first pickup is Delivery #1.";
  }
  if (kind !== "pickup" || !houseId || !assigned?.id) {
    box.classList.add("hidden");
    return;
  }
  const delivered = assigned.delivered || {};
  let deliveredAny = false;
  const rows = requestEntries(assigned)
    .map((entry) => {
      const material = findTrackerMaterial(entry.material);
      const sent = material ? Number(delivered[material.id] || 0) : 0;
      const remaining = Math.max(entry.qty - sent, 0);
      if (sent > 0) deliveredAny = true;
      const done = material && remaining <= 1e-9;
      const checked = material && state.batchLines.some((line) => line.material_id === material.id);
      const note = !material
        ? `${entry.detail || ""} · not tracked in warehouse`
        : sent > 0
          ? done
            ? `needed ${formatNumber(entry.qty)} · already delivered ${formatNumber(sent)} · nothing left to deliver`
            : `needed ${formatNumber(entry.qty)} · already delivered ${formatNumber(sent)} · remaining ${formatNumber(remaining)}`
          : entry.detail || "";
      const attrs = material && !done
        ? `data-material-id="${material.id}" data-qty="${remaining}"`
        : "disabled";
      return `<label class="req-row${material ? "" : " req-untracked"}${done ? " req-done" : ""}"><input type="checkbox" ${attrs}${checked ? " checked" : ""}><span><strong>${escapeHtml(entry.label)}</strong><small>${escapeHtml(note)}</small></span></label>`;
    })
    .join("");
  const deliveryNote = deliveredAny
    ? " Quantities already delivered are subtracted, so this is a follow-up delivery."
    : "";
  const markup = `<div class="req-heading"><strong>Materials assigned to ${escapeHtml(houseId)}</strong><small>From calculator · ${escapeHtml(assigned.created_by)}, ${escapeHtml(String(assigned.created_at).replace("T", " "))}. Tick each item as you load it.${deliveryNote}</small></div><div class="req-list">${rows}</div>`;
  // Rebuilding the panel on every tick collapses its height and makes the dialog jump,
  // so only rebuild when the content really changed and otherwise just sync the ticks.
  const signature = markup.replace(/ checked/g, "");
  if (box.dataset.signature === signature) {
    $$("input[data-material-id]", box).forEach((input) => {
      input.checked = state.batchLines.some(
        (line) => line.material_id === Number(input.dataset.materialId),
      );
    });
  } else {
    const scroller = box.closest("dialog");
    const dialogScroll = scroller ? scroller.scrollTop : 0;
    const listScroll = $(".req-list", box)?.scrollTop || 0;
    box.innerHTML = markup;
    box.dataset.signature = signature;
    $(".req-list", box).scrollTop = listScroll;
    if (scroller) scroller.scrollTop = dialogScroll;
  }
  box.classList.remove("hidden");
}

async function loadHouseRequest() {
  const houseId = $("#movement-house-id").value.trim();
  if (!houseId || houseId in requestCache) return renderHouseRequest();
  try {
    const { requests, delivered, next_delivery_number: nextDeliveryNumber } = await request(
      `/api/calculator/requests?house_id=${encodeURIComponent(houseId)}`,
    );
    requestCache[houseId] = {
      ...(requests[0] || {}),
      data: requests[0]
        ? typeof requests[0].data === "string"
          ? JSON.parse(requests[0].data)
          : requests[0].data
        : null,
      delivered,
      next_delivery_number: nextDeliveryNumber,
    };
  } catch (error) {
    requestCache[houseId] = null;
  }
  renderHouseRequest();
}

function toggleRequestItem(event) {
  const box = event.target.closest("input[data-material-id]");
  if (!box) return;
  const id = Number(box.dataset.materialId);
  state.batchLines = state.batchLines.filter((line) => line.material_id !== id);
  if (box.checked) state.batchLines.push({ material_id: id, qty: box.dataset.qty });
  renderMaterialLines();
}

function updateHouseSelection() {
  const value = $("#movement-house-id").value.trim();
  const house = state.dashboard.house_list.find(
    (item) => item.id.toLocaleLowerCase() === value.toLocaleLowerCase(),
  );
  if (house) {
    const isNewSelection =
      state.selectedHouseId.toLocaleLowerCase() !== house.id.toLocaleLowerCase();
    $("#movement-house-id").value = house.id;
    state.selectedHouseId = house.id;
    $("#movement-postcode").value = house.postcode || "";
    $("#movement-owner").value = house.owner_name || "";
    $("#movement-employee").value = house.employee_name || "";
    if (isNewSelection) {
      $$("#movement-form [name='team']").forEach((input) => {
        input.value = house.employee_name || "";
      });
    }
  } else if (state.selectedHouseId !== value) {
    state.selectedHouseId = "";
    $("#movement-postcode").value = "";
    $("#movement-owner").value = "";
    $("#movement-employee").value = "";
  }
  renderHouseSuggestions();
  renderHouseLiveStock();
  renderMaterialLines();
  loadHouseRequest();
}

function selectHouseSuggestion(event) {
  const button = event.target.closest("[data-house-id]");
  if (!button) return;
  $("#movement-house-id").value = button.dataset.houseId;
  $("#house-suggestions").classList.add("hidden");
  updateHouseSelection();
}

function handleHouseSearchKey(event) {
  if (event.key === "Escape") {
    $("#house-suggestions").classList.add("hidden");
  } else if (event.key === "Enter") {
    event.preventDefault();
    const first = $("[data-house-id]", $("#house-suggestions"));
    if (first && !$("#house-suggestions").classList.contains("hidden")) {
      $("#movement-house-id").value = first.dataset.houseId;
      updateHouseSelection();
    }
  }
}

function renderHouseLiveStock() {
  if (!state.dashboard) return;
  const houseId = $("#movement-house-id").value.trim();
  const liveStock = $("#house-live-stock");
  const house = state.dashboard.house_list.find(
    (item) => item.id.toLocaleLowerCase() === houseId.toLocaleLowerCase(),
  );
  const submit = document.querySelector("#movement-form button[type=submit]");
  const closed = Boolean(house && house.is_finished);
  if (submit) submit.disabled = closed;
  if (!houseId) {
    liveStock.innerHTML = `<strong>Select a house to see its current on-site materials.</strong><div id="house-live-stock-list"></div>`;
    return;
  }
  const materials = state.dashboard.materials.flatMap((material) => {
    const quantity = material.balances[`house:${houseId}`] ?? 0;
    return Math.abs(quantity) > 1e-9 ? [{ material, quantity }] : [];
  });
  liveStock.innerHTML = `
    <strong>On site at ${escapeHtml(house?.id || houseId)}${house?.owner_name ? ` · ${escapeHtml(house.owner_name)}` : ""}</strong>
    ${closed ? `<div style="color:#b3261e;font-weight:600;margin:6px 0">This house is closed. Reopen it to record movements.</div>` : ""}
    <div class="house-live-stock-list">${
      materials.length
        ? materials
            .map(
              ({ material, quantity }) =>
                `<span>${escapeHtml(material.name)} <b>${escapeHtml(formatQuantity(quantity, material.unit))}</b></span>`,
            )
            .join("")
        : `<span>No recorded materials on site yet.</span>`
    }
    </div>`;
}

async function start() {
  bindEvents();
  $("#today-label").textContent = new Date().toLocaleDateString();
  $("#movement-date").value = localDate();
  updateMovementFields();
  try {
    await checkAuthentication();
  } catch (error) {
    showAuthScreen(false);
    $("#auth-error").textContent = `Could not connect to the tracker: ${error.message}`;
    $("#auth-error").classList.remove("hidden");
  }
  let refreshFailureShown = false;
  setInterval(() => {
    if (!state.user) return;
    refresh(true)
      .then(() => {
        refreshFailureShown = false;
      })
      .catch((error) => {
        if (!refreshFailureShown) {
          showToast(`Live stock refresh failed: ${error.message}`, true);
          refreshFailureShown = true;
        }
      });
  }, 15000);
}

start();
