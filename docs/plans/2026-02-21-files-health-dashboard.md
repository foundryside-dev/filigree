# Files & Code Health Dashboard Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add two new dashboard tabs — Files (file list + detail panel + timeline) and Code Health (hotspots, severity donut, scan coverage, recent scans) — consuming existing backend APIs.

**Architecture:** Two new view modules (`views/files.js`, `views/health.js`) matching the two new tabs. File detail reuses the existing `#detailPanel` slide-in. All API functions added to `api.js`. State fields added to `state.js`. Router and HTML shell updated for the two new views.

**Tech Stack:** Vanilla JS ES modules, Tailwind CDN, CSS conic-gradient for donut chart. No new dependencies.

---

### Task 1: Add API functions for files and findings

**Files:**
- Modify: `src/filigree/static/js/api.js:332` (append after last function)

**Step 1: Add file/findings fetch functions to api.js**

Append these functions after the existing `postReload()` at line 324:

```js
// --- File & Findings API ---

export async function fetchFiles(params) {
  const qs = params ? "?" + new URLSearchParams(params) : "";
  const resp = await fetch(apiUrl("/files" + qs));
  if (!resp.ok) return null;
  return resp.json();
}

export async function fetchFileDetail(fileId) {
  const resp = await fetch(apiUrl(`/files/${encodeURIComponent(fileId)}`));
  if (!resp.ok) return null;
  return resp.json();
}

export async function fetchFileFindings(fileId, params) {
  const qs = params ? "?" + new URLSearchParams(params) : "";
  const resp = await fetch(apiUrl(`/files/${encodeURIComponent(fileId)}/findings` + qs));
  if (!resp.ok) return null;
  return resp.json();
}

export async function fetchFileTimeline(fileId, params) {
  const qs = params ? "?" + new URLSearchParams(params) : "";
  const resp = await fetch(apiUrl(`/files/${encodeURIComponent(fileId)}/timeline` + qs));
  if (!resp.ok) return null;
  return resp.json();
}

export async function fetchHotspots(limit) {
  const qs = limit ? `?limit=${limit}` : "";
  const resp = await fetch(apiUrl("/files/hotspots" + qs));
  if (!resp.ok) return null;
  return resp.json();
}

export async function fetchFileSchema() {
  const resp = await fetch(apiUrl("/files/_schema"));
  if (!resp.ok) return null;
  return resp.json();
}

export async function postFileAssociation(fileId, body) {
  try {
    const resp = await fetch(apiUrl(`/files/${encodeURIComponent(fileId)}/associations`), {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      return { ok: false, error: extractError(err, "Association failed") };
    }
    return { ok: true, data: await resp.json() };
  } catch (_e) {
    return { ok: false, error: "Network error" };
  }
}
```

**Step 2: Verify no syntax errors**

Run: `node --check src/filigree/static/js/api.js`
Expected: No output (clean parse)

**Step 3: Commit**

```bash
git add src/filigree/static/js/api.js
git commit -m "feat(dashboard): add file/findings API client functions"
```

---

### Task 2: Add state fields and severity constants

**Files:**
- Modify: `src/filigree/static/js/state.js:124` (add before closing brace of `state`)

**Step 1: Add file-related state fields**

Add after the `_activePopover: null` line (line 123), before the closing `};`:

```js
  // File views
  filesData: null,
  filesPage: { offset: 0, limit: 25 },
  filesSort: "updated_at",
  filesSearch: "",
  filesCriticalOnly: false,
  selectedFile: null,
  fileDetailData: null,
  fileDetailTab: "findings",
  hotspots: null,
```

**Step 2: Add severity color constants**

Add after the `TYPE_ICONS` export (after line 33):

```js
export const SEVERITY_COLORS = {
  critical: { bg: "bg-red-900/50", text: "text-red-400", border: "border-red-800", hex: "#EF4444" },
  high: { bg: "bg-orange-900/50", text: "text-orange-400", border: "border-orange-800", hex: "#F97316" },
  medium: { bg: "bg-yellow-900/50", text: "text-yellow-400", border: "border-yellow-800", hex: "#EAB308" },
  low: { bg: "bg-blue-900/50", text: "text-blue-400", border: "border-blue-800", hex: "#3B82F6" },
  info: { bg: "bg-slate-800/50", text: "text-slate-400", border: "border-slate-700", hex: "#64748B" },
};
```

**Step 3: Verify no syntax errors**

Run: `node --check src/filigree/static/js/state.js`
Expected: No output (clean parse)

**Step 4: Commit**

```bash
git add src/filigree/static/js/state.js
git commit -m "feat(dashboard): add file state fields and severity color constants"
```

---

### Task 3: Update router for files and health views

**Files:**
- Modify: `src/filigree/static/js/router.js:32-59` (switchView function)
- Modify: `src/filigree/static/js/router.js:114-139` (parseHash function)

**Step 1: Add files and health to switchView**

In `switchView()`, add toggle lines after the workflowView toggle (line 39):

```js
  const filesEl = document.getElementById("filesView");
  if (filesEl) filesEl.classList.toggle("hidden", view !== "files");
  const healthEl = document.getElementById("healthView");
  if (healthEl) healthEl.classList.toggle("hidden", view !== "health");
```

Add button class assignments after the btnWorkflow line (line 49):

```js
  const btnFiles = document.getElementById("btnFiles");
  if (btnFiles) btnFiles.className = view === "files" ? ACTIVE_CLASS : INACTIVE_CLASS;
  const btnHealth = document.getElementById("btnHealth");
  if (btnHealth) btnHealth.className = view === "health" ? ACTIVE_CLASS : INACTIVE_CLASS;
```

