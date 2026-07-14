// MTG Proxy Studio — multi-project shell. Vanilla JS, no build step.
//
// State model:
//   projects:      list from GET /api/projects
//   activeProject: name of the currently-open project (null = none)
//   project:       full JSON of the active project
//   entryThumbs:   Map<entryIndex, {thumb_url}>
//   activeIndex:   which entry's printings are on-screen
//   printings:     printings list for the current entry
//   filter, visibleCount: printings-grid pagination + filters

const PAGE_SIZE = 27;

const state = {
  projects: [],
  activeProject: null,
  project: null,
  entryThumbs: new Map(),
  activeIndex: null,
  printings: [],
  filter: { digital: true, english: true, frame: "all" },
  entryFilter: "",
  visibleCount: PAGE_SIZE,
  view: "landing",   // "landing" | "new" | "picker"
  npFiles: [],       // pending File[] queued in the New Project form
  dragIndex: null,   // entry index being dragged (drag-drop reorder)
};

// --- Utilities --------------------------------------------------------------
const $ = (sel) => document.querySelector(sel);
const el = (tag, attrs = {}, ...children) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "html") n.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) n.setAttribute(k, v);
  }
  for (const c of children) if (c != null) n.append(c.nodeType ? c : document.createTextNode(c));
  return n;
};

// Inline-SVG icon helpers. Kept as functions returning DOM so the caller
// can attach event listeners; strokes inherit currentColor.
function svgIcon(paths, size = 16) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.innerHTML = paths;
  return svg;
}
const ICON_TRASH = () => svgIcon(
  '<path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>' +
  '<path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/>' +
  '<path d="M10 11v6"/><path d="M14 11v6"/>');
const ICON_X = () => svgIcon('<path d="M18 6 6 18"/><path d="M6 6l12 12"/>', 14);
const ICON_COPY = () => svgIcon(
  '<rect x="9" y="9" width="13" height="13" rx="2"/>' +
  '<path d="M5 15V5a2 2 0 0 1 2-2h10"/>', 14);

function hasFiles(dt) {
  return dt && Array.from(dt.types || []).includes("Files");
}

// --- Back-alignment offsets (persist per-browser — they describe the
// printer, not the deck, so once you dial your printer in they're set
// for every future export).
const OFFSET_KEYS = ["relmtgproxy:back-offset-x", "relmtgproxy:back-offset-y"];

function loadOffsets() {
  const x = parseFloat(localStorage.getItem(OFFSET_KEYS[0]) || "0");
  const y = parseFloat(localStorage.getItem(OFFSET_KEYS[1]) || "0");
  return { x: Number.isFinite(x) ? x : 0, y: Number.isFinite(y) ? y : 0 };
}
function saveOffsets(x, y) {
  localStorage.setItem(OFFSET_KEYS[0], String(x));
  localStorage.setItem(OFFSET_KEYS[1], String(y));
}
function currentOffsets() {
  const xInput = $("#back-offset-x");
  const yInput = $("#back-offset-y");
  const x = parseFloat(xInput?.value ?? "0") || 0;
  const y = parseFloat(yInput?.value ?? "0") || 0;
  return { x, y };
}
function applyOffsetsToInputs({ x, y }) {
  const xi = $("#back-offset-x");
  const yi = $("#back-offset-y");
  if (xi) xi.value = String(x);
  if (yi) yi.value = String(y);
}

async function downloadRegistrationTest() {
  const { x, y } = currentOffsets();
  const params = new URLSearchParams({
    flip_edge: "long",
    back_offset_x: String(x),
    back_offset_y: String(y),
  });
  // Trigger a real browser download via a temporary anchor — that way
  // the browser handles the file save + shows progress in its own UI.
  const a = document.createElement("a");
  a.href = `/api/registration-test?${params}`;
  a.download = "registration_test.pdf";
  document.body.appendChild(a);
  a.click();
  a.remove();
  toast("Registration test PDF downloading — duplex-print on plain paper.", "ok");
}

// --- Paper size (persist per-browser — user's paper stock stays put across
// projects; they'll usually only own one kind of paper).
const PAPER_KEY = "relmtgproxy:paper";
const PAPER_DEFAULT = "A4";
const PAPER_VALID = new Set(["A4", "Letter"]);

function loadPaper() {
  const raw = localStorage.getItem(PAPER_KEY) || "";
  return PAPER_VALID.has(raw) ? raw : PAPER_DEFAULT;
}
function savePaper(paper) {
  if (PAPER_VALID.has(paper)) localStorage.setItem(PAPER_KEY, paper);
}
function currentPaper() {
  const sel = $("#paper-size");
  const v = sel?.value || PAPER_DEFAULT;
  return PAPER_VALID.has(v) ? v : PAPER_DEFAULT;
}

// --- Print resolution target (600 or 1200 DPI). Persisted for the same
// reason as paper — it describes the printer, not the deck.
const DPI_KEY = "relmtgproxy:dpi-target";
const DPI_DEFAULT = 600;
const DPI_VALID = new Set([600, 1200]);

function loadDpiTarget() {
  const raw = parseInt(localStorage.getItem(DPI_KEY) || "", 10);
  return DPI_VALID.has(raw) ? raw : DPI_DEFAULT;
}
function saveDpiTarget(dpi) {
  if (DPI_VALID.has(dpi)) localStorage.setItem(DPI_KEY, String(dpi));
}
function currentDpiTarget() {
  const sel = $("#dpi-target");
  const v = parseInt(sel?.value || "", 10);
  return DPI_VALID.has(v) ? v : DPI_DEFAULT;
}
function updateUpscaleLabel() {
  const label = $("#upscale-label");
  if (label) label.textContent = `${currentDpiTarget()} DPI upscale`;
}

// --- Cut-line style (full lines vs. corner-only trim marks vs. margin
// ticks). Persisted per-browser like the other export knobs.
const CUT_LINES_MODE_KEY = "relmtgproxy:cut-lines-mode";
const CUT_LINES_MODE_DEFAULT = "full";
const CUT_LINES_MODE_VALID = new Set(["full", "corners", "ticks"]);

function loadCutLinesMode() {
  const raw = localStorage.getItem(CUT_LINES_MODE_KEY) || "";
  return CUT_LINES_MODE_VALID.has(raw) ? raw : CUT_LINES_MODE_DEFAULT;
}
function saveCutLinesMode(v) {
  if (CUT_LINES_MODE_VALID.has(v)) localStorage.setItem(CUT_LINES_MODE_KEY, v);
}
function currentCutLinesMode() {
  const sel = $("#cut-lines-mode");
  const v = sel?.value || CUT_LINES_MODE_DEFAULT;
  return CUT_LINES_MODE_VALID.has(v) ? v : CUT_LINES_MODE_DEFAULT;
}

// --- Bleed — extra mm of extended-edge content past each cut line so
// tiny miscuts don't show white. Uses PIL edge-repeat padding server-
// side, so the original card image is not scaled. Persisted per-browser.
const BLEED_KEY = "relmtgproxy:bleed";
const BLEED_DEFAULT = "0";
const BLEED_VALID = new Set(["0", "2", "3"]);

function loadBleed() {
  const raw = localStorage.getItem(BLEED_KEY) || "";
  return BLEED_VALID.has(raw) ? raw : BLEED_DEFAULT;
}
function saveBleed(v) {
  if (BLEED_VALID.has(v)) localStorage.setItem(BLEED_KEY, v);
}
function currentBleed() {
  const sel = $("#bleed");
  const v = sel?.value || BLEED_DEFAULT;
  return BLEED_VALID.has(v) ? v : BLEED_DEFAULT;
}

// --- Cut-guide colour. Small preset list rather than a colour wheel;
// the server accepts any #rrggbb via cli.py if a power user wants more.
const CUT_COLOR_KEY = "relmtgproxy:cut-color";
const CUT_COLOR_DEFAULT = "#4d8bff";
const CUT_COLOR_VALID = new Set(["#4d8bff", "#000000", "#ff2d5a", "#e600ff", "#8a8a8a"]);

function loadCutColor() {
  const raw = localStorage.getItem(CUT_COLOR_KEY) || "";
  return CUT_COLOR_VALID.has(raw) ? raw : CUT_COLOR_DEFAULT;
}
function saveCutColor(v) {
  if (CUT_COLOR_VALID.has(v)) localStorage.setItem(CUT_COLOR_KEY, v);
}
function currentCutColor() {
  const sel = $("#cut-color");
  const v = sel?.value || CUT_COLOR_DEFAULT;
  return CUT_COLOR_VALID.has(v) ? v : CUT_COLOR_DEFAULT;
}

// --- Card spacing (gutter). "None (touching)" makes adjacent cards share a
// single guillotine cut — fewer passes on a paper cutter. Kept at 3 mm by
// default (the project's paper-trimmer convention). Persisted per-browser.
const SPACING_KEY = "relmtgproxy:spacing";
const SPACING_DEFAULT = "3";
const SPACING_VALID = new Set(["0", "3"]);

function loadSpacing() {
  const raw = localStorage.getItem(SPACING_KEY) || "";
  return SPACING_VALID.has(raw) ? raw : SPACING_DEFAULT;
}
function saveSpacing(v) {
  if (SPACING_VALID.has(v)) localStorage.setItem(SPACING_KEY, v);
}
function currentSpacing() {
  const sel = $("#spacing");
  const v = sel?.value || SPACING_DEFAULT;
  return SPACING_VALID.has(v) ? v : SPACING_DEFAULT;
}

// --- Corner fill. "off" disables; any other value is the #rrggbb colour
// composited into each card's rounded-corner die-cut so a physical corner-
// rounder can't expose a white sliver. Default off. Persisted per-browser.
const CORNER_FILL_KEY = "relmtgproxy:corner-fill";
const CORNER_FILL_DEFAULT = "off";
const CORNER_FILL_VALID = new Set(["off", "#000000", "#ffffff", "#8a8a8a"]);

function loadCornerFill() {
  const raw = localStorage.getItem(CORNER_FILL_KEY) || "";
  return CORNER_FILL_VALID.has(raw) ? raw : CORNER_FILL_DEFAULT;
}
function saveCornerFill(v) {
  if (CORNER_FILL_VALID.has(v)) localStorage.setItem(CORNER_FILL_KEY, v);
}
function currentCornerFill() {
  const sel = $("#corner-fill");
  const v = sel?.value || CORNER_FILL_DEFAULT;
  return CORNER_FILL_VALID.has(v) ? v : CORNER_FILL_DEFAULT;
}

// --- PNG raster DPI (only relevant when Format=PNGs). "auto" means match
// the upscale Resolution so a 1200 DPI upscale isn't silently discarded
// by rendering PNGs at 300 DPI. Persisted per-browser.
const PNG_DPI_KEY = "relmtgproxy:png-dpi";
const PNG_DPI_DEFAULT = "auto";
const PNG_DPI_VALID = new Set(["auto", "150", "300", "600", "1200"]);

function loadPngDpi() {
  const raw = localStorage.getItem(PNG_DPI_KEY) || "";
  return PNG_DPI_VALID.has(raw) ? raw : PNG_DPI_DEFAULT;
}
function savePngDpi(v) {
  if (PNG_DPI_VALID.has(v)) localStorage.setItem(PNG_DPI_KEY, v);
}
// Resolve the picker's "auto" to a concrete DPI to send to the server.
// If upscale is off, there's no benefit going past ~300 DPI — the source
// data isn't there — so auto falls back to 300 in that case.
function resolvePngDpi() {
  const sel = $("#png-dpi");
  const raw = sel?.value || PNG_DPI_DEFAULT;
  if (raw !== "auto") return parseInt(raw, 10);
  const upscaleOn = $("#upscale-checkbox")?.checked;
  return upscaleOn ? currentDpiTarget() : 300;
}
function updatePngDpiChipVisibility() {
  const chip = $("#png-dpi-picker");
  const format = $("#export-format")?.value || "pdf";
  if (chip) chip.hidden = format !== "png";
}

// Mute the controls that have no effect under the current settings, so the
// panel only ever shows live options. Quality/Print-DPI need upscaling on;
// the back-alignment row needs a back mode selected.
function refreshExportPanelState() {
  const upscaleOn = $("#upscale-checkbox")?.checked;
  for (const id of ["#quality-picker", "#dpi-picker"]) {
    $(id)?.classList.toggle("is-muted", !upscaleOn);
  }
  const backsOn = ($("#backs-mode")?.value || "none") !== "none";
  $("#align-controls")?.classList.toggle("is-muted", !backsOn);
}

// --- Deck-grid zoom --------------------------------------------------------
const ZOOM_KEY = "relmtgproxy:deck-columns";
const ZOOM_MIN = 2;
const ZOOM_MAX = 6;
const ZOOM_DEFAULT = 3;

function loadZoom() {
  const raw = parseInt(localStorage.getItem(ZOOM_KEY) || "", 10);
  return Number.isFinite(raw) && raw >= ZOOM_MIN && raw <= ZOOM_MAX ? raw : ZOOM_DEFAULT;
}
function applyZoom(cols) {
  cols = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, cols));
  document.documentElement.style.setProperty("--deck-cols", cols);
  const slider = document.getElementById("zoom-slider");
  const label = document.getElementById("zoom-label");
  if (slider) slider.value = String(cols);
  if (label) label.textContent = String(cols);
  localStorage.setItem(ZOOM_KEY, String(cols));
}
function currentZoom() {
  const raw = parseInt(document.documentElement.style.getPropertyValue("--deck-cols") || "", 10);
  return Number.isFinite(raw) ? raw : ZOOM_DEFAULT;
}

