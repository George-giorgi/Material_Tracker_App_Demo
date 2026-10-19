const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const state = {
  dashboard: null,
  movements: [],
  batchLines: [],
  selectedHouseId: "",
  viewingHouseId: "",
  houseDetailRequest: 0,
  presentation: null,
  user: null,
  setupRequired: false,
};
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
    title: "A clearer view of materials",
    narration:
      "We will follow a fictional job from the warehouse to a house, record a later delivery, return unused material, and transfer stock to another site.",
    instruction:
      "This isolated demo uses fictional quantities and people. Each action is saved only in the demo database on this computer.",
    example: "Story: one team, two sites, and a traceable material ledger.",
  },
  {
    title: "1. Add a house and its owner",
    narration:
      "Start by creating a site record so later movements can be assigned to the right house and responsible people.",
    instruction:
      "Open the house form, review the example details, and click Save house. Then return to this guide and continue.",
    example: "House DEMO-SHOW-301 · Owner Riley Example · Employee Jordan Demo",
    action: "house",
    actionLabel: "Open and prefill house form",
  },
  {
    title: "2. Record the first warehouse pickup",
    narration:
      "The first pickup records what the team actually takes from warehouse stock and places it directly on the selected house.",
    instruction:
      "Review the prefilled quantities and click Save movement. The warehouse and house balances will update together.",
    example:
      "House DEMO-SHOW-301 · Board 100mm · 12 units · Collected by Jordan Demo · Demo Retrofit Crew",
    action: "initial-pickup",
    actionLabel: "Open and prefill pickup",
  },
  {
    title: "3. Mark a later delivery",
    narration:
      "When the team needs more material later, mark the pickup as an extra delivery. The ledger numbers it after the earlier delivery for that house.",
    instruction:
      "The extra-delivery box is preselected. Save this movement to see the numbered extra delivery in the activity and movement history.",
    example: "House DEMO-SHOW-301 · Board 100mm · 4 units · Extra delivery",
    action: "extra-pickup",
    actionLabel: "Open and prefill extra delivery",
  },
  {
    title: "4. Return unused material",
    narration:
      "Unused material can be returned to the warehouse instead of being left as an unexplained site balance.",
    instruction:
      "Review the return details and save. The returned quantity is subtracted from the house and added back to warehouse stock.",
    example: "Return 2 boards from DEMO-SHOW-301 · Recorded by Jordan Demo",
    action: "return",
    actionLabel: "Open and prefill return",
  },
  {
    title: "5. Transfer stock to another site",
    narration:
      "If material is moved between jobs, record both the source and destination. This keeps the balance attached to the place currently holding it.",
    instruction:
      "The form is prefilled from DEMO-SHOW-301 to DEMO-202. Save the transfer to update both site balances.",
    example:
      "Transfer 5 boards · From DEMO-SHOW-301 · To DEMO-202 · Responsible team Demo Retrofit Crew",
    action: "transfer",
    actionLabel: "Open and prefill transfer",
  },
  {
    title: "6. See balances update",
    narration:
      "Inventory brings the movement history together into a live view of what remains in the warehouse, with teams, and across houses.",
    instruction:
      "Review the Board 100mm row. Its warehouse and on-site totals reflect the pickup, return, and transfer you recorded.",
    example:
      "Tip: open Houses & teams to compare the material held at each site.",
    action: "inventory",
    actionLabel: "Show updated inventory",
  },
  {
    title: "7. Mark the job finished",
    narration:
      "When work is complete, mark the house finished. Its history and remaining stock stay available, while it is no longer counted as active.",
    instruction:
      "The guide will minimize. On the house card, click Mark house finished, then click Resume presentation. You can reopen it if work starts again.",
    example: "Finished houses remain visible for accountability.",
    action: "finish-house",
    actionLabel: "Show the house card",
  },
  {
    title: "8. Find the complete house history",
    narration:
      "Search the movement ledger by the demonstration house ID to review its pickups, return, transfer, responsible person, and team.",
    instruction:
      "The search is filled in for you. Review all matching movements and their delivery labels.",
    example: "Search: DEMO-SHOW-301",
    action: "history",
    actionLabel: "Show house movement history",
  },
  {
    title: "Demo complete",
    narration:
      "This walkthrough showed house setup, accountable pickups, extra deliveries, returns, site-to-site transfers, live balances, and house completion.",
    instruction:
      "All example data is fictional and remains in the separate demo app. The production tracker and its database were not used.",
    example:
      "For the matching spoken narration and recording order, open DEMO-PRESENTATION.md in the demo project.",
  },
];
let toastTimer;
let movementSearchRequest = 0;

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
  return `${kind === "house" ? "House" : "Team"} ${reference}`;
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
  $("#account-details").textContent = `${user.username} · ${user.role === "admin" ? "Administrator" : "Employee"}`;
  $("#account-manage-users").classList.toggle("hidden", user.role !== "admin");
  $("#open-stocktake").classList.toggle("hidden", user.role !== "admin");
  $("#open-material").classList.toggle("hidden", user.role !== "admin");
  $("#open-house").classList.toggle("hidden", user.role !== "admin");
  $("#open-import-empty").classList.toggle("hidden", user.role !== "admin");
  $("#start-presentation").classList.toggle("hidden", user.role !== "admin");
  updateMovementFields();
  if (user.role !== "admin") closePresentation();
}

