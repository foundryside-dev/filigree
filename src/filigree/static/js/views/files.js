// ---------------------------------------------------------------------------
// Files view — file list table, file detail panel, file timeline.
// ---------------------------------------------------------------------------

import {
  fetchFileDetail,
  fetchFileFindings,
  fetchFiles,
  fetchFileTimeline,
  patchFileFinding,
  postFileAssociation,
} from "../api.js";
import { updateHash } from "../router.js";
import { SEVERITY_COLORS, state } from "../state.js";
import { escHtml, escJsSingle, showCreateForm, showToast } from "../ui.js";
import { renderHealthOverview } from "./health.js";

function formatLineRange(f, prefix = "L") {
  if (!f.line_start) return "";
  if (f.line_end && f.line_end !== f.line_start)
    return `${prefix}${f.line_start}\u2013${f.line_end}`;
  return `${prefix}${f.line_start}`;
}

// --- Accumulated page state for "load more" ---
let _findingsAccum = [];
let _timelineAccum = [];

// --- Findings filter + selection state (module-local, resets on file change) ---
let _findingsFilters = { severity: null, status: null };
let _selectedFinding = null;

// --- Sort direction state ---
let _filesSortDir = "DESC";

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

// Inline scan source filter (replaces filterFilesByScanSource from health.js)
function filterFilesByScanSourceInline(source) {
  state.filesScanSource = source || "";
  state.filesPage.offset = 0;
  loadFiles();
}
window.filterFilesByScanSourceInline = filterFilesByScanSourceInline;