function toast(message, kind = "") {
  const t = $("#toast");
  t.textContent = message;
  t.className = `toast show ${kind}`;
  clearTimeout(toast._h);
  toast._h = setTimeout(() => { t.className = "toast"; }, 3500);
}

function confirmAction(message) {
  return new Promise((resolve) => {
    const dlg = $("#confirm-dialog");
    $("#confirm-message").textContent = message;
    const done = (v) => {
      $("#confirm-yes").removeEventListener("click", yes);
      $("#confirm-no").removeEventListener("click", no);
      dlg.removeEventListener("cancel", cancel);
      dlg.close();
      resolve(v);
    };
    const yes = () => done(true);
    const no = () => done(false);
    const cancel = () => done(false);
    $("#confirm-yes").addEventListener("click", yes);
    $("#confirm-no").addEventListener("click", no);
    dlg.addEventListener("cancel", cancel);
    dlg.showModal();
  });
}

async function api(path, opts = {}) {
  // JSON is the default content-type; skip it when the caller is sending
  // FormData (multipart) so the browser picks the right boundary.
  const isForm = opts.body instanceof FormData;
  const headers = isForm ? (opts.headers || {}) : {
    "content-type": "application/json",
    ...(opts.headers || {}),
  };
  const resp = await fetch(path, { ...opts, headers });
  if (resp.status === 204) return null;
  const text = await resp.text();
  const body = text ? JSON.parse(text) : null;
  if (!resp.ok) {
    const detail = body?.detail ?? body?.message ?? text ?? resp.statusText;
    const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    err.body = body;
    err.status = resp.status;
    throw err;
  }
  return body;
}

// --- View switching ---------------------------------------------------------
function setView(v) {
  state.view = v;
  $("#landing").hidden = v !== "landing";
  $("#new-project").hidden = v !== "new";
  $("#deck-view").hidden = v !== "deck";
  const inDeck = v === "deck";
  // The export panel (and its narrow-screen drawer toggle) only make sense
  // once a deck is open. Leaving the deck also dismisses an open drawer.
  $("#export-panel").hidden = !inDeck;
  $("#export-drawer-toggle").hidden = !inDeck;
  if (!inDeck) closeExportDrawer();
}

// --- Export panel drawer (narrow screens only) -----------------------------
function openExportDrawer() {
  $("#export-panel")?.classList.add("open");
  const bd = $("#export-backdrop");
  if (bd) { bd.hidden = false; bd.classList.add("open"); }
}
function closeExportDrawer() {
  $("#export-panel")?.classList.remove("open");
  const bd = $("#export-backdrop");
  if (bd) { bd.classList.remove("open"); bd.hidden = true; }
}

// --- Projects ---------------------------------------------------------------
async function refreshProjects() {
  state.projects = await api("/api/projects");
  renderProjectList();
}

function renderProjectList() {
  const list = $("#project-list");
  list.innerHTML = "";
  if (state.projects.length === 0) {
    list.append(el("li", { class: "project-list-empty" }, "No projects yet."));
    return;
  }
  for (const p of state.projects) {
    const active = p.name === state.activeProject;
    list.append(el("li", {
      class: `project-item ${active ? "active" : ""}`,
      onclick: () => openProject(p.name),
    },
      el("div", { class: "p-meta" },
        el("div", { class: "p-name" }, p.name),
        el("div", { class: "p-sub" }, `${p.entries} card${p.entries === 1 ? "" : "s"}`),
      ),
      el("button", {
        class: "icon-btn",
        title: "Delete project",
        "aria-label": `Delete ${p.name}`,
        onclick: async (ev) => { ev.stopPropagation(); await deleteProject(p.name); },
      }, ICON_TRASH()),
    ));
  }
}

async function openProject(name) {
  state.activeProject = name;
  state.activeIndex = null;
  state.printings = [];
  location.hash = `#project=${encodeURIComponent(name)}`;
  try {
    state.project = await api(`/api/projects/${encodeURIComponent(name)}`);
  } catch (e) {
    toast(`Could not open ${name}: ${e.message}`, "err");
    state.activeProject = null;
    setView("landing");
    renderProjectList();
    return;
  }
  $("#project-badge").hidden = false;
  $("#project-badge").textContent = name;
  document.title = `${name} — RelMTG Proxy Studio`;
  setView("deck");
  renderProjectList();

  // Reset picker state.
  $("#printings").innerHTML = "";
  $("#load-more-bar").hidden = true;
  renderDeckGrid();

  // Populate entry thumbnails in the background.
  state.entryThumbs.clear();
  await Promise.all(state.project.entries.map((_, i) => refreshEntryThumb(i)));
  renderDeckGrid();
}

function closeProject() {
  state.activeProject = null;
  state.project = null;
  state.activeIndex = null;
  state.entryThumbs.clear();
  state.printings = [];
  location.hash = "";
  $("#project-badge").hidden = true;
  $("#project-badge").textContent = "";
  document.title = "MTG Proxy Studio";
  setView("landing");
  renderProjectList();
}

async function deleteProject(name) {
  const ok = await confirmAction(`Delete project "${name}"? This can't be undone.`);
  if (!ok) return;
  try {
    await api(`/api/projects/${encodeURIComponent(name)}`, { method: "DELETE" });
  } catch (e) {
    toast(`Delete failed: ${e.message}`, "err");
    return;
  }
  toast(`Deleted "${name}"`, "ok");
  if (state.activeProject === name) closeProject();
  await refreshProjects();
}

// --- New project ------------------------------------------------------------
function openNewProjectForm() {
  setView("new");
  $("#np-name").value = "";
  $("#np-decklist").value = "";
  $("#new-project-status").className = "status";
  $("#new-project-status").textContent = "";
  $("#new-project-failures").hidden = true;
  $("#new-project-failures").innerHTML = "";
  state.npFiles = [];
  renderNpFileList();
  setTimeout(() => $("#np-name").focus(), 50);
}

function renderNpFileList() {
  const ul = $("#np-file-list");
  ul.innerHTML = "";
  for (const [i, f] of state.npFiles.entries()) {
    const previewUrl = URL.createObjectURL(f);
    ul.append(el("li", {},
      el("img", { src: previewUrl, alt: "" }),
      el("span", {}, f.name),
      el("button", {
        class: "icon-btn",
        title: "Remove",
        onclick: () => {
          state.npFiles.splice(i, 1);
          renderNpFileList();
        },
      }, "×"),
    ));
  }
}

function acceptNpFiles(fileList) {
  const wanted = /^image\/(png|jpe?g|webp)$/i;
  for (const f of fileList) {
    if (!wanted.test(f.type) && !/\.(png|jpe?g|webp)$/i.test(f.name)) continue;
    state.npFiles.push(f);
  }
  renderNpFileList();
}

async function createProject() {
  const name = $("#np-name").value.trim();
  const decklist = $("#np-decklist").value;
  const files = state.npFiles;
  const status = $("#new-project-status");
  const failuresUl = $("#new-project-failures");
  failuresUl.hidden = true; failuresUl.innerHTML = "";

  if (!name) {
    status.className = "status err";
    status.textContent = "Give the project a name.";
    return;
  }
  if (!decklist.trim() && files.length === 0) {
    status.className = "status err";
    status.textContent = "Paste a deck list or drop card art.";
    return;
  }

  const btn = $("#btn-create");
  btn.disabled = true;
  status.className = "status";
  status.textContent = "";

  const moxUrl = /^\s*(?:https?:\/\/)?(?:www\.)?moxfield\.com\//i.test(decklist);
  const arkUrl = /^\s*(?:https?:\/\/)?(?:www\.)?archidekt\.com\//i.test(decklist);
  openProgressModal({
    title: "Creating your deck",
    sub: moxUrl
      ? "Contacting Moxfield…"
      : arkUrl
        ? "Contacting Archidekt…"
        : (decklist.trim() ? "Resolving cards on Scryfall…" : "Setting up…"),
  });

  let result;
  try {
    result = await streamCreateProject({ name, decklist });
  } catch (e) {
    closeProgressModal();
    btn.disabled = false;
    if (e.failures) {
      // Decklist parse errors have line-by-line context.
      failuresUl.hidden = false;
      for (const [lineno, text] of e.failures) {
        failuresUl.append(el("li", {},
          el("strong", {}, `line ${lineno}: `), text));
      }
    } else {
      status.className = "status err";
      status.textContent = `error: ${e.message}`;
    }
    return;
  }

  // Second phase: upload any queued art into the new project.
  if (files.length) {
    setProgressSub(`Uploading ${files.length} image${files.length === 1 ? "" : "s"}…`);
    try {
      await uploadFiles(result.name, files);
    } catch (e) {
      closeProgressModal();
      status.className = "status err";
      status.textContent = `upload failed: ${e.message}`;
      btn.disabled = false;
      return;
    }
  }

  closeProgressModal();
  btn.disabled = false;
  await refreshProjects();
  await openProject(result.name);

  if (result.failures?.length) {
    for (const f of result.failures) toast(`Skipped ${f.name}: ${f.message}`, "err");
  } else {
    const total = (result.entries || 0) + files.length;
    toast(`Created "${result.name}" with ${total} cards`, "ok");
  }
}

// --- Streaming project creation --------------------------------------------
// Consumes the SSE stream from POST /api/projects/stream and updates the
// progress overlay for each event. Resolves with the final `done` payload,
// or rejects with an Error carrying `failures` when the server reports a
// decklist parse error.
async function streamCreateProject(payload) {
  const resp = await fetch("/api/projects/stream", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!resp.ok || !resp.body) {
    const text = await resp.text().catch(() => "");
    throw new Error(text || `${resp.status} ${resp.statusText}`);
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let done = null;
  let error = null;

  while (true) {
    const { value, done: streamDone } = await reader.read();
    if (streamDone) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const raw = buf.slice(0, idx); buf = buf.slice(idx + 2);
      const evt = parseSSE(raw);
      if (!evt) continue;
      if (evt.event === "start") {
        setProgressTotal(evt.data.total);
      } else if (evt.event === "phase") {
        if (evt.data.phase === "moxfield-fetch") {
          setProgressSub("Fetching deck from Moxfield…");
        } else if (evt.data.phase === "archidekt-fetch") {
          setProgressSub("Fetching deck from Archidekt…");
        } else if (evt.data.phase === "resolving") {
          setProgressSub("Resolving cards on Scryfall…");
        }
      } else if (evt.event === "progress") {
        setProgressStep(evt.data.index, evt.data.total, evt.data.name);
      } else if (evt.event === "done") {
        done = evt.data;
      } else if (evt.event === "error") {
        error = evt.data;
      }
    }
  }

  if (error) {
    const e = new Error(error.message || "Project creation failed");
    if (error.failures) e.failures = error.failures;
    throw e;
  }
  if (!done) throw new Error("Server closed the stream without a result");
  return done;
}

// --- Progress modal --------------------------------------------------------
function openProgressModal({ title, sub }) {
  const dlg = $("#progress-modal");
  $("#progress-title").textContent = title || "Working…";
  setProgressSub(sub || "");
  setProgressStep(0, 0, "");
  $("#progress-bar").style.width = "0%";
  if (!dlg.open) dlg.showModal();
}
function closeProgressModal() {
  const dlg = $("#progress-modal");
  if (dlg.open) dlg.close();
}
function setProgressSub(text) {
  $("#progress-sub").textContent = text || "";
}
function setProgressTotal(total) {
  $("#progress-counter").textContent = `0 / ${total}`;
  $("#progress-bar").style.width = total === 0 ? "100%" : "0%";
}
function setProgressStep(index, total, name) {
  if (total && total > 0) {
    // Index is 0-based; show 1-based counter, and use (index + 1) / total
    // so the bar is at 100% only on the final card.
    const shown = Math.min(index + 1, total);
    $("#progress-counter").textContent = `${shown} / ${total}`;
    $("#progress-bar").style.width = `${(shown / total) * 100}%`;
  } else {
    $("#progress-counter").textContent = `${index}`;
  }
  $("#progress-current").textContent = name || "";
}

async function uploadFiles(projectName, files) {
  const fd = new FormData();
  for (const f of files) fd.append("files", f, f.name);
  const res = await api(
    `/api/projects/${encodeURIComponent(projectName)}/uploads`,
    { method: "POST", body: fd },
  );
  return res;
}

// --- Entries ----------------------------------------------------------------
async function refreshEntryThumb(index) {
  if (!state.activeProject) return;
  try {
    const data = await api(`/api/projects/${encodeURIComponent(state.activeProject)}/entries/${index}/thumb`);
    state.entryThumbs.set(index, data);
  } catch {
    state.entryThumbs.set(index, { thumb_url: null });
  }
}

