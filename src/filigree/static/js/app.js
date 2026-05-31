// ---------------------------------------------------------------------------
// app.js — entry point for the Filigree dashboard.
//
// Imports every module, wires up late-bound callbacks, registers views,
// sets up keyboard shortcuts, and runs the init sequence.
//
// This is the ONLY file loaded by <script type="module"> in dashboard.html.
// All functions referenced by inline onclick/onchange handlers are exposed
// on `window` at the bottom of this file.
// ---------------------------------------------------------------------------

// --- Module imports ---

import { fetchAllData, fetchDashboardConfig, fetchFileSchema, fetchProjects } from "./api.js";
import {
  applyFilters,
  applyTypeFilter,
  clearTypeFilter,
  confirmSavePreset,
  debouncedSearch,
  callbacks as filtersCallbacks,
  loadPreset,
  loadProjectFilterSettings,
  populatePresets,
  savePreset,
  populateTypeFilter,
  toggleBlocked,
  toggleCardSelect,
  toggleMultiSelect,
  toggleReady,
  toggleStatusPill,
  onDoneTimeBoundChange,
  trackChanges,
  updateTypeFilterUI,
} from "./filters.js";
import {
  parseHash,
  registerView,
  render,
  callbacks as routerCallbacks,
  switchKanbanMode,
  switchView,
  updateHash,
} from "./router.js";
import { CATEGORY_COLORS, REFRESH_INTERVAL, state, THEME_COLORS } from "./state.js";
import {
  batchCloseSelected,
  batchSetPriority,
  closeSettingsMenu,
  copyIssueId,
  escHtml,
  escJsSingle,
  endTour,
  reloadServer,
  showCreateForm,
  showToast,
  showTourStep,
  startTour,
  submitCreateForm,
  toggleGraphLegend,
  toggleKanbanLegend,
  toggleSettingsMenu,
  toggleTheme,
  callbacks as uiCallbacks,
  updateBatchBar,
} from "./ui.js";
import {
  addComment,
  addDependency,
  claimIssue,
  closeDetail,
  closeIssue,
  confirmClaim,
  detailBack,
  callbacks as detailCallbacks,
  loadTransitions,
  moveIssueTo,
  openDetail,
  releaseIssue,
  removeDependency,
  reopenIssue,
  showAddBlocker,
  updateIssue,
} from "./views/detail.js";
import { computeHealthScore, computeImpactScores } from "./analytics.js";
import {
  callbacks as graphCallbacks,
  graphFit,
  renderGraph,
  refreshCriticalPathState,
  setCriticalPathStateFromPath,
  showHealthBreakdown,
  toggleCriticalPath,
} from "./views/graph.js";
import {
  renderGraphSidebar,
  graphSidebarSelectAll,
  graphSidebarClearAll,
  rebuildTreeIndex,
  attachSidebarListeners,
  callbacks as sidebarCallbacks,
} from "./views/graphSidebar.js";
import {
  initDragAndDrop,
  callbacks as kanbanCallbacks,
  renderKanban,
  toggleEpicExpand,
} from "./views/kanban.js";
import {
  loadMetrics,
  renderSparkline,
  showStaleIssues,
  updateStaleBadge,
} from "./views/metrics.js";
import {
  loadPlanView,
  loadWorkflow,
  loadWorkflowInModal,
  showWorkflowModal,
} from "./views/workflow.js";
import {
  closeFinding,
  clearScanSourceFilter,
  closeFileDetail,
  createIssueFromFinding,
  filesPageNext,
  filesPagePrev,
  filterFindings,
  filterTimeline,
  loadFiles,
  loadMoreFindings,
  loadMoreTimeline,
  openFileDetail,
  selectFinding,
  showLinkIssueModal,
  sortFiles,
  submitLinkIssue,
  switchFileTab,
} from "./views/files.js";
import {
  collapseAllReleaseTree,
  loadReleases,
  retryReleaseTree,
  scrollToReleaseCard,
  toggleReleaseExpand,
  toggleReleaseTreeNode,
} from "./views/releases.js";
import { loadReady } from "./views/ready.js";