Note: We use `getElementById` + null check (rather than bare `.className =`) because these elements don't exist yet in the HTML — this way the router change is safe to deploy before the HTML change.

**Step 2: Add files and health to parseHash**

In `parseHash()`, add cases before the final `else` block (before line 130):

```js
  } else if (view === "files") {
    state.currentView = "files";
  } else if (view === "health") {
    state.currentView = "health";
```

**Step 3: Verify no syntax errors**

Run: `node --check src/filigree/static/js/router.js`
Expected: No output (clean parse)

**Step 4: Commit**

```bash
git add src/filigree/static/js/router.js
git commit -m "feat(dashboard): add files and health views to router"
```

---

### Task 4: Update dashboard.html with view containers and nav buttons

**Files:**
- Modify: `src/filigree/static/dashboard.html:142-148` (nav buttons)
- Modify: `src/filigree/static/dashboard.html:313-327` (before detail panel)

**Step 1: Add nav buttons**

After the Workflow button (line 147), add:

```html
      <button id="btnFiles" onclick="switchView('files')" class="px-3 py-1 rounded text-xs font-medium" title="File records and scan findings">Files</button>
      <button id="btnHealth" onclick="switchView('health')" class="px-3 py-1 rounded text-xs font-medium" title="Code health — hotspots, severity breakdown, scan coverage">Health</button>
```

**Step 2: Add view containers**

After the `</div>` closing workflowView (line 321) and before the detail panel comment (line 323), add:

```html
  <!-- Files view -->
  <div id="filesView" class="flex-1 hidden overflow-y-auto p-6">
    <div class="max-w-6xl mx-auto">
      <div class="flex items-center gap-3 mb-4">
        <span class="text-base font-semibold text-primary">Files</span>
        <div class="flex items-center gap-2">
          <input id="filesSearch" type="text" placeholder="Filter by path..."
                 class="bg-overlay text-primary text-xs rounded px-3 py-1 border border-strong w-64 focus:outline-none focus-accent">
          <label class="flex items-center gap-1 text-xs text-secondary">
            <input type="checkbox" id="filesCriticalOnly" style="accent-color:var(--accent)"> Critical only
          </label>
        </div>
      </div>
      <div id="filesContent" class="text-secondary text-xs">Loading...</div>
    </div>
  </div>

  <!-- Code Health view -->
  <div id="healthView" class="flex-1 hidden overflow-y-auto p-6">
    <div class="max-w-5xl mx-auto">
      <div class="flex items-center gap-3 mb-4">
        <span class="text-base font-semibold text-primary">Code Health</span>
      </div>
      <div id="healthContent" class="text-secondary text-xs">Loading...</div>
    </div>
  </div>
```

**Step 3: Commit**

```bash
git add src/filigree/static/dashboard.html
git commit -m "feat(dashboard): add Files and Health view containers and nav buttons"
```

---

### Task 5: Implement File List View (`views/files.js`)

This is the largest task. It creates the Files view module with the sortable file table.

**Files:**
- Create: `src/filigree/static/js/views/files.js`

**Step 1: Create the files view module**