function renderDeckGrid() {
  const grid = $("#deck-grid");
  grid.innerHTML = "";
  if (!state.project) return;

  const needle = state.entryFilter.trim().toLowerCase();
  const entries = state.project.entries;
  let visibleCount = 0;

  entries.forEach((entry, i) => {
    if (needle && !entry.name.toLowerCase().includes(needle)) return;
    visibleCount += 1;
    const thumb = state.entryThumbs.get(i);
    const thumbUrl = thumb?.thumb_url || null;
    const isCustom = !!entry.custom_image_path;

    const sub = isCustom
      ? "custom art"
      : `${(entry.selected_print.set || "?").toUpperCase()} · ${entry.selected_print.collector_number || "?"}`;

    const tile = el("div", {
      class: `deck-card ${isCustom ? "is-custom" : ""}`,
      "data-index": i,
      draggable: "true",
      onclick: () => openPickerModal(i),
      ondragstart: (ev) => onDeckDragStart(ev, i),
      ondragend: onDeckDragEnd,
      ondragover: (ev) => onDeckDragOver(ev, i),
      ondrop: onDeckDrop,
    },
      el("div", { class: "img-wrap" },
        thumbUrl
          ? el("img", { class: "thumb", src: thumbUrl, alt: entry.name, loading: "lazy" })
          : el("div", { class: "missing-thumb", title: "No thumbnail yet" }),
        el("div", { class: "qty-badge" }, `×${entry.quantity}`),
        isCustom ? el("div", { class: "custom-badge" }, "custom") : null,
        el("div", { class: "card-actions" },
          el("button", {
            class: "action-btn dup-btn",
            title: "Duplicate card",
            "aria-label": `Duplicate ${entry.name}`,
            onclick: async (ev) => { ev.stopPropagation(); await duplicateEntry(i); },
          }, ICON_COPY()),
          el("button", {
            class: "action-btn del-btn",
            title: "Remove card",
            "aria-label": `Remove ${entry.name}`,
            onclick: async (ev) => { ev.stopPropagation(); await deleteEntry(i); },
          }, ICON_X()),
        ),
      ),
      el("div", { class: "caption" },
        el("div", { class: "card-name", title: entry.name }, entry.name),
        el("div", { class: "card-sub" }, sub),
      ),
    );
    grid.append(tile);
  });

  // Count + empty state.
  const total = entries.length;
  const suffix = total === 1 ? "card" : "cards";
  $("#deck-count").textContent =
    (needle && visibleCount !== total)
      ? `${visibleCount} of ${total} ${suffix}`
      : `${total} ${suffix}`;
  $("#deck-empty").hidden = total !== 0;

  // Trailing "+ Add card" / "+ Add token" placeholders — always visible so
  // an empty deck still has an obvious way to add its first card via search.
  // Hidden while the user is filtering, since they'd sit at the wrong place
  // in the visible list.
  if (!needle) {
    grid.append(makeAddTile("card", "Add card",
      "Search Scryfall for a card by name"));
    grid.append(makeAddTile("token", "Add token",
      "Search Scryfall for a token by name"));
  }

  // Ensure the grid-level drop handler is attached once the grid exists.
  _wireGridLevelDrop();
}

function makeAddTile(kind, label, sub) {
  return el("div", {
    class: `deck-card add-tile add-tile-${kind}`,
    role: "button",
    tabindex: "0",
    "aria-label": label,
    title: label,
    onclick: () => openSearchModal(kind),
    onkeydown: (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        openSearchModal(kind);
      }
    },
  },
    el("div", { class: "img-wrap" },
      el("div", { class: "add-tile-inner" },
        svgIcon('<path d="M12 5v14"/><path d="M5 12h14"/>', 28),
        el("div", { class: "add-tile-label" }, label),
      ),
    ),
    el("div", { class: "caption" },
      el("div", { class: "card-name" }, label),
      el("div", { class: "card-sub" }, sub),
    ),
  );
}

// --- Drag/drop reordering (deck grid) --------------------------------------
// A single "drop placeholder" element is inserted into the grid at the
// intended drop position. Because it's a real grid child, all subsequent
// tiles reflow, so you see the cards visibly move to make room.
let _dropPlaceholder = null;
let _draggedEl = null;

function _placeholder() {
  if (!_dropPlaceholder) {
    _dropPlaceholder = document.createElement("div");
    _dropPlaceholder.className = "drop-placeholder";
  }
  return _dropPlaceholder;
}

function _cleanupDrag() {
  if (_dropPlaceholder && _dropPlaceholder.parentNode) {
    _dropPlaceholder.parentNode.removeChild(_dropPlaceholder);
  }
  if (_draggedEl) {
    _draggedEl.style.display = "";
    _draggedEl.classList.remove("dragging");
    _draggedEl = null;
  }
  state.dragIndex = null;
}

function onDeckDragStart(ev, i) {
  state.dragIndex = i;
  _draggedEl = ev.currentTarget;
  ev.dataTransfer.effectAllowed = "move";
  ev.dataTransfer.setData("text/plain", String(i));
  _draggedEl.classList.add("dragging");
  // Hide the source tile once the browser has snapshotted the drag image
  // (which happens synchronously). Deferring the display:none by one tick
  // avoids cancelling the drag on some browsers, and lets the placeholder
  // truly own the tile's old slot.
  setTimeout(() => { if (_draggedEl) _draggedEl.style.display = "none"; }, 0);
}

function onDeckDragEnd() {
  _cleanupDrag();
}

function onDeckDragOver(ev, i) {
  if (state.dragIndex === null) return;
  ev.preventDefault();
  ev.dataTransfer.dropEffect = "move";
  const target = ev.currentTarget;
  if (target === _draggedEl) return;
  const rect = target.getBoundingClientRect();
  const after = (ev.clientX - rect.left) > rect.width / 2;
  const ph = _placeholder();
  if (after) target.after(ph);
  else target.before(ph);
}

async function onDeckDrop(ev) {
  ev.preventDefault();
  const from = state.dragIndex;
  const grid = $("#deck-grid");
  if (from === null || !_dropPlaceholder || _dropPlaceholder.parentNode !== grid) {
    _cleanupDrag();
    return;
  }
  // Target index = position of placeholder among the still-visible tiles
  // (the source tile is display:none so it doesn't count).
  let to = 0;
  for (const child of grid.children) {
    if (child === _dropPlaceholder) break;
    if (child === _draggedEl) continue;
    if (child.classList.contains("deck-card")) to += 1;
  }
  _cleanupDrag();
  if (to === from) return;
  await reorderEntry(from, to);
}

// Also handle drops that land outside any specific card — e.g. into the grid
// gutter. Placeholder is already in position, we just need to commit.
function _wireGridLevelDrop() {
  const grid = $("#deck-grid");
  if (!grid || grid._dropWired) return;
  grid.addEventListener("dragover", (ev) => {
    if (state.dragIndex !== null) ev.preventDefault();
  });
  grid.addEventListener("drop", (ev) => {
    if (state.dragIndex !== null) onDeckDrop(ev);
  });
  grid._dropWired = true;
}

async function reorderEntry(from, to) {
  const n = state.project.entries.length;
  const order = [...Array(n).keys()];
  order.splice(from, 1);
  order.splice(to, 0, from);
  try {
    await api(`/api/projects/${encodeURIComponent(state.activeProject)}/order`, {
      method: "PUT",
      body: JSON.stringify({ order }),
    });
  } catch (e) {
    toast(`Reorder failed: ${e.message}`, "err");
    return;
  }
  // Re-fetch the project so indices + thumbs stay in sync.
  state.project = await api(`/api/projects/${encodeURIComponent(state.activeProject)}`);
  const oldThumbs = state.entryThumbs;
  const newThumbs = new Map();
  order.forEach((oldIdx, newIdx) => {
    if (oldThumbs.has(oldIdx)) newThumbs.set(newIdx, oldThumbs.get(oldIdx));
  });
  state.entryThumbs = newThumbs;
  // Track active pointer through the reorder.
  if (state.activeIndex !== null) {
    state.activeIndex = order.indexOf(state.activeIndex);
  }
  renderDeckGrid();
}

async function setEntryQuantity(index, quantity) {
  if (!state.activeProject) return;
  const entries = state.project?.entries;
  if (!entries || !entries[index]) return;
  const q = Math.max(1, Math.min(999, Math.round(quantity)));
  if (q === entries[index].quantity) return;
  try {
    const res = await api(
      `/api/projects/${encodeURIComponent(state.activeProject)}/entries/${index}/quantity`,
      { method: "POST", body: JSON.stringify({ quantity: q }) },
    );
    entries[index].quantity = res.entry.quantity;
    renderDeckGrid();
    if (state.activeIndex === index) updatePickerQuantityUI();
    await refreshProjects();
  } catch (e) {
    toast(`Update failed: ${e.message}`, "err");
    if (state.activeIndex === index) updatePickerQuantityUI();
  }
}

async function splitEntry(index) {
  const entries = state.project?.entries;
  if (!entries || !entries[index]) return;
  const total = entries[index].quantity;
  if (total < 2) {
    toast("Need at least 2 copies to split", "err");
    return;
  }
  const half = Math.floor(total / 2);
  // Duplicate creates a clone at index+1 with the same quantity, then we
  // resize both halves. Original keeps the larger half on odd totals.
  await duplicateEntry(index);
  await setEntryQuantity(index, total - half);
  await setEntryQuantity(index + 1, half);
  toast(`Split ${total}× into ${total - half} + ${half}`, "ok");
}

async function duplicateEntry(index) {
  if (!state.activeProject) return;
  try {
    await api(
      `/api/projects/${encodeURIComponent(state.activeProject)}/entries/${index}/duplicate`,
      { method: "POST" },
    );
  } catch (e) {
    toast(`Duplicate failed: ${e.message}`, "err");
    return;
  }
  // Refetch: indices past `index` all shifted by +1.
  state.project = await api(`/api/projects/${encodeURIComponent(state.activeProject)}`);
  const oldThumbs = state.entryThumbs;
  const newThumbs = new Map();
  // Preserve thumbnails: entries 0..index stay put, `index+1` mirrors `index`,
  // entries `index+2..end` came from `index+1..end-1` of the old list.
  oldThumbs.forEach((v, k) => {
    if (k <= index) newThumbs.set(k, v);
    else newThumbs.set(k + 1, v);
  });
  if (oldThumbs.has(index)) newThumbs.set(index + 1, oldThumbs.get(index));
  state.entryThumbs = newThumbs;
  if (state.activeIndex !== null && index < state.activeIndex) {
    state.activeIndex += 1;
  }
  await refreshProjects();
  renderDeckGrid();
  toast(`Duplicated ${state.project.entries[index].name}`, "ok");
}

async function deleteEntry(index) {
  const entry = state.project.entries[index];
  const ok = await confirmAction(
    `Remove ${entry.quantity}× ${entry.name} from "${state.activeProject}"?`
  );
  if (!ok) return;
  try {
    await api(
      `/api/projects/${encodeURIComponent(state.activeProject)}/entries/${index}`,
      { method: "DELETE" },
    );
  } catch (e) {
    toast(`Remove failed: ${e.message}`, "err");
    return;
  }
  toast(`Removed ${entry.name}`, "ok");

  // Refresh: entry indices shift after a delete, so refetch the project.
  const wasActive = state.activeIndex === index;
  state.project = await api(`/api/projects/${encodeURIComponent(state.activeProject)}`);
  state.entryThumbs.clear();
  await Promise.all(state.project.entries.map((_, i) => refreshEntryThumb(i)));
  if (wasActive) {
    // If the deleted card was the one being previewed in the modal, dismiss it.
    closePickerModal();
  } else if (state.activeIndex !== null && index < state.activeIndex) {
    // Adjust the active pointer since deleting an earlier index shifted us.
    state.activeIndex -= 1;
  }
  await refreshProjects();
  renderDeckGrid();
}

// --- Printings --------------------------------------------------------------
async function loadPrintings(entry) {
  $("#picker-title").textContent = entry.name;
  $("#picker-subtitle").textContent = "loading printings…";
  $("#printings").innerHTML = "";
  $("#load-more-bar").hidden = true;
  state.visibleCount = PAGE_SIZE;
  try {
    const data = await api(`/api/prints/${entry.oracle_id}`);
    state.printings = data.printings;
    renderPrintings();
    $("#printings-scroll").scrollTop = 0;
  } catch (e) {
    $("#picker-subtitle").textContent = "";
    toast(`Could not load printings: ${e.message}`, "err");
  }
}

async function selectPrinting(scryfallId) {
  if (state.activeIndex === null || !state.activeProject) return;
  try {
    const res = await api(
      `/api/projects/${encodeURIComponent(state.activeProject)}/select`,
      {
        method: "POST",
        body: JSON.stringify({ entry_index: state.activeIndex, scryfall_id: scryfallId }),
      },
    );
    const entry = state.project.entries[state.activeIndex];
    entry.selected_print = res.entry.selected_print;
    entry.layout = res.entry.layout;
    entry.back = res.entry.back;
    // Server now clears custom_image_path when a Scryfall printing is
    // picked — reflect that locally too so the deck-grid tile drops its
    // "custom" badge without a refetch.
    entry.custom_image_path = res.entry.custom_image_path ?? null;
    entry.oracle_id = res.entry.oracle_id || entry.oracle_id;
    state.entryThumbs.set(state.activeIndex, { thumb_url: res.entry.thumb_url });
    renderDeckGrid();
    renderPrintings();
    toast(`Saved: ${entry.name} → ${entry.selected_print.set.toUpperCase()} ${entry.selected_print.collector_number}`, "ok");
  } catch (e) {
    toast(`Save failed: ${e.message}`, "err");
  }
}

