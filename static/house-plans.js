import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";

const form = document.querySelector("#plan-upload-form");
const fileInput = document.querySelector("#plan-file");
const statusMessage = document.querySelector("#plan-status");
const uploadButton = document.querySelector("#upload-button");
const planList = document.querySelector("#plan-list");

async function requestJson(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...options });
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error("The server returned an unreadable response.");
  }
  if (!response.ok) throw new Error(payload.error || "The request failed.");
  return payload;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function renderPlan(plan) {
  const card = element("article", "plan-card");
  card.append(element("h3", "", plan.filename));
  card.append(
    element(
      "p",
      "plan-meta",
      `${plan.analysis.content_type} PDF · ${plan.analysis.page_count} page(s) · Uploaded by ${plan.uploaded_by} · ${plan.uploaded_at}`,
    ),
  );

  const source = element("a", "source-link", "Open original PDF in a new tab");
  source.href = `/api/house-plans/${encodeURIComponent(plan.id)}/file`;
  source.target = "_blank";
  source.rel = "noreferrer";
  card.append(source);

  const limits = element("p", "muted");
  const ocrMessage = plan.analysis.ocr_available
    ? "Tesseract OCR is available."
    : "Tesseract executable not found; pages without embedded text cannot be OCR-processed.";
  limits.textContent = `${ocrMessage} Measurements and labels are unverified candidates.`;
  card.append(limits);
  if (plan.analysis.limitations?.length) {
    const limitationList = element("ul", "muted");
    plan.analysis.limitations.forEach((limitation) => {
      limitationList.append(element("li", "", limitation));
    });
    card.append(limitationList);
  }

  const dimensions = plan.analysis.dimensions || [];
  const dimensionSection = element("section", "analysis-section");
  dimensionSection.append(element("h4", "", `Dimension candidates (${dimensions.length})`));
  dimensions.forEach((dimension) => {
    dimensionSection.append(
      element("span", "tag", `${dimension.text} · page ${dimension.page} · unverified`),
    );
  });
  if (!dimensions.length) {
    dimensionSection.append(element("p", "muted", "No dimension candidates detected."));
  }
  card.append(dimensionSection);

  const details = element("details", "analysis-details");
  details.append(element("summary", "", "Page analysis and extracted text"));
  for (const page of plan.analysis.pages) {
    const pageSection = element("section", "plan-page");
    pageSection.append(element("h4", "", `Page ${page.page}`));
    pageSection.append(
      element(
        "p",
        "",
        `${page.vector_objects} vector objects · ${page.embedded_images} embedded image(s) · largest image covers ${Math.round(page.largest_image_coverage * 100)}% · OCR: ${page.ocr_status}`,
      ),
    );
    if (page.labels.length) {
      pageSection.append(element("p", "", `Text labels: ${page.labels.join(" · ")}`));
    }
    if (page.text) {
      pageSection.append(element("pre", "extracted-text", page.text));
    } else {
      pageSection.append(element("p", "muted", "No text was extracted from this page."));
    }
    details.append(pageSection);
  }
  card.append(details);
  card.append(createWallWorkspace(plan));
  return card;
}