export async function loadFiles() {
  const container = document.getElementById("filesContent");
  if (!container) return;

  // --- Code Quality Overview (collapsible) ---
  let overview = document.getElementById("filesOverview");
  if (!overview) {
    const projectKey = state.currentProjectKey || "__default__";
    const storageKey = `filigree_files_overview_collapsed.${projectKey}`;
    const collapsed = localStorage.getItem(storageKey) === "1";

    overview = document.createElement("details");
    overview.id = "filesOverview";
    overview.className = "mb-4 rounded";
    overview.style.cssText =
      "background:var(--surface-raised);border:1px solid var(--border-default)";
    if (!collapsed) overview.open = true;
    overview.innerHTML =
      '<summary class="cursor-pointer select-none text-xs font-medium px-4 py-3" style="color:var(--text-secondary)">' +
      "Code Quality Overview</summary>" +
      '<div id="filesOverviewContent" class="px-4 pb-3"></div>';

    const overviewCallbacks = {
      onClickFile: (fileId) => `openFileDetail('${escJsSingle(fileId)}')`,
      onClickScan: (source) => `filterFilesByScanSourceInline('${escJsSingle(source)}')`,
    };

    // Guard against concurrent loads but allow re-render on subsequent loadFiles()
    const loadOverview = (content) => {
      if (content.dataset.loading === "1") return;
      content.dataset.loading = "1";
      renderHealthOverview(content, overviewCallbacks).then(() => {
        content.dataset.loading = "";
      });
    };

    overview.addEventListener("toggle", () => {
      localStorage.setItem(storageKey, overview.open ? "0" : "1");
      if (overview.open) {
        const content = document.getElementById("filesOverviewContent");
        if (content) loadOverview(content);
      }
    });

    container.parentNode.insertBefore(overview, container);

    // Load overview data if expanded
    if (!collapsed) {
      const content = document.getElementById("filesOverviewContent");
      if (content) loadOverview(content);
    }
  }

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

  try {
    const params = {
      limit: state.filesPage.limit,
      offset: state.filesPage.offset,
      sort: state.filesSort,
      direction: _filesSortDir,
    };
    if (state.filesSearch) params.path_prefix = state.filesSearch;
    if (state.filesCriticalOnly) params.has_severity = "critical";
    if (state.filesScanSource) params.scan_source = state.filesScanSource;

    const data = await fetchFiles(params);
    if (!data) {
      container.innerHTML = '<div class="text-red-400">Failed to load files.</div>';
      return;
    }

    state.filesData = data;

    // Active scan_source filter chip
    const scanChip = state.filesScanSource
      ? '<div class="flex items-center gap-2 mb-3 px-3 py-1.5 rounded text-xs" style="background:var(--surface-overlay);border:1px solid var(--border-default)">' +
        '<span style="color:var(--text-secondary)">Filtered by source:</span>' +
        '<span class="font-medium" style="color:var(--text-primary)">' +
        escHtml(state.filesScanSource) +
        "</span>" +
        '<button onclick="clearScanSourceFilter()" class="ml-1 rounded-full px-1.5" style="color:var(--text-muted)" title="Clear filter">&times;</button>' +
        "</div>"
      : "";

    if (!data.results.length) {
      container.innerHTML =
        scanChip +
        '<div class="p-6 text-center" style="color:var(--text-muted)">' +
        '<div class="font-medium mb-2" style="color:var(--text-primary)">No files found</div>' +
        "<div>No files match the current filters.</div></div>";
      return;
    }

    const sortArrow = (col) =>
      state.filesSort === col
        ? ` <span style="color:var(--accent)">${_filesSortDir === "ASC" ? "&#9650;" : "&#9660;"}</span>`
        : "";

    const headerCols = [
      { key: "path", label: "Path", cls: "text-left" },
      { key: "language", label: "Lang", cls: "text-left" },
      { key: null, label: "Critical", cls: "text-center" },
      { key: null, label: "High", cls: "text-center" },
      { key: null, label: "Medium", cls: "text-center" },
      { key: null, label: "Low", cls: "text-center" },
      { key: null, label: "Closed", cls: "text-center" },
      { key: null, label: "Issues", cls: "text-center" },
      { key: null, label: "Obs", cls: "text-center" },
      { key: "updated_at", label: "Last Update", cls: "text-right" },
    ];

    const headHtml = headerCols
      .map((h) => {
        const sortAttr = h.key
          ? ` cursor-pointer" onclick="sortFiles('${h.key}')" role="button" tabindex="0"`
          : '"';
        return `<th class="${h.cls} py-2 px-3 font-medium${sortAttr} style="color:var(--text-muted)">${h.label}${h.key ? sortArrow(h.key) : ""}</th>`;
      })
      .join("");

    const rowsHtml = data.results
      .map((f) => {
        const s = f.summary || {};
        const border = healthBorderClass(s);
        const assocCount = f.associations_count || 0;
        const safeFileId = escJsSingle(f.id);
        const updated = f.updated_at ? new Date(f.updated_at).toLocaleDateString() : "\u2014";
        const closedCount = (s.total_findings || 0) - (s.open_findings || 0);
        return (
          `<tr class="bg-overlay-hover cursor-pointer ${border}" onclick="openFileDetail('${safeFileId}')" role="button" tabindex="0">` +
          `<td class="py-2 px-3 text-accent truncate max-w-xs" title="${escHtml(f.path)}">${escHtml(f.path)}</td>` +
          `<td class="py-2 px-3">${escHtml(f.language || "\u2014")}</td>` +
          `<td class="py-2 px-3 text-center">${severityBadge("critical", s.critical)}</td>` +
          `<td class="py-2 px-3 text-center">${severityBadge("high", s.high)}</td>` +
          `<td class="py-2 px-3 text-center">${severityBadge("medium", s.medium)}</td>` +
          `<td class="py-2 px-3 text-center">${severityBadge("low", s.low)}</td>` +
          `<td class="py-2 px-3 text-center">${closedCount ? `<span class="text-xs" style="color:var(--status-done)">${closedCount}</span>` : "\u2014"}</td>` +
          `<td class="py-2 px-3 text-center">${assocCount || "\u2014"}</td>` +
          `<td class="py-2 px-3 text-center">${f.observation_count ? `<span class="text-xs px-1.5 py-0.5 rounded" style="color:var(--text-secondary);border:1px dashed var(--border-strong)">${f.observation_count}</span>` : "\u2014"}</td>` +
          `<td class="py-2 px-3 text-right" style="color:var(--text-muted)">${updated}</td>` +
          "</tr>"
        );
      })
      .join("");

    const paginationHtml = buildPagination(data);

    container.innerHTML =
      scanChip +
      '<div class="rounded overflow-hidden" style="background:var(--surface-raised);border:1px solid var(--border-default)">' +
      '<table class="text-xs w-full">' +
      "<thead><tr>" +
      headHtml +
      "</tr></thead>" +
      "<tbody>" +
      rowsHtml +
      "</tbody>" +
      "</table></div>" +
      paginationHtml;
  } catch (err) {
    console.warn("[files:list] Failed to load file list:", err);
    container.innerHTML = '<div class="text-red-400">Failed to load files.</div>';
  }
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
  if (state.filesSort === column) {
    _filesSortDir = _filesSortDir === "DESC" ? "ASC" : "DESC";
  } else {
    _filesSortDir = column === "path" ? "ASC" : "DESC";
  }
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
  state.selectedIssue = null;
  state.selectedFile = fileId;
  updateHash();
  state.fileDetailTab = "findings";
  _findingsFilters = { severity: null, status: null };

  const panel = document.getElementById("detailPanel");
  const content = document.getElementById("detailContent");

  content.innerHTML = '<div class="text-xs" style="color:var(--text-muted)">Loading...</div>';
  panel.classList.remove("translate-x-full");

  try {
    const data = await fetchFileDetail(fileId);
    if (!data) {
      content.innerHTML = '<div class="text-red-400">File not found.</div>';
      return;
    }

    state.fileDetailData = data;
    renderFileDetail(data);
  } catch (err) {
    console.warn("[files:detail] Failed to load file detail:", err);
    content.innerHTML = '<div class="text-red-400">Failed to load file details.</div>';
  }
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
    (f.language
      ? `<span>Language: <b style="color:var(--text-primary)">${escHtml(f.language)}</b></span>`
      : "") +
    (f.file_type
      ? `<span>Type: <b style="color:var(--text-primary)">${escHtml(f.file_type)}</b></span>`
      : "") +
    (f.first_seen
      ? `<span>First seen: ${new Date(f.first_seen).toLocaleDateString()}</span>`
      : "") +
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

  // Observations badge (shown only if observations exist)
  if (data.observation_count > 0) {
    html +=
      '<div class="flex items-center gap-2 mb-4 px-3 py-2 rounded" style="background:var(--surface-overlay);border:1px dashed var(--border-strong)">' +
      `<span class="text-xs" style="color:var(--text-secondary)">${data.observation_count} pending observation(s)</span>` +
      '<span class="text-xs" style="color:var(--text-muted)">\u2014 use <code>list_observations</code> to triage</span>' +
      "</div>";
  }

  // Tab buttons
  const findingsActive = state.fileDetailTab === "findings";
  const tabActive = "px-3 py-1 rounded text-xs font-medium bg-accent text-primary";
  const tabInactive =
    "px-3 py-1 rounded text-xs font-medium bg-overlay text-secondary bg-overlay-hover";

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
      const statusColor =
        a.issue_status === "closed" || a.issue_status === "done"
          ? "var(--status-done)"
          : "var(--status-wip)";
      const safeIssueId = escJsSingle(a.issue_id);
      html +=
        `<div class="flex items-center gap-2 py-1 cursor-pointer bg-overlay-hover rounded px-2" onclick="openDetail('${safeIssueId}')" role="button" tabindex="0">` +
        `<span class="w-2 h-2 rounded-full" style="background:${statusColor}"></span>` +
        `<span class="text-xs truncate" style="color:var(--text-primary)">${escHtml(a.issue_title || a.issue_id)}</span>` +
        `<span class="text-xs" style="color:var(--text-muted)">${escHtml(a.assoc_type)}</span>` +
        "</div>";
    }
    html += "</div>";
  }

  // Link to Issue button
  const safeFileId = escJsSingle(f.id);
  html +=
    '<div class="mt-4 pt-3" style="border-top:1px solid var(--border-default)">' +
    `<button onclick="showLinkIssueModal('${safeFileId}')" class="text-xs bg-overlay px-3 py-1 rounded bg-overlay-hover" style="color:var(--text-primary)">Link to Issue</button>` +
    "</div>";

  content.innerHTML = html;

  // Load initial tab content
  if (findingsActive) loadFindingsTab(f.id);
  else loadTimelineTab(f.id);
}