// ---------------------------------------------------------------------------
// Core data fetching (lives here because it touches every module)
// ---------------------------------------------------------------------------

async function fetchData() {
  document.getElementById("refreshIndicator").style.opacity = "1";
  try {
    if (!state.graphConfigLoaded) {
      await loadDashboardConfig();
    }
    await loadRegistryFallbackBanner();
    const data = await fetchAllData();
    if (!data) {
      console.warn("fetchData: non-OK response");
      return;
    }
    state.allIssues = data.issues;
    state.allDeps = data.deps;
    state.stats = data.stats;
    state.issueMap = {};
    state.allIssues.forEach((i) => {
      state.issueMap[i.id] = i;
    });
    rebuildTreeIndex();
    trackChanges(state.allIssues);
    computeImpactScores();
    computeHealthScore();
    if (state.criticalPathActive) await refreshCriticalPathState();
    updateStaleBadge();
    renderSparkline();
    updateStats();
    render();
  } finally {
    setTimeout(() => {
      document.getElementById("refreshIndicator").textContent =
        `Updated ${new Date().toLocaleTimeString()}`;
      document.getElementById("refreshIndicator").style.opacity = "0.5";
    }, 300);
  }
}

async function loadDashboardConfig() {
  try {
    const config = await fetchDashboardConfig();
    if (config) {
      state.graphConfig = config;
      state.graphConfigLoaded = true;
      return;
    }
  } catch (err) {
    console.warn("[loadDashboardConfig] Failed to load config, using defaults:", err);
  }
  state.graphConfig = {
    graph_v2_enabled: false,
    graph_api_mode: "legacy",
    graph_mode_configured: null,
  };
  state.graphConfigLoaded = true;
}

async function loadRegistryFallbackBanner() {
  const banner = document.getElementById("registryFallbackBanner");
  const rotationBanner = document.getElementById("clarionRotationBanner");
  if (!banner && !rotationBanner) return;
  try {
    const schema = await fetchFileSchema();
    if (banner) {
      if (schema?.config_flags?.allow_local_fallback) {
        banner.classList.remove("hidden");
      } else {
        banner.classList.add("hidden");
      }
    }
    if (rotationBanner) {
      if (schema?.config_flags?.clarion_instance_rotated) {
        rotationBanner.classList.remove("hidden");
      } else {
        rotationBanner.classList.add("hidden");
      }
    }
  } catch (err) {
    console.warn("[loadRegistryFallbackBanner] Failed to load file schema:", err);
    if (banner) banner.classList.add("hidden");
    if (rotationBanner) rotationBanner.classList.add("hidden");
  }
}

function updateStats() {
  if (!state.stats) return;
  const s = state.stats;
  const byCat = s.by_category || {};
  document.getElementById("readyFilterCount").textContent = s.ready_count;
  document.getElementById("footOpen").textContent = byCat.open || 0;
  document.getElementById("footActive").textContent = byCat.wip || 0;
  document.getElementById("footReady").textContent = s.ready_count;
  document.getElementById("footBlocked").textContent = s.blocked_count;
  document.getElementById("footDeps").textContent = s.total_dependencies;
  document.getElementById("blockedCount").textContent = s.blocked_count;
  populateTypeFilter();
}

// ---------------------------------------------------------------------------
// Multi-project support
// ---------------------------------------------------------------------------

function setProject(key, opts) {
  state.currentProjectKey = key;
  state.API_BASE = key ? `/api/p/${encodeURIComponent(key)}` : "/api";
  state.graphConfigLoaded = false;
  state.graphData = null;
  state.graphQuery = {};
  state.graphQueryKey = "";
  state.graphFallbackNotice = "";
  setCriticalPathStateFromPath([]);
  loadProjectFilterSettings();
  const sel = document.getElementById("projectSwitcher");
  if (sel) sel.value = key;
  const proj = state.allProjects.find((p) => p.key === key);
  document.title = proj ? `Filigree \u2014 ${proj.name}` : "Filigree Dashboard";
  state.selectedCards.clear();
  if (!opts?.keepDetail) closeDetail();
  updateHash();
  loadDashboardConfig().finally(fetchData);
}