function renderPrintings() {
  const grid = $("#printings");
  const loadBar = $("#load-more-bar");
  grid.innerHTML = "";
  loadBar.hidden = true;
  if (state.activeIndex === null) return;

  const entry = state.project.entries[state.activeIndex];
  const filtered = state.printings.filter(passesFilter);
  const total = state.printings.length;
  const hidden = total - filtered.length;
  $("#picker-subtitle").textContent = hidden > 0
    ? `${filtered.length} of ${total} printings (${hidden} hidden by filters)`
    : `${total} printings on Scryfall`;
  const currentId = entry.selected_print.scryfall_id;
  const selectedIndex = filtered.findIndex((p) => p.id === currentId);
  const visible = filtered.slice(0, state.visibleCount);
  if (selectedIndex >= state.visibleCount) {
    visible.unshift(filtered[selectedIndex]);
  }
  for (const p of visible) {
    grid.append(makePrintingTile(p, p.id === currentId));
  }
  if (filtered.length === 0) {
    grid.append(el("p", { class: "empty" },
      `No printings match the current filters (${total} hidden).`));
    return;
  }
  const remaining = filtered.length - state.visibleCount;
  if (remaining > 0) {
    loadBar.hidden = false;
    const nextBatch = Math.min(remaining, PAGE_SIZE);
    $("#load-more-remaining").textContent = `(${nextBatch} more, ${remaining} remaining)`;
  }
}

function makePrintingTile(p, isSelected) {
  const imgWrap = el("div", { class: "img-wrap" });
  if (p.image_url) {
    imgWrap.append(el("img", { class: "face", src: p.image_url, alt: p.name, loading: "lazy" }));
  }
  if (p.back_image_url) {
    imgWrap.append(el("img", {
      class: "face-back", src: p.back_image_url, alt: `${p.name} (back)`, loading: "lazy",
    }));
  }
  const setYear = p.released_at?.slice(0, 4) || "";
  return el("div", {
    class: `printing ${isSelected ? "selected" : ""}`,
    "data-id": p.id,
    onclick: () => selectPrinting(p.id),
  },
    imgWrap,
    el("div", { class: "caption" },
      el("div", { class: "set-line" },
        el("span", { class: "set-code" }, (p.set || "").toUpperCase()),
        el("span", { class: "set-name" }, [p.set_name, setYear].filter(Boolean).join(" · ")),
      ),
      el("div", { class: "cn" },
        `#${p.collector_number || "?"}${p.digital ? " · digital" : ""}${p.lang && p.lang !== "en" ? " · " + p.lang : ""}`),
    ),
  );
}

function passesFilter(p) {
  if (state.filter.digital && p.digital) return false;
  if (state.filter.english && p.lang && p.lang !== "en") return false;
  const f = state.filter.frame;
  if (f === "all") return true;
  if (f === "showcase") return (p.frame_effects || []).includes("showcase");
  if (f === "borderless") return p.border_color === "borderless";
  if (f === "extended") return (p.frame_effects || []).includes("extendedart");
  return p.frame === f;
}

function updatePickerQuantityUI() {
  if (state.activeIndex === null) return;
  const entry = state.project?.entries[state.activeIndex];
  if (!entry) return;
  const input = $("#picker-qty-input");
  if (input) input.value = String(entry.quantity);
  const splitBtn = $("#picker-split-btn");
  if (splitBtn) splitBtn.disabled = entry.quantity < 2;
}

function openPickerModal(index) {
  state.activeIndex = index;
  const entry = state.project.entries[index];
  const dlg = $("#picker-modal");
  if (!dlg.open) dlg.showModal();
  updatePickerQuantityUI();

  // Default tab: Library for custom entries (they have no Scryfall printings
  // to offer anyway), Printings otherwise.
  const initialTab = entry.custom_image_path ? "library" : "printings";
  setPickerTab(initialTab);

  if (entry.custom_image_path) {
    showCustomInModal(entry);
  } else {
    loadPrintings(entry);
  }
}

function setPickerTab(tab) {
  const printings = $("#printings-scroll");
  const library = $("#picker-library-scroll");
  const filters = $("#picker-filters");
  const tabPrintings = $("#picker-tab-printings");
  const tabLibrary = $("#picker-tab-library");

  printings.hidden = tab !== "printings";
  library.hidden = tab !== "library";
  // Frame / digital / English filters only make sense for Scryfall.
  if (filters) filters.style.visibility = tab === "printings" ? "" : "hidden";
  tabPrintings.classList.toggle("active", tab === "printings");
  tabLibrary.classList.toggle("active", tab === "library");
  tabPrintings.setAttribute("aria-selected", tab === "printings");
  tabLibrary.setAttribute("aria-selected", tab === "library");

  if (tab === "library") {
    // Refresh assets each time so newly-uploaded art shows up.
    (async () => {
      try {
        libraryState.assets = await api("/api/library");
      } catch (e) {
        toast(`Could not load library: ${e.message}`, "err");
        libraryState.assets = [];
      }
      updateLibraryCount();
      renderPickerLibrary();
    })();
  } else if (tab === "printings") {
    // If we haven't loaded printings for this entry yet (e.g., a custom-only
    // entry that defaulted to Library and the user clicked Printings), do so.
    const entry = state.activeIndex !== null
      ? state.project?.entries[state.activeIndex]
      : null;
    if (entry && state.printings.length === 0 && entry.oracle_id) {
      loadPrintings(entry);
    }
  }
}

function closePickerModal() {
  const dlg = $("#picker-modal");
  if (dlg.open) dlg.close();
  state.activeIndex = null;
  state.printings = [];
  $("#printings").innerHTML = "";
  $("#load-more-bar").hidden = true;
}

function showCustomInModal(entry) {
  // Derive the upload URL from the stored path (works for both library
  // assets under `_library/` and legacy per-project uploads).
  const parts = entry.custom_image_path.split("/");
  const subdir = parts.length >= 2 ? parts[parts.length - 2] : state.activeProject;
  const filename = parts[parts.length - 1];
  const url = `/uploads/${encodeURIComponent(subdir)}/${encodeURIComponent(filename)}`;

  $("#picker-title").textContent = entry.name;
  $("#picker-subtitle").textContent = "custom uploaded art";
  $("#load-more-bar").hidden = true;

  const grid = $("#printings");
  grid.innerHTML = "";
  grid.append(el("div", { class: "custom-detail" },
    el("img", { src: url, alt: entry.name }),
    el("div", { class: "cd-body" },
      el("h3", {}, entry.name),
      el("p", {}, "This entry uses custom uploaded art — there are no Scryfall printings to swap between."),
      el("p", {}, "Switch to the ",
        el("strong", {}, "Library"),
        " tab to swap this card's art with another asset from your library."),
    ),
  ));
}

// --- Art library ------------------------------------------------------------
const libraryState = {
  assets: [],
  selected: new Set(),
  filter: "",
};

// --- Backs library ---------------------------------------------------------
const backsState = {
  assets: [],
};

async function openBacksModal() {
  const dlg = $("#backs-modal");
  if (!dlg.open) dlg.showModal();
  await refreshBacks();
}

function closeBacksModal() {
  const dlg = $("#backs-modal");
  if (dlg.open) dlg.close();
}

async function refreshBacks() {
  try {
    backsState.assets = await api("/api/backs");
  } catch (e) {
    toast(`Could not load backs: ${e.message}`, "err");
    backsState.assets = [];
  }
  updateBacksCount();
  renderBacksGrid();
}

function updateBacksCount() {
  const badge = $("#nav-backs-count");
  if (!badge) return;
  const n = backsState.assets.length;
  badge.textContent = String(n);
  badge.hidden = n === 0;
}

function renderBacksGrid() {
  const grid = $("#backs-grid");
  grid.innerHTML = "";
  const current = state.project?.default_back_filename || null;
  const status = $("#backs-selection-status");
  const clearBtn = $("#backs-clear-default");

  if (!state.activeProject) {
    status.textContent = "Open a project to set its default back image.";
    clearBtn.hidden = true;
  } else if (current) {
    status.textContent = `Current default for "${state.activeProject}": ${current}`;
    clearBtn.hidden = false;
  } else {
    status.textContent = `No back chosen for "${state.activeProject}" — click one to set it, or leave for the built-in default.`;
    clearBtn.hidden = true;
  }

  $("#backs-empty").hidden = backsState.assets.length > 0;

  for (const asset of backsState.assets) {
    const isSelected = asset.filename === current;
    const tile = el("div", {
      class: `library-item ${isSelected ? "selected" : ""}`,
      onclick: () => setProjectBack(asset.filename),
    },
      el("div", { class: "img-wrap" },
        el("img", { src: asset.url, alt: asset.filename, loading: "lazy" }),
      ),
      el("div", { class: "check" },
        svgIcon('<path d="M20 6L9 17l-5-5"/>', 12),
      ),
      el("button", {
        class: "del-lib",
        title: "Delete back",
        "aria-label": `Delete ${asset.filename}`,
        onclick: async (ev) => {
          ev.stopPropagation();
          await deleteBack(asset.filename);
        },
      }, ICON_X()),
      el("div", { class: "caption", title: asset.filename }, asset.filename),
    );
    grid.append(tile);
  }
}

async function setProjectBack(filename) {
  if (!state.activeProject) {
    toast("Open a project first, then pick a back for it.", "err");
    return;
  }
  try {
    const res = await api(
      `/api/projects/${encodeURIComponent(state.activeProject)}/default-back`,
      { method: "POST", body: JSON.stringify({ filename }) },
    );
    state.project.default_back_filename = res.default_back_filename;
    renderBacksGrid();
    toast(`Default back for "${state.activeProject}" set to ${filename}`, "ok");
  } catch (e) {
    toast(`Set failed: ${e.message}`, "err");
  }
}

async function clearProjectBack() {
  if (!state.activeProject) return;
  try {
    await api(
      `/api/projects/${encodeURIComponent(state.activeProject)}/default-back`,
      { method: "POST", body: JSON.stringify({ filename: null }) },
    );
    state.project.default_back_filename = null;
    renderBacksGrid();
    toast("Reverted to built-in default back", "ok");
  } catch (e) {
    toast(`Reset failed: ${e.message}`, "err");
  }
}

async function deleteBack(filename) {
  const ok = await confirmAction(
    `Remove "${filename}" from the backs library? Any project currently using it will fall back to the built-in default.`,
  );
  if (!ok) return;
  try {
    await api(`/api/backs/${encodeURIComponent(filename)}`, { method: "DELETE" });
  } catch (e) {
    toast(`Delete failed: ${e.message}`, "err");
    return;
  }
  // If the deleted file was the current project's default, clear the ref.
  if (state.project?.default_back_filename === filename) {
    state.project.default_back_filename = null;
  }
  await refreshBacks();
  toast(`Removed ${filename}`, "ok");
}

async function uploadBacks(files) {
  const fd = new FormData();
  for (const f of files) fd.append("files", f, f.name);
  try {
    await api("/api/backs/uploads", { method: "POST", body: fd });
  } catch (e) {
    toast(`Upload failed: ${e.message}`, "err");
    return;
  }
  toast(`Uploaded ${files.length} back image${files.length === 1 ? "" : "s"}`, "ok");
  await refreshBacks();
}

async function openLibraryModal() {
  const dlg = $("#library-modal");
  libraryState.selected.clear();
  libraryState.filter = "";
  $("#library-filter").value = "";
  updateLibraryButtons();
  if (!dlg.open) dlg.showModal();
  await refreshLibrary();
}

function closeLibraryModal() {
  const dlg = $("#library-modal");
  if (dlg.open) dlg.close();
  libraryState.selected.clear();
}

async function refreshLibrary() {
  try {
    libraryState.assets = await api("/api/library");
  } catch (e) {
    toast(`Could not load library: ${e.message}`, "err");
    libraryState.assets = [];
  }
  updateLibraryCount();
  renderLibrary();
}

function renderLibraryGrid(container, { mode, filter, emptyEl }) {
  container.innerHTML = "";
  const needle = (filter || "").trim().toLowerCase();
  const filtered = libraryState.assets.filter(
    (a) => !needle || a.filename.toLowerCase().includes(needle),
  );
  if (emptyEl) emptyEl.hidden = filtered.length > 0;

  // In swap mode, highlight the asset currently referenced by the active entry.
  const activeEntry = state.activeIndex !== null
    ? state.project?.entries[state.activeIndex]
    : null;
  const currentFilename = (mode === "swap" && activeEntry?.custom_image_path)
    ? activeEntry.custom_image_path.split("/").pop()
    : null;

  for (const asset of filtered) {
    const isSelected = mode === "multi"
      ? libraryState.selected.has(asset.filename)
      : asset.filename === currentFilename;

    const tile = el("div", {
      class: `library-item ${isSelected ? "selected" : ""}`,
      "data-filename": asset.filename,
      onclick: () => {
        if (mode === "swap") swapEntryWithLibrary(asset.filename);
        else if (mode === "add") addLibraryToDeck(asset.filename);
        else toggleLibrarySelection(asset.filename);
      },
    },
      el("div", { class: "img-wrap" },
        el("img", { src: asset.url, alt: asset.filename, loading: "lazy" }),
      ),
      el("div", { class: "check" },
        svgIcon('<path d="M20 6L9 17l-5-5"/>', 12),
      ),
      el("button", {
        class: "del-lib",
        title: "Delete from library",
        "aria-label": `Delete ${asset.filename} from library`,
        onclick: async (ev) => {
          ev.stopPropagation();
          await deleteLibraryAsset(asset.filename);
        },
      }, ICON_X()),
      el("div", { class: "caption", title: asset.filename }, asset.filename),
    );
    container.append(tile);
  }
}