// --- Findings Tab (split-pane: left list + right detail) ---

function renderFindingListItem(f) {
  const c = SEVERITY_COLORS[f.severity] || SEVERITY_COLORS.info;
  const lines = formatLineRange(f);
  const selected = _selectedFinding && _selectedFinding.id === f.id;
  const selClass = selected ? "border-l-2 border-l-sky-400" : "";
  const safeFindingId = escJsSingle(f.id);
  return (
    `<div class="flex items-center gap-2 px-3 py-2 cursor-pointer text-xs bg-overlay-hover rounded mb-1 ${selClass}" style="background:var(--surface-overlay);border:1px solid var(--border-default)" onclick="selectFinding('${safeFindingId}')" role="button" tabindex="0">` +
    `<span class="px-1.5 py-0.5 rounded shrink-0 ${c.bg} ${c.text}" style="border:1px solid;${c.border}">${escHtml(f.severity)}</span>` +
    `<span style="color:var(--text-primary)" class="truncate flex-1">${escHtml(f.rule_id)}</span>` +
    (lines ? `<span class="shrink-0" style="color:var(--text-muted)">${lines}</span>` : "") +
    "</div>"
  );
}

function renderFindingDetail(f) {
  if (!f) {
    return '<div class="flex items-center justify-center h-full text-xs" style="color:var(--text-muted)">Select a finding to view details</div>';
  }
  const c = SEVERITY_COLORS[f.severity] || SEVERITY_COLORS.info;
  const lines = formatLineRange(f, "Line ");
  return (
    '<div class="text-xs space-y-2">' +
    `<div class="flex items-center gap-2"><span class="px-1.5 py-0.5 rounded ${c.bg} ${c.text}" style="border:1px solid;${c.border}">${escHtml(f.severity)}</span>` +
    `<span class="font-medium" style="color:var(--text-primary)">${escHtml(f.rule_id)}</span></div>` +
    `<div style="color:var(--text-secondary)">${escHtml(f.message)}</div>` +
    '<div class="flex flex-wrap gap-3" style="color:var(--text-muted)">' +
    `<span>Source: ${escHtml(f.scan_source || "\u2014")}</span>` +
    `<span>Status: ${escHtml(f.status)}</span>` +
    (lines ? `<span>${lines}</span>` : "") +
    `<span>Seen: ${f.seen_count || 1}×</span>` +
    "</div>" +
    (f.first_seen
      ? `<div style="color:var(--text-muted)">First seen: ${new Date(f.first_seen).toLocaleDateString()}</div>`
      : "") +
    (f.suggestion
      ? '<div class="mt-2 rounded p-2" style="background:var(--surface-base);border:1px solid var(--border-default)"><div class="font-medium mb-1" style="color:var(--text-secondary)">Suggestion</div><div style="color:var(--text-primary);white-space:pre-wrap">' +
        escHtml(f.suggestion) +
        "</div></div>"
      : "") +
    '<div class="pt-2" style="border-top:1px solid var(--border-default)">' +
    '<div class="flex gap-2">' +
    `<button onclick="createIssueFromFinding()" class="text-xs px-3 py-1 rounded bg-accent-hover" style="background:var(--accent);color:var(--surface-base)">Create Ticket</button>` +
    `<button onclick="closeFinding()" class="text-xs px-3 py-1 rounded bg-overlay bg-overlay-hover" style="color:var(--text-primary);border:1px solid var(--border-strong)">Close Finding</button>` +
    "</div>" +
    "</div></div>"
  );
}