async function checkAuthentication() {
  const result = await request("/api/auth/status");
  if (result.user) {
    showWorkspace(result.user);
    await refresh();
  } else {
    showAuthScreen(result.setup_required);
  }
}

function setTab(tab) {
  $$(".nav-item").forEach((button) =>
    button.classList.toggle("active", button.dataset.tab === tab),
  );
  $$(".page").forEach((page) =>
    page.classList.toggle("active", page.dataset.page === tab),
  );
  if (tab === "movements") renderMovementTable();
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
      const houseSelects = $$(".site-reference", activeFields);
      const destination =
        state.dashboard.house_list.find(
          (house) => house.id === "DEMO-202" && house.id !== houseId,
        ) || state.dashboard.house_list.find((house) => house.id !== houseId);
      if (houseSelects.length !== 2 || !destination) {
        showToast(
          "Add another destination house in the demo before testing a transfer.",
          true,
        );
        $("#movement-dialog").close();
        return;
      }
      houseSelects[0].value = houseId;
      houseSelects[1].value = destination.id;
      $(".transfer-block .location-kind", activeFields).value = "house";
      $$(".location-kind", activeFields).forEach((select) =>
        select.dispatchEvent(new Event("change")),
      );
      houseSelects[0].value = houseId;
      houseSelects[1].value = destination.id;
      $("#movement-form [name='notes']").value =
        `Fictional presentation transfer to ${destination.id}.`;
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
    $(`.house-finish-toggle[data-house-id="${houseId}"]`)?.scrollIntoView({
      behavior: "smooth",
      block: "center",
    });
    minimizePresentation();
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

function renderOverview() {
  const data = state.dashboard;
  const materials = data.materials;
  $("#stat-materials").textContent = materials.length;
  $("#stat-houses").textContent = data.houses;
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
      <div class="activity-copy"><strong>${escapeHtml(item.material)}</strong>${isExtraDelivery(item) ? `<span class="status-pill extra-delivery">Extra delivery #${item.delivery_number}</span>` : `<span class="${isHouseToHouseTransfer(item) ? "status-pill house-transfer" : ""}">${escapeHtml(movementDisplayLabel(item))}</span>`}<span>${escapeHtml(item.date)}${item.department ? ` · ${escapeHtml(item.department)}` : ""}${item.team ? ` · ${escapeHtml(item.team)}` : ""}</span></div>
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
      <td><span class="material-name"><span class="material-dot"></span>${escapeHtml(item.name)}</span></td>
      <td class="code-cell">${escapeHtml(item.code || "—")}</td>
      <td class="${item.warehouse_qty === null ? "qty-unknown" : "qty-cell"}">${escapeHtml(formatQuantity(item.warehouse_qty, item.unit))}</td>
      <td class="${locationTotal(item, "team") < 0 ? "qty-negative" : ""}">${escapeHtml(formatQuantity(locationTotal(item, "team"), item.unit))}</td>
      <td class="${locationTotal(item, "house") < 0 ? "qty-negative" : ""}">${escapeHtml(formatQuantity(locationTotal(item, "house"), item.unit))}</td>
      <td>${escapeHtml(item.unit)}</td>
      <td><span class="status-pill ${item.needs_stocktake ? "warning" : ""}">${item.negative_locations?.length ? "Reconcile" : item.needs_stocktake ? "Count needed" : "Tracked"}</span></td>
      <td>${canAdministerInventory ? `<button type="button" class="text-button stock-edit" data-material-id="${item.id}">Edit warehouse stock</button>` : ""}</td>
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

function renderHouses() {
  const data = state.dashboard;
  const canManageHouses = state.user?.role === "admin";
  const houseCards = data.house_list.map((house) => {
    const held = data.materials.flatMap((material) => {
      const quantity = material.balances[`house:${house.id}`] || 0;
      return Math.abs(quantity) > 1e-9
        ? [{ name: material.name, qty: quantity, unit: material.unit }]
        : [];
    });
    const hasNegative = held.some((item) => item.qty < 0);
    return `
      <article class="card house-card ${house.is_finished ? "house-finished" : ""}">
        <div class="house-top"><span class="house-avatar">⌂</span><div class="house-meta"><h2>${escapeHtml(house.id)}</h2><p>${escapeHtml([house.owner_name, house.employee_name, house.postcode].filter(Boolean).join(" · ") || "Site details not recorded")}</p></div><span class="status-pill ${house.is_finished ? "finished" : hasNegative ? "warning" : ""}">${house.is_finished ? "Finished" : hasNegative ? "Reconcile" : `${held.length} materials`}</span></div>
        <div class="house-stock"><div class="house-stock-title">Materials on site</div>
          ${held.length ? held.map((item) => `<div class="balance-row"><span>${escapeHtml(item.name)}</span><span class="${item.qty < 0 ? "qty-negative" : ""}">${escapeHtml(formatQuantity(item.qty, item.unit))}</span></div>`).join("") : `<div class="balance-row"><span>No balance recorded</span><span>—</span></div>`}
        </div>
        <div class="house-card-actions">
          ${canManageHouses ? `
            <button class="text-button house-open" type="button" data-house-id="${escapeHtml(house.id)}">View details &amp; history <span>→</span></button>
            ${house.is_finished && held.some((item) => Math.abs(item.qty) > 1e-9) ? `<button class="text-button house-finish-toggle" type="button" data-house-id="${escapeHtml(house.id)}" data-action="closeout">Close out remaining stock</button>` : ""}
            <button class="text-button house-finish-toggle" type="button" data-house-id="${escapeHtml(house.id)}" data-finished="${house.is_finished ? "true" : "false"}">${house.is_finished ? "Reopen house" : "Mark house finished"}</button>` : ""}
        </div>
      </article>`;
  });
  const teams = [
    ...new Set(
      data.locations.filter((location) => location.startsWith("team:")),
    ),
  ];
  const teamCards = teams.map((location) => {
    const name = formatLocation(location);
    const held = data.materials.flatMap((material) => {
      const quantity = material.balances[location] || 0;
      return Math.abs(quantity) > 1e-9
        ? [{ name: material.name, qty: quantity, unit: material.unit }]
        : [];
    });
    const hasNegative = held.some((item) => item.qty < 0);
    return `
      <article class="team-card">
        <div class="house-top"><span class="house-avatar">♧</span><div class="house-meta"><h2>${escapeHtml(name)}</h2><p>Materials collected and currently held by this team</p></div>${hasNegative ? `<span class="status-pill warning">Reconcile</span>` : ""}</div>
        <div>${held.length ? held.map((item) => `<span class="team-badge ${item.qty < 0 ? "negative-badge" : ""}">${escapeHtml(item.name)} · ${escapeHtml(formatQuantity(item.qty, item.unit))}</span>`).join(" ") : `<span class="team-badge">No positive balance recorded</span>`}</div>
      </article>`;
  });
  $("#houses-list").innerHTML = [...houseCards, ...teamCards].join("");
  $("#houses-empty").classList.toggle(
    "hidden",
    data.house_list.length > 0 || teams.length > 0,
  );
}

function renderHouseDetail(result) {
  const { house, movements } = result;
  $("#house-detail-title").textContent = house.id;
  $("#house-detail-subtitle").textContent =
    [house.owner_name, house.employee_name, house.postcode]
      .filter(Boolean)
      .join(" · ") || "House details have not been recorded.";
  $("#house-detail-facts").innerHTML = [
    ["Owner", house.owner_name],
    ["Assigned employee", house.employee_name],
    ["Postcode / Eircode", house.postcode],
    ["Status", house.is_finished ? "Finished" : "Active"],
  ]
    .map(
      ([label, value]) => `
        <article class="card house-detail-fact">
          <div class="house-stock-title">${escapeHtml(label)}</div>
          <strong>${escapeHtml(value || "Not recorded")}</strong>
        </article>`,
    )
    .join("");

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
          <td>${escapeHtml(item.date)}</td>
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
  state.viewingHouseId = houseId;
  $("#houses-overview").classList.add("hidden");
  $("#house-detail").classList.remove("hidden");
  $("#house-detail-title").textContent = houseId;
  $("#house-detail-subtitle").textContent = "Loading house record…";
  $("#house-detail-facts").innerHTML = "";
  $("#house-detail-stock").innerHTML = "";
  $("#house-movement-history").innerHTML = "";
  $("#house-detail-usage-card").classList.add("hidden");
  $("#house-history-empty").classList.add("hidden");
  $(".house-history-card .table-wrap").classList.remove("hidden");
  loadHouseDetail(houseId);
}

function showHousesOverview() {
  state.viewingHouseId = "";
  state.houseDetailRequest += 1;
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
      <td>${escapeHtml(item.date)}</td>
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

function openHouseDialog() {
  $("#house-form").reset();
  $("#house-dialog").showModal();
}

function renderAll(preserveMovementLines = false) {
  $("#demo-banner").classList.toggle("hidden", !state.dashboard.demo_mode);
  renderOverview();
  renderInventory();
  renderHouses();
  renderMovementTable();
  refreshMovementOptions(preserveMovementLines);
}

async function refresh(preserveMovementLines = false) {
  const search = $("#movement-search")?.value.trim() || "";
  const [dashboard, ledger] = await Promise.all([
    request("/api/dashboard"),
    request(
      `/api/movements${search ? `?q=${encodeURIComponent(search)}` : ""}`,
    ),
  ]);
  state.dashboard = dashboard;
  state.movements = ledger.movements;
  renderAll(preserveMovementLines);
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

async function submitHouse(event) {
  event.preventDefault();
  const formElement = event.currentTarget;
  const button = $("button[type='submit']", formElement);
  button.disabled = true;
  const form = new FormData(formElement);
  try {
    await request("/api/houses", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.fromEntries(form)),
    });
    $("#house-dialog").close();
    formElement.reset();
    showToast("House saved and ready to search.");
    try {
      await refresh();
    } catch (error) {
      showToast(
        `House was saved, but the tracker could not refresh: ${error.message}`,
        true,
      );
    }
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function toggleHouseFinished(event) {
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
  button.disabled = true;
  try {
    const result = await request("/api/houses/status", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: houseId, finished }),
    });
    showToast(
      finished
        ? `${houseId} marked finished; ${result.materials_closed_out} remaining material balance(s) recorded as used.`
        : `${houseId} reopened.`,
    );
    try {
      await refresh();
    } catch (error) {
      showToast(
        `House status was saved, but the tracker could not refresh: ${error.message}`,
        true,
      );
    }
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
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
  $("#user-list").innerHTML = result.users
    .map(
      (user) => `
        <div class="user-row">
          <span><strong>${escapeHtml(user.display_name)}</strong><small>${escapeHtml(user.username)}</small></span>
          <span>${user.role === "admin" ? "Administrator" : "Employee"}${user.is_active ? "" : " · Disabled"}</span>
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
    showToast("Employee account created.");
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
  $$(".nav-item").forEach((button) =>
    button.addEventListener("click", () => setTab(button.dataset.tab)),
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
  $("#open-house").addEventListener("click", openHouseDialog);
  $("#back-to-houses").addEventListener("click", showHousesOverview);
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
  $("#houses-list").addEventListener("click", toggleHouseFinished);
  $("#stocktake-form").addEventListener("submit", submitStocktake);
  $("#import-form").addEventListener("submit", submitImport);
  $("#material-search").addEventListener("input", renderInventory);
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

function updateHouseSelection() {
  const value = $("#movement-house-id").value.trim();
  const house = state.dashboard.house_list.find(
    (item) => item.id.toLocaleLowerCase() === value.toLocaleLowerCase(),
  );
  if (house) {
    $("#movement-house-id").value = house.id;
    state.selectedHouseId = house.id;
    $("#movement-postcode").value = house.postcode || "";
    $("#movement-owner").value = house.owner_name || "";
    $("#movement-employee").value = house.employee_name || "";
  } else if (state.selectedHouseId !== value) {
    state.selectedHouseId = "";
    $("#movement-postcode").value = "";
    $("#movement-owner").value = "";
    $("#movement-employee").value = "";
  }
  renderHouseSuggestions();
  renderHouseLiveStock();
  renderMaterialLines();
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