```js
// ---------------------------------------------------------------------------
// Files view — file list table, file detail panel, file timeline.
// ---------------------------------------------------------------------------

import { fetchFileDetail, fetchFileFindings, fetchFiles, fetchFileTimeline, postFileAssociation } from "../api.js";
import { SEVERITY_COLORS, state } from "../state.js";
import { escHtml, showToast } from "../ui.js";

// --- Callbacks for functions not yet available at import time ---
export const callbacks = { openDetail: null, fetchData: null };

// --- Severity helpers ---

function severityBadge(severity, count) {
  if (!count) return "";
  const c = SEVERITY_COLORS[severity] || SEVERITY_COLORS.info;
  return `<span class="inline-flex items-center px-1.5 py-0.5 rounded text-xs ${c.bg} ${c.text} ${c.border}" style="border-width:1px">${count}</span>`;
}

function healthBorderClass(summary) {
  if (!summary) return "";
  if (summary.critical > 0) return "border-l-4 border-l-red-500";
  if (summary.high > 0) return "border-l-4 border-l-orange-500";
  if (summary.medium > 0) return "border-l-4 border-l-yellow-500";
  return "border-l-4 border-l-emerald-500";
}

// --- File List ---

let _searchTimeout = null;

export async function loadFiles() {
  const container = document.getElementById("filesContent");
  if (!container) return;

  // Wire up search input (once)
  const searchInput = document.getElementById("filesSearch");
  if (searchInput && !searchInput._wired) {
    searchInput._wired = true;
    searchInput.addEventListener("input", () => {
      clearTimeout(_searchTimeout);
      _searchTimeout = setTimeout(() => {
        state.filesSearch = searchInput.value.trim();
        state.filesPage.offset = 0;
        loadFiles();
      }, 300);
    });
  }

  // Wire up critical-only checkbox (once)
  const critBox = document.getElementById("filesCriticalOnly");
  if (critBox && !critBox._wired) {
    critBox._wired = true;
    critBox.addEventListener("change", () => {
      state.filesCriticalOnly = critBox.checked;
      state.filesPage.offset = 0;
      loadFiles();
    });
  }

  container.innerHTML = '<div style="color:var(--text-muted)">Loading...</div>';

  const params = {
    limit: state.filesPage.limit,
    offset: state.filesPage.offset,
    sort: state.filesSort,
  };
  if (state.filesSearch) params.path_prefix = state.filesSearch;
  if (state.filesCriticalOnly) params.min_findings = 1;

  const data = await fetchFiles(params);
  if (!data) {
    container.innerHTML = '<div class="text-red-400">Failed to load files.</div>';
    return;
  }

  state.filesData = data;

  if (!data.results.length) {
    container.innerHTML =
      '<div class="p-6 text-center" style="color:var(--text-muted)">' +
      '<div class="font-medium mb-2" style="color:var(--text-primary)">No files tracked yet</div>' +
      '<div>Ingest scan results via POST /api/v1/scan-results to start tracking files.</div></div>';
    return;
  }

  const sortArrow = (col) =>
    state.filesSort === col ? ' <span style="color:var(--accent)">&#9660;</span>' : "";

  const headerCols = [
    { key: "path", label: "Path", cls: "text-left" },
    { key: "language", label: "Lang", cls: "text-left" },
    { key: null, label: "Critical", cls: "text-center" },
    { key: null, label: "High", cls: "text-center" },
    { key: null, label: "Medium", cls: "text-center" },
    { key: null, label: "Low", cls: "text-center" },
    { key: null, label: "Issues", cls: "text-center" },
    { key: "updated_at", label: "Last Update", cls: "text-right" },
  ];

  const headHtml = headerCols
    .map((h) => {
      const sortable = h.key ? ` cursor-pointer" onclick="sortFiles('${h.key}')" role="button" tabindex="0"` : '"';
      return `<th class="${h.cls} py-2 px-3 font-medium${sortable} style="color:var(--text-muted)">${h.label}${h.key ? sortArrow(h.key) : ""}</th>`;
    })
    .join("");

  const rowsHtml = data.results
    .map((f) => {
      const s = f.summary || {};
      const border = healthBorderClass(s);
      const assocCount = f.associations_count || 0;
      const updated = f.updated_at ? new Date(f.updated_at).toLocaleDateString() : "\u2014";
      return (
        `<tr class="bg-overlay-hover cursor-pointer ${border}" onclick="openFileDetail('${escHtml(f.id)}')" role="button" tabindex="0">` +
        `<td class="py-2 px-3 text-accent truncate max-w-xs" title="${escHtml(f.path)}">${escHtml(f.path)}</td>` +
        `<td class="py-2 px-3">${escHtml(f.language || "\u2014")}</td>` +
        `<td class="py-2 px-3 text-center">${severityBadge("critical", s.critical)}</td>` +
        `<td class="py-2 px-3 text-center">${severityBadge("high", s.high)}</td>` +
        `<td class="py-2 px-3 text-center">${severityBadge("medium", s.medium)}</td>` +
        `<td class="py-2 px-3 text-center">${severityBadge("low", s.low)}</td>` +
        `<td class="py-2 px-3 text-center">${assocCount || "\u2014"}</td>` +
        `<td class="py-2 px-3 text-right" style="color:var(--text-muted)">${updated}</td>` +
        "</tr>"
      );
    })
    .join("");

  const paginationHtml = buildPagination(data);

  container.innerHTML =
    '<div class="rounded overflow-hidden" style="background:var(--surface-raised);border:1px solid var(--border-default)">' +
    '<table class="text-xs w-full">' +
    "<thead><tr>" + headHtml + "</tr></thead>" +
    "<tbody>" + rowsHtml + "</tbody>" +
    "</table></div>" +
    paginationHtml;
}

function buildPagination(data) {
  const { total, limit, offset, has_more } = data;
  const page = Math.floor(offset / limit) + 1;
  const totalPages = Math.ceil(total / limit);
  if (totalPages <= 1) return "";

  return (
    '<div class="flex items-center justify-between mt-3">' +
    `<span class="text-xs" style="color:var(--text-muted)">${total} files</span>` +
    '<div class="flex gap-2">' +
    `<button onclick="filesPagePrev()" class="text-xs px-2 py-1 rounded bg-overlay bg-overlay-hover${offset === 0 ? " opacity-30 pointer-events-none" : ""}" ${offset === 0 ? "disabled" : ""}>&#8592; Prev</button>` +
    `<span class="text-xs py-1" style="color:var(--text-secondary)">Page ${page} of ${totalPages}</span>` +
    `<button onclick="filesPageNext()" class="text-xs px-2 py-1 rounded bg-overlay bg-overlay-hover${!has_more ? " opacity-30 pointer-events-none" : ""}" ${!has_more ? "disabled" : ""}>Next &#8594;</button>` +
    "</div></div>"
  );
}

export function sortFiles(column) {
  state.filesSort = column;
  state.filesPage.offset = 0;
  loadFiles();
}

export function filesPagePrev() {
  state.filesPage.offset = Math.max(0, state.filesPage.offset - state.filesPage.limit);
  loadFiles();
}

export function filesPageNext() {
  if (state.filesData?.has_more) {
    state.filesPage.offset += state.filesPage.limit;
    loadFiles();
  }
}

// --- File Detail (in detail panel) ---

export async function openFileDetail(fileId) {
  state.selectedFile = fileId;
  state.fileDetailTab = "findings";

  const panel = document.getElementById("detailPanel");
  const content = document.getElementById("detailContent");

  content.innerHTML = '<div class="text-xs" style="color:var(--text-muted)">Loading...</div>';
  panel.classList.remove("translate-x-full");

  const data = await fetchFileDetail(fileId);
  if (!data) {
    content.innerHTML = '<div class="text-red-400">File not found.</div>';
    return;
  }

  state.fileDetailData = data;
  renderFileDetail(data);
}