function renderFindingsFilterBar() {
  const sevs = ["all", "critical", "high", "medium", "low"];
  const sevPills = sevs
    .map((s) => {
      const active = s === "all" ? !_findingsFilters.severity : _findingsFilters.severity === s;
      const cls = active
        ? "text-xs px-2 py-1 rounded bg-accent text-primary"
        : "text-xs px-2 py-1 rounded bg-overlay text-secondary bg-overlay-hover";
      return `<button onclick="filterFindings('severity','${s}')" class="${cls}">${s === "all" ? "All" : s.charAt(0).toUpperCase() + s.slice(1)}</button>`;
    })
    .join("");
  const statusOpts = ["all", "open", "acknowledged", "fixed", "false_positive", "unseen_in_latest"]
    .map((s) => {
      const selected = s === "all" ? !_findingsFilters.status : _findingsFilters.status === s;
      return `<option value="${s}"${selected ? " selected" : ""}>${s === "all" ? "All statuses" : s}</option>`;
    })
    .join("");
  return (
    '<div class="flex items-center gap-2 mb-2 flex-wrap">' +
    sevPills +
    `<select onchange="filterFindings('status',this.value)" class="text-xs rounded px-2 py-1" style="background:var(--surface-overlay);color:var(--text-primary);border:1px solid var(--border-default)">${statusOpts}</select>` +
    "</div>"
  );
}