function renderLibrary() {
  renderLibraryGrid($("#library-grid"), {
    mode: "multi",
    filter: libraryState.filter,
    emptyEl: $("#library-empty"),
  });
}

function renderPickerLibrary() {
  renderLibraryGrid($("#picker-library-grid"), {
    mode: "swap",
    filter: $("#picker-library-filter")?.value || "",
    emptyEl: $("#picker-library-empty"),
  });
}

function toggleLibrarySelection(filename) {
  if (libraryState.selected.has(filename)) libraryState.selected.delete(filename);
  else libraryState.selected.add(filename);
  renderLibrary();
  updateLibraryButtons();
}

function updateLibraryButtons() {
  const n = libraryState.selected.size;
  const label = $("#library-selection-count");
  const btn = $("#library-add-selected");
  btn.disabled = n === 0 || !state.activeProject;
  if (!state.activeProject) {
    label.textContent = "Open a project to add cards to a deck.";
  } else if (n === 0) {
    label.textContent = "Select assets to add.";
  } else {
    label.textContent = `${n} selected`;
  }
  btn.textContent = btn.disabled ? "Add to deck" : `Add ${n} to deck`;
}

function updateLibraryCount() {
  const badge = $("#nav-library-count");
  if (!badge) return;
  const n = libraryState.assets.length;
  badge.textContent = String(n);
  badge.hidden = n === 0;
}

async function swapEntryWithLibrary(filename) {
  if (state.activeIndex === null || !state.activeProject) return;
  try {
    const res = await api(
      `/api/projects/${encodeURIComponent(state.activeProject)}/entries/${state.activeIndex}/select-library`,
      {
        method: "POST",
        body: JSON.stringify({ filename }),
      },
    );
    const entry = state.project.entries[state.activeIndex];
    entry.custom_image_path = res.entry.custom_image_path;
    state.entryThumbs.set(state.activeIndex, { thumb_url: res.entry.thumb_url });
    renderDeckGrid();
    renderPickerLibrary();
    toast(`Art swapped to ${filename}`, "ok");
  } catch (e) {
    toast(`Swap failed: ${e.message}`, "err");
  }
}

async function deleteLibraryAsset(filename) {
  const ok = await confirmAction(
    `Remove "${filename}" from the library? Existing decks that reference it will show a broken image.`,
  );
  if (!ok) return;
  try {
    await api(`/api/library/${encodeURIComponent(filename)}`, { method: "DELETE" });
  } catch (e) {
    toast(`Delete failed: ${e.message}`, "err");
    return;
  }
  libraryState.selected.delete(filename);
  await refreshLibrary();
  updateLibraryButtons();
  toast(`Removed ${filename}`, "ok");
}

async function addSelectedLibraryToDeck() {
  if (!state.activeProject || libraryState.selected.size === 0) return;
  const filenames = [...libraryState.selected];
  try {
    await api(
      `/api/projects/${encodeURIComponent(state.activeProject)}/entries/from-library`,
      { method: "POST", body: JSON.stringify({ filenames }) },
    );
  } catch (e) {
    toast(`Add failed: ${e.message}`, "err");
    return;
  }
  toast(`Added ${filenames.length} card${filenames.length === 1 ? "" : "s"}`, "ok");
  closeLibraryModal();
  state.project = await api(`/api/projects/${encodeURIComponent(state.activeProject)}`);
  state.entryThumbs.clear();
  await Promise.all(state.project.entries.map((_, i) => refreshEntryThumb(i)));
  await refreshProjects();
  renderDeckGrid();
}

async function uploadToLibraryOnly(files) {
  const fd = new FormData();
  for (const f of files) fd.append("files", f, f.name);
  try {
    await api("/api/library/uploads", { method: "POST", body: fd });
  } catch (e) {
    toast(`Upload failed: ${e.message}`, "err");
    return false;
  }
  toast(`Uploaded ${files.length} to library`, "ok");
  await refreshLibrary();
  return true;
}

// --- Add-art dropdown menu -------------------------------------------------
function openAddArtMenu() {
  const menu = $("#add-art-menu");
  const btn = $("#btn-add-cards");
  menu.hidden = false;
  btn.setAttribute("aria-expanded", "true");
  // Dismiss on next click-outside.
  const dismiss = (ev) => {
    if (menu.contains(ev.target) || btn.contains(ev.target)) return;
    closeAddArtMenu();
    document.removeEventListener("mousedown", dismiss, true);
  };
  document.addEventListener("mousedown", dismiss, true);
}

function closeAddArtMenu() {
  const menu = $("#add-art-menu");
  const btn = $("#btn-add-cards");
  menu.hidden = true;
  btn.setAttribute("aria-expanded", "false");
}

// --- Scryfall search (add card / add token) --------------------------------
const searchState = {
  kind: "card",       // "card" | "token"
  tab: "scryfall",    // "scryfall" | "library"
  query: "",
  reqId: 0,           // monotonic to discard out-of-order fetches
  debounceTimer: null,
};

function openSearchModal(kind) {
  if (!state.activeProject) {
    toast("Open a project first to add cards.", "err");
    return;
  }
  searchState.kind = kind === "token" ? "token" : "card";
  searchState.query = "";
  const dlg = $("#search-modal");
  const input = $("#search-input");
  const results = $("#search-results");
  const empty = $("#search-empty");
  const emptyTitle = $("#search-empty-title");
  const emptySub = $("#search-empty-sub");

  $("#search-title").textContent =
    searchState.kind === "token" ? "Add token" : "Add card";
  $("#search-subtitle").textContent =
    searchState.kind === "token"
      ? "Search Scryfall for a token by name — or pick one from your library."
      : "Search Scryfall for a card by name — or pick one from your library.";
  input.placeholder = searchState.kind === "token"
    ? "e.g. Zombie, Treasure, Angel…"
    : "e.g. Sol Ring, Lightning Bolt…";
  emptyTitle.textContent = searchState.kind === "token"
    ? "Type a token name above."
    : "Type a card name above.";
  emptySub.textContent = "Click any result to add it to this deck.";

  input.value = "";
  results.innerHTML = "";
  empty.hidden = false;

  const libFilter = $("#search-library-filter");
  if (libFilter) libFilter.value = "";

  setSearchTab("scryfall");

  if (!dlg.open) dlg.showModal();
  setTimeout(() => input.focus(), 30);
}

function setSearchTab(tab) {
  searchState.tab = tab === "library" ? "library" : "scryfall";
  const scryScroll = $("#search-scroll");
  const libScroll = $("#search-library-scroll");
  const inputWrap = $("#search-input-wrap");
  const tabScry = $("#search-tab-scryfall");
  const tabLib = $("#search-tab-library");

  scryScroll.hidden = searchState.tab !== "scryfall";
  libScroll.hidden = searchState.tab !== "library";
  // The main search box drives Scryfall only; hide it on the library tab
  // (which has its own filter field baked into the toolbar).
  if (inputWrap) inputWrap.hidden = searchState.tab !== "scryfall";

  tabScry.classList.toggle("active", searchState.tab === "scryfall");
  tabLib.classList.toggle("active", searchState.tab === "library");
  tabScry.setAttribute("aria-selected", searchState.tab === "scryfall");
  tabLib.setAttribute("aria-selected", searchState.tab === "library");

  if (searchState.tab === "library") {
    (async () => {
      try {
        libraryState.assets = await api("/api/library");
      } catch (e) {
        toast(`Could not load library: ${e.message}`, "err");
        libraryState.assets = [];
      }
      updateLibraryCount();
      renderSearchLibrary();
    })();
  } else {
    setTimeout(() => $("#search-input")?.focus(), 30);
  }
}

function renderSearchLibrary() {
  renderLibraryGrid($("#search-library-grid"), {
    mode: "add",
    filter: $("#search-library-filter")?.value || "",
    emptyEl: $("#search-library-empty"),
  });
}

async function addLibraryToDeck(filename) {
  if (!state.activeProject) return;
  try {
    await api(
      `/api/projects/${encodeURIComponent(state.activeProject)}/entries/from-library`,
      { method: "POST", body: JSON.stringify({ filenames: [filename] }) },
    );
  } catch (e) {
    toast(`Add failed: ${e.message}`, "err");
    return;
  }
  closeSearchModal();
  state.project = await api(`/api/projects/${encodeURIComponent(state.activeProject)}`);
  const newIndex = state.project.entries.length - 1;
  await refreshEntryThumb(newIndex);
  await refreshProjects();
  renderDeckGrid();
  toast(`Added ${filename}`, "ok");
}

function closeSearchModal() {
  const dlg = $("#search-modal");
  if (dlg.open) dlg.close();
  if (searchState.debounceTimer) {
    clearTimeout(searchState.debounceTimer);
    searchState.debounceTimer = null;
  }
}

function scheduleSearch(rawQuery) {
  const query = (rawQuery || "").trim();
  searchState.query = query;
  if (searchState.debounceTimer) clearTimeout(searchState.debounceTimer);
  const empty = $("#search-empty");
  const emptyTitle = $("#search-empty-title");
  const emptySub = $("#search-empty-sub");
  const results = $("#search-results");
  if (!query) {
    results.innerHTML = "";
    empty.hidden = false;
    emptyTitle.textContent = searchState.kind === "token"
      ? "Type a token name above."
      : "Type a card name above.";
    emptySub.textContent = "Click any result to add it to this deck.";
    return;
  }
  searchState.debounceTimer = setTimeout(() => runSearch(query), 220);
}

async function runSearch(query) {
  const results = $("#search-results");
  const empty = $("#search-empty");
  const emptyTitle = $("#search-empty-title");
  const emptySub = $("#search-empty-sub");
  const reqId = ++searchState.reqId;
  emptyTitle.textContent = "Searching Scryfall…";
  emptySub.textContent = "";
  empty.hidden = false;
  results.innerHTML = "";

  const params = new URLSearchParams({ q: query, kind: searchState.kind });
  let data;
  try {
    data = await api(`/api/scryfall/search?${params}`);
  } catch (e) {
    if (reqId !== searchState.reqId) return;
    empty.hidden = false;
    emptyTitle.textContent = "Search failed.";
    emptySub.textContent = e.message || "Try again in a moment.";
    return;
  }
  if (reqId !== searchState.reqId) return;

  const rows = data.results || [];
  if (rows.length === 0) {
    empty.hidden = false;
    emptyTitle.textContent = "No results.";
    emptySub.textContent = searchState.kind === "token"
      ? "Try a broader name — e.g. \"Treasure\" or \"Zombie\"."
      : "Check the spelling or try a partial name.";
    return;
  }
  empty.hidden = true;
  for (const p of rows) {
    results.append(makeSearchTile(p));
  }
}

function makeSearchTile(p) {
  const imgWrap = el("div", { class: "img-wrap" });
  if (p.image_url) {
    imgWrap.append(el("img", {
      class: "face", src: p.image_url, alt: p.name, loading: "lazy",
    }));
  }
  if (p.back_image_url) {
    imgWrap.append(el("img", {
      class: "face-back", src: p.back_image_url,
      alt: `${p.name} (back)`, loading: "lazy",
    }));
  }
  const setYear = p.released_at?.slice(0, 4) || "";
  return el("div", {
    class: "printing",
    "data-id": p.id,
    onclick: () => addFromSearch(p),
  },
    imgWrap,
    el("div", { class: "caption" },
      el("div", { class: "set-line" },
        el("span", { class: "set-code" }, (p.set || "").toUpperCase()),
        el("span", { class: "set-name" },
          [p.name, [p.set_name, setYear].filter(Boolean).join(" · ")]
            .filter(Boolean).join(" — ")),
      ),
      el("div", { class: "cn" },
        `#${p.collector_number || "?"}${p.digital ? " · digital" : ""}${p.lang && p.lang !== "en" ? " · " + p.lang : ""}`),
    ),
  );
}

async function addFromSearch(printing) {
  if (!state.activeProject) return;
  const button = $("#search-close");
  try {
    button.disabled = true;
    await api(
      `/api/projects/${encodeURIComponent(state.activeProject)}/entries/from-scryfall`,
      { method: "POST", body: JSON.stringify({ scryfall_id: printing.id }) },
    );
  } catch (e) {
    toast(`Add failed: ${e.message}`, "err");
    button.disabled = false;
    return;
  }
  button.disabled = false;
  closeSearchModal();
  // Re-fetch the project and thumbs so the new entry appears in the grid.
  state.project = await api(`/api/projects/${encodeURIComponent(state.activeProject)}`);
  const newIndex = state.project.entries.length - 1;
  await refreshEntryThumb(newIndex);
  await refreshProjects();
  renderDeckGrid();
  toast(`Added ${printing.name}`, "ok");
}