function renderFileDetail(data) {
  const content = document.getElementById("detailContent");
  const f = data.file;
  const s = data.summary || {};
  const assocs = data.associations || [];

  // Header
  let html =
    '<div class="flex items-center gap-2 mb-4">' +
    '<button onclick="closeFileDetail()" class="text-xs bg-overlay px-2 py-1 rounded bg-overlay-hover" title="Close">&times;</button>' +
    `<span class="text-sm font-semibold truncate" style="color:var(--text-primary)" title="${escHtml(f.path)}">${escHtml(f.path)}</span>` +
    "</div>";

  // Metadata row
  html +=
    '<div class="flex flex-wrap gap-3 mb-4 text-xs" style="color:var(--text-muted)">' +
    (f.language ? `<span>Language: <b style="color:var(--text-primary)">${escHtml(f.language)}</b></span>` : "") +
    (f.file_type ? `<span>Type: <b style="color:var(--text-primary)">${escHtml(f.file_type)}</b></span>` : "") +
    (f.first_seen ? `<span>First seen: ${new Date(f.first_seen).toLocaleDateString()}</span>` : "") +
    (f.updated_at ? `<span>Updated: ${new Date(f.updated_at).toLocaleDateString()}</span>` : "") +
    "</div>";

  // Summary bar
  html +=
    '<div class="flex gap-2 mb-4">' +
    severityBadge("critical", s.critical) +
    severityBadge("high", s.high) +
    severityBadge("medium", s.medium) +
    severityBadge("low", s.low) +
    severityBadge("info", s.info) +
    "</div>";

  // Tab buttons
  const findingsActive = state.fileDetailTab === "findings";
  const tabActive = "px-3 py-1 rounded text-xs font-medium bg-accent text-primary";
  const tabInactive = "px-3 py-1 rounded text-xs font-medium bg-overlay text-secondary bg-overlay-hover";

  html +=
    '<div class="flex gap-1 mb-4">' +
    `<button onclick="switchFileTab('findings')" class="${findingsActive ? tabActive : tabInactive}">Findings</button>` +
    `<button onclick="switchFileTab('timeline')" class="${!findingsActive ? tabActive : tabInactive}">Timeline</button>` +
    "</div>";

  // Tab content placeholder
  html += '<div id="fileTabContent"></div>';

  // Associated issues (always visible below tabs)
  if (assocs.length) {
    html +=
      '<div class="mt-4 pt-4" style="border-top:1px solid var(--border-default)">' +
      '<div class="text-xs font-medium mb-2" style="color:var(--text-secondary)">Associated Issues</div>';
    for (const a of assocs) {
      const statusColor = a.issue_status === "closed" || a.issue_status === "done" ? "var(--status-done)" : "var(--status-wip)";
      html +=
        `<div class="flex items-center gap-2 py-1 cursor-pointer bg-overlay-hover rounded px-2" onclick="openDetail('${escHtml(a.issue_id)}')" role="button" tabindex="0">` +
        `<span class="w-2 h-2 rounded-full" style="background:${statusColor}"></span>` +
        `<span class="text-xs truncate" style="color:var(--text-primary)">${escHtml(a.issue_title || a.issue_id)}</span>` +
        `<span class="text-xs" style="color:var(--text-muted)">${escHtml(a.assoc_type)}</span>` +
        "</div>";
    }
    html += "</div>";
  }

  // Link to Issue button
  html +=
    '<div class="mt-4 pt-3" style="border-top:1px solid var(--border-default)">' +
    `<button onclick="showLinkIssueModal('${escHtml(f.id)}')" class="text-xs bg-overlay px-3 py-1 rounded bg-overlay-hover" style="color:var(--text-primary)">Link to Issue</button>` +
    "</div>";

  content.innerHTML = html;

  // Load initial tab content
  if (findingsActive) loadFindingsTab(f.id);
  else loadTimelineTab(f.id);
}

// --- Findings Tab ---

async function loadFindingsTab(fileId, offset) {
  const container = document.getElementById("fileTabContent");
  if (!container) return;
  container.innerHTML = '<div style="color:var(--text-muted)">Loading findings...</div>';

  const data = await fetchFileFindings(fileId, { limit: 20, offset: offset || 0, sort: "severity" });
  if (!data) {
    container.innerHTML = '<div class="text-red-400">Failed to load findings.</div>';
    return;
  }

  if (!data.results.length) {
    container.innerHTML = '<div style="color:var(--text-muted)">No findings for this file.</div>';
    return;
  }

  let html = "";
  for (const f of data.results) {
    const c = SEVERITY_COLORS[f.severity] || SEVERITY_COLORS.info;
    const lines = f.line_start ? (f.line_end && f.line_end !== f.line_start ? `L${f.line_start}-${f.line_end}` : `L${f.line_start}`) : "";
    html +=
      `<details class="rounded mb-1" style="background:var(--surface-overlay);border:1px solid var(--border-default)">` +
      `<summary class="flex items-center gap-2 px-3 py-2 cursor-pointer text-xs bg-overlay-hover">` +
      `<span class="px-1.5 py-0.5 rounded ${c.bg} ${c.text}" style="border:1px solid;${c.border}">${escHtml(f.severity)}</span>` +
      `<span style="color:var(--text-primary)" class="truncate flex-1">${escHtml(f.rule_id)}</span>` +
      (lines ? `<span style="color:var(--text-muted)">${lines}</span>` : "") +
      `<span style="color:var(--text-muted)">seen:${f.seen_count || 1}</span>` +
      "</summary>" +
      '<div class="px-3 py-2 text-xs" style="color:var(--text-secondary)">' +
      `<div class="mb-1">${escHtml(f.message)}</div>` +
      `<div style="color:var(--text-muted)">Source: ${escHtml(f.scan_source || "\u2014")} | Status: ${escHtml(f.status)}</div>` +
      (f.first_seen ? `<div style="color:var(--text-muted)">First seen: ${new Date(f.first_seen).toLocaleDateString()}</div>` : "") +
      "</div></details>";
  }

  // Pagination for findings
  if (data.has_more) {
    const nextOffset = (offset || 0) + 20;
    html += `<button onclick="loadMoreFindings('${escHtml(fileId)}', ${nextOffset})" class="text-xs mt-2 px-3 py-1 rounded bg-overlay bg-overlay-hover" style="color:var(--accent)">Load more...</button>`;
  }

  container.innerHTML = html;
}