async function loadFindingsTab(fileId, offset) {
  const container = document.getElementById("fileTabContent");
  if (!container) return;

  const isFirstPage = !offset;
  if (isFirstPage) {
    _findingsAccum = [];
    _selectedFinding = null;
    container.innerHTML = '<div style="color:var(--text-muted)">Loading findings...</div>';
  }

  try {
    const params = { limit: 20, offset: offset || 0, sort: "severity" };
    if (_findingsFilters.severity) params.severity = _findingsFilters.severity;
    if (_findingsFilters.status) params.status = _findingsFilters.status;

    const data = await fetchFileFindings(fileId, params);
    if (!data) {
      container.innerHTML = '<div class="text-red-400">Failed to load findings.</div>';
      return;
    }

    _findingsAccum = _findingsAccum.concat(data.results);

    if (!_findingsAccum.length) {
      container.innerHTML =
        renderFindingsFilterBar() +
        '<div style="color:var(--text-muted)">No findings match the current filters.</div>';
      return;
    }

    let listHtml = _findingsAccum.map(renderFindingListItem).join("");
    if (data.has_more) {
      const nextOffset = (offset || 0) + 20;
      listHtml += `<button onclick="loadMoreFindings('${escJsSingle(fileId)}', ${nextOffset})" class="text-xs mt-2 px-3 py-1 rounded bg-overlay bg-overlay-hover w-full text-center" style="color:var(--accent)">Load more...</button>`;
    }

    container.innerHTML =
      renderFindingsFilterBar() +
      '<div class="flex gap-3" style="min-height:180px">' +
      `<div class="w-1/2 overflow-y-auto pr-1" style="max-height:400px">${listHtml}</div>` +
      `<div id="findingDetailPane" class="flex-1 rounded p-3" style="background:var(--surface-overlay);border:1px solid var(--border-default)">${renderFindingDetail(_selectedFinding)}</div>` +
      "</div>";
  } catch (err) {
    console.warn("[files:findings] Failed to load findings:", err);
    container.innerHTML = '<div class="text-red-400">Failed to load findings.</div>';
  }
}

export function loadMoreFindings(fileId, offset) {
  loadFindingsTab(fileId, offset);
}

export function selectFinding(findingId) {
  _selectedFinding = _findingsAccum.find((f) => f.id === findingId) || null;
  // Re-render the detail pane only
  const pane = document.getElementById("findingDetailPane");
  if (pane) pane.innerHTML = renderFindingDetail(_selectedFinding);
  // Update left-panel selection highlight
  const container = document.getElementById("fileTabContent");
  if (container) {
    container.querySelectorAll('[onclick^="selectFinding"]').forEach((el) => {
      const elId = el.getAttribute("onclick").match(/selectFinding\('([^']+)'\)/)?.[1];
      el.classList.toggle("border-l-2", elId === findingId);
      el.classList.toggle("border-l-sky-400", elId === findingId);
    });
  }
}

export function filterFindings(type, value) {
  if (type === "severity") {
    _findingsFilters.severity = value === "all" ? null : value;
  } else if (type === "status") {
    _findingsFilters.status = value === "all" ? null : value;
  }
  if (state.selectedFile) loadFindingsTab(state.selectedFile, 0);
}

const SEVERITY_PRIORITY_MAP = { critical: 0, high: 1, medium: 2, low: 3, info: 4 };

export async function createIssueFromFinding() {
  if (!_selectedFinding) return;
  const f = _selectedFinding;
  // Open the Create Issue modal, then pre-fill fields
  await showCreateForm();
  const modalEl = document.getElementById("createModal");
  if (modalEl) {
    modalEl.dataset.findingFileId = String(
      state.fileDetailData?.file?.id || state.selectedFile || "",
    );
    modalEl.dataset.findingId = String(f.id);
  }
  const titleEl = document.getElementById("createTitle");
  const descEl = document.getElementById("createDesc");
  const typeEl = document.getElementById("createType");
  const prioEl = document.getElementById("createPriority");
  if (titleEl) titleEl.value = `[${f.severity}] ${f.rule_id}`;
  if (descEl) {
    const lines = formatLineRange(f, "Line ");
    const filePath = state.fileDetailData?.file?.path || "";
    descEl.value = [
      f.message,
      "",
      `File: ${filePath}`,
      lines ? `Location: ${lines}` : "",
      `Source: ${f.scan_source || "unknown"}`,
      `Status: ${f.status}`,
      `Seen: ${f.seen_count || 1} time(s)`,
    ]
      .filter(Boolean)
      .join("\n");
  }
  if (typeEl) typeEl.value = "bug";
  if (prioEl) prioEl.value = String(SEVERITY_PRIORITY_MAP[f.severity] ?? 2);
}