// --- Upscaler comparison test ---------------------------------------------
const upscaleTestState = {
  query: "",
  reqId: 0,
  debounceTimer: null,
  runningId: null,   // Scryfall id of the currently-running test
};

function openUpscaleTestModal() {
  const dlg = $("#upscale-test-modal");
  showUpscaleStep("search");
  const input = $("#upscale-test-input");
  input.value = "";
  $("#upscale-test-results").innerHTML = "";
  $("#upscale-test-hint").textContent =
    "Click a card in the results to run the test.";
  if (!dlg.open) dlg.showModal();
  setTimeout(() => input.focus(), 30);
}

function closeUpscaleTestModal() {
  const dlg = $("#upscale-test-modal");
  if (dlg.open) dlg.close();
  if (upscaleTestState.debounceTimer) {
    clearTimeout(upscaleTestState.debounceTimer);
    upscaleTestState.debounceTimer = null;
  }
  upscaleTestState.runningId = null;
}

function showUpscaleStep(step) {
  $("#upscale-test-search").hidden = step !== "search";
  $("#upscale-test-running").hidden = step !== "running";
  $("#upscale-test-done").hidden = step !== "done";
}

function scheduleUpscaleTestSearch(rawQuery) {
  const query = (rawQuery || "").trim();
  upscaleTestState.query = query;
  if (upscaleTestState.debounceTimer) clearTimeout(upscaleTestState.debounceTimer);
  const results = $("#upscale-test-results");
  const hint = $("#upscale-test-hint");
  if (!query) {
    results.innerHTML = "";
    hint.textContent = "Click a card in the results to run the test.";
    return;
  }
  upscaleTestState.debounceTimer = setTimeout(() => runUpscaleTestSearch(query), 220);
}

async function runUpscaleTestSearch(query) {
  const results = $("#upscale-test-results");
  const hint = $("#upscale-test-hint");
  const reqId = ++upscaleTestState.reqId;
  hint.textContent = "Searching Scryfall…";
  results.innerHTML = "";
  const params = new URLSearchParams({ q: query, kind: "card" });
  let data;
  try {
    data = await api(`/api/scryfall/search?${params}`);
  } catch (e) {
    if (reqId !== upscaleTestState.reqId) return;
    hint.textContent = `Search failed: ${e.message}`;
    return;
  }
  if (reqId !== upscaleTestState.reqId) return;
  const rows = data.results || [];
  if (rows.length === 0) {
    hint.textContent = "No results.";
    return;
  }
  hint.textContent = "Click a card to start the test.";
  for (const p of rows) {
    results.append(el("div", {
      class: "printing",
      "data-id": p.id,
      onclick: () => startUpscaleTest(p),
    },
      el("div", { class: "img-wrap" },
        p.image_url
          ? el("img", { class: "face", src: p.image_url, alt: p.name, loading: "lazy" })
          : null,
      ),
      el("div", { class: "caption" },
        el("div", { class: "set-line" },
          el("span", { class: "set-code" }, (p.set || "").toUpperCase()),
          el("span", { class: "set-name" }, p.name),
        ),
        el("div", { class: "cn" }, `#${p.collector_number || "?"}`),
      ),
    ));
  }
}

async function startUpscaleTest(printing) {
  upscaleTestState.runningId = printing.id;
  showUpscaleStep("running");
  $("#upscale-test-thumb").src = printing.image_url || "";
  $("#upscale-test-name").textContent =
    `${printing.name} — ${(printing.set || "?").toUpperCase()} ${printing.collector_number || ""}`;
  $("#upscale-test-status").textContent = "Starting…";
  $("#upscale-test-bar").style.width = "0%";

  const resp = await fetch(
    `/api/upscale-test/stream?scryfall_id=${encodeURIComponent(printing.id)}`,
    { method: "POST" },
  );
  if (!resp.ok || !resp.body) {
    toast(`Test failed: ${resp.status} ${resp.statusText}`, "err");
    showUpscaleStep("search");
    return;
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let totalModels = 3;
  let modelIdx = 0;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const raw = buf.slice(0, idx); buf = buf.slice(idx + 2);
      const evt = parseSSE(raw);
      if (!evt) continue;
      if (upscaleTestState.runningId !== printing.id) {
        // User closed / started another test — bail out.
        try { reader.cancel(); } catch {}
        return;
      }
      if (evt.event === "start") {
        totalModels = (evt.data.models || []).length || 3;
      } else if (evt.event === "progress") {
        const { phase, quality, index, total } = evt.data;
        if (phase === "download") {
          $("#upscale-test-status").textContent = "Downloading source image…";
        } else if (phase === "upscale") {
          modelIdx = index;
          $("#upscale-test-status").textContent =
            `Upscaling ${index + 1}/${total} · ${quality} (native 4×)…`;
          $("#upscale-test-bar").style.width =
            `${((index) / total) * 100}%`;
        } else if (phase === "downsample") {
          $("#upscale-test-status").textContent =
            `Downsampling ${quality} → 2× (600 DPI)…`;
          $("#upscale-test-bar").style.width =
            `${((index + 0.5) / total) * 100}%`;
        }
      } else if (evt.event === "done") {
        renderUpscaleTestResults(evt.data);
        $("#upscale-test-bar").style.width = "100%";
      } else if (evt.event === "error") {
        toast(`Test failed: ${evt.data.message || "unknown"}`, "err");
        showUpscaleStep("search");
        return;
      }
    }
  }
}

function renderUpscaleTestResults(data) {
  showUpscaleStep("done");
  $("#upscale-test-done-name").textContent = data.name || "";
  $("#upscale-test-done-sub").textContent =
    `Scryfall id: ${data.scryfall_id} · click any tile for full-size preview.`;

  const grid = $("#upscale-test-grid");
  grid.innerHTML = "";
  const rows = [
    { label: "1200 DPI (native 4× — no downsample)", key: "1200" },
    { label: "600 DPI (LANCZOS downsample from 4×)", key: "600" },
  ];
  // Column header row: model names
  const header = el("div", { class: "upscale-test-row upscale-test-header" },
    el("div", { class: "upscale-test-corner" }, ""),
    ...data.results.map((r) => el("div", { class: "upscale-test-colhead" },
      el("div", { class: "colhead-title" }, r.quality),
      el("div", { class: "colhead-sub" }, r.model),
    )),
  );
  grid.append(header);

  for (const row of rows) {
    const rowEl = el("div", { class: "upscale-test-row" },
      el("div", { class: "upscale-test-rowhead" }, row.label),
      ...data.results.map((r) => {
        const url = row.key === "1200" ? r.url_1200 : r.url_600;
        const dpi = row.key === "1200" ? r.dpi_1200 : r.dpi_600;
        if (!url) {
          const msg = r.unavailable || r.error || "Not available";
          return el("div", { class: "upscale-test-cell disabled" },
            el("div", { class: "cell-empty" }, msg),
          );
        }
        return el("div", {
          class: "upscale-test-cell",
          onclick: () => openUpscaleLightbox(url,
            `${r.quality} · ${row.label}` +
            (dpi ? ` · ${Math.round(dpi.x)}×${Math.round(dpi.y)} DPI` : "")),
        },
          el("img", { class: "cell-img", src: url, alt: `${r.quality} ${row.key} DPI`, loading: "lazy" }),
          el("div", { class: "cell-meta" },
            dpi ? el("span", { class: "cell-dpi" },
              `${Math.round(dpi.x)}×${Math.round(dpi.y)} DPI`) : null,
            el("a", { class: "cell-download", href: url, download: "" }, "download"),
          ),
        );
      }),
    );
    grid.append(rowEl);
  }
}

function openUpscaleLightbox(url, caption) {
  const dlg = $("#upscale-test-lightbox");
  $("#upscale-test-lightbox-img").src = url;
  $("#upscale-test-lightbox-caption").textContent = caption || "";
  if (!dlg.open) dlg.showModal();
}

function closeUpscaleLightbox() {
  const dlg = $("#upscale-test-lightbox");
  if (dlg.open) dlg.close();
}

async function addCardsFromFiles(files) {
  if (!files || !files.length || !state.activeProject) return;
  try {
    await uploadFiles(state.activeProject, files);
  } catch (e) {
    toast(`Upload failed: ${e.message}`, "err");
    return;
  }
  state.project = await api(`/api/projects/${encodeURIComponent(state.activeProject)}`);
  state.entryThumbs.clear();
  await Promise.all(state.project.entries.map((_, i) => refreshEntryThumb(i)));
  renderDeckGrid();
  await refreshProjects();
  toast(`Added ${files.length} card${files.length === 1 ? "" : "s"}`, "ok");
}

function loadMorePrintings() {
  state.visibleCount += PAGE_SIZE;
  renderPrintings();
}

// --- Export (SSE) -----------------------------------------------------------
function updateExportButtonLabel() {
  const format = $("#export-format")?.value || "pdf";
  const label = $("#btn-export .btn-label");
  if (label) label.textContent = format === "png" ? "Export PNGs" : "Export PDF";
}

// Holds the in-flight export's AbortController so the Cancel button can tear
// down its SSE stream. Aborting stops the client read; modern Starlette sees
// the disconnect and stops the server generator at the next card boundary
// (nothing is written to disk for a cancelled run).
let exportAbort = null;

function cancelExport() {
  if (exportAbort) exportAbort.abort();
}

function runExport() {
  if (!state.activeProject) return;
  const btn = $("#btn-export");
  const cancelBtn = $("#btn-cancel-export");
  const status = $("#export-status");
  btn.disabled = true;
  if (cancelBtn) cancelBtn.hidden = false;
  exportAbort = new AbortController();
  status.className = "status";
  status.textContent = "starting…";

  // Any prior download link is stale for a new export — hide until we
  // know the new file's path from the `done` event.
  $("#download-fronts").hidden = true;
  $("#download-backs").hidden = true;
  clearPngDownloads();

  const backs = $("#backs-mode")?.value || "none";
  const upscale = $("#upscale-checkbox")?.checked ? "true" : "false";
  const quality = $("#quality-mode")?.value || "quality";
  const format = $("#export-format")?.value || "pdf";
  const paper = currentPaper();
  const dpiTarget = currentDpiTarget();
  const cutColor = currentCutColor();
  const bleed = currentBleed();
  const spacing = currentSpacing();
  const cornerFill = currentCornerFill();
  const { x: offsetX, y: offsetY } = currentOffsets();
  const params = new URLSearchParams({
    backs, upscale, quality, format, paper,
    dpi_target: String(dpiTarget),
    cut_color: cutColor,
    cut_lines: currentCutLinesMode(),
    bleed,
    gutter: spacing,
    back_offset_x: String(offsetX),
    back_offset_y: String(offsetY),
  });
  // Corner fill: only send when enabled; the colour rides along so the
  // server fills the die-cut with the user's chosen colour.
  if (cornerFill !== "off") {
    params.set("corner_fill", "true");
    params.set("corner_color", cornerFill);
  }
  // Only send png_dpi when the user actually asked for PNGs — no point
  // making the server rasterise a PDF export it isn't going to produce.
  if (format === "png") {
    params.set("png_dpi", String(resolvePngDpi()));
  }
  fetch(`/api/projects/${encodeURIComponent(state.activeProject)}/export?${params}`, {
    method: "POST",
    signal: exportAbort.signal,
  }).then(async (resp) => {
    if (!resp.ok || !resp.body) {
      throw new Error(`${resp.status} ${resp.statusText}`);
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) !== -1) {
        const raw = buf.slice(0, idx); buf = buf.slice(idx + 2);
        const evt = parseSSE(raw);
        if (evt) handleExportEvent(evt, status);
      }
    }
  }).catch((e) => {
    // AbortError is the user clicking Cancel — a deliberate stop, not a fault.
    if (e.name === "AbortError") {
      status.className = "status warn";
      status.textContent = "cancelled";
      toast("Export cancelled", "");
    } else {
      status.className = "status err";
      status.textContent = `error: ${e.message}`;
    }
  }).finally(() => {
    btn.disabled = false;
    if (cancelBtn) cancelBtn.hidden = true;
    exportAbort = null;
  });
}

function parseSSE(raw) {
  let event = "message", data = "";
  let hasFields = false;
  for (const line of raw.split("\n")) {
    // Colon-prefixed lines are SSE comments (keep-alive heartbeats). Ignore.
    if (line.startsWith(":")) continue;
    if (line.startsWith("event:")) { event = line.slice(6).trim(); hasFields = true; }
    else if (line.startsWith("data:")) { data += line.slice(5).trim(); hasFields = true; }
  }
  if (!hasFields) return null;   // pure-comment block; nothing to dispatch
  try { return { event, data: data ? JSON.parse(data) : {} }; } catch { return null; }
}