export function loadMoreFindings(fileId, offset) {
  loadFindingsTab(fileId, offset);
}

// --- Timeline Tab ---

async function loadTimelineTab(fileId, offset) {
  const container = document.getElementById("fileTabContent");
  if (!container) return;
  container.innerHTML = '<div style="color:var(--text-muted)">Loading timeline...</div>';

  const data = await fetchFileTimeline(fileId, { limit: 20, offset: offset || 0 });
  if (!data) {
    container.innerHTML = '<div class="text-red-400">Failed to load timeline.</div>';
    return;
  }

  if (!data.results.length) {
    container.innerHTML = '<div style="color:var(--text-muted)">No events for this file yet.</div>';
    return;
  }

  // Filter pills
  let html =
    '<div class="flex gap-1 mb-3">' +
    '<button onclick="filterTimeline(\'all\')" class="text-xs px-2 py-1 rounded bg-accent text-primary" id="tlFilterAll">All</button>' +
    '<button onclick="filterTimeline(\'finding\')" class="text-xs px-2 py-1 rounded bg-overlay text-secondary bg-overlay-hover" id="tlFilterFinding">Findings</button>' +
    '<button onclick="filterTimeline(\'association\')" class="text-xs px-2 py-1 rounded bg-overlay text-secondary bg-overlay-hover" id="tlFilterAssoc">Associations</button>' +
    "</div>";

  html += '<div id="timelineEvents">';
  html += renderTimelineEvents(data.results);
  html += "</div>";

  if (data.has_more) {
    const nextOffset = (offset || 0) + 20;
    html += `<button onclick="loadMoreTimeline('${escHtml(fileId)}', ${nextOffset})" class="text-xs mt-2 px-3 py-1 rounded bg-overlay bg-overlay-hover" style="color:var(--accent)">Load more...</button>`;
  }

  container.innerHTML = html;
  // Store events for client-side filtering
  window._timelineEvents = data.results;
}

function renderTimelineEvents(events) {
  let html = "";
  for (const ev of events) {
    const dotColor = ev.type === "finding_created" ? "#EF4444"
      : ev.type === "finding_updated" ? "#3B82F6"
      : "#10B981";
    const time = ev.timestamp ? new Date(ev.timestamp).toLocaleString() : "";
    const evData = ev.data || {};

    let detail = "";
    if (ev.type === "finding_created") {
      const sev = evData.severity || "info";
      const c = SEVERITY_COLORS[sev] || SEVERITY_COLORS.info;
      detail = `<span class="px-1 py-0.5 rounded ${c.bg} ${c.text}" style="border:1px solid;${c.border}">${sev}</span> ${escHtml(evData.rule_id || "")} — ${escHtml(evData.message || "New finding")}`;
    } else if (ev.type === "finding_updated") {
      detail = `${escHtml(evData.rule_id || "Finding")} status: ${escHtml(evData.old_status || "?")} → ${escHtml(evData.new_status || evData.status || "?")}`;
    } else if (ev.type === "association_created") {
      detail = `Linked to issue ${escHtml(evData.issue_id || "")} (${escHtml(evData.assoc_type || "")})`;
    } else {
      detail = escHtml(ev.type);
    }

    html +=
      '<div class="flex gap-3 mb-2 timeline-event" data-type="' + ev.type + '">' +
      '<div class="flex flex-col items-center">' +
      `<div class="w-2.5 h-2.5 rounded-full mt-1 shrink-0" style="background:${dotColor}"></div>` +
      '<div class="w-px flex-1" style="background:var(--border-default)"></div>' +
      "</div>" +
      '<div class="flex-1 pb-3">' +
      `<div class="text-xs" style="color:var(--text-muted)">${time}</div>` +
      `<div class="text-xs mt-0.5" style="color:var(--text-primary)">${detail}</div>` +
      "</div></div>";
  }
  return html;
}

export function filterTimeline(type) {
  const events = window._timelineEvents || [];
  const filtered = type === "all" ? events : events.filter((e) => e.type.startsWith(type));
  const container = document.getElementById("timelineEvents");
  if (container) container.innerHTML = renderTimelineEvents(filtered);

  // Update active pill
  const pills = ["tlFilterAll", "tlFilterFinding", "tlFilterAssoc"];
  const activeMap = { all: "tlFilterAll", finding: "tlFilterFinding", association: "tlFilterAssoc" };
  for (const id of pills) {
    const el = document.getElementById(id);
    if (el) {
      el.className = id === activeMap[type]
        ? "text-xs px-2 py-1 rounded bg-accent text-primary"
        : "text-xs px-2 py-1 rounded bg-overlay text-secondary bg-overlay-hover";
    }
  }
}