export async function closeFinding() {
  if (!_selectedFinding || !state.selectedFile) return;
  const result = await patchFileFinding(state.selectedFile, _selectedFinding.id, {
    status: "fixed",
  });
  if (!result.ok) {
    showToast(result.error || "Failed to close finding", "error");
    return;
  }
  showToast("Finding closed", "success");
  await loadFindingsTab(state.selectedFile, 0);
}

// --- Timeline Tab ---

async function loadTimelineTab(fileId, offset) {
  const container = document.getElementById("fileTabContent");
  if (!container) return;

  const isFirstPage = !offset;
  if (isFirstPage) {
    _timelineAccum = [];
    container.innerHTML = '<div style="color:var(--text-muted)">Loading timeline...</div>';
  }

  try {
    const params = { limit: 20, offset: offset || 0 };
    if (state.timelineFilter) params.event_type = state.timelineFilter;
    const data = await fetchFileTimeline(fileId, params);
    if (!data) {
      container.innerHTML = '<div class="text-red-400">Failed to load timeline.</div>';
      return;
    }

    _timelineAccum = _timelineAccum.concat(data.results);

    // Filter pills: derive active state from stored filter so refreshes stay in sync.
    const activeFilter = state.timelineFilter || "all";
    const pillClass = (type) =>
      type === activeFilter
        ? "text-xs px-2 py-1 rounded bg-accent text-primary"
        : "text-xs px-2 py-1 rounded bg-overlay text-secondary bg-overlay-hover";

    let html =
      '<div class="flex gap-1 mb-3">' +
      `<button onclick="filterTimeline('all')" class="${pillClass("all")}" id="tlFilterAll">All</button>` +
      `<button onclick="filterTimeline('finding')" class="${pillClass("finding")}" id="tlFilterFinding">Findings</button>` +
      `<button onclick="filterTimeline('association')" class="${pillClass("association")}" id="tlFilterAssoc">Associations</button>` +
      `<button onclick="filterTimeline('file_metadata_update')" class="${pillClass("file_metadata_update")}" id="tlFilterMeta">Metadata</button>` +
      "</div>";

    if (!_timelineAccum.length) {
      container.innerHTML =
        html + '<div style="color:var(--text-muted)">No events for this filter yet.</div>';
      return;
    }

    html += '<div id="timelineEvents">';
    html += renderTimelineEvents(_timelineAccum);
    html += "</div>";

    if (data.has_more) {
      const nextOffset = (offset || 0) + 20;
      html += `<button onclick="loadMoreTimeline('${escJsSingle(fileId)}', ${nextOffset})" class="text-xs mt-2 px-3 py-1 rounded bg-overlay bg-overlay-hover" style="color:var(--accent)">Load more...</button>`;
    }

    container.innerHTML = html;
  } catch (err) {
    console.warn("[files:timeline] Failed to load timeline:", err);
    container.innerHTML = '<div class="text-red-400">Failed to load timeline.</div>';
  }
}

const EVENT_TYPE_LABELS = {
  finding_created: "Finding Created",
  finding_updated: "Finding Updated",
  association_created: "Association",
  file_metadata_update: "Metadata",
};

