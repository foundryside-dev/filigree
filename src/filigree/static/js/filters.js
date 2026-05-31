// ---------------------------------------------------------------------------
// Filtering, search, presets, type-filter, and change tracking.
// ---------------------------------------------------------------------------

import { fetchSearch, fetchTypeInfo } from "./api.js";
import { render } from "./router.js";
import { state } from "./state.js";

// --- Callbacks for functions not available at import time ---

export const callbacks = {
  showToast: null,
  renderKanban: null,
};

const PROJECT_FILTERS_STORAGE_KEY = "filigree_project_filter_settings";
const PRESETS_STORAGE_KEY = "filigree_presets";
const DEFAULT_PROJECT_FILTERS = Object.freeze({
  open: true,
  active: true,
  closed: false,
  priority: "all",
  doneTimeBound: "7",
  ready: true,
  blocked: false,
});
const VALID_PRIORITY_FILTERS = new Set(["all", "0-1", "2", "3-4"]);
const VALID_DONE_TIME_BOUNDS = new Set(["0", "1", "3", "7", "14", "30"]);

function getProjectStorageKey() {
  return state.currentProjectKey || "__default__";
}

function normalizeProjectFilters(raw) {
  const src = raw && typeof raw === "object" ? raw : {};
  const priority = VALID_PRIORITY_FILTERS.has(src.priority)
    ? src.priority
    : DEFAULT_PROJECT_FILTERS.priority;
  const doneTimeBound = VALID_DONE_TIME_BOUNDS.has(String(src.doneTimeBound))
    ? String(src.doneTimeBound)
    : DEFAULT_PROJECT_FILTERS.doneTimeBound;
  return {
    open: src.open === undefined ? DEFAULT_PROJECT_FILTERS.open : !!src.open,
    active: src.active === undefined ? DEFAULT_PROJECT_FILTERS.active : !!src.active,
    closed: src.closed === undefined ? DEFAULT_PROJECT_FILTERS.closed : !!src.closed,
    priority,
    doneTimeBound,
    ready: src.ready === undefined ? DEFAULT_PROJECT_FILTERS.ready : !!src.ready,
    blocked: src.blocked === undefined ? DEFAULT_PROJECT_FILTERS.blocked : !!src.blocked,
  };
}

function normalizeFilterState(raw) {
  const normalizedProject = normalizeProjectFilters(raw);
  return {
    ...normalizedProject,
    search: typeof raw?.search === "string" ? raw.search : "",
  };
}