function showDownload(sel, serverPath) {
  // Server returns paths relative to CWD like "output/example.pdf".
  // The FastAPI app mounts /output as StaticFiles, so a leading slash
  // turns it into a browser-fetchable URL.
  const anchor = document.querySelector(sel);
  if (!anchor) return;
  const url = "/" + serverPath.replace(/^\/*/, "");
  const filename = serverPath.split("/").pop();
  anchor.href = url;
  anchor.setAttribute("download", filename);
  anchor.querySelector(".dl-name").textContent = filename;
  anchor.hidden = false;
}


function clearPngDownloads() {
  for (const sel of ["#download-pngs-fronts", "#download-pngs-backs"]) {
    const row = document.querySelector(sel);
    if (!row) continue;
    row.innerHTML = "";
    row.hidden = true;
  }
  clearPngFolderHint();
}

function clearPngFolderHint() {
  const box = $("#png-folder-hint");
  const path = $("#png-folder-path");
  const sub = $("#png-folder-hint-sub");
  if (box) box.hidden = true;
  if (path) path.textContent = "";
  if (sub) sub.textContent = "";
}

// After a PNG export, show the on-disk folder path prominently so users
// can open it in Finder / Explorer / Files rather than click-downloading
// each page. `absPath` (may be undefined) is what we copy to the clipboard
// because it's the fully-qualified path the OS file manager wants.
function showPngFolderHint({ relPath, absPath, count, backsCount }) {
  const box = $("#png-folder-hint");
  const path = $("#png-folder-path");
  const sub = $("#png-folder-hint-sub");
  if (!box || !path || !relPath) return;
  path.textContent = relPath;
  path.dataset.absPath = absPath || relPath;
  const total = (count || 0) + (backsCount || 0);
  const parts = [];
  if (count) parts.push(`${count} front page${count === 1 ? "" : "s"}`);
  if (backsCount) parts.push(`${backsCount} back page${backsCount === 1 ? "" : "s"}`);
  if (sub) sub.textContent = total
    ? `— ${parts.join(" + ")}`
    : "";
  box.hidden = false;
}

async function copyPngFolderPath() {
  const path = $("#png-folder-path");
  const value = path?.dataset.absPath || path?.textContent || "";
  if (!value) return;
  try {
    await navigator.clipboard.writeText(value);
    toast("Folder path copied", "ok");
  } catch {
    // Fall back for browsers without the async Clipboard API — select
    // the text so the user can Cmd+C / Ctrl+C themselves.
    const range = document.createRange();
    range.selectNodeContents(path);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    toast("Selected — press ⌘C / Ctrl+C to copy", "");
  }
}

function renderPngDownloads(sel, serverPaths, labelPrefix) {
  const row = document.querySelector(sel);
  if (!row || !serverPaths?.length) return;
  row.innerHTML = "";
  const summary = document.createElement("span");
  summary.className = "dim png-row-label";
  summary.textContent = `${labelPrefix} (${serverPaths.length}):`;
  row.append(summary);
  serverPaths.forEach((p, i) => {
    const url = "/" + p.replace(/^\/*/, "");
    const filename = p.split("/").pop();
    const a = document.createElement("a");
    a.className = "download-btn tiny";
    a.href = url;
    a.setAttribute("download", filename);
    a.title = filename;
    a.textContent = `p${i + 1}`;
    row.append(a);
  });
  row.hidden = false;
}

// --- First-run upscaler hint ----------------------------------------------
// Prints without upscaling look softer than they need to. If we know the
// user has no backend installed AND they just finished an export at 300 DPI,
// nudge them once toward `setup-upscaler`. Never nag more than once per
// browser — the localStorage key persists the dismissal.
const UPSCALER_HINT_KEY = "relmtgproxy:upscaler-hint-shown";

async function maybeSuggestUpscaler() {
  if (localStorage.getItem(UPSCALER_HINT_KEY) === "1") return;
  // If the user *did* upscale this run, no hint is needed even if we
  // don't know why (they may have a system-wide install of the binary).
  if ($("#upscale-checkbox")?.checked) return;
  try {
    const data = await api("/api/upscaler/status");
    const anyInstalled =
      data.ncnn_binary_installed ||
      (data.models || []).some((m) => m.mps?.installed || m.ncnn?.installed);
    if (anyInstalled) return;   // installed but not toggled on → user's call
  } catch {
    return;   // status endpoint failure — say nothing
  }
  // Deliberately quiet: a longer-lived info toast, not a modal.
  const hint = "Tip: install the upscaler for sharper 600 DPI prints — see the README (`python cli.py setup-upscaler`).";
  toast(hint, "ok");
  localStorage.setItem(UPSCALER_HINT_KEY, "1");
}

// --- Upscaler model manager -----------------------------------------------
async function openUpscalerModelsModal() {
  const dlg = $("#upscalerModelsModal") || $("#upscaler-models-modal");
  if (!dlg) return;
  if (!dlg.open) dlg.showModal();
  await refreshUpscalerModels();
}

function closeUpscalerModelsModal() {
  const dlg = $("#upscaler-models-modal");
  if (dlg?.open) dlg.close();
}

async function refreshUpscalerModels() {
  const body = $("#upscaler-models-body");
  const hint = $("#upscaler-models-hint");
  if (!body) return;
  body.innerHTML = "<p class=\"dim\">Loading…</p>";
  try {
    const data = await api("/api/upscaler/status");
    renderUpscalerModels(data);
  } catch (e) {
    body.innerHTML = "";
    body.append(el("p", { class: "err" }, `Failed to load models: ${e.message}`));
    if (hint) hint.textContent = "";
  }
}

function renderUpscalerModels(data) {
  const body = $("#upscaler-models-body");
  const hint = $("#upscaler-models-hint");
  body.innerHTML = "";
  const canMps = !!data.torch_mps_available;
  const canNcnnCustom = !!data.ncnn_binary_installed;
  for (const m of data.models) {
    const backends = el("div", { class: "upscaler-model-backends" });
    backends.append(makeBackendLine(m, "ncnn", canNcnnCustom));
    backends.append(makeBackendLine(m, "mps", canMps));
    const row = el("div", { class: "upscaler-model-row" },
      el("div", {},
        el("h3", {}, m.quality),
        el("p", {}, m.description || ""),
        el("p", { class: "dim" }, `ncnn model: ${m.ncnn_name}`),
      ),
      backends,
    );
    body.append(row);
  }
  if (hint) {
    const parts = [];
    if (!canNcnnCustom) parts.push("ncnn base binary not installed — run `python cli.py setup-upscaler` first.");
    if (!canMps) parts.push("PyTorch/MPS not detected — `pip install torch` to enable that backend.");
    hint.textContent = parts.join("  ·  ");
  }
}

function makeBackendLine(model, backend, backendAvailable) {
  const entry = model[backend];
  const line = el("div", { class: "upscaler-backend-line" });
  line.append(el("span", { class: "backend-name" }, backend));

  if (!entry.installable) {
    line.classList.add("unavailable");
    if (backend === "ncnn") {
      line.append(el("span", { class: "backend-status" },
        "base bundle — install with cli.py setup-upscaler"));
    } else {
      line.append(el("span", { class: "backend-status" }, "no MPS weights available"));
    }
    return line;
  }

  const status = el("span", { class: "backend-status" });
  const actions = el("div", {});
  if (entry.installed) {
    line.classList.add("installed");
    status.textContent = "installed";
    const btn = el("button", {
      class: "ghost",
      type: "button",
      onclick: () => uninstallUpscalerModel(model.quality, backend),
    }, "Uninstall");
    actions.append(btn);
  } else {
    status.textContent = "not installed";
    const disabled = !backendAvailable;
    const btn = el("button", {
      class: "primary",
      type: "button",
      disabled: disabled ? "disabled" : null,
      title: disabled
        ? (backend === "ncnn"
            ? "Install the ncnn base binary first (cli.py setup-upscaler)"
            : "Install PyTorch first (pip install torch)")
        : `Download files for ${model.quality} (${backend})`,
      onclick: () => installUpscalerModel(model.quality, backend),
    }, "Install");
    actions.append(btn);
  }
  line.append(status, actions);
  return line;
}

async function installUpscalerModel(quality, backend) {
  const btnLabel = `${quality} · ${backend}`;
  toast(`Installing ${btnLabel}…`, "ok");
  try {
    await api(`/api/upscaler/install/${encodeURIComponent(quality)}?backend=${backend}`,
      { method: "POST" });
    toast(`${btnLabel} installed`, "ok");
  } catch (e) {
    toast(`Install failed: ${e.message}`, "err");
  }
  await refreshUpscalerModels();
}

async function uninstallUpscalerModel(quality, backend) {
  const ok = await confirmAction(
    `Uninstall ${quality} (${backend})? You can reinstall from this panel later.`);
  if (!ok) return;
  try {
    await api(`/api/upscaler/install/${encodeURIComponent(quality)}?backend=${backend}`,
      { method: "DELETE" });
    toast(`${quality} · ${backend} uninstalled`, "ok");
  } catch (e) {
    toast(`Uninstall failed: ${e.message}`, "err");
  }
  await refreshUpscalerModels();
}

function handleExportEvent(evt, status) {
  const { event, data } = evt;
  if (event === "start") {
    const mode = data.upscale
      ? ` (upscaled${data.quality && data.quality !== "quality" ? ", " + data.quality : ""})`
      : "";
    status.textContent = `exporting ${data.total} cards${mode}…`;
  } else if (event === "progress") {
    if (data.phase === "render") status.textContent = "rendering PDF…";
    else if (data.phase === "corner") status.textContent = "filling corners…";
    else if (data.phase === "bleed") status.textContent = "adding bleed…";
    else if (data.phase === "rasterise") status.textContent = "rasterising PNGs…";
    else if (data.phase === "upscale") status.textContent = `upscale ${data.index + 1}/${data.total}: ${data.name}`;
    else if (data.phase === "back") status.textContent = `back ${data.index + 1}/${data.total}: ${data.name}`;
    else status.textContent = `download ${data.index + 1}/${data.total}: ${data.name}`;
  } else if (event === "done") {
    status.className = "status ok";
    status.textContent = "done";
    if (data.format === "png") {
      showPngFolderHint({
        relPath: data.output_dir,
        absPath: data.output_dir_abs,
        count: data.page_count,
        backsCount: data.backs_page_count,
      });
      renderPngDownloads("#download-pngs-fronts", data.png_paths, "Fronts");
      renderPngDownloads("#download-pngs-backs", data.backs_png_paths, "Backs");
    } else {
      showDownload("#download-fronts", data.path);
      if (data.backs_path) showDownload("#download-backs", data.backs_path);
    }
    toast(data.format === "png"
      ? "Export complete — files saved to disk"
      : "Export complete — click to download", "ok");
    maybeSuggestUpscaler();
  } else if (event === "error") {
    status.className = "status err";
    status.textContent = `error: ${data.message}`;
    toast(`Export failed: ${data.message}`, "err");
  }
}