function renderTimelineEvents(events) {
  let html = "";
  for (const ev of events) {
    const DOT_COLORS = {
      finding_created: "#EF4444",
      finding_updated: "#3B82F6",
      file_metadata_update: "#A855F7",
    };
    const dotColor = DOT_COLORS[ev.type] || "#10B981";
    const time = ev.timestamp ? new Date(ev.timestamp).toLocaleString() : "";
    const evData = ev.data || {};
    const label = EVENT_TYPE_LABELS[ev.type] || ev.type;

    let detail = "";
    if (ev.type === "finding_created") {
      const sev = evData.severity || "info";
      const c = SEVERITY_COLORS[sev] || SEVERITY_COLORS.info;
      detail = `<span class="px-1 py-0.5 rounded ${c.bg} ${c.text}" style="border:1px solid;${c.border}">${escHtml(sev)}</span> ${escHtml(evData.rule_id || "")} \u2014 ${escHtml(evData.message || "New finding")}`;
    } else if (ev.type === "finding_updated") {
      const ruleLabel = escHtml(evData.rule_id || "Finding");
      if (evData.old_status) {
        detail = `${ruleLabel} status: ${escHtml(evData.old_status)} \u2192 ${escHtml(evData.new_status || evData.status || "?")}`;
      } else {
        detail = `${ruleLabel} \u2014 Status: ${escHtml(evData.new_status || evData.status || "?")}`;
      }
    } else if (ev.type === "association_created") {
      detail = `Linked to issue ${escHtml(evData.issue_id || "")} (${escHtml(evData.assoc_type || "")})`;
    } else if (ev.type === "file_metadata_update") {
      detail = `${escHtml(evData.field || "metadata")} changed: ${escHtml(evData.old_value || "?")} \u2192 ${escHtml(evData.new_value || "?")}`;
    } else {
      detail = escHtml(ev.type);
    }

    html +=
      '<div class="flex gap-3 mb-2 timeline-event" data-type="' +
      escHtml(ev.type) +
      '">' +
      '<div class="flex flex-col items-center">' +
      `<div class="w-2.5 h-2.5 rounded-full mt-1 shrink-0" style="background:${dotColor}"></div>` +
      '<div class="w-px flex-1" style="background:var(--border-default)"></div>' +
      "</div>" +
      '<div class="flex-1 pb-3">' +
      '<details class="group">' +
      `<summary class="text-xs cursor-pointer bg-overlay-hover rounded px-1 -mx-1 flex items-center gap-2" style="list-style:none">` +
      `<span style="color:var(--text-muted)">${time}</span>` +
      `<span class="font-medium" style="color:var(--text-primary)">${escHtml(label)}</span>` +
      `<span class="text-xs" style="color:var(--text-muted)">&#9662;</span>` +
      "</summary>" +
      `<div class="text-xs mt-1 pl-1" style="color:var(--text-primary)">${detail}</div>` +
      "</details>" +
      "</div></div>";
  }
  return html;
}

export function filterTimeline(type) {
  // Update active pill immediately for responsiveness
  const pills = ["tlFilterAll", "tlFilterFinding", "tlFilterAssoc", "tlFilterMeta"];
  const activeMap = {
    all: "tlFilterAll",
    finding: "tlFilterFinding",
    association: "tlFilterAssoc",
    file_metadata_update: "tlFilterMeta",
  };
  for (const id of pills) {
    const el = document.getElementById(id);
    if (el) {
      el.className =
        id === activeMap[type]
          ? "text-xs px-2 py-1 rounded bg-accent text-primary"
          : "text-xs px-2 py-1 rounded bg-overlay text-secondary bg-overlay-hover";
    }
  }

  // Store active filter and re-fetch from server with event_type param
  state.timelineFilter = type === "all" ? null : type;
  if (state.selectedFile) {
    loadTimelineTab(state.selectedFile, 0);
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

export function clearScanSourceFilter() {
  state.filesScanSource = "";
  state.filesPage.offset = 0;
  loadFiles();
}

// --- Link to Issue modal ---

export function showLinkIssueModal(fileId) {
  const modal = document.createElement("div");
  modal.id = "linkIssueModal";
  modal.className = "fixed inset-0 bg-black/50 flex items-center justify-center z-50";
  modal.onclick = (ev) => {
    if (ev.target === modal) modal.remove();
  };
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
    `<button onclick="submitLinkIssue('${escJsSingle(fileId)}')" class="text-xs bg-accent text-white px-3 py-1 rounded bg-accent-hover">Link</button>` +
    ' <button onclick="document.getElementById(\'linkIssueModal\').remove()" class="text-xs text-muted text-primary-hover">Cancel</button>' +
    "</div>";
  document.body.appendChild(modal);
  document.getElementById("linkIssueId").focus();
}

export async function submitLinkIssue(fileId) {
  const issueId = document.getElementById("linkIssueId").value.trim();
  const assocType = document.getElementById("linkAssocType").value;
  if (!issueId) return;

  const result = await postFileAssociation(fileId, {
    issue_id: issueId,
    assoc_type: assocType,
  });
  const modal = document.getElementById("linkIssueModal");
  if (modal) modal.remove();

  if (result.ok) {
    showToast("Association created", "success");
    openFileDetail(fileId); // Refresh
  } else {
    showToast(result.error || "Failed to create association", "error");
  }
}