async function loadProjects() {
  try {
    const projects = await fetchProjects(6);
    if (!projects) return;
    state.allProjects = projects;
    const currentMissing =
      !!state.currentProjectKey &&
      !state.allProjects.some((p) => p.key === state.currentProjectKey);
    const sel = document.getElementById("projectSwitcher");
    if (!sel) return;
    sel.innerHTML = "";
    state.allProjects.forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p.key;
      opt.textContent = p.name;
      if (p.key === state.currentProjectKey) opt.selected = true;
      sel.appendChild(opt);
    });
    const wrap = document.getElementById("projectSwitcherWrap");
    if (wrap) wrap.style.display = state.allProjects.length > 1 ? "" : "none";

    if (currentMissing) {
      const fallbackKey = state.allProjects.length > 0 ? state.allProjects[0].key : "";
      setProject(fallbackKey);
      showToast("Selected project was removed. Switched to an available project.", "warning");
    }
  } catch (_e) {
    /* best-effort */
  }
}

// ---------------------------------------------------------------------------
// Clear search helper (used by inline onclick in HTML and kanban empty state)
// ---------------------------------------------------------------------------

function clearSearch() {
  document.getElementById("filterSearch").value = "";
  state.searchResults = null;
  const clearBtn = document.getElementById("searchClear");
  if (clearBtn) clearBtn.classList.add("hidden");
  render();
}

// ---------------------------------------------------------------------------
// Sort helper for Kanban list mode (called by column header onclick)
// ---------------------------------------------------------------------------

function sortListMode(col) {
  if (state._listSortCol === col) {
    state._listSortDir = state._listSortDir === "asc" ? "desc" : "asc";
  } else {
    state._listSortCol = col;
    state._listSortDir = "asc";
  }
  renderKanban();
}

// ---------------------------------------------------------------------------
// Wire up late-bound callbacks
// ---------------------------------------------------------------------------

// ui.js callbacks
uiCallbacks.fetchData = fetchData;
uiCallbacks.openDetail = openDetail;
uiCallbacks.renderGraph = renderGraph;
uiCallbacks.loadWorkflow = loadWorkflow;

// router.js callbacks
routerCallbacks.openDetail = openDetail;
routerCallbacks.closeDetail = closeDetail;
routerCallbacks.updateBatchBar = updateBatchBar;
routerCallbacks.updateTypeFilterUI = updateTypeFilterUI;
routerCallbacks.renderKanban = renderKanban;

// filters.js callbacks
filtersCallbacks.showToast = showToast;
filtersCallbacks.renderKanban = renderKanban;

// kanban.js callbacks
kanbanCallbacks.openDetail = openDetail;
kanbanCallbacks.fetchData = fetchData;
kanbanCallbacks.updateBatchBar = updateBatchBar;
kanbanCallbacks.updateHash = updateHash;

// graph.js callbacks
graphCallbacks.openDetail = openDetail;
graphCallbacks.fetchData = fetchData;

// graphSidebar.js callbacks
sidebarCallbacks.renderGraph = renderGraph;

// detail.js callbacks
detailCallbacks.fetchData = fetchData;
detailCallbacks.render = render;

// ---------------------------------------------------------------------------
// Register views with the router
// ---------------------------------------------------------------------------

registerView("kanban", renderKanban);
registerView("graph", () => {
  renderGraphSidebar();
  renderGraph();
});
registerView("insights", loadMetrics);
registerView("files", loadFiles);
registerView("ready", loadReady);
registerView("releases", loadReleases);

// ---------------------------------------------------------------------------
// Keyboard shortcuts
// ---------------------------------------------------------------------------