function createWallWorkspace(plan) {
  const section = element("section", "model-workspace");
  section.append(element("h4", "", "Trace the plan and build a 3D model"));
  section.append(
    element(
      "p",
      "model-warning",
      "The 3D model is built only from the walls you trace. First calibrate against a dimension printed on the PDF; then trace each wall. The measurements you supply are saved in metres and are not independently verified.",
    ),
  );

  const tracer = element("section", "plan-tracing");
  const traceHelp = element(
    "p",
    "trace-help",
    "Choose a page and enter a printed dimension. Drag on that line to calibrate, then switch to Trace walls and drag along each wall centreline. Add labeled point notes or request a tentative exterior outline to review. Scroll the plan to reach other areas.",
  );
  tracer.append(traceHelp);

  const pageControls = element("div", "trace-controls");
  const pageLabel = element("label", "trace-field", "Plan page");
  const pageSelect = document.createElement("select");
  pageSelect.className = "trace-page";
  plan.analysis.pages.forEach((page) => {
    const option = document.createElement("option");
    option.value = String(page.page);
    option.textContent = `Page ${page.page}`;
    pageSelect.append(option);
  });
  pageLabel.append(pageSelect);
  pageControls.append(pageLabel);

  const calibrationLabel = element("label", "trace-field", "Printed dimension (m)");
  const calibrationLength = document.createElement("input");
  calibrationLength.className = "calibration-length";
  calibrationLength.type = "number";
  calibrationLength.min = "0.001";
  calibrationLength.max = "10000";
  calibrationLength.step = "any";
  calibrationLength.placeholder = "e.g. 4.20";
  calibrationLength.required = true;
  calibrationLabel.append(calibrationLength);
  pageControls.append(calibrationLabel);

  const heightLabel = element("label", "trace-field", "Wall height (m)");
  const wallHeight = document.createElement("input");
  wallHeight.className = "trace-height";
  wallHeight.type = "number";
  wallHeight.min = "1";
  wallHeight.max = "10";
  wallHeight.step = "any";
  wallHeight.placeholder = "confirm metres";
  heightLabel.append(wallHeight);
  pageControls.append(heightLabel);

  const thicknessLabel = element("label", "trace-field", "Wall thickness (m)");
  const wallThickness = document.createElement("input");
  wallThickness.className = "trace-thickness";
  wallThickness.type = "number";
  wallThickness.min = "0.03";
  wallThickness.max = "1";
  wallThickness.step = "any";
  wallThickness.placeholder = "confirm metres";
  thicknessLabel.append(wallThickness);
  pageControls.append(thicknessLabel);
  tracer.append(pageControls);

  const traceActions = element("div", "trace-actions");
  const calibrationButton = element("button", "mode-button secondary", "Calibrate scale");
  calibrationButton.type = "button";
  calibrationButton.dataset.traceMode = "calibrate";
  const wallButton = element("button", "mode-button secondary", "Trace walls");
  wallButton.type = "button";
  wallButton.dataset.traceMode = "wall";
  const noteButton = element("button", "mode-button secondary", "Place a note");
  noteButton.type = "button";
  noteButton.dataset.traceMode = "note";
  const exteriorLabel = element("label", "trace-exterior");
  const exteriorInput = document.createElement("input");
  exteriorInput.type = "checkbox";
  exteriorInput.className = "trace-exterior-input";
  exteriorInput.checked = true;
  exteriorLabel.append(exteriorInput, document.createTextNode("Exterior wall"));
  traceActions.append(calibrationButton, wallButton, noteButton, exteriorLabel);
  tracer.append(traceActions);

  const noteControls = element("div", "plan-note-controls");
  const noteLabel = element("label", "trace-field", "Point note");
  const noteInput = document.createElement("input");
  noteInput.className = "plan-note-input";
  noteInput.type = "text";
  noteInput.maxLength = 80;
  noteInput.placeholder = "e.g. Vent";
  noteLabel.append(noteInput);
  noteControls.append(noteLabel);
  const suggestButton = element("button", "secondary suggest-exterior", "Suggest exterior outline");
  suggestButton.type = "button";
  noteControls.append(suggestButton);
  tracer.append(noteControls);

  const suggestionActions = element("div", "suggestion-actions");
  const acceptSuggestionButton = element(
    "button",
    "secondary accept-outline",
    "Add reviewed outline as exterior walls",
  );
  acceptSuggestionButton.type = "button";
  acceptSuggestionButton.hidden = true;
  const clearSuggestionButton = element("button", "secondary clear-outline", "Dismiss suggestion");
  clearSuggestionButton.type = "button";
  clearSuggestionButton.hidden = true;
  suggestionActions.append(acceptSuggestionButton, clearSuggestionButton);
  tracer.append(suggestionActions);

  const traceStatus = element(
    "p",
    "trace-status",
    "Calibrate the scale using a known dimension from the selected PDF page.",
  );
  traceStatus.setAttribute("role", "status");
  tracer.append(traceStatus);

  const planFrame = element("div", "plan-tracer");
  const planZoomControls = element("div", "plan-zoom-controls");
  const zoomOutButton = element("button", "plan-zoom-button secondary", "−");
  zoomOutButton.type = "button";
  zoomOutButton.setAttribute("aria-label", "Zoom out on source plan");
  const fitPageButton = element("button", "plan-zoom-button secondary", "Fit page");
  fitPageButton.type = "button";
  const zoomInButton = element("button", "plan-zoom-button secondary", "+");
  zoomInButton.type = "button";
  zoomInButton.setAttribute("aria-label", "Zoom in on source plan");
  const zoomLevel = element("span", "plan-zoom-level", "100%");
  planZoomControls.append(zoomOutButton, fitPageButton, zoomInButton, zoomLevel);
  tracer.append(planZoomControls);

  const planLayer = element("div", "plan-image-layer");
  const pageImage = element("img", "plan-reference");
  pageImage.alt = `${plan.filename} page 1 plan for wall tracing`;
  pageImage.decoding = "async";
  const traceCanvas = element("canvas", "trace-overlay");
  traceCanvas.setAttribute("aria-label", "Trace confirmed house walls over the source PDF");
  planLayer.append(pageImage, traceCanvas);
  planFrame.append(planLayer);
  tracer.append(planFrame);
  section.append(tracer);

  const modelStatus = element(
    "p",
    "model-status",
    "No 3D model yet. Calibrate the drawing and trace walls to create one.",
  );
  modelStatus.setAttribute("role", "status");
  section.append(modelStatus);

  const controls = element("div", "viewer-controls");
  const presetButtons = [
    ["3D", "3d"],
    ["Top", "top"],
    ["Front", "front"],
    ["Rear", "rear"],
    ["Side", "side"],
    ["Technical / wireframe", "wireframe"],
    ["Cutaway", "cutaway"],
    ["Roof unavailable", "roof"],
  ];
  presetButtons.forEach(([label, action]) => {
    const button = element("button", "viewer-button secondary", label);
    button.type = "button";
    button.dataset.view = action;
    if (action === "roof") {
      button.disabled = true;
      button.title = "The uploaded floor plan does not contain verified roof geometry.";
    }
    controls.append(button);
  });
  section.append(controls);

  const viewport = element("div", "viewer-viewport");
  viewport.setAttribute("aria-label", "Interactive Three.js GLB house model");
  viewport.append(element("p", "viewer-empty", "Your saved 3D model will appear here."));
  section.append(viewport);
  section.append(
    element(
      "p",
      "viewer-help",
      "Real Three.js GLB model · drag to orbit · scroll to zoom · right-drag to pan · click a wall for its measured length. Choose a color beside each wall and save to keep the marking. The floor is created only when the exterior trace forms a closed loop; its wood finish is illustrative.",
    ),
  );
  const modelNoteControls = element("div", "model-note-controls");
  const modelNoteTitleLabel = element("label", "trace-field", "3D note title");
  const modelNoteTitle = document.createElement("input");
  modelNoteTitle.className = "model-note-title";
  modelNoteTitle.type = "text";
  modelNoteTitle.maxLength = 80;
  modelNoteTitle.placeholder = "e.g. Vent";
  modelNoteTitleLabel.append(modelNoteTitle);
  const modelNoteTextLabel = element("label", "trace-field", "Note");
  const modelNoteText = document.createElement("input");
  modelNoteText.className = "model-note-text";
  modelNoteText.type = "text";
  modelNoteText.maxLength = 250;
  modelNoteText.placeholder = "Optional details";
  modelNoteTextLabel.append(modelNoteText);
  const placeModelNoteButton = element("button", "secondary place-model-note", "Place note on 3D model");
  placeModelNoteButton.type = "button";
  modelNoteControls.append(modelNoteTitleLabel, modelNoteTextLabel, placeModelNoteButton);
  section.append(modelNoteControls);
  const modelNoteHelp = element(
    "p",
    "viewer-help",
    "Enter a title and optional note, choose Place note, then click a visible wall or floor in the 3D viewer. Notes are saved with the model.",
  );
  section.append(modelNoteHelp);
  const savedModelNotes = element("div", "saved-model-notes");
  section.append(savedModelNotes);

  const wallListHeading = element("h5", "wall-list-heading", "Traced walls");
  section.append(wallListHeading);
  const savedWalls = element("div", "saved-walls");
  section.append(savedWalls);
  section.append(element("h5", "wall-list-heading", "Plan notes"));
  const savedAnnotations = element("div", "saved-annotations");
  section.append(savedAnnotations);

  const actionRow = element("div", "model-actions");
  const saveButton = element("button", "save-geometry", "Save traces and build 3D model");
  saveButton.type = "button";
  saveButton.disabled = true;
  const downloadLink = element("a", "download-model", "Download GLB");
  downloadLink.hidden = true;
  downloadLink.download = `house-plan-${plan.id}.glb`;
  actionRow.append(saveButton, downloadLink);
  section.append(actionRow);

  const geometryDetails = element("details", "geometry-details");
  geometryDetails.append(
    element("summary", "", "Advanced: enter wall coordinates in metres"),
  );
  const wallForm = element("form", "wall-form");
  const wallGrid = element("div", "wall-form-grid");
  const wallFields = [
    ["label", "Wall label", "text", "Wall 1"],
    ["x1", "Start X (m)", "number", ""],
    ["y1", "Start Y (m)", "number", ""],
    ["x2", "End X (m)", "number", ""],
    ["y2", "End Y (m)", "number", ""],
    ["height", "Height (m)", "number", "Required"],
    ["thickness", "Thickness (m)", "number", "Required"],
    ["page", "Source page", "number", "1"],
  ];
  wallFields.forEach(([name, label, type, placeholder]) => {
    const field = element("label", "wall-field", label);
    const input = document.createElement("input");
    input.name = name;
    input.type = type;
    input.required = true;
    input.autocomplete = "off";
    if (type === "number") {
      input.step = name === "page" ? "1" : "any";
      input.inputMode = "decimal";
      if (name === "page") {
        input.min = "1";
        input.max = String(plan.analysis.page_count);
        input.value = "1";
      }
    } else {
      input.maxLength = 80;
    }
    input.placeholder = placeholder;
    field.append(input);
    wallGrid.append(field);
  });
  wallForm.append(wallGrid);
  const manualExteriorLabel = element("label", "exterior-field");
  const manualExteriorInput = document.createElement("input");
  manualExteriorInput.name = "exterior";
  manualExteriorInput.type = "checkbox";
  manualExteriorLabel.append(
    manualExteriorInput,
    document.createTextNode("Exterior wall"),
  );
  wallForm.append(manualExteriorLabel);
  const addButton = element("button", "add-wall", "Add measured wall");
  addButton.type = "submit";
  wallForm.append(addButton);
  geometryDetails.append(wallForm);
  section.append(geometryDetails);
  section.append(
    element(
      "p",
      "muted",
      "Doors, windows, room labels, furniture, ceilings, and roof geometry are not inferred or fabricated. Roof view remains unavailable until verified roof geometry is supported.",
    ),
  );

  const viewer = createViewer(viewport, modelStatus);
  const walls = [];
  const annotations = [];
  const modelAnnotations = [];
  const fileId = String(plan.id);
  const scalesByPage = new Map();
  const calibrationByPage = new Map();
  let traceMode = "calibrate";
  let traceStart = null;
  let draftSegment = null;
  let suggestedOutline = [];
  let lastSavedSignature = "";
  let pageZoom = 1;

  function setTraceStatus(message, isError = false) {
    traceStatus.textContent = message;
    traceStatus.classList.toggle("error", isError);
  }

  function currentPage() {
    return Number(pageSelect.value);
  }

  function currentScale() {
    return scalesByPage.get(currentPage()) || 0;
  }

  function geometryPayload() {
    return {
      walls,
      annotations,
      model_annotations: modelAnnotations,
      calibration: Object.fromEntries(calibrationByPage),
    };
  }

  function geometrySignature() {
    return JSON.stringify(geometryPayload());
  }

  function updateSaveButton() {
    saveButton.disabled = (!walls.length && !annotations.length && !modelAnnotations.length)
      || Boolean(lastSavedSignature && geometrySignature() === lastSavedSignature);
  }

  function restoreCalibrationScales() {
    for (const [pageNumber, metresPerPoint] of calibrationByPage) {
      const page = plan.analysis.pages.find((item) => item.page === pageNumber);
      if (!page || page.width_points <= 0) continue;
      const renderScale = Math.min(
        1.75,
        3200 / Math.max(page.width_points, page.height_points),
      );
      const renderedWidth = pageNumber === currentPage() && pageImage.naturalWidth
        ? pageImage.naturalWidth
        : page.width_points * renderScale;
      scalesByPage.set(
        pageNumber,
        metresPerPoint * page.width_points / renderedWidth,
      );
    }
  }

  function renderTraceOverlay() {
    if (!pageImage.naturalWidth || !pageImage.naturalHeight) return;
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    traceCanvas.width = Math.round(pageImage.naturalWidth * ratio);
    traceCanvas.height = Math.round(pageImage.naturalHeight * ratio);
    const context = traceCanvas.getContext("2d");
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, pageImage.naturalWidth, pageImage.naturalHeight);
    const scale = currentScale();
    context.lineWidth = Math.max(3, pageImage.naturalWidth / 400);
    context.lineCap = "round";
    for (const wall of walls.filter((item) => item.page === currentPage())) {
      if (!scale) continue;
      context.strokeStyle = wall.color || (wall.exterior ? "#147b58" : "#3587c7");
      context.beginPath();
      context.moveTo(wall.x1 / scale, wall.y1 / scale);
      context.lineTo(wall.x2 / scale, wall.y2 / scale);
      context.stroke();
    }
    for (const annotation of annotations.filter((item) => item.page === currentPage())) {
      const x = annotation.x * pageImage.naturalWidth;
      const y = annotation.y * pageImage.naturalHeight;
      context.fillStyle = "#b33c32";
      context.strokeStyle = "#fff";
      context.lineWidth = Math.max(2, pageImage.naturalWidth / 900);
      context.beginPath();
      context.arc(x, y, Math.max(8, pageImage.naturalWidth / 100), 0, Math.PI * 2);
      context.fill();
      context.stroke();
      context.fillStyle = "#fff";
      context.font = `bold ${Math.max(11, pageImage.naturalWidth / 75)}px sans-serif`;
      context.textAlign = "center";
      context.textBaseline = "middle";
      context.fillText("!", x, y);
      context.font = `600 ${Math.max(12, pageImage.naturalWidth / 90)}px sans-serif`;
      context.textAlign = "left";
      context.textBaseline = "bottom";
      context.lineWidth = Math.max(3, pageImage.naturalWidth / 300);
      context.strokeStyle = "#fff";
      context.strokeText(annotation.label, x + 12, y - 9);
      context.fillStyle = "#8c2c25";
      context.fillText(annotation.label, x + 12, y - 9);
    }
    if (suggestedOutline.length) {
      context.save();
      context.strokeStyle = "#e18b22";
      context.fillStyle = "#e18b22";
      context.lineWidth = Math.max(4, pageImage.naturalWidth / 300);
      context.setLineDash([14, 9]);
      context.beginPath();
      suggestedOutline.forEach((point, index) => {
        const x = point.x * pageImage.naturalWidth;
        const y = point.y * pageImage.naturalHeight;
        if (index === 0) context.moveTo(x, y);
        else context.lineTo(x, y);
      });
      context.closePath();
      context.stroke();
      context.setLineDash([]);
      for (const point of suggestedOutline) {
        context.beginPath();
        context.arc(
          point.x * pageImage.naturalWidth,
          point.y * pageImage.naturalHeight,
          Math.max(5, pageImage.naturalWidth / 180),
          0,
          Math.PI * 2,
        );
        context.fill();
      }
      context.restore();
    }
    if (draftSegment) {
      context.strokeStyle = traceMode === "calibrate" ? "#d8962f" : "#b447c2";
      context.setLineDash([12, 8]);
      context.beginPath();
      context.moveTo(draftSegment.x1, draftSegment.y1);
      context.lineTo(draftSegment.x2, draftSegment.y2);
      context.stroke();
      context.setLineDash([]);
    }
  }

  function setPageZoom(zoom, keepPosition = false) {
    const previousZoom = pageZoom;
    pageZoom = Math.max(0.5, Math.min(3, Math.round(zoom * 4) / 4));
    planLayer.style.width = `${pageZoom * 100}%`;
    zoomLevel.textContent = `${Math.round(pageZoom * 100)}%`;
    zoomOutButton.disabled = pageZoom <= 0.5;
    zoomInButton.disabled = pageZoom >= 3;
    if (keepPosition && previousZoom !== pageZoom) {
      const ratio = pageZoom / previousZoom;
      planFrame.scrollLeft *= ratio;
      planFrame.scrollTop *= ratio;
    } else if (!keepPosition) {
      planFrame.scrollTo({ left: 0, top: 0 });
    }
    renderTraceOverlay();
  }

  function renderWalls(selectedId = "") {
    savedWalls.replaceChildren();
    walls.forEach((wall) => {
      const row = element("div", "saved-wall-row");
      const length = Math.hypot(wall.x2 - wall.x1, wall.y2 - wall.y1);
      row.append(
        element(
          "span",
          "",
          `${wall.label} · page ${wall.page} · ${length.toFixed(2)} m · ${wall.height} m high · ${wall.thickness} m thick${wall.exterior ? " · exterior" : " · partition"}`,
        ),
      );
      const colorPicker = document.createElement("input");
      colorPicker.className = "wall-color";
      colorPicker.type = "color";
      colorPicker.value = wall.color || (wall.exterior ? "#d77b55" : "#5797c4");
      colorPicker.setAttribute("aria-label", `Color for ${wall.label}`);
      colorPicker.title = "Choose this wall's color";
      colorPicker.addEventListener("input", () => {
        wall.color = colorPicker.value.toLowerCase();
        lastSavedSignature = "";
        saveButton.disabled = false;
        downloadLink.hidden = true;
        modelStatus.textContent = "Unsaved wall color. Save traces to rebuild the 3D model.";
        viewer.setWallColor(wall.id, wall.color);
        renderTraceOverlay();
      });
      const select = element("button", "select-wall secondary", "Inspect");
      select.type = "button";
      select.addEventListener("click", () => viewer.select(wall.id));
      row.append(colorPicker, select);
      const remove = element("button", "remove-wall secondary", "Remove");
      remove.type = "button";
      remove.addEventListener("click", () => {
        walls.splice(walls.indexOf(wall), 1);
        lastSavedSignature = "";
        modelStatus.textContent = "Unsaved geometry. Save traces to rebuild the 3D model.";
        downloadLink.hidden = true;
        viewer.clear();
        renderWalls();
      });
      row.append(remove);
      if (wall.id === selectedId) row.classList.add("selected");
      savedWalls.append(row);
    });
    updateSaveButton();
    renderTraceOverlay();
  }

  function renderAnnotations() {
    savedAnnotations.replaceChildren();
    annotations.forEach((annotation) => {
      const row = element(
        "div",
        "saved-annotation-row",
        `${annotation.label} · page ${annotation.page}`,
      );
      const remove = element("button", "remove-annotation secondary", "Remove");
      remove.type = "button";
      remove.addEventListener("click", () => {
        annotations.splice(annotations.indexOf(annotation), 1);
        lastSavedSignature = "";
        modelStatus.textContent = "Unsaved plan notes. Save to keep your changes.";
        downloadLink.hidden = true;
        renderAnnotations();
      });
      row.append(remove);
      savedAnnotations.append(row);
    });
    updateSaveButton();
    renderTraceOverlay();
  }

  function renderModelNotes() {
    savedModelNotes.replaceChildren();
    modelAnnotations.forEach((annotation) => {
      const row = element("div", "saved-model-note");
      const content = element("span", "saved-model-note-content");
      content.append(element("strong", "", annotation.title));
      if (annotation.note) content.append(element("span", "", annotation.note));
      row.append(content);
      const remove = element("button", "secondary remove-model-note", "Remove");
      remove.type = "button";
      remove.addEventListener("click", () => {
        modelAnnotations.splice(modelAnnotations.indexOf(annotation), 1);
        lastSavedSignature = "";
        modelStatus.textContent = "Unsaved 3D model notes. Save to keep your changes.";
        downloadLink.hidden = true;
        viewer.setAnnotations(modelAnnotations);
        renderModelNotes();
      });
      row.append(remove);
      savedModelNotes.append(row);
    });
    viewer.setAnnotations(modelAnnotations);
    updateSaveButton();
  }

  function pointFromEvent(event) {
    const rectangle = traceCanvas.getBoundingClientRect();
    return {
      x: Math.max(
        0,
        Math.min(
          pageImage.naturalWidth,
          (event.clientX - rectangle.left) * pageImage.naturalWidth / rectangle.width,
        ),
      ),
      y: Math.max(
        0,
        Math.min(
          pageImage.naturalHeight,
          (event.clientY - rectangle.top) * pageImage.naturalHeight / rectangle.height,
        ),
      ),
    };
  }

  function setTraceMode(mode) {
    traceMode = mode;
    traceStart = null;
    draftSegment = null;
    calibrationButton.classList.toggle("active", mode === "calibrate");
    wallButton.classList.toggle("active", mode === "wall");
    noteButton.classList.toggle("active", mode === "note");
    traceCanvas.classList.toggle("calibration-mode", mode === "calibrate");
    traceCanvas.classList.toggle("wall-mode", mode === "wall");
    traceCanvas.classList.toggle("note-mode", mode === "note");
    renderTraceOverlay();
  }

  function addTracedWall(start, end) {
    const scale = currentScale();
    if (!scale) {
      setTraceStatus("Calibrate the selected page before tracing walls.", true);
      return;
    }
    const height = Number(wallHeight.value);
    const thickness = Number(wallThickness.value);
    const metres = Math.hypot(end.x - start.x, end.y - start.y) * scale;
    if (!Number.isFinite(height) || height < 1 || height > 10) {
      setTraceStatus("Enter a wall height from 1 to 10 metres.", true);
      return;
    }
    if (!Number.isFinite(thickness) || thickness < 0.03 || thickness > 1) {
      setTraceStatus("Enter a wall thickness from 0.03 to 1 metre.", true);
      return;
    }
    if (metres < 0.1) {
      setTraceStatus("Trace a wall at least 0.1 metres long.", true);
      return;
    }
    const page = currentPage();
    walls.push({
      id: crypto.randomUUID(),
      label: `${exteriorInput.checked ? "Exterior" : "Partition"} wall ${walls.length + 1}`,
      x1: start.x * scale,
      y1: start.y * scale,
      x2: end.x * scale,
      y2: end.y * scale,
      height,
      thickness,
      exterior: exteriorInput.checked,
      color: exteriorInput.checked ? "#d77b55" : "#5797c4",
      page,
    });
    lastSavedSignature = "";
    modelStatus.textContent = "Unsaved traced geometry. Save the traces to generate the GLB model.";
    downloadLink.hidden = true;
    viewer.clear();
    setTraceStatus(
      `Wall added at the calibrated scale: ${metres.toFixed(2)} m. Verify its length and wall type against the PDF.`,
    );
    renderWalls();
  }

  traceCanvas.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || !pageImage.naturalWidth) return;
    event.preventDefault();
    traceCanvas.setPointerCapture(event.pointerId);
    traceStart = pointFromEvent(event);
    draftSegment = traceMode === "note"
      ? null
      : {
          x1: traceStart.x,
          y1: traceStart.y,
          x2: traceStart.x,
          y2: traceStart.y,
        };
    renderTraceOverlay();
  });
  traceCanvas.addEventListener("pointermove", (event) => {
    if (!traceStart || traceMode === "note") return;
    const point = pointFromEvent(event);
    draftSegment.x2 = point.x;
    draftSegment.y2 = point.y;
    renderTraceOverlay();
  });
  traceCanvas.addEventListener("pointerup", (event) => {
    if (!traceStart) return;
    const start = traceStart;
    const end = pointFromEvent(event);
    const pixelLength = Math.hypot(end.x - start.x, end.y - start.y);
    traceStart = null;
    draftSegment = null;
    if (traceMode === "note") {
      const label = noteInput.value.trim();
      if (!label || label.length > 80) {
        setTraceStatus("Enter a note, such as Vent, then click its position on the plan.", true);
        renderTraceOverlay();
        return;
      }
      annotations.push({
        id: crypto.randomUUID(),
        label,
        x: end.x / pageImage.naturalWidth,
        y: end.y / pageImage.naturalHeight,
        page: currentPage(),
      });
      lastSavedSignature = "";
      downloadLink.hidden = true;
      modelStatus.textContent = "Unsaved plan note. Save to keep your changes.";
      setTraceStatus(`Added “${label}” to page ${currentPage()}. Save to keep this note.`);
      renderAnnotations();
      return;
    }
    if (pixelLength < 4) {
      setTraceStatus("Drag along a drawing line to mark a measurement.", true);
      renderTraceOverlay();
      return;
    }
    if (traceMode === "calibrate") {
      const knownLength = Number(calibrationLength.value);
      if (!Number.isFinite(knownLength) || knownLength <= 0 || knownLength > 10000) {
        setTraceStatus("Enter the dimension printed on the plan in metres.", true);
        renderTraceOverlay();
        return;
      }
      const scale = knownLength / pixelLength;
      scalesByPage.set(currentPage(), scale);
      const pageInfo = plan.analysis.pages.find(
        (item) => item.page === currentPage(),
      );
      if (!pageInfo || pageInfo.width_points <= 0) {
        setTraceStatus("The selected PDF page dimensions are unavailable.", true);
        renderTraceOverlay();
        return;
      }
      calibrationByPage.set(
        currentPage(),
        scale * pageImage.naturalWidth / pageInfo.width_points,
      );
      restoreCalibrationScales();
      setTraceStatus(
        `Scale recorded from your ${knownLength} m calibration on page ${currentPage()}. Confirm the dimension and endpoints before using the scale.`,
      );
      renderWalls();
      return;
    }
    addTracedWall(start, end);
  });
  traceCanvas.addEventListener("pointercancel", () => {
    traceStart = null;
    draftSegment = null;
    renderTraceOverlay();
  });
  traceCanvas.addEventListener("contextmenu", (event) => event.preventDefault());
  pageImage.addEventListener("load", () => {
    restoreCalibrationScales();
    setPageZoom(1);
    renderTraceOverlay();
  });
  pageImage.addEventListener("error", () => {
    setTraceStatus("The source plan page could not be rendered. Refresh the plans and try again.", true);
  });
  pageSelect.addEventListener("change", () => {
    const page = currentPage();
    suggestedOutline = [];
    acceptSuggestionButton.hidden = true;
    clearSuggestionButton.hidden = true;
    pageImage.src = `/api/house-plans/${encodeURIComponent(fileId)}/page/${page}.png`;
    pageImage.alt = `${plan.filename} page ${page} plan for wall tracing`;
    calibrationLength.value = "";
    setTraceStatus(
      scalesByPage.has(page)
        ? `Page ${page} already has a scale calibrated by you. Check it against a known printed dimension.`
        : `Enter a known printed dimension for page ${page}, then calibrate its scale.`,
    );
    renderTraceOverlay();
  });
  zoomOutButton.addEventListener("click", () => {
    setPageZoom(pageZoom - 0.25, true);
  });
  zoomInButton.addEventListener("click", () => {
    setPageZoom(pageZoom + 0.25, true);
  });
  fitPageButton.addEventListener("click", () => setPageZoom(1));
  calibrationButton.addEventListener("click", () => setTraceMode("calibrate"));
  wallButton.addEventListener("click", () => setTraceMode("wall"));
  noteButton.addEventListener("click", () => setTraceMode("note"));
  suggestButton.addEventListener("click", async () => {
    suggestButton.disabled = true;
    setTraceStatus("Looking for a possible closed exterior outline. Review it carefully.");
    suggestedOutline = [];
    renderTraceOverlay();
    try {
      const result = await requestJson(
        `/api/house-plans/${encodeURIComponent(fileId)}/suggestions/exterior/${currentPage()}`,
      );
      suggestedOutline = result.points || [];
      acceptSuggestionButton.hidden = suggestedOutline.length === 0;
      clearSuggestionButton.hidden = suggestedOutline.length === 0;
      setTraceStatus(result.message, suggestedOutline.length === 0);
      renderTraceOverlay();
    } catch (error) {
      setTraceStatus(error.message, true);
    } finally {
      suggestButton.disabled = false;
    }
  });
  clearSuggestionButton.addEventListener("click", () => {
    suggestedOutline = [];
    acceptSuggestionButton.hidden = true;
    clearSuggestionButton.hidden = true;
    setTraceStatus("The suggested outline was dismissed; no walls were added.");
    renderTraceOverlay();
  });
  acceptSuggestionButton.addEventListener("click", () => {
    const scale = currentScale();
    const height = Number(wallHeight.value);
    const thickness = Number(wallThickness.value);
    if (!scale) {
      setTraceStatus("Calibrate this PDF page before accepting the suggested outline.", true);
      return;
    }
    if (!Number.isFinite(height) || height < 1 || height > 10) {
      setTraceStatus("Enter a confirmed wall height from 1 to 10 metres first.", true);
      return;
    }
    if (!Number.isFinite(thickness) || thickness < 0.03 || thickness > 1) {
      setTraceStatus("Enter a confirmed wall thickness from 0.03 to 1 metre first.", true);
      return;
    }
    const points = suggestedOutline.map((point) => ({
      x: point.x * pageImage.naturalWidth,
      y: point.y * pageImage.naturalHeight,
    }));
    const segments = points.map((point, index) => [
      point,
      points[(index + 1) % points.length],
    ]);
    if (
      segments.length < 3
      || segments.some(([start, end]) => Math.hypot(
        end.x - start.x,
        end.y - start.y,
      ) * scale < 0.1)
    ) {
      setTraceStatus("The suggested outline contains a segment too short to use. Dismiss it and trace manually.", true);
      return;
    }
    segments.forEach(([start, end], index) => {
      walls.push({
        id: crypto.randomUUID(),
        label: `Suggested exterior wall ${walls.length + 1}`,
        x1: start.x * scale,
        y1: start.y * scale,
        x2: end.x * scale,
        y2: end.y * scale,
        height,
        thickness,
        exterior: true,
        color: "#d77b55",
        page: currentPage(),
      });
    });
    suggestedOutline = [];
    acceptSuggestionButton.hidden = true;
    clearSuggestionButton.hidden = true;
    lastSavedSignature = "";
    modelStatus.textContent = "Unsaved suggested walls. Inspect each segment against the PDF before saving.";
    downloadLink.hidden = true;
    viewer.clear();
    setTraceStatus("Added the reviewed outline as draft exterior walls. Check every wall and remove any incorrect segment before saving.");
    renderWalls();
  });
  traceActions.addEventListener("click", (event) => {
    const mode = event.target.closest("[data-trace-mode]")?.dataset.traceMode;
    if (mode) setTraceMode(mode);
  });

  wallForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!wallForm.reportValidity()) return;
    const data = new FormData(wallForm);
    const number = (name) => Number(data.get(name));
    walls.push({
      id: crypto.randomUUID(),
      label: String(data.get("label")).trim(),
      x1: number("x1"),
      y1: number("y1"),
      x2: number("x2"),
      y2: number("y2"),
      height: number("height"),
      thickness: number("thickness"),
      exterior: data.has("exterior"),
      color: data.has("exterior") ? "#d77b55" : "#5797c4",
      page: number("page"),
    });
    lastSavedSignature = "";
    wallForm.reset();
    wallForm.elements.label.value = `Wall ${walls.length + 1}`;
    modelStatus.textContent = "Unsaved measured geometry. Confirm these dimensions against the source PDF.";
    downloadLink.hidden = true;
    viewer.clear();
    renderWalls();
  });

  placeModelNoteButton.addEventListener("click", () => {
    const title = modelNoteTitle.value.trim();
    const note = modelNoteText.value.trim();
    if (!title) {
      modelStatus.textContent = "Enter a title for the 3D model note first.";
      modelStatus.classList.add("error");
      modelNoteTitle.focus();
      return;
    }
    if (!walls.length) {
      modelStatus.textContent = "Add and build at least one wall before placing a 3D note.";
      modelStatus.classList.add("error");
      return;
    }
    viewer.startAnnotationPlacement((position) => {
      modelAnnotations.push({
        id: crypto.randomUUID(),
        title,
        note,
        x: position.x,
        y: position.y,
        z: position.z,
      });
      lastSavedSignature = "";
      downloadLink.hidden = true;
      modelStatus.classList.remove("error");
      modelStatus.textContent = `Placed “${title}” on the 3D model. Save to keep the note.`;
      placeModelNoteButton.classList.remove("active");
      renderModelNotes();
    });
    placeModelNoteButton.classList.add("active");
    modelStatus.classList.remove("error");
    modelStatus.textContent = `Click a visible surface in the 3D model to place “${title}”.`;
  });

  saveButton.addEventListener("click", async () => {
    if (!walls.length && !annotations.length && !modelAnnotations.length) return;
    saveButton.disabled = true;
    modelStatus.classList.remove("error");
    modelStatus.textContent = "Saving confirmed wall geometry and generating the GLB model…";
    try {
      await requestJson(`/api/house-plans/${encodeURIComponent(fileId)}/geometry`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(geometryPayload()),
      });
      lastSavedSignature = geometrySignature();
      if (!walls.length) {
        modelStatus.textContent =
          `Saved ${annotations.length} plan note(s) and ${modelAnnotations.length} 3D note(s). Add walls to build a 3D model.`;
        downloadLink.hidden = true;
        setTraceStatus("Your plan and 3D notes are saved.");
        return;
      }
      const result = await viewer.loadModel(
        `/api/house-plans/${encodeURIComponent(fileId)}/model.glb`,
      );
      viewer.setAnnotations(modelAnnotations);
      const floorMessage = result.floorGenerated
        ? " A closed exterior loop produced the illustrative wood floor."
        : " No floor was added because the exterior walls do not form one closed loop.";
      modelStatus.textContent =
        `Saved ${walls.length} user-confirmed wall(s); the real Three.js GLB model is ready.` + floorMessage;
      downloadLink.href = `/api/house-plans/${encodeURIComponent(fileId)}/model.glb`;
      downloadLink.hidden = false;
      setTraceStatus("The GLB contains only the traced wall geometry and any closed-loop floor.");
    } catch (error) {
      modelStatus.textContent = error.message;
      modelStatus.classList.add("error");
    } finally {
      updateSaveButton();
    }
  });

  controls.addEventListener("click", (event) => {
    const action = event.target.closest("button")?.dataset.view;
    if (action) viewer.setView(action);
  });

  const geometryUrl = `/api/house-plans/${encodeURIComponent(fileId)}/geometry`;
  requestJson(geometryUrl)
    .then(({ geometry }) => {
      geometry.walls.forEach((wall) => walls.push({ ...wall, page: wall.page || 1 }));
      (geometry.annotations || []).forEach((annotation) => {
        annotations.push({ ...annotation, page: annotation.page || 1 });
      });
      (geometry.model_annotations || []).forEach((annotation) => {
        modelAnnotations.push(annotation);
      });
      Object.entries(geometry.calibration || {}).forEach(([page, scale]) => {
        const pageNumber = Number(page);
        if (Number.isFinite(pageNumber) && Number.isFinite(scale)) {
          calibrationByPage.set(pageNumber, scale);
        }
      });
      restoreCalibrationScales();
      if (calibrationByPage.has(currentPage())) {
        setTraceStatus(
          `Restored your saved scale for page ${currentPage()}. Check it against a known printed dimension before adding walls.`,
        );
      }
      renderWalls();
      renderAnnotations();
      renderModelNotes();
      if (walls.length) {
        modelStatus.textContent = `Loaded ${walls.length} saved wall(s). Check their measurements against the PDF.`;
        viewer.loadModel(
          `/api/house-plans/${encodeURIComponent(fileId)}/model.glb`,
        ).then(({ floorGenerated }) => {
          viewer.setAnnotations(modelAnnotations);
          modelStatus.textContent = floorGenerated
            ? "Loaded the saved GLB model and closed-loop illustrative floor."
            : "Loaded the saved GLB model. No closed exterior loop was available for a floor.";
          downloadLink.href = `/api/house-plans/${encodeURIComponent(fileId)}/model.glb`;
          downloadLink.hidden = false;
          saveButton.disabled = true;
          lastSavedSignature = geometrySignature();
        }).catch((error) => {
          modelStatus.textContent = error.message;
          modelStatus.classList.add("error");
        });
      } else if (annotations.length) {
        modelStatus.textContent = `Loaded ${annotations.length} saved plan note(s). Add walls to build a 3D model.`;
      } else if (modelAnnotations.length) {
        modelStatus.textContent =
          `Loaded ${modelAnnotations.length} saved 3D note(s). Add walls to place them on a model.`;
      }
    })
    .catch((error) => {
      modelStatus.textContent = error.message;
      modelStatus.classList.add("error");
    });

  pageImage.src = `/api/house-plans/${encodeURIComponent(fileId)}/page/1.png`;
  setTraceMode("calibrate");
  return section;
}