export function loadMoreTimeline(fileId, offset) {
  loadTimelineTab(fileId, offset);
}

// --- Tab switching ---

export function switchFileTab(tab) {
  state.fileDetailTab = tab;
  if (state.fileDetailData) renderFileDetail(state.fileDetailData);
}

// --- Close file detail ---

export function closeFileDetail() {
  state.selectedFile = null;
  state.fileDetailData = null;
  const panel = document.getElementById("detailPanel");
  if (panel) panel.classList.add("translate-x-full");
}

// --- Link to Issue modal ---

export function showLinkIssueModal(fileId) {
  const modal = document.createElement("div");
  modal.id = "linkIssueModal";
  modal.className = "fixed inset-0 bg-black/50 flex items-center justify-center z-50";
  modal.onclick = (ev) => { if (ev.target === modal) modal.remove(); };
  modal.innerHTML =
    '<div class="rounded-lg p-4 w-80 shadow-xl" style="background:var(--surface-raised);border:1px solid var(--border-strong)">' +
    '<div class="text-sm mb-3" style="color:var(--text-primary)">Link File to Issue</div>' +
    '<div class="mb-2"><label class="text-xs block mb-1" style="color:var(--text-muted)">Issue ID</label>' +
    '<input id="linkIssueId" type="text" class="w-full bg-overlay text-primary text-xs rounded px-3 py-1 border border-strong focus:outline-none focus-accent" placeholder="e.g. filigree-abc123"></div>' +
    '<div class="mb-3"><label class="text-xs block mb-1" style="color:var(--text-muted)">Association Type</label>' +
    '<select id="linkAssocType" class="w-full bg-overlay text-primary text-xs rounded px-2 py-1 border border-strong">' +
    '<option value="scan_finding">scan_finding</option>' +
    '<option value="bug_in">bug_in</option>' +
    '<option value="task_for">task_for</option>' +
    '<option value="mentioned_in">mentioned_in</option>' +
    "</select></div>" +
    `<button onclick="submitLinkIssue('${escHtml(fileId)}')" class="text-xs bg-accent text-white px-3 py-1 rounded bg-accent-hover">Link</button>` +
    ' <button onclick="document.getElementById(\'linkIssueModal\').remove()" class="text-xs text-muted text-primary-hover">Cancel</button>' +
    "</div>";
  document.body.appendChild(modal);
  document.getElementById("linkIssueId").focus();
}

export async function submitLinkIssue(fileId) {
  const issueId = document.getElementById("linkIssueId").value.trim();
  const assocType = document.getElementById("linkAssocType").value;
  if (!issueId) return;

  const result = await postFileAssociation(fileId, { issue_id: issueId, assoc_type: assocType });
  const modal = document.getElementById("linkIssueModal");
  if (modal) modal.remove();

  if (result.ok) {
    showToast("Association created", "success");
    openFileDetail(fileId); // Refresh
  } else {
    showToast(result.error || "Failed to create association", "error");
  }
}
```

**Step 2: Verify no syntax errors**

Run: `node --check src/filigree/static/js/views/files.js`
Expected: No output (clean parse)

**Step 3: Commit**

```bash
git add src/filigree/static/js/views/files.js
git commit -m "feat(dashboard): add File List, Detail, and Timeline views"
```

---

### Task 6: Implement Code Health View (`views/health.js`)

**Files:**
- Create: `src/filigree/static/js/views/health.js`

**Step 1: Create the health view module**

```js
// ---------------------------------------------------------------------------
// Code Health view — hotspots, severity donut, scan coverage, recent scans.
// ---------------------------------------------------------------------------

import { fetchFiles, fetchHotspots } from "../api.js";
import { SEVERITY_COLORS, state } from "../state.js";
import { escHtml } from "../ui.js";

// --- Callbacks ---
export const callbacks = {};

// --- Main loader ---

export async function loadHealth() {
  const container = document.getElementById("healthContent");
  if (!container) return;
  container.innerHTML = '<div style="color:var(--text-muted)">Loading...</div>';

  // Fetch hotspots and file list in parallel
  const [hotspots, fileData] = await Promise.all([
    fetchHotspots(10),
    fetchFiles({ limit: 1, offset: 0 }),
  ]);

  if (!hotspots && !fileData) {
    container.innerHTML =
      '<div class="p-6 text-center" style="color:var(--text-muted)">' +
      '<div class="font-medium mb-2" style="color:var(--text-primary)">No file data yet</div>' +
      '<div>Ingest scan results to see code health metrics.</div></div>';
    return;
  }

  state.hotspots = hotspots;

  // Compute aggregate severity counts from hotspots
  const agg = { critical: 0, high: 0, medium: 0, low: 0, info: 0 };
  if (hotspots) {
    for (const h of hotspots) {
      const b = h.findings_breakdown || {};
      agg.critical += b.critical || 0;
      agg.high += b.high || 0;
      agg.medium += b.medium || 0;
      agg.low += b.low || 0;
      agg.info += b.info || 0;
    }
  }

  const totalFiles = fileData?.total || 0;
  const filesWithFindings = hotspots?.length || 0;

  // Build 2x2 grid
  container.innerHTML =
    '<div class="grid grid-cols-1 md:grid-cols-2 gap-4">' +
    renderHotspotsWidget(hotspots) +
    renderDonutWidget(agg) +
    renderCoverageWidget(filesWithFindings, totalFiles) +
    renderRecentScansWidget() +
    "</div>";
}