document.addEventListener("keydown", (e) => {
  const active = document.activeElement;
  if (
    active &&
    (active.tagName === "INPUT" || active.tagName === "SELECT" || active.tagName === "TEXTAREA")
  ) {
    return;
  }

  if (e.key === "/" && !e.ctrlKey && !e.metaKey) {
    e.preventDefault();
    document.getElementById("filterSearch").focus();
    return;
  }

  if (e.key === "Escape") {
    if (state.selectedIssue) closeDetail();
    else if (state.selectedFile) closeFileDetail();
    else clearSearch();
    return;
  }

  if (e.key === "?" && e.shiftKey) {
    e.preventDefault();
    localStorage.removeItem("filigree_tour_done");
    startTour();
    return;
  }

  if (e.key === "?") {
    const existing = document.getElementById("helpModal");
    if (existing) {
      existing.remove();
      return;
    }
    const modal = document.createElement("div");
    modal.id = "helpModal";
    modal.className = "fixed inset-0 bg-black/50 flex items-center justify-center z-50";
    modal.onclick = (ev) => {
      if (ev.target === modal) modal.remove();
    };
    modal.innerHTML =
      '<div class="rounded-lg p-5 w-80 shadow-xl" style="background:var(--surface-raised);border:1px solid var(--border-strong)">' +
      '<div class="text-sm font-semibold mb-3" style="color:var(--text-primary)">Keyboard Shortcuts</div>' +
      '<div class="text-xs space-y-1" style="color:var(--text-primary)">' +
      '<div><kbd class="bg-overlay px-1 rounded">/</kbd> Focus search</div>' +
      '<div><kbd class="bg-overlay px-1 rounded">Esc</kbd> Close panel / clear search</div>' +
      '<div><kbd class="bg-overlay px-1 rounded">j</kbd> / <kbd class="bg-overlay px-1 rounded">k</kbd> Navigate cards</div>' +
      '<div><kbd class="bg-overlay px-1 rounded">Enter</kbd> Open issue detail</div>' +
      '<div><kbd class="bg-overlay px-1 rounded">c</kbd> Focus comment input (in detail)</div>' +
      '<div><kbd class="bg-overlay px-1 rounded">x</kbd> Close issue (in detail)</div>' +
      '<div><kbd class="bg-overlay px-1 rounded">m</kbd> Move issue to status (in detail)</div>' +
      '<div><kbd class="bg-overlay px-1 rounded">?</kbd> This help</div>' +
      '<div><kbd class="bg-overlay px-1 rounded">Shift+?</kbd> Replay guided tour</div>' +
      "</div>" +
      '<div class="mt-3 pt-2" style="border-top:1px solid var(--border-default)">' +
      '<button onclick="document.getElementById(\'helpModal\').remove();startTour();" class="text-xs" style="color:var(--accent)">Take the guided tour &rarr;</button>' +
      "</div>" +
      '<button onclick="document.getElementById(\'helpModal\').remove()" class="text-xs text-muted text-primary-hover mt-2">Close</button>' +
      "</div>";
    document.body.appendChild(modal);
    return;
  }

  // j/k card navigation
  if (e.key === "j" || e.key === "k") {
    const cards = Array.from(document.querySelectorAll(".card[tabindex]"));
    if (!cards.length) return;
    let idx = cards.indexOf(active);
    if (e.key === "j") idx = Math.min(idx + 1, cards.length - 1);
    else idx = Math.max(idx - 1, 0);
    cards[idx].focus();
    return;
  }

  // Enter to open detail
  if (e.key === "Enter" && active && active.classList.contains("card")) {
    const id = active.getAttribute("data-id");
    if (id) openDetail(id);
    return;
  }

  // Shortcuts when detail panel is open
  if (state.selectedIssue) {
    if (e.key === "c") {
      const ci = document.getElementById("commentInput");
      if (ci) {
        e.preventDefault();
        ci.focus();
      }
    }
    if (e.key === "x") {
      e.preventDefault();
      closeIssue(state.selectedIssue);
    }
    if (e.key === "m") {
      e.preventDefault();
      const issueId = state.selectedIssue;
      loadTransitions(issueId)
        .then((transitions) => {
          if (!transitions) return;
          const ready = transitions.filter((t) => t.ready);
          if (!ready.length) {
            showToast("No valid transitions", "info");
            return;
          }
          const moveModal = document.createElement("div");
          moveModal.id = "moveModal";
          moveModal.className = "fixed inset-0 bg-black/50 flex items-center justify-center z-50";
          moveModal.onclick = (ev) => {
            if (ev.target === moveModal) moveModal.remove();
          };
          moveModal.innerHTML =
            '<div class="rounded-lg p-4 w-64 shadow-xl" style="background:var(--surface-raised);border:1px solid var(--border-strong)">' +
            '<div class="text-sm mb-3" style="color:var(--text-primary)">Move to...</div>' +
            '<div class="flex flex-col gap-2">' +
            ready
              .map(
                (t) =>
                  `<button onclick="moveIssueTo('${escJsSingle(issueId)}','${escJsSingle(t.to)}')" class="text-xs text-left bg-overlay bg-overlay-hover px-3 py-2 rounded" style="color:var(--text-primary)">${escHtml(t.to)}</button>`,
              )
              .join("") +
            "</div>" +
            '<button onclick="document.getElementById(\'moveModal\').remove()" class="text-xs text-muted text-primary-hover mt-3">Cancel (Esc)</button>' +
            "</div>";
          document.body.appendChild(moveModal);
          moveModal.querySelector("button").focus();
          moveModal.addEventListener("keydown", (ev) => {
            if (ev.key === "Escape") moveModal.remove();
          });
        })
        .catch(() => {
          showToast("Could not load transitions", "error");
        });
    }
  }
});