function readProjectFilterSettings() {
  try {
    const raw = localStorage.getItem(PROJECT_FILTERS_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch (_e) {
    return {};
  }
}

function writeProjectFilterSettings(settings) {
  try {
    localStorage.setItem(PROJECT_FILTERS_STORAGE_KEY, JSON.stringify(settings));
  } catch (_e) {
    // best effort
  }
}

function readPresets() {
  try {
    const raw = localStorage.getItem(PRESETS_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch (_e) {
    return {};
  }
}

function writePresets(presets) {
  try {
    localStorage.setItem(PRESETS_STORAGE_KEY, JSON.stringify(presets));
  } catch (_e) {
    // best effort
  }
}

function updateToggleButtons() {
  const readyBtn = document.getElementById("btnReadyFilter");
  if (readyBtn) {
    readyBtn.className = state.readyFilter
      ? "px-2 py-1 rounded text-xs font-medium bg-emerald-900/50 text-emerald-400 border border-emerald-700"
      : "px-2 py-1 rounded text-xs font-medium bg-overlay text-secondary border border-strong";
  }
  const blockedBtn = document.getElementById("btnBlocked");
  if (blockedBtn) {
    blockedBtn.className = state.blockedFilter
      ? "px-2 py-1 rounded text-xs font-medium bg-red-900/50 text-red-400 border border-red-700"
      : "px-2 py-1 rounded text-xs font-medium bg-overlay text-secondary border border-strong";
  }
}

export function saveProjectFilterSettings() {
  const settings = readProjectFilterSettings();
  settings[getProjectStorageKey()] = normalizeProjectFilters(getFilterState());
  writeProjectFilterSettings(settings);
}

export function loadProjectFilterSettings() {
  const settings = readProjectFilterSettings();
  const saved = settings[getProjectStorageKey()];
  const filterState = saved
    ? normalizeFilterState(saved)
    : normalizeFilterState(DEFAULT_PROJECT_FILTERS);
  applyFilterState(filterState, { skipPersist: true });
}

// --- Filtered issue list ---

export function getFilteredIssues() {
  let items = state.allIssues.slice();

  const showOpen = state.statusPills.open;
  const showActive = state.statusPills.active;
  const showDone = state.statusPills.done;

  items = items.filter((i) => {
    const cat = i.status_category || "open";
    if (cat === "open" && !showOpen) return false;
    if (cat === "wip" && !showActive) return false;
    if (cat === "done" && !showDone) return false;
    return true;
  });

  // Done time-bound filter
  if (showDone) {
    const doneTimeBound = parseInt(document.getElementById("doneTimeBound")?.value || "7", 10);
    if (doneTimeBound > 0) {
      const cutoff = Date.now() - doneTimeBound * 86400000;
      items = items.filter((i) => {
        const cat = i.status_category || "open";
        if (cat === "done") {
          const closedAt = i.closed_at || i.updated_at;
          return closedAt && new Date(closedAt).getTime() >= cutoff;
        }
        return true;
      });
    }
  }

  const prio = document.getElementById("filterPriority")?.value || "all";
  if (prio === "0-1") items = items.filter((i) => i.priority <= 1);
  else if (prio === "2") items = items.filter((i) => i.priority === 2);
  else if (prio === "3-4") items = items.filter((i) => i.priority >= 3);

  if (state.typeTemplate) {
    items = items.filter((i) => i.type === state.typeTemplate.type);
  }

  if (state.searchResults !== null) {
    items = items.filter((i) => state.searchResults.has(i.id));
  }

  if (state.readyFilter) {
    items.sort((a, b) => (b.is_ready ? 1 : 0) - (a.is_ready ? 1 : 0) || a.priority - b.priority);
  }

  if (state.blockedFilter) {
    items = items.filter((i) =>
      (i.blocked_by || []).some((bid) => {
        const b = state.issueMap[bid];
        return b && (b.status_category || "open") !== "done";
      }),
    );
  }

  return items;
}

// --- Filter application ---

export function applyFilters() {
  saveProjectFilterSettings();
  render();
}

// --- Toggle buttons ---

export function toggleReady() {
  state.readyFilter = !state.readyFilter;
  updateToggleButtons();
  saveProjectFilterSettings();
  render();
}

export function toggleBlocked() {
  state.blockedFilter = !state.blockedFilter;
  if (state.blockedFilter) state.readyFilter = false;
  updateToggleButtons();
  saveProjectFilterSettings();
  render();
}

// --- Status pill toggles ---

const PILL_ON = "px-2 py-1 rounded text-xs font-medium bg-accent text-primary";
const PILL_OFF =
  "px-2 py-1 rounded text-xs font-medium bg-overlay text-secondary border border-strong";

function syncPillUI() {
  const pillOpen = document.getElementById("pillOpen");
  const pillActive = document.getElementById("pillActive");
  const pillDone = document.getElementById("pillDone");
  if (pillOpen) pillOpen.className = state.statusPills.open ? PILL_ON : PILL_OFF;
  if (pillActive) pillActive.className = state.statusPills.active ? PILL_ON : PILL_OFF;
  if (pillDone) {
    pillDone.className = state.statusPills.done ? PILL_ON : PILL_OFF;
    const dropdown = document.getElementById("doneTimeBound");
    if (dropdown) {
      if (state.statusPills.done) {
        const days = dropdown.value || "7";
        pillDone.textContent = days === "0" ? "Done: All" : `Done: ${days}d`;
      } else {
        pillDone.textContent = "Done";
        dropdown.classList.add("hidden");
      }
    }
  }
}

export function toggleStatusPill(category) {
  if (category === "done") {
    const dropdown = document.getElementById("doneTimeBound");
    if (!state.statusPills.done) {
      // Phase 1: off → on + show dropdown
      state.statusPills.done = true;
      syncPillUI();
      if (dropdown) dropdown.classList.remove("hidden");
      applyFilters();
    } else if (dropdown && !dropdown.classList.contains("hidden")) {
      // Phase 2: on + dropdown visible → on + hide dropdown
      dropdown.classList.add("hidden");
    } else {
      // Phase 3: on + dropdown hidden → off
      state.statusPills.done = false;
      syncPillUI();
      applyFilters();
    }
    return;
  }
  state.statusPills[category] = !state.statusPills[category];
  syncPillUI();
  applyFilters();
}

export function onDoneTimeBoundChange() {
  syncPillUI();
  applyFilters();
  const dropdown = document.getElementById("doneTimeBound");
  if (dropdown) dropdown.classList.add("hidden");
}

export function toggleMultiSelect() {
  state.multiSelectMode = !state.multiSelectMode;
  if (!state.multiSelectMode) state.selectedCards.clear();
  const btn = document.getElementById("btnMultiSelect");
  if (btn) {
    btn.className = state.multiSelectMode
      ? "px-2 py-1 rounded text-xs font-medium bg-accent text-primary"
      : "px-2 py-1 rounded text-xs font-medium bg-overlay text-secondary border border-strong";
  }
  render();
}

export function toggleCardSelect(e, issueId) {
  if (!state.multiSelectMode) return;
  e.stopPropagation();
  if (state.selectedCards.has(issueId)) state.selectedCards.delete(issueId);
  else state.selectedCards.add(issueId);
  render();
}

// --- Search ---

export function debouncedSearch() {
  clearTimeout(state._searchTimeout);
  state._searchTimeout = setTimeout(doSearch, 200);
}

export async function doSearch() {
  const q = document.getElementById("filterSearch").value.trim();
  const clearBtn = document.getElementById("searchClear");
  if (clearBtn) clearBtn.classList.toggle("hidden", !q);
  if (!q) {
    state.searchResults = null;
    render();
    return;
  }
  try {
    const data = await fetchSearch(q, 100);
    if (!data) {
      state.searchResults = new Set();
      render();
      return;
    }
    state.searchResults = new Set(data.results.map((i) => i.id));
  } catch (err) {
    console.warn("[search] Failed to fetch search results:", err);
    state.searchResults = new Set();
    if (callbacks.showToast) callbacks.showToast("Search failed — showing no results", "error");
  }
  render();
}

// --- Filter state (presets) ---

export function getFilterState() {
  return {
    open: state.statusPills.open,
    active: state.statusPills.active,
    closed: state.statusPills.done,
    priority: document.getElementById("filterPriority")?.value || "all",
    doneTimeBound: document.getElementById("doneTimeBound")?.value || "7",
    ready: state.readyFilter,
    blocked: state.blockedFilter,
    search: document.getElementById("filterSearch")?.value || "",
  };
}

export function applyFilterState(filterState, opts = {}) {
  const normalized = normalizeFilterState(filterState);
  state.statusPills.open = normalized.open;
  state.statusPills.active = normalized.active;
  state.statusPills.done = normalized.closed;
  // Restore the done time-bound dropdown BEFORE syncPillUI(): syncPillUI() reads
  // this dropdown's value to render the "Done: Xd" pill label, so the value must
  // be in place first or the label reflects the previous/default window.
  const doneEl = document.getElementById("doneTimeBound");
  if (doneEl) doneEl.value = normalized.doneTimeBound || "7";
  syncPillUI();
  const prioEl = document.getElementById("filterPriority");
  if (prioEl) prioEl.value = normalized.priority;
  state.readyFilter = normalized.ready;
  state.blockedFilter = normalized.blocked;
  updateToggleButtons();
  if (!opts.skipPersist) saveProjectFilterSettings();
  const searchEl = document.getElementById("filterSearch");
  if (normalized.search) {
    if (searchEl) searchEl.value = normalized.search;
    doSearch();
  } else {
    if (searchEl) searchEl.value = "";
    state.searchResults = null;
    render();
  }
}

export function savePreset() {
  const existing = document.getElementById("presetNameModal");
  if (existing) existing.remove();

  const modal = document.createElement("div");
  modal.id = "presetNameModal";
  modal.className = "fixed inset-0 bg-black/50 flex items-center justify-center z-50";
  modal.innerHTML =
    '<div class="rounded-lg p-4 w-72 shadow-xl" style="background:var(--surface-raised);border:1px solid var(--border-strong)">' +
    '<div class="text-sm mb-2" style="color:var(--text-primary)">Save filter preset</div>' +
    '<input id="presetNameInput" type="text" placeholder="Preset name..." class="w-full text-xs rounded px-3 py-2 mb-3 focus:outline-none" style="background:var(--surface-overlay);color:var(--text-primary);border:1px solid var(--border-strong)">' +
    '<div class="flex justify-end gap-2">' +
    '<button onclick="document.getElementById(\'presetNameModal\').remove()" class="text-xs bg-overlay bg-overlay-hover px-3 py-1.5 rounded">Cancel</button>' +
    '<button onclick="confirmSavePreset()" class="text-xs px-3 py-1.5 rounded bg-accent-hover" style="background:var(--accent);color:var(--surface-base)">Save</button>' +
    "</div></div>";
  document.body.appendChild(modal);
  modal.onclick = (e) => {
    if (e.target === modal) modal.remove();
  };
  document.getElementById("presetNameInput").focus();
}

export function confirmSavePreset() {
  const name = document.getElementById("presetNameInput").value.trim();
  if (!name) {
    if (callbacks.showToast) callbacks.showToast("Name is required", "error");
    return;
  }
  const presets = readPresets();
  presets[name] = getFilterState();
  writePresets(presets);
  document.getElementById("presetNameModal").remove();
  populatePresets();
  if (callbacks.showToast) {
    callbacks.showToast(`Preset "${name}" saved`, "success");
  }
}

export function loadPreset() {
  const name = document.getElementById("filterPreset").value;
  if (!name) return;
  const presets = readPresets();
  if (presets[name]) applyFilterState(presets[name]);
  document.getElementById("filterPreset").value = "";
}

export function populatePresets() {
  const select = document.getElementById("filterPreset");
  if (!select) return;
  const presets = readPresets();
  select.innerHTML = '<option value="">Presets...</option>';
  Object.keys(presets)
    .sort()
    .forEach((name) => {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      select.appendChild(opt);
    });
}

// --- Type filter ---

export function populateTypeFilter() {
  const select = document.getElementById("filterType");
  if (!select) return;
  const types = {};
  state.allIssues.forEach((i) => {
    types[i.type] = true;
  });
  const currentVal = select.value;
  // Preserve first option, rebuild the rest
  select.innerHTML = '<option value="">All types</option>';
  Object.keys(types)
    .sort()
    .forEach((t) => {
      const opt = document.createElement("option");
      opt.value = t;
      opt.textContent = t;
      select.appendChild(opt);
    });
  select.value = currentVal;
  // If the previously selected type no longer exists, the value silently
  // falls back to "".  Clear stale typeTemplate so renderKanban() shows
  // the default 3-category board instead of an empty type-specific board.
  if (select.value !== currentVal) {
    state.typeTemplate = null;
  }
}

export async function applyTypeFilter() {
  const typeName = document.getElementById("filterType").value;
  const seq = ++state._typeFilterSeq;
  if (!typeName) {
    state.typeTemplate = null;
    if (callbacks.renderKanban) callbacks.renderKanban();
    updateTypeFilterUI(false);
    return;
  }
  try {
    const data = await fetchTypeInfo(typeName);
    if (seq !== state._typeFilterSeq) return; // Superseded by newer selection
    if (data) {
      state.typeTemplate = data;
    } else {
      state.typeTemplate = null;
    }
  } catch (err) {
    if (seq !== state._typeFilterSeq) return;
    console.warn("[typeFilter] Failed to load type info:", err);
    state.typeTemplate = null;
    const sel = document.getElementById("filterType");
    if (sel) sel.value = "";
    if (callbacks.showToast) callbacks.showToast("Could not load type filter", "error");
  }
  if (callbacks.renderKanban) callbacks.renderKanban();
  updateTypeFilterUI(!!state.typeTemplate);
}

export function updateTypeFilterUI(isFiltered) {
  const btnBoard = document.getElementById("btnBoard");
  const btnClust = document.getElementById("btnCluster");
  const pill = document.getElementById("typeFilterPill");
  const label = document.getElementById("typeFilterLabel");
  if (isFiltered && state.typeTemplate) {
    if (btnBoard) btnBoard.classList.add("opacity-50", "pointer-events-none");
    if (btnClust) btnClust.classList.add("opacity-50", "pointer-events-none");
    if (pill) pill.classList.remove("hidden");
    if (label) label.textContent = state.typeTemplate.type;
  } else {
    if (btnBoard) btnBoard.classList.remove("opacity-50", "pointer-events-none");
    if (btnClust) btnClust.classList.remove("opacity-50", "pointer-events-none");
    if (pill) pill.classList.add("hidden");
  }
}

export function clearTypeFilter() {
  state.typeTemplate = null;
  const typeSelect = document.getElementById("filterType");
  if (typeSelect) typeSelect.value = "";
  updateTypeFilterUI(false);
  if (callbacks.renderKanban) callbacks.renderKanban();
}

// --- Change tracking ---

export function trackChanges(newIssues) {
  state.changedIds.clear();
  newIssues.forEach((i) => {
    const prev = state.previousIssueState[i.id];
    if (
      prev &&
      (prev.status !== i.status ||
        prev.priority !== i.priority ||
        prev.assignee !== i.assignee ||
        prev.updated_at !== i.updated_at)
    ) {
      state.changedIds.add(i.id);
    }
  });
  state.previousIssueState = {};
  newIssues.forEach((i) => {
    state.previousIssueState[i.id] = {
      status: i.status,
      priority: i.priority,
      assignee: i.assignee,
      updated_at: i.updated_at,
    };
  });
}