// --- Widget 1: Top 10 Hotspot Files ---

function renderHotspotsWidget(hotspots) {
  if (!hotspots || !hotspots.length) {
    return (
      '<div class="rounded p-4" style="background:var(--surface-raised);border:1px solid var(--border-default)">' +
      '<div class="text-xs font-medium mb-3" style="color:var(--text-secondary)">Top Hotspot Files</div>' +
      '<div style="color:var(--text-muted)" class="text-xs">No hotspots found.</div></div>'
    );
  }

  const maxScore = hotspots[0]?.score || 1;

  const rows = hotspots
    .map((h) => {
      const f = h.file || {};
      const b = h.findings_breakdown || {};
      const total = (b.critical || 0) + (b.high || 0) + (b.medium || 0) + (b.low || 0) + (b.info || 0);
      if (total === 0) return "";

      // Stacked bar segments
      const segments = ["critical", "high", "medium", "low", "info"]
        .filter((s) => b[s])
        .map((s) => {
          const pct = ((b[s] / total) * 100).toFixed(1);
          return `<div style="width:${pct}%;background:${SEVERITY_COLORS[s].hex}" class="h-full"></div>`;
        })
        .join("");

      const barWidth = ((h.score / maxScore) * 100).toFixed(1);

      return (
        `<div class="flex items-center gap-2 mb-2 cursor-pointer bg-overlay-hover rounded px-2 py-1" onclick="switchView('files');setTimeout(()=>openFileDetail('${escHtml(f.id)}'),100)" role="button" tabindex="0">` +
        `<span class="text-xs truncate w-48" style="color:var(--text-primary)" title="${escHtml(f.path)}">${escHtml(f.path)}</span>` +
        `<div class="flex-1 h-3 rounded overflow-hidden flex" style="background:var(--surface-base);max-width:${barWidth}%">` +
        segments +
        "</div>" +
        `<span class="text-xs w-8 text-right" style="color:var(--text-muted)">${h.score}</span>` +
        "</div>"
      );
    })
    .join("");

  return (
    '<div class="rounded p-4" style="background:var(--surface-raised);border:1px solid var(--border-default)">' +
    '<div class="text-xs font-medium mb-3" style="color:var(--text-secondary)">Top Hotspot Files</div>' +
    rows +
    "</div>"
  );
}

// --- Widget 2: Findings by Severity (CSS donut) ---

function renderDonutWidget(agg) {
  const total = agg.critical + agg.high + agg.medium + agg.low + agg.info;
  if (total === 0) {
    return (
      '<div class="rounded p-4" style="background:var(--surface-raised);border:1px solid var(--border-default)">' +
      '<div class="text-xs font-medium mb-3" style="color:var(--text-secondary)">Findings by Severity</div>' +
      '<div style="color:var(--text-muted)" class="text-xs">No findings to display.</div></div>'
    );
  }

  // Build conic-gradient segments
  const segments = [];
  let cumPct = 0;
  for (const sev of ["critical", "high", "medium", "low", "info"]) {
    if (agg[sev]) {
      const pct = (agg[sev] / total) * 100;
      segments.push(`${SEVERITY_COLORS[sev].hex} ${cumPct}% ${cumPct + pct}%`);
      cumPct += pct;
    }
  }
  const gradient = `conic-gradient(${segments.join(", ")})`;

  const legend = ["critical", "high", "medium", "low", "info"]
    .filter((s) => agg[s])
    .map(
      (s) =>
        `<span class="flex items-center gap-1"><span class="w-2 h-2 rounded-full" style="background:${SEVERITY_COLORS[s].hex}"></span>${s}: ${agg[s]}</span>`,
    )
    .join("");

  return (
    '<div class="rounded p-4" style="background:var(--surface-raised);border:1px solid var(--border-default)">' +
    '<div class="text-xs font-medium mb-3" style="color:var(--text-secondary)">Findings by Severity</div>' +
    '<div class="flex items-center gap-6">' +
    '<div class="relative" style="width:120px;height:120px">' +
    `<div style="width:100%;height:100%;border-radius:50%;background:${gradient}"></div>` +
    '<div class="absolute inset-0 flex items-center justify-center">' +
    '<div class="rounded-full flex items-center justify-center" style="width:64px;height:64px;background:var(--surface-raised)">' +
    `<span class="text-lg font-bold" style="color:var(--text-primary)">${total}</span>` +
    "</div></div></div>" +
    `<div class="flex flex-col gap-1 text-xs" style="color:var(--text-secondary)">${legend}</div>` +
    "</div></div>"
  );
}

// --- Widget 3: Scan Coverage ---

function renderCoverageWidget(withFindings, total) {
  const pct = total > 0 ? ((withFindings / total) * 100).toFixed(0) : 0;

  return (
    '<div class="rounded p-4" style="background:var(--surface-raised);border:1px solid var(--border-default)">' +
    '<div class="text-xs font-medium mb-3" style="color:var(--text-secondary)">Scan Coverage</div>' +
    `<div class="text-2xl font-bold mb-2" style="color:var(--accent)">${pct}%</div>` +
    '<div class="h-3 rounded overflow-hidden mb-2" style="background:var(--surface-base)">' +
    `<div class="h-full rounded" style="width:${pct}%;background:var(--accent)"></div>` +
    "</div>" +
    `<div class="text-xs" style="color:var(--text-muted)">${withFindings} files with findings out of ${total} tracked</div>` +
    "</div>"
  );
}