// ---------------------------------------------------------------------------
// Visibility-change refresh
// ---------------------------------------------------------------------------

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) fetchData();
});

// ---------------------------------------------------------------------------
// Theme init (must run synchronously before first render)
// ---------------------------------------------------------------------------

(function initTheme() {
  const saved = localStorage.getItem("filigree_theme");
  if (saved === "light") {
    document.body.dataset.theme = "light";
    const btn = document.getElementById("themeToggle");
    if (btn) btn.innerHTML = "&#9790; Toggle theme";
    CATEGORY_COLORS.wip = "#0284C7";
    THEME_COLORS.textPrimary = "#0F2027";
    THEME_COLORS.textSecondary = "#3D6070";
    THEME_COLORS.graphOutline = "#F0F6F8";
    THEME_COLORS.graphEdge = "#9BBBC8";
    THEME_COLORS.accent = "#0284C7";
  }
})();

// ---------------------------------------------------------------------------
// Init sequence
// ---------------------------------------------------------------------------

const hashResult = parseHash();
populatePresets();
loadProjectFilterSettings();

(async function init() {
  await loadProjects();
  const hashProject = hashResult.project;
  if (hashProject) {
    const found = state.allProjects.find((p) => p.key === hashProject);
    if (found) {
      setProject(found.key, { keepDetail: true });
    } else if (state.allProjects.length > 0) {
      setProject(state.allProjects[0].key, { keepDetail: true });
    } else {
      fetchData();
    }
  } else if (state.allProjects.length > 0) {
    setProject(state.allProjects[0].key, { keepDetail: true });
  } else {
    await loadDashboardConfig();
    fetchData();
  }
})().then(() => {
  switchView(state.currentView);
  if (state.kanbanMode === "cluster") switchKanbanMode("cluster");
  else if (state.kanbanMode === "list") switchKanbanMode("list");
  else switchKanbanMode("board");
  if (state.selectedIssue) openDetail(state.selectedIssue);
  if (!localStorage.getItem("filigree_tour_done")) setTimeout(startTour, 1500);
  initDragAndDrop();
  attachSidebarListeners();
  fetch("/api/health")
    .then((r) => r.json())
    .then((d) => {
      const el = document.getElementById("footVersion");
      if (el && d.version) el.textContent = `v${d.version}`;
    })
    .catch((err) => {
      console.warn("[health] Failed to fetch version:", err);
    });
});

setInterval(() => {
  if (!document.hidden) fetchData();
}, REFRESH_INTERVAL);

setInterval(loadProjects, 60000);

// ---------------------------------------------------------------------------
// Expose functions on window for inline onclick/onchange handlers
// ---------------------------------------------------------------------------