// --- Keyboard nav (inside the printings modal) -----------------------------
// Left/Right cycle between deck entries without closing the modal, so you
// can rip through picking art for a whole deck.
document.addEventListener("keydown", (ev) => {
  if (["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName)) return;
  const dlg = $("#picker-modal");
  if (!dlg?.open || state.activeIndex === null) return;
  const entries = state.project?.entries || [];
  if (!entries.length) return;
  if (ev.key === "ArrowRight" || ev.key === "l") {
    ev.preventDefault();
    openPickerModal(Math.min(entries.length - 1, state.activeIndex + 1));
  } else if (ev.key === "ArrowLeft" || ev.key === "h") {
    ev.preventDefault();
    openPickerModal(Math.max(0, state.activeIndex - 1));
  }
});

// --- Wire up ----------------------------------------------------------------
function readInitialProjectFromHash() {
  const m = /project=([^&]+)/.exec(location.hash);
  return m ? decodeURIComponent(m[1]) : null;
}

window.addEventListener("DOMContentLoaded", async () => {
  const onFilterChange = () => {
    state.visibleCount = PAGE_SIZE;
    renderPrintings();
    $("#printings-scroll").scrollTop = 0;
  };
  $("#filter-digital").addEventListener("change", (e) => { state.filter.digital = e.target.checked; onFilterChange(); });
  $("#filter-english").addEventListener("change", (e) => { state.filter.english = e.target.checked; onFilterChange(); });
  $("#filter-frame").addEventListener("change", (e) => { state.filter.frame = e.target.value; onFilterChange(); });
  $("#filter-entries").addEventListener("input", (e) => { state.entryFilter = e.target.value; renderDeckGrid(); });
  $("#btn-load-more").addEventListener("click", loadMorePrintings);
  $("#btn-export").addEventListener("click", runExport);
  $("#btn-cancel-export")?.addEventListener("click", cancelExport);
  $("#export-format")?.addEventListener("change", updateExportButtonLabel);
  updateExportButtonLabel();

  // Export panel: narrow-screen drawer + live-option muting.
  $("#export-drawer-toggle")?.addEventListener("click", openExportDrawer);
  $("#export-drawer-close")?.addEventListener("click", closeExportDrawer);
  $("#export-backdrop")?.addEventListener("click", closeExportDrawer);
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") closeExportDrawer();
  });
  $("#upscale-checkbox")?.addEventListener("change", refreshExportPanelState);
  $("#backs-mode")?.addEventListener("change", refreshExportPanelState);
  refreshExportPanelState();
  $("#btn-new-project").addEventListener("click", openNewProjectForm);
  $("#btn-hero-new")?.addEventListener("click", openNewProjectForm);
  $("#brand-link")?.addEventListener("click", (ev) => {
    ev.preventDefault();
    // Clicking the logo goes home (project list) without deleting anything.
    if (state.activeProject) closeProject();
  });
  $("#btn-cancel-new").addEventListener("click", () => {
    setView(state.activeProject ? "picker" : "landing");
  });
  $("#btn-create").addEventListener("click", createProject);
  // Enter in the name field jumps to the textarea; Cmd/Ctrl+Enter in the
  // textarea submits — small productivity nicety.
  $("#np-name")?.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); $("#np-decklist").focus(); }
  });
  $("#np-decklist")?.addEventListener("keydown", (ev) => {
    if ((ev.metaKey || ev.ctrlKey) && ev.key === "Enter") {
      ev.preventDefault(); createProject();
    }
  });

  // --- New Project drop-zone -----------------------------------------------
  const dz = $("#np-dropzone");
  const fi = $("#np-file-input");
  if (dz && fi) {
    fi.addEventListener("change", (ev) => {
      acceptNpFiles(ev.target.files);
      fi.value = "";  // allow re-selecting the same file later
    });
    dz.addEventListener("dragover", (ev) => {
      ev.preventDefault();
      dz.classList.add("dragging");
    });
    dz.addEventListener("dragleave", () => dz.classList.remove("dragging"));
    dz.addEventListener("drop", (ev) => {
      ev.preventDefault();
      dz.classList.remove("dragging");
      acceptNpFiles(ev.dataTransfer.files);
    });
  }

  // --- Existing-project "Add art" ------------------------------------------
  const addBtn = $("#btn-add-cards");
  const entriesFi = $("#entries-file-input");
  addBtn?.addEventListener("click", (ev) => {
    ev.stopPropagation();
    const menu = $("#add-art-menu");
    if (menu.hidden) openAddArtMenu();
    else closeAddArtMenu();
  });
  $("#menu-upload-new")?.addEventListener("click", () => {
    closeAddArtMenu();
    entriesFi.click();
  });
  $("#menu-open-library")?.addEventListener("click", () => {
    closeAddArtMenu();
    openLibraryModal();
  });
  entriesFi?.addEventListener("change", async (ev) => {
    const files = Array.from(ev.target.files || []);
    entriesFi.value = "";
    if (files.length) await addCardsFromFiles(files);
  });

  // --- Sidebar Library + Backs entries ------------------------------------
  refreshLibrary().catch(() => {});
  refreshBacks().catch(() => {});
  $("#nav-library")?.addEventListener("click", openLibraryModal);
  $("#nav-upscale-test")?.addEventListener("click", openUpscaleTestModal);

  // Upscale-test modal wiring
  const upDlg = $("#upscale-test-modal");
  $("#upscale-test-close")?.addEventListener("click", closeUpscaleTestModal);
  upDlg?.addEventListener("click", (ev) => {
    if (ev.target === upDlg) closeUpscaleTestModal();
  });
  $("#upscale-test-input")?.addEventListener("input", (ev) => {
    scheduleUpscaleTestSearch(ev.target.value);
  });
  $("#upscale-test-again")?.addEventListener("click", () => {
    upscaleTestState.runningId = null;
    openUpscaleTestModal();
  });
  const upLb = $("#upscale-test-lightbox");
  $("#upscale-test-lightbox-close")?.addEventListener("click", closeUpscaleLightbox);
  upLb?.addEventListener("click", (ev) => {
    if (ev.target === upLb) closeUpscaleLightbox();
  });

  // Upscaler-models modal
  $("#nav-upscaler-models")?.addEventListener("click", openUpscalerModelsModal);
  const umDlg = $("#upscaler-models-modal");
  $("#upscaler-models-close")?.addEventListener("click", closeUpscalerModelsModal);
  umDlg?.addEventListener("click", (ev) => {
    if (ev.target === umDlg) closeUpscalerModelsModal();
  });
  $("#nav-backs")?.addEventListener("click", openBacksModal);

  // Backs modal wiring
  const backsModal = $("#backs-modal");
  $("#backs-close")?.addEventListener("click", closeBacksModal);
  backsModal?.addEventListener("click", (ev) => {
    if (ev.target === backsModal) closeBacksModal();
  });
  const backsFi = $("#backs-file-input");
  $("#backs-upload-more")?.addEventListener("click", () => backsFi?.click());
  backsFi?.addEventListener("change", async (ev) => {
    const files = Array.from(ev.target.files || []);
    backsFi.value = "";
    if (files.length) await uploadBacks(files);
  });
  $("#backs-clear-default")?.addEventListener("click", clearProjectBack);

  // --- Library modal wiring ------------------------------------------------
  const libModal = $("#library-modal");
  $("#library-close")?.addEventListener("click", closeLibraryModal);
  libModal?.addEventListener("click", (ev) => {
    if (ev.target === libModal) closeLibraryModal();
  });
  $("#library-filter")?.addEventListener("input", (ev) => {
    libraryState.filter = ev.target.value;
    renderLibrary();
  });
  $("#library-add-selected")?.addEventListener("click", addSelectedLibraryToDeck);
  // Uploads from *inside* the library modal go to the library only —
  // they don't automatically become deck entries. Use the selection +
  // "Add to deck" flow for that.
  const libFi = $("#library-file-input");
  $("#library-upload-more")?.addEventListener("click", () => libFi?.click());
  libFi?.addEventListener("change", async (ev) => {
    const files = Array.from(ev.target.files || []);
    libFi.value = "";
    if (files.length) await uploadToLibraryOnly(files);
  });

  // Drop-onto-deck for existing projects.
  const deckView = $("#deck-view");
  const dropHint = $("#deck-drop-hint");
  if (deckView && dropHint) {
    let depth = 0;
    deckView.addEventListener("dragenter", (ev) => {
      if (!hasFiles(ev.dataTransfer)) return;
      depth += 1; dropHint.hidden = false;
    });
    deckView.addEventListener("dragover", (ev) => {
      if (hasFiles(ev.dataTransfer)) ev.preventDefault();
    });
    deckView.addEventListener("dragleave", () => {
      depth = Math.max(0, depth - 1);
      if (depth === 0) dropHint.hidden = true;
    });
    deckView.addEventListener("drop", async (ev) => {
      if (!hasFiles(ev.dataTransfer)) return;
      ev.preventDefault();
      depth = 0; dropHint.hidden = true;
      const files = Array.from(ev.dataTransfer.files || [])
        .filter((f) => /^image\/(png|jpe?g|webp)$/i.test(f.type)
                       || /\.(png|jpe?g|webp)$/i.test(f.name));
      if (files.length) await addCardsFromFiles(files);
    });
  }

  // --- Scryfall search modal ---------------------------------------------
  const searchModal = $("#search-modal");
  $("#search-close")?.addEventListener("click", closeSearchModal);
  searchModal?.addEventListener("click", (ev) => {
    if (ev.target === searchModal) closeSearchModal();
  });
  searchModal?.addEventListener("close", () => {
    if (searchState.debounceTimer) {
      clearTimeout(searchState.debounceTimer);
      searchState.debounceTimer = null;
    }
  });
  $("#search-input")?.addEventListener("input", (ev) => {
    scheduleSearch(ev.target.value);
  });
  $("#search-input")?.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      if (searchState.debounceTimer) clearTimeout(searchState.debounceTimer);
      runSearch(searchState.query);
    }
  });
  $("#search-tab-scryfall")?.addEventListener("click", () => setSearchTab("scryfall"));
  $("#search-tab-library")?.addEventListener("click", () => setSearchTab("library"));
  $("#search-library-filter")?.addEventListener("input", renderSearchLibrary);

  // Modal picker close: close button + Esc + backdrop click.
  const pickerModal = $("#picker-modal");
  $("#picker-close")?.addEventListener("click", closePickerModal);
  pickerModal?.addEventListener("close", () => {
    state.activeIndex = null;
    state.printings = [];
  });
  // Tab switching in the picker modal.
  $("#picker-tab-printings")?.addEventListener("click", () => setPickerTab("printings"));
  $("#picker-tab-library")?.addEventListener("click",   () => setPickerTab("library"));
  $("#picker-library-filter")?.addEventListener("input", renderPickerLibrary);

  // Quantity stepper inside the picker modal.
  $("#picker-qty-dec")?.addEventListener("click", () => {
    if (state.activeIndex === null) return;
    const cur = state.project.entries[state.activeIndex].quantity;
    setEntryQuantity(state.activeIndex, cur - 1);
  });
  $("#picker-qty-inc")?.addEventListener("click", () => {
    if (state.activeIndex === null) return;
    const cur = state.project.entries[state.activeIndex].quantity;
    setEntryQuantity(state.activeIndex, cur + 1);
  });
  $("#picker-qty-input")?.addEventListener("change", (ev) => {
    if (state.activeIndex === null) return;
    const v = parseInt(ev.target.value, 10);
    if (Number.isFinite(v)) setEntryQuantity(state.activeIndex, v);
    else updatePickerQuantityUI();  // revert bad input
  });
  $("#picker-split-btn")?.addEventListener("click", async () => {
    if (state.activeIndex === null) return;
    await splitEntry(state.activeIndex);
  });
  // Backdrop click (outside .picker-modal-header + printings-scroll) closes.
  pickerModal?.addEventListener("click", (ev) => {
    // A click directly on the <dialog> element itself (not a descendant)
    // means the user hit the backdrop area.
    if (ev.target === pickerModal) closePickerModal();
  });

  // --- Back-alignment offsets --------------------------------------------
  applyOffsetsToInputs(loadOffsets());
  const persistOffsets = () => {
    const { x, y } = currentOffsets();
    saveOffsets(x, y);
  };
  $("#back-offset-x")?.addEventListener("change", persistOffsets);
  $("#back-offset-y")?.addEventListener("change", persistOffsets);
  $("#btn-align-test")?.addEventListener("click", downloadRegistrationTest);

  // --- Paper size ---------------------------------------------------------
  const paperSel = $("#paper-size");
  if (paperSel) {
    paperSel.value = loadPaper();
    paperSel.addEventListener("change", () => savePaper(paperSel.value));
  }

  // --- Print resolution --------------------------------------------------
  const dpiSel = $("#dpi-target");
  if (dpiSel) {
    dpiSel.value = String(loadDpiTarget());
    dpiSel.addEventListener("change", () => {
      const v = parseInt(dpiSel.value, 10);
      saveDpiTarget(v);
      updateUpscaleLabel();
    });
    updateUpscaleLabel();
  }

  // --- PNG DPI (only surfaced when Format=PNG) ---------------------------
  const pngDpiSel = $("#png-dpi");
  if (pngDpiSel) {
    pngDpiSel.value = loadPngDpi();
    pngDpiSel.addEventListener("change", () => savePngDpi(pngDpiSel.value));
  }
  // Toggle chip visibility whenever the Format dropdown changes.
  $("#export-format")?.addEventListener("change", updatePngDpiChipVisibility);
  updatePngDpiChipVisibility();

  // PNG folder hint — copy button. The hint element itself is shown/hidden
  // by the export done handler.
  $("#png-folder-copy")?.addEventListener("click", copyPngFolderPath);

  // --- Cut-guide colour -------------------------------------------------
  const cutColorSel = $("#cut-color");
  if (cutColorSel) {
    cutColorSel.value = loadCutColor();
    cutColorSel.addEventListener("change", () => saveCutColor(cutColorSel.value));
  }

  // --- Cut-line style ---------------------------------------------------
  const cutLinesSel = $("#cut-lines-mode");
  if (cutLinesSel) {
    cutLinesSel.value = loadCutLinesMode();
    cutLinesSel.addEventListener("change", () => saveCutLinesMode(cutLinesSel.value));
  }

  // --- Bleed dropdown ---------------------------------------------------
  const bleedSel = $("#bleed");
  if (bleedSel) {
    bleedSel.value = loadBleed();
    bleedSel.addEventListener("change", () => saveBleed(bleedSel.value));
  }

  // --- Card spacing (gutter) --------------------------------------------
  const spacingSel = $("#spacing");
  if (spacingSel) {
    spacingSel.value = loadSpacing();
    spacingSel.addEventListener("change", () => saveSpacing(spacingSel.value));
  }

  // --- Corner fill ------------------------------------------------------
  const cornerFillSel = $("#corner-fill");
  if (cornerFillSel) {
    cornerFillSel.value = loadCornerFill();
    cornerFillSel.addEventListener("change", () => saveCornerFill(cornerFillSel.value));
  }

  // --- Deck-grid zoom -----------------------------------------------------
  applyZoom(loadZoom());
  $("#zoom-slider")?.addEventListener("input", (ev) => {
    applyZoom(parseInt(ev.target.value, 10));
  });

  await refreshProjects();
  const initial = readInitialProjectFromHash();
  if (initial && state.projects.some((p) => p.name === initial)) {
    await openProject(initial);
  } else {
    setView("landing");
  }
});

// Keyboard zoom (+/-) — only when the deck view is showing and focus isn't
// in an input. Also accept =/+/_ as convenience aliases.
document.addEventListener("keydown", (ev) => {
  if (state.view !== "deck") return;
  if (["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName)) return;
  if ($("#picker-modal")?.open) return;
  if ($("#search-modal")?.open) return;
  if (ev.key === "+" || ev.key === "=") {
    ev.preventDefault();
    applyZoom(currentZoom() - 1);   // + = fewer/larger cards (zoom in)
  } else if (ev.key === "-" || ev.key === "_") {
    ev.preventDefault();
    applyZoom(currentZoom() + 1);   // - = more/smaller cards (zoom out)
  }
});