// --- Widget 4: Recent Scan Activity ---

function renderRecentScansWidget() {
  // Lightweight: derive from hotspots data timestamps
  // Since there's no scan_runs table, show a placeholder with guidance
  return (
    '<div class="rounded p-4" style="background:var(--surface-raised);border:1px solid var(--border-default)">' +
    '<div class="text-xs font-medium mb-3" style="color:var(--text-secondary)">Recent Scan Activity</div>' +
    '<div class="text-xs" style="color:var(--text-muted)">' +
    '<div class="mb-2">Scan ingestion stats appear here after posting results.</div>' +
    '<div class="mb-1">Ingest scans via:</div>' +
    '<code class="block rounded px-2 py-1 text-xs" style="background:var(--surface-base);color:var(--text-secondary)">POST /api/v1/scan-results</code>' +
    '<div class="mt-2">Returns: files_created, files_updated, findings_created, findings_updated</div>' +
    "</div></div>"
  );
}
```

**Step 2: Verify no syntax errors**

Run: `node --check src/filigree/static/js/views/health.js`
Expected: No output (clean parse)

**Step 3: Commit**

```bash
git add src/filigree/static/js/views/health.js
git commit -m "feat(dashboard): add Code Health view with hotspots, donut, coverage"
```

---

### Task 7: Wire everything together in app.js

**Files:**
- Modify: `src/filigree/static/js/app.js`

**Step 1: Add imports**

After the workflow import (line 105), add:

```js
import {
  closeFileDetail,
  callbacks as filesCallbacks,
  filesPageNext,
  filesPagePrev,
  filterTimeline,
  loadFiles,
  loadMoreFindings,
  loadMoreTimeline,
  openFileDetail,
  showLinkIssueModal,
  sortFiles,
  submitLinkIssue,
  switchFileTab,
} from "./views/files.js";
import { loadHealth } from "./views/health.js";
```

**Step 2: Wire up callbacks**

After the `detailCallbacks` wiring (line 239), add:

```js
// files.js callbacks
filesCallbacks.openDetail = openDetail;
filesCallbacks.fetchData = fetchData;
```

**Step 3: Register views**

After the workflow registration (line 249), add:

```js
registerView("files", loadFiles);
registerView("health", loadHealth);
```

**Step 4: Expose functions on window**

After the workflow window assignments (line 539), add:

```js
// Files
window.loadFiles = loadFiles;
window.openFileDetail = openFileDetail;
window.closeFileDetail = closeFileDetail;
window.sortFiles = sortFiles;
window.filesPagePrev = filesPagePrev;
window.filesPageNext = filesPageNext;
window.switchFileTab = switchFileTab;
window.loadMoreFindings = loadMoreFindings;
window.loadMoreTimeline = loadMoreTimeline;
window.filterTimeline = filterTimeline;
window.showLinkIssueModal = showLinkIssueModal;
window.submitLinkIssue = submitLinkIssue;

// Health
window.loadHealth = loadHealth;
```

**Step 5: Verify no syntax errors**

Run: `node --check src/filigree/static/js/app.js`
Expected: No output (clean parse)

**Step 6: Commit**

```bash
git add src/filigree/static/js/app.js
git commit -m "feat(dashboard): wire Files and Health views into app entry point"
```

---

### Task 8: Manual smoke test

**Step 1: Start the dashboard**

Run: `cd /home/user/filigree && uv run filigree dashboard --port 8377`

**Step 2: Verify in browser**

- Navigate to `http://localhost:8377`
- Confirm "Files" and "Health" buttons appear in the nav bar
- Click "Files" — should show the file list table (or empty state if no files)
- Click "Health" — should show the 2x2 widget grid (or empty state)
- If there are files, click a file row — detail panel should slide in
- Test tab switching between Findings and Timeline in detail
- Test the "Link to Issue" modal opens
- Verify all existing views (Kanban, Graph, etc.) still work

**Step 3: Ingest test data if needed**

If no files exist, create test data:

```bash
curl -X POST http://localhost:8377/api/v1/scan-results \
  -H "Content-Type: application/json" \
  -d '{
    "scan_source": "test",
    "findings": [
      {"path": "src/main.py", "rule_id": "E001", "severity": "critical", "message": "Test critical finding"},
      {"path": "src/main.py", "rule_id": "E002", "severity": "high", "message": "Test high finding"},
      {"path": "src/utils.py", "rule_id": "W001", "severity": "medium", "message": "Test medium finding"}
    ]
  }'
```

Then refresh the Files and Health tabs to see populated data.

---

### Task 9: Run CI checks and final commit

**Step 1: Run the full pre-push CI pipeline**

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/filigree/
uv run pytest --tb=short
```

**Step 2: Fix any issues found**

Address lint, type, or test failures.

**Step 3: Update filigree issues**

Close the completed step issues:

```bash
filigree update filigree-ca796b --status=in_progress
filigree close filigree-ca796b --reason="File List View implemented"
filigree update filigree-7e1b95 --status=in_progress
filigree close filigree-7e1b95 --reason="File Detail View implemented"
filigree update filigree-cbc0cd --status=in_progress
filigree close filigree-cbc0cd --reason="File Timeline View implemented"
filigree update filigree-078908 --status=in_progress
filigree close filigree-078908 --reason="Hotspot Dashboard implemented"
filigree update filigree-64066c --status=in_progress
filigree close filigree-64066c --reason="Scan Ingestion Feedback UI implemented"
```