// App-level functions
window.setProject = setProject;
window.fetchData = fetchData;
window.clearSearch = clearSearch;

// Router
window.switchView = switchView;
window.switchKanbanMode = switchKanbanMode;
window.render = render;

// Filters
window.applyFilters = applyFilters;
window.toggleReady = toggleReady;
window.toggleBlocked = toggleBlocked;
window.toggleStatusPill = toggleStatusPill;
window.onDoneTimeBoundChange = onDoneTimeBoundChange;
window.toggleMultiSelect = toggleMultiSelect;
window.toggleCardSelect = toggleCardSelect;
window.debouncedSearch = debouncedSearch;
window.confirmSavePreset = confirmSavePreset;
window.loadPreset = loadPreset;
window.savePreset = savePreset;
window.applyTypeFilter = applyTypeFilter;
window.clearTypeFilter = clearTypeFilter;

// UI utilities
window.showCreateForm = showCreateForm;
window.submitCreateForm = submitCreateForm;
window.startTour = startTour;
window.showTourStep = showTourStep;
window.endTour = endTour;
window.showToast = showToast;
window.toggleSettingsMenu = toggleSettingsMenu;
window.closeSettingsMenu = closeSettingsMenu;
window.reloadServer = reloadServer;
window.toggleTheme = toggleTheme;
window.toggleGraphLegend = toggleGraphLegend;
window.toggleKanbanLegend = toggleKanbanLegend;
window.batchSetPriority = batchSetPriority;
window.batchCloseSelected = batchCloseSelected;
window.copyIssueId = copyIssueId;

// Kanban
window.toggleEpicExpand = toggleEpicExpand;
window.sortListMode = sortListMode;

// Graph
window.renderGraph = renderGraph;
window.graphFit = graphFit;
window.toggleCriticalPath = toggleCriticalPath;
window.showHealthBreakdown = showHealthBreakdown;

// Graph sidebar
window.graphSidebarSelectAll = graphSidebarSelectAll;
window.graphSidebarClearAll = graphSidebarClearAll;

// Detail panel
window.openDetail = openDetail;
window.closeDetail = closeDetail;
window.detailBack = detailBack;
window.updateIssue = updateIssue;
window.closeIssue = closeIssue;
window.reopenIssue = reopenIssue;
window.claimIssue = claimIssue;
window.confirmClaim = confirmClaim;
window.releaseIssue = releaseIssue;
window.moveIssueTo = moveIssueTo;
window.removeDependency = removeDependency;
window.showAddBlocker = showAddBlocker;
window.addDependency = addDependency;
window.addComment = addComment;

// Metrics
window.loadMetrics = loadMetrics;
window.showStaleIssues = showStaleIssues;

// Workflow
window.loadWorkflow = loadWorkflow;
window.loadPlanView = loadPlanView;
window.showWorkflowModal = showWorkflowModal;
window.loadWorkflowInModal = loadWorkflowInModal;

// Files
window.loadFiles = loadFiles;
window.openFileDetail = openFileDetail;
window.closeFileDetail = closeFileDetail;
window.clearScanSourceFilter = clearScanSourceFilter;
window.sortFiles = sortFiles;
window.filesPagePrev = filesPagePrev;
window.filesPageNext = filesPageNext;
window.switchFileTab = switchFileTab;
window.loadMoreFindings = loadMoreFindings;
window.loadMoreTimeline = loadMoreTimeline;
window.filterTimeline = filterTimeline;
window.filterFindings = filterFindings;
window.selectFinding = selectFinding;
window.createIssueFromFinding = createIssueFromFinding;
window.closeFinding = closeFinding;
window.showLinkIssueModal = showLinkIssueModal;
window.submitLinkIssue = submitLinkIssue;

// Releases
window.loadReady = loadReady;
window.loadReleases = loadReleases;
window._scrollToReleaseCard = scrollToReleaseCard;
window._toggleReleaseExpand = toggleReleaseExpand;
window._retryReleaseTree = retryReleaseTree;
window._toggleReleaseTreeNode = toggleReleaseTreeNode;
window._collapseAllReleaseTree = collapseAllReleaseTree;
