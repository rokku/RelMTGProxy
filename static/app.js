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
  $("#btn-export").hidden = v !== "deck";
  $("#backs-picker").hidden = v !== "deck";
  $("#upscale-toggle").hidden = v !== "deck";
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

  const looksLikeUrl = /^\s*(?:https?:\/\/)?(?:www\.)?moxfield\.com\//i.test(decklist);
  openProgressModal({
    title: "Creating your deck",
    sub: looksLikeUrl
      ? "Contacting Moxfield…"
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

  // Ensure the grid-level drop handler is attached once the grid exists.
  _wireGridLevelDrop();
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
  $("#picker-subtitle").textContent = `${entry.quantity}× — loading printings…`;
  $("#printings").innerHTML = "";
  $("#load-more-bar").hidden = true;
  state.visibleCount = PAGE_SIZE;
  try {
    const data = await api(`/api/prints/${entry.oracle_id}`);
    state.printings = data.printings;
    $("#picker-subtitle").textContent =
      `${entry.quantity}×  ·  ${state.printings.length} printings on Scryfall`;
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
      `No printings match the current filters (${state.printings.length} hidden).`));
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

function openPickerModal(index) {
  state.activeIndex = index;
  const entry = state.project.entries[index];
  const dlg = $("#picker-modal");
  if (!dlg.open) dlg.showModal();

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
  $("#picker-subtitle").textContent = `${entry.quantity}×  ·  custom uploaded art`;
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
function runExport() {
  if (!state.activeProject) return;
  const btn = $("#btn-export");
  const status = $("#export-status");
  btn.disabled = true;
  status.className = "status";
  status.textContent = "starting…";

  // Any prior download link is stale for a new export — hide until we
  // know the new file's path from the `done` event.
  $("#download-fronts").hidden = true;
  $("#download-backs").hidden = true;

  const backs = $("#backs-mode")?.value || "none";
  const upscale = $("#upscale-checkbox")?.checked ? "true" : "false";
  const params = new URLSearchParams({ backs, upscale });
  fetch(`/api/projects/${encodeURIComponent(state.activeProject)}/export?${params}`, {
    method: "POST",
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
    status.className = "status err";
    status.textContent = `error: ${e.message}`;
  }).finally(() => { btn.disabled = false; });
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


function handleExportEvent(evt, status) {
  const { event, data } = evt;
  if (event === "start") {
    const mode = data.upscale ? " (upscaled)" : "";
    status.textContent = `exporting ${data.total} cards${mode}…`;
  } else if (event === "progress") {
    if (data.phase === "render") status.textContent = "rendering PDF…";
    else if (data.phase === "upscale") status.textContent = `upscale ${data.index + 1}/${data.total}: ${data.name}`;
    else if (data.phase === "back") status.textContent = `back ${data.index + 1}/${data.total}: ${data.name}`;
    else status.textContent = `download ${data.index + 1}/${data.total}: ${data.name}`;
  } else if (event === "done") {
    status.className = "status ok";
    status.textContent = "done";
    showDownload("#download-fronts", data.path);
    if (data.backs_path) showDownload("#download-backs", data.backs_path);
    toast("Export complete — click to download", "ok");
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

  // --- Sidebar Library entry ------------------------------------------------
  // Prime the count once at startup so the badge reflects reality even
  // before the user opens the library modal for the first time.
  refreshLibrary().catch(() => {});
  $("#nav-library")?.addEventListener("click", openLibraryModal);

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
  // Backdrop click (outside .picker-modal-header + printings-scroll) closes.
  pickerModal?.addEventListener("click", (ev) => {
    // A click directly on the <dialog> element itself (not a descendant)
    // means the user hit the backdrop area.
    if (ev.target === pickerModal) closePickerModal();
  });

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
  if (ev.key === "+" || ev.key === "=") {
    ev.preventDefault();
    applyZoom(currentZoom() - 1);   // + = fewer/larger cards (zoom in)
  } else if (ev.key === "-" || ev.key === "_") {
    ev.preventDefault();
    applyZoom(currentZoom() + 1);   // - = more/smaller cards (zoom out)
  }
});