function createViewer(container, status) {
  let renderer = null;
  let scene = null;
  let camera = null;
  let orbit = null;
  let modelRoot = null;
  let modelRadius = 4;
  let modelCenter = new THREE.Vector3();
  let cutaway = false;
  let wireframe = false;
  let activeView = "3d";
  let animationFrame = 0;
  let resizeObserver = null;
  let annotationLayer = null;
  let annotationElements = new Map();
  let modelAnnotations = [];
  let placementCallback = null;
  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  let pointerDown = null;

  function setStatus(message) {
    status.textContent = message;
    status.classList.remove("error");
  }

  function render() {
    if (!renderer || !scene || !camera) return;
    renderer.render(scene, camera);
    renderModelAnnotations();
  }

  function renderModelAnnotations() {
    if (!renderer || !camera || !annotationLayer || !modelRoot) return;
    const width = renderer.domElement.clientWidth;
    const height = renderer.domElement.clientHeight;
    for (const annotation of modelAnnotations) {
      const marker = annotationElements.get(annotation.id);
      if (!marker) continue;
      const position = modelRoot.localToWorld(
        new THREE.Vector3(annotation.x, annotation.y, annotation.z),
      );
      position.project(camera);
      const visible = position.z >= -1
        && position.z <= 1
        && Math.abs(position.x) <= 1.05
        && Math.abs(position.y) <= 1.05;
      marker.hidden = !visible;
      if (visible) {
        marker.style.left = `${(position.x + 1) * width / 2}px`;
        marker.style.top = `${(1 - position.y) * height / 2}px`;
      }
    }
  }

  function setAnnotations(annotations) {
    modelAnnotations = annotations;
    if (!annotationLayer) return;
    annotationElements.clear();
    annotationLayer.replaceChildren();
    modelAnnotations.forEach((annotation) => {
      const marker = element("div", "model-note-marker");
      const pin = element("span", "model-note-pin", "•");
      const callout = element("span", "model-note-callout");
      callout.append(element("strong", "", annotation.title));
      if (annotation.note) callout.append(element("span", "", annotation.note));
      marker.append(pin, callout);
      annotationLayer.append(marker);
      annotationElements.set(annotation.id, marker);
    });
    renderModelAnnotations();
  }

  function startAnnotationPlacement(callback) {
    if (!modelRoot) {
      setStatus("Build the 3D model before placing a note.");
      return;
    }
    placementCallback = callback;
    renderer.domElement.classList.add("placing-model-note");
  }

  function renderLoop() {
    animationFrame = 0;
    if (!renderer || !scene || !camera) return;
    orbit.update();
    updateCutaway();
    render();
  }

  function invalidate() {
    if (!animationFrame && renderer) {
      animationFrame = requestAnimationFrame(renderLoop);
    }
    render();
  }

  function resize() {
    if (!renderer || !camera) return;
    const width = Math.max(container.clientWidth, 1);
    const height = Math.max(container.clientHeight, 1);
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    render();
  }

  function initializeRenderer() {
    if (renderer) return;
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.1;
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    renderer.domElement.className = "house-model-canvas";
    renderer.domElement.setAttribute(
      "aria-label",
      "Interactive 3D house model. Drag to rotate, scroll to zoom, and right-drag to pan.",
    );
    annotationLayer = element("div", "model-note-layer");
    container.replaceChildren(renderer.domElement, annotationLayer);

    scene = new THREE.Scene();
    scene.background = new THREE.Color("#e7ece9");
    camera = new THREE.PerspectiveCamera(42, 1, 0.01, 1000);
    const hemisphere = new THREE.HemisphereLight("#ffffff", "#8d8474", 2.1);
    scene.add(hemisphere);
    const sunlight = new THREE.DirectionalLight("#fff1da", 3.2);
    sunlight.position.set(-9, 15, 11);
    sunlight.castShadow = true;
    sunlight.shadow.mapSize.set(2048, 2048);
    sunlight.shadow.camera.left = -40;
    sunlight.shadow.camera.right = 40;
    sunlight.shadow.camera.top = 40;
    sunlight.shadow.camera.bottom = -40;
    scene.add(sunlight);
    const ground = new THREE.Mesh(
      new THREE.PlaneGeometry(200, 200),
      new THREE.MeshStandardMaterial({ color: "#e7e8e4", roughness: 0.96 }),
    );
    ground.rotation.x = -Math.PI / 2;
    ground.position.y = -0.035;
    ground.receiveShadow = true;
    scene.add(ground);
    orbit = new OrbitControls(camera, renderer.domElement);
    orbit.enableDamping = true;
    orbit.dampingFactor = 0.08;
    orbit.screenSpacePanning = true;
    orbit.minPolarAngle = 0.03;
    orbit.maxPolarAngle = Math.PI * 0.49;
    orbit.mouseButtons.LEFT = THREE.MOUSE.ROTATE;
    orbit.mouseButtons.MIDDLE = THREE.MOUSE.DOLLY;
    orbit.mouseButtons.RIGHT = THREE.MOUSE.PAN;
    orbit.addEventListener("change", invalidate);
    renderer.domElement.addEventListener("pointerdown", (event) => {
      pointerDown = { x: event.clientX, y: event.clientY };
    });
    renderer.domElement.addEventListener("pointerup", (event) => {
      if (!pointerDown || Math.hypot(
        event.clientX - pointerDown.x,
        event.clientY - pointerDown.y,
      ) > 4) {
        pointerDown = null;
        return;
      }
      pointerDown = null;
      const rectangle = renderer.domElement.getBoundingClientRect();
      pointer.x = ((event.clientX - rectangle.left) / rectangle.width) * 2 - 1;
      pointer.y = -((event.clientY - rectangle.top) / rectangle.height) * 2 + 1;
      raycaster.setFromCamera(pointer, camera);
      const hit = raycaster.intersectObject(modelRoot, true)[0];
      if (!hit) return;
      if (placementCallback) {
        const callback = placementCallback;
        placementCallback = null;
        renderer.domElement.classList.remove("placing-model-note");
        callback(modelRoot.worldToLocal(hit.point.clone()));
        return;
      }
      let node = hit.object;
      while (node && !node.userData.wallId) node = node.parent;
      if (node?.userData.wallId) {
        const label = node.name || "Wall";
        const length = Number(node.userData.lengthMetres);
        setStatus(`${label}: ${length.toFixed(2)} m long · confirmed trace · not independently verified.`);
      }
    });
    resizeObserver = new ResizeObserver(resize);
    resizeObserver.observe(container);
    resize();
  }

  function fitModel() {
    const bounds = new THREE.Box3().setFromObject(modelRoot);
    bounds.getCenter(modelCenter);
    const sphere = bounds.getBoundingSphere(new THREE.Sphere());
    modelRadius = Math.max(sphere.radius, 0.5);
    orbit.target.copy(modelCenter);
    camera.near = Math.max(0.01, modelRadius / 1000);
    camera.far = Math.max(1000, modelRadius * 20);
    camera.updateProjectionMatrix();
    applyView("3d");
    orbit.update();
  }

  function updateCutaway() {
    if (!modelRoot) return;
    const direction = camera.position.clone().sub(orbit.target);
    direction.y = 0;
    if (direction.lengthSq()) direction.normalize();
    for (const node of modelRoot.children) {
      if (!node.userData.exterior || !node.userData.midpoint) {
        node.visible = true;
        continue;
      }
      const [x, , z] = node.userData.midpoint;
      const relative = new THREE.Vector3(x, 0, z).sub(
        new THREE.Vector3(modelCenter.x, 0, modelCenter.z),
      );
      node.visible = !cutaway || relative.dot(direction) <= modelRadius * 0.08;
    }
  }

  function applyView(action) {
    activeView = action;
    if (!camera || !orbit) return;
    const distance = modelRadius * 2.7;
    const target = orbit.target;
    if (action === "top") {
      camera.up.set(0, 0, -1);
      camera.position.set(target.x, target.y + distance * 1.6, target.z + 0.001);
      orbit.minPolarAngle = 0;
      orbit.maxPolarAngle = Math.PI / 2;
    } else {
      camera.up.set(0, 1, 0);
    }
    if (action === "front") {
      camera.position.set(target.x, target.y + modelRadius * 0.25, target.z + distance);
      orbit.minPolarAngle = 0.03;
      orbit.maxPolarAngle = Math.PI * 0.49;
    } else if (action === "rear") {
      camera.position.set(target.x, target.y + modelRadius * 0.25, target.z - distance);
      orbit.minPolarAngle = 0.03;
      orbit.maxPolarAngle = Math.PI * 0.49;
    } else if (action === "side") {
      camera.position.set(target.x + distance, target.y + modelRadius * 0.25, target.z);
      orbit.minPolarAngle = 0.03;
      orbit.maxPolarAngle = Math.PI * 0.49;
    } else if (action === "3d") {
      camera.position.set(
        target.x + distance * 0.85,
        target.y + distance * 0.68,
        target.z + distance,
      );
      orbit.minPolarAngle = 0.03;
      orbit.maxPolarAngle = Math.PI * 0.49;
    }
    camera.lookAt(target);
    orbit.update();
  }

  async function loadModel(url) {
    let response;
    try {
      response = await fetch(url, { cache: "no-store" });
    } catch {
      throw new Error("The 3D model could not be reached. Check the connection and sign-in.");
    }
    if (!response.ok) {
      let message = "The 3D model could not be generated.";
      try {
        message = (await response.json()).error || message;
      } catch {
        message = "The server returned an unreadable 3D model response.";
      }
      throw new Error(message);
    }
    const floorGenerated = response.headers.get("X-BRUSNA-Floor-Generated") === "true";
    const arrayBuffer = await response.arrayBuffer();
    initializeRenderer();
    const loader = new GLTFLoader();
    const gltf = await new Promise((resolve, reject) => {
      loader.parse(arrayBuffer, "", resolve, reject);
    });
    if (modelRoot) scene.remove(modelRoot);
    modelRoot = gltf.scene;
    modelRoot.traverse((object) => {
      if (object.isMesh) {
        object.castShadow = true;
        object.receiveShadow = true;
        const materials = Array.isArray(object.material)
          ? object.material
          : [object.material];
        materials.forEach((material) => {
          if (material) material.wireframe = wireframe;
        });
      }
    });
    scene.add(modelRoot);
    modelRoot.children.forEach((node) => {
      if (node.userData.wallId && node.userData.color) {
        paintWall(node, node.userData.color);
      }
    });
    fitModel();
    renderModelAnnotations();
    updateCutaway();
    renderer.render(scene, camera);
    return { floorGenerated };
  }

  function setView(action) {
    if (action === "wireframe") {
      wireframe = !wireframe;
      modelRoot?.traverse((object) => {
        if (!object.isMesh) return;
        const materials = Array.isArray(object.material)
          ? object.material
          : [object.material];
        materials.forEach((material) => {
          if (material) material.wireframe = wireframe;
        });
      });
      render();
    } else if (action === "cutaway") {
      cutaway = !cutaway;
      updateCutaway();
      render();
    } else if (["3d", "top", "front", "rear", "side"].includes(action)) {
      applyView(action);
      invalidate();
    }
  }

  function select(id) {
    const node = modelRoot?.children.find((child) => child.userData.wallId === id);
    if (node) {
      const length = Number(node.userData.lengthMetres);
      setStatus(`${node.name}: ${length.toFixed(2)} m long · confirmed trace · not independently verified.`);
    }
  }

  function paintWall(node, color) {
    node.traverse((object) => {
      if (!object.isMesh) return;
      const materials = Array.isArray(object.material)
        ? object.material
        : [object.material];
      materials.forEach((material) => {
        if (!material || material.name === "Dark wall tops") return;
        material.color.set(color);
        material.needsUpdate = true;
      });
    });
    render();
  }

  function setWallColor(id, color) {
    const node = modelRoot?.children.find((child) => child.userData.wallId === id);
    if (node) paintWall(node, color);
  }

  function clear() {
    if (!renderer) return;
    resizeObserver?.disconnect();
    resizeObserver = null;
    if (animationFrame) cancelAnimationFrame(animationFrame);
    animationFrame = 0;
    orbit.dispose();
    const disposed = new Set();
    scene.traverse((object) => {
      if (object.geometry && !disposed.has(object.geometry)) {
        disposed.add(object.geometry);
        object.geometry.dispose();
      }
      const materials = Array.isArray(object.material)
        ? object.material
        : [object.material];
      materials.filter(Boolean).forEach((material) => {
        if (disposed.has(material)) return;
        disposed.add(material);
        Object.values(material).forEach((value) => {
          if (value?.isTexture && !disposed.has(value)) {
            disposed.add(value);
            value.dispose();
          }
        });
        material.dispose();
      });
    });
    renderer.dispose();
    renderer = null;
    scene = null;
    camera = null;
    orbit = null;
    modelRoot = null;
    annotationLayer = null;
    annotationElements = new Map();
    placementCallback = null;
    modelAnnotations = [];
    container.replaceChildren(element("p", "viewer-empty", "Save the updated traces to rebuild the 3D model."));
  }

  return {
    loadModel,
    setView,
    select,
    setWallColor,
    setAnnotations,
    startAnnotationPlacement,
    clear,
  };
}

async function refreshPlans() {
  const result = await requestJson("/api/house-plans");
  planList.replaceChildren();
  if (!result.plans.length) {
    planList.append(element("p", "empty card", "No plans uploaded yet."));
    return;
  }
  result.plans.forEach((plan) => planList.append(renderPlan(plan)));
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = fileInput.files?.[0];
  if (!file) return;
  uploadButton.disabled = true;
  statusMessage.classList.remove("error");
  statusMessage.textContent = "Uploading and analysing the PDF…";
  try {
    const data = new FormData();
    data.append("file", file);
    await requestJson("/api/house-plans", { method: "POST", body: data });
    statusMessage.textContent = "PDF analysed and saved.";
    form.reset();
    await refreshPlans();
  } catch (error) {
    statusMessage.classList.add("error");
    statusMessage.textContent = error.message;
  } finally {
    uploadButton.disabled = false;
  }
});

document.querySelector("#refresh-plans").addEventListener(
  "click",
  async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      await refreshPlans();
    } catch (error) {
      planList.replaceChildren(element("p", "error", error.message));
    } finally {
      button.disabled = false;
    }
  },
);

refreshPlans().catch((error) => {
  planList.replaceChildren(element("p", "error card", error.message));
});
