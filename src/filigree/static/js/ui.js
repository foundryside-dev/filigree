// ---------------------------------------------------------------------------
// UI utilities — popovers, toasts, tours, modals, settings, theme, etc.
// Extracted from dashboard.html into an ES module.
// ---------------------------------------------------------------------------

import {
  fetchTypes,
  patchFileFinding,
  postBatchClose,
  postBatchUpdate,
  postCreateIssue,
  postReload,
} from "./api.js";
import { CATEGORY_COLORS, state, THEME_COLORS, TOUR_STEPS } from "./state.js";

// ---------------------------------------------------------------------------
// Late-bound callbacks — wired up by app.js after all modules load.
// Functions here that need to trigger a full data refresh or open the detail
// panel call through this object so there is no circular-import.
// ---------------------------------------------------------------------------
export const callbacks = {
  fetchData: null,
  openDetail: null,
  renderGraph: null,
  loadWorkflow: null,
};

// ---------------------------------------------------------------------------
// Time formatting
// ---------------------------------------------------------------------------
export function relativeTime(isoStr) {
  if (!isoStr) return "";
  const diff = Date.now() - new Date(isoStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
}

// ---------------------------------------------------------------------------
// XSS prevention
// ---------------------------------------------------------------------------
export function escHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#x27;");
}

export function escJsSingle(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/\\/g, "\\\\")
    .replace(/'/g, "\\'")
    .replace(/\r/g, "\\r")
    .replace(/\n/g, "\\n")
    .replace(/</g, "\\x3C")
    .replace(/>/g, "\\x3E");
}

// ---------------------------------------------------------------------------
// Onboarding tour
// ---------------------------------------------------------------------------
export function startTour() {
  showTourStep(0);
}

export function showTourStep(index) {
  const prev = document.getElementById("tourOverlay");
  if (prev) prev.remove();
  document.querySelectorAll(".tour-tooltip").forEach((el) => {
    el.remove();
  });
  document.querySelectorAll(".tour-highlight").forEach((el) => {
    el.classList.remove("tour-highlight");
  });

  if (index >= TOUR_STEPS.length) {
    localStorage.setItem("filigree_tour_done", "true");
    return;
  }

  const step = TOUR_STEPS[index];
  const targetEl = step.el ? document.querySelector(step.el) : null;
  if (targetEl) targetEl.classList.add("tour-highlight");

  const overlay = document.createElement("div");
  overlay.id = "tourOverlay";
  overlay.className = "tour-overlay";

  const tooltip = document.createElement("div");
  tooltip.className = "tour-tooltip rounded-lg p-4 shadow-xl";
  tooltip.style.cssText = "background:var(--surface-raised);border:1px solid var(--accent)";
  tooltip.innerHTML =
    `<div class="text-sm mb-3 leading-relaxed" style="color:var(--text-primary)">${escHtml(step.text)}</div>` +
    '<div class="flex items-center justify-between">' +
    `<span class="text-xs" style="color:var(--text-muted)">${index + 1} of ${TOUR_STEPS.length}</span>` +
    '<div class="flex gap-2">' +
    '<button onclick="endTour()" class="text-xs text-muted text-primary-hover px-2 py-1">Skip</button>' +
    `<button onclick="showTourStep(${index + 1})" class="text-xs px-3 py-1.5 rounded bg-accent-hover" style="background:var(--accent);color:var(--surface-base)">` +
    (index === TOUR_STEPS.length - 1 ? "Done" : "Next") +
    "</button>" +
    "</div>" +
    "</div>";

  document.body.appendChild(overlay);
  document.body.appendChild(tooltip);

  if (targetEl) {
    const rect = targetEl.getBoundingClientRect();
    if (step.pos === "bottom") {
      tooltip.style.top = `${rect.bottom + 12}px`;
      tooltip.style.left = `${Math.max(16, Math.min(rect.left, window.innerWidth - 380))}px`;
    } else if (step.pos === "top") {
      tooltip.style.left = `${Math.max(16, Math.min(rect.left, window.innerWidth - 380))}px`;
      // Position above element, clamped so it never goes off-screen
      const ttH = tooltip.offsetHeight;
      tooltip.style.top = `${Math.max(16, rect.top - 12 - ttH)}px`;
    }
  } else {
    tooltip.style.top = "50%";
    tooltip.style.left = "50%";
    tooltip.style.transform = "translate(-50%, -50%)";
  }

  overlay.onclick = () => {
    endTour();
  };
}

export function endTour() {
  const overlay = document.getElementById("tourOverlay");
  if (overlay) overlay.remove();
  document.querySelectorAll(".tour-tooltip").forEach((el) => {
    el.remove();
  });
  document.querySelectorAll(".tour-highlight").forEach((el) => {
    el.classList.remove("tour-highlight");
  });
  localStorage.setItem("filigree_tour_done", "true");
}

// ---------------------------------------------------------------------------
// Copy issue ID to clipboard
// ---------------------------------------------------------------------------
export function copyIssueId(id, event) {
  event.stopPropagation();
  navigator.clipboard.writeText(id).then(
    () => showToast("Copied " + id, "success"),
    () => showToast("Copy failed", "error"),
  );
}

// ---------------------------------------------------------------------------
// Render a clickable issue ID span (reusable across views)
// ---------------------------------------------------------------------------
export function issueIdChip(id) {
  const safeId = escJsSingle(id);
  return (
    `<span class="cursor-pointer hover:underline" style="color:var(--text-muted)" ` +
    `title="Click to copy" tabindex="0" role="button" ` +
    `onclick="copyIssueId('${safeId}', event)" ` +
    `onkeydown="if(event.key==='Enter')copyIssueId('${safeId}', event)"` +
    `>${escHtml(id)}</span>`
  );
}

// ---------------------------------------------------------------------------
// Button loading state
// ---------------------------------------------------------------------------
export function setLoading(el, loading) {
  if (!el) return;
  if (loading) {
    el.classList.add("btn-loading");
    el.dataset.origText = el.textContent;
    el.textContent = "Saving...";
  } else {
    el.classList.remove("btn-loading");
    if (el.dataset.origText) {
      el.textContent = el.dataset.origText;
      delete el.dataset.origText;
    }
  }
}

// ---------------------------------------------------------------------------
// Toast notifications
// ---------------------------------------------------------------------------
export function showToast(message, type) {
  const container = document.getElementById("toastContainer");
  const toast = document.createElement("div");
  const bg =
    type === "error"
      ? "bg-red-900/90 border-red-700 text-red-200"
      : type === "success"
        ? "bg-emerald-900/90 border-emerald-700 text-emerald-200"
        : "border text-primary";
  toast.className = `px-4 py-2 rounded border text-xs shadow-lg ${bg}`;
  if (type !== "error" && type !== "success") {
    toast.style.background = "var(--surface-raised)";
    toast.style.borderColor = "var(--border-strong)";
    toast.style.opacity = "0.95";
  }
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => {
    toast.remove();
  }, 4000);
}

// ---------------------------------------------------------------------------
// Batch action bar
// ---------------------------------------------------------------------------
export function updateBatchBar() {
  const bar = document.getElementById("batchBar");
  if (!bar) return;
  if (state.selectedCards.size > 0) {
    bar.classList.remove("hidden");
    document.getElementById("batchCount").textContent = `${state.selectedCards.size} selected`;
  } else {
    bar.classList.add("hidden");
  }
}

function batchFailureItems(result) {
  if (!result?.ok) return [];
  const data = result.data || {};
  if (Array.isArray(data.errors)) return data.errors;
  if (Array.isArray(data.failed)) return data.failed;
  return [];
}

export function batchActionErrorMessage(result, actionLabel, selectedCount) {
  if (!result?.ok) return result?.error || `${actionLabel} failed`;
  const failures = batchFailureItems(result);
  if (failures.length === 0) return "";
  const issueLabel = selectedCount === 1 ? "issue" : "issues";
  return `${actionLabel} failed for ${failures.length} of ${selectedCount} ${issueLabel}`;
}

async function handleBatchActionResult(result, { actionLabel, successMessage, selectedCount }) {
  const errorMessage = batchActionErrorMessage(result, actionLabel, selectedCount);
  if (errorMessage) {
    showToast(errorMessage, "error");
    if (result?.ok && callbacks.fetchData) await callbacks.fetchData();
    return false;
  }

  state.selectedCards.clear();
  state.multiSelectMode = false;
  showToast(successMessage, "success");
  if (callbacks.fetchData) await callbacks.fetchData();
  return true;
}

export async function batchSetPriority() {
  const existing = document.getElementById("batchPrioModal");
  if (existing) existing.remove();
  const modal = document.createElement("div");
  modal.id = "batchPrioModal";
  modal.className = "fixed inset-0 bg-black/50 flex items-center justify-center z-50";
  modal.innerHTML =
    '<div class="rounded-lg p-4 w-72 shadow-xl" style="background:var(--surface-raised);border:1px solid var(--border-strong)">' +
    `<div class="text-sm mb-2" style="color:var(--text-primary)">Set priority for ${state.selectedCards.size} issues</div>` +
    '<select id="batchPrioSelect" class="w-full text-xs rounded px-2 py-2 mb-3" style="background:var(--surface-overlay);color:var(--text-primary);border:1px solid var(--border-strong)">' +
    '<option value="0">P0 \u2014 Critical</option><option value="1">P1 \u2014 High</option>' +
    '<option value="2" selected>P2 \u2014 Medium</option><option value="3">P3 \u2014 Low</option><option value="4">P4 \u2014 Backlog</option>' +
    "</select>" +
    '<div class="flex justify-end gap-2">' +
    '<button id="batchPrioCancel" class="text-xs bg-overlay bg-overlay-hover px-3 py-1.5 rounded">Cancel</button>' +
    '<button id="batchPrioConfirm" class="text-xs px-3 py-1.5 rounded bg-accent-hover" style="background:var(--accent);color:var(--surface-base)">Apply</button>' +
    "</div></div>";
  document.body.appendChild(modal);
  document.getElementById("batchPrioCancel").onclick = () => {
    modal.remove();
  };
  modal.onclick = (e) => {
    if (e.target === modal) modal.remove();
  };
  document.getElementById("batchPrioConfirm").onclick = async () => {
    const prio = parseInt(document.getElementById("batchPrioSelect").value, 10);
    const selectedIds = Array.from(state.selectedCards);
    modal.remove();
    const result = await postBatchUpdate(selectedIds, { priority: prio });
    await handleBatchActionResult(result, {
      actionLabel: "Priority update",
      successMessage: "Priority updated",
      selectedCount: selectedIds.length,
    });
  };
}

export async function batchCloseSelected() {
  const existing = document.getElementById("batchCloseModal");
  if (existing) existing.remove();
  const count = state.selectedCards.size;
  const modal = document.createElement("div");
  modal.id = "batchCloseModal";
  modal.className = "fixed inset-0 bg-black/50 flex items-center justify-center z-50";
  modal.innerHTML =
    '<div class="rounded-lg p-4 w-72 shadow-xl" style="background:var(--surface-raised);border:1px solid var(--border-strong)">' +
    `<div class="text-sm mb-3" style="color:var(--text-primary)">Close ${count} issue${count !== 1 ? "s" : ""}?</div>` +
    '<div class="flex justify-end gap-2">' +
    '<button id="batchCloseCancel" class="text-xs bg-overlay bg-overlay-hover px-3 py-1.5 rounded">Cancel</button>' +
    '<button id="batchCloseConfirm" class="text-xs bg-red-600 text-white px-3 py-1.5 rounded hover:bg-red-700">Close All</button>' +
    "</div></div>";
  document.body.appendChild(modal);
  document.getElementById("batchCloseCancel").onclick = () => {
    modal.remove();
  };
  modal.onclick = (e) => {
    if (e.target === modal) modal.remove();
  };
  document.getElementById("batchCloseConfirm").onclick = async () => {
    const selectedIds = Array.from(state.selectedCards);
    modal.remove();
    const result = await postBatchClose(selectedIds);
    await handleBatchActionResult(result, {
      actionLabel: "Batch close",
      successMessage: `${count} issue${count !== 1 ? "s" : ""} closed`,
      selectedCount: selectedIds.length,
    });
  };
}

// ---------------------------------------------------------------------------
// Settings dropdown
// ---------------------------------------------------------------------------
export function toggleSettingsMenu(e) {
  e.stopPropagation();
  const dd = document.getElementById("settingsDropdown");
  if (dd.classList.contains("hidden")) {
    dd.classList.remove("hidden");
    setTimeout(() => {
      document.addEventListener("click", _settingsOutsideClick);
    }, 0);
  } else {
    closeSettingsMenu();
  }
}

export function _settingsOutsideClick(e) {
  const dd = document.getElementById("settingsDropdown");
  if (dd && !dd.contains(e.target) && e.target.id !== "settingsGear") closeSettingsMenu();
}

export function closeSettingsMenu() {
  document.getElementById("settingsDropdown").classList.add("hidden");
  document.removeEventListener("click", _settingsOutsideClick);
}

// ---------------------------------------------------------------------------
// Reload server
// ---------------------------------------------------------------------------
export async function reloadServer() {
  closeSettingsMenu();
  const ind = document.getElementById("refreshIndicator");
  ind.textContent = "Reloading...";
  ind.style.opacity = "1";
  try {
    const data = await postReload();
    ind.textContent = data.ok ? `Reloaded (${data.projects} projects)` : "Reload failed";
    ind.style.color = data.ok ? "" : "#EF4444";
    setTimeout(() => {
      ind.style.opacity = "0";
      ind.textContent = "Refreshing...";
      ind.style.color = "";
    }, 2000);
    if (data.ok && callbacks.fetchData) callbacks.fetchData();
  } catch (_e) {
    ind.textContent = "Reload failed";
    ind.style.color = "#EF4444";
    setTimeout(() => {
      ind.style.opacity = "0";
      ind.textContent = "Refreshing...";
      ind.style.color = "";
    }, 2000);
  }
}

// ---------------------------------------------------------------------------
// Theme toggle
// ---------------------------------------------------------------------------
export function toggleTheme() {
  const current = document.body.dataset.theme;
  const next = current === "light" ? "dark" : "light";
  document.body.dataset.theme = next;
  localStorage.setItem("filigree_theme", next);
  document.getElementById("themeToggle").innerHTML =
    `${next === "light" ? "&#9790;" : "&#9788;"} Toggle theme`;
  // Update JS color objects for light theme
  CATEGORY_COLORS.wip = next === "light" ? "#0284C7" : "#38BDF8";
  THEME_COLORS.textPrimary = next === "light" ? "#0F2027" : "#E2EEF2";
  THEME_COLORS.textSecondary = next === "light" ? "#3D6070" : "#8FAAB8";
  THEME_COLORS.graphOutline = next === "light" ? "#F0F6F8" : "#0B1215";
  THEME_COLORS.graphEdge = next === "light" ? "#9BBBC8" : "#2A4454";
  THEME_COLORS.accent = next === "light" ? "#0284C7" : "#38BDF8";
  // Re-render graphs if visible so they pick up new colors
  if (state.currentView === "graph" && callbacks.renderGraph) callbacks.renderGraph();
}

// ---------------------------------------------------------------------------
// Focus management
// ---------------------------------------------------------------------------
export function trapFocus(panel) {
  const focusable = panel.querySelectorAll("button, input, select, [tabindex]");
  if (focusable.length) focusable[0].focus();
}

// ---------------------------------------------------------------------------
// Legend toggles
// ---------------------------------------------------------------------------
export function toggleGraphLegend() {
  const legend = document.getElementById("graphLegend");
  legend.classList.toggle("hidden");
  const btn = document.getElementById("btnGraphLegend");
  btn.className = legend.classList.contains("hidden")
    ? "px-2 py-0.5 rounded bg-overlay bg-overlay-hover"
    : "px-2 py-0.5 rounded bg-accent text-primary";
}

export function toggleKanbanLegend() {
  const legend = document.getElementById("kanbanLegend");
  legend.classList.toggle("hidden");
  const btn = document.getElementById("btnKanbanLegend");
  btn.className = legend.classList.contains("hidden")
    ? "px-2 py-0.5 rounded bg-overlay bg-overlay-hover"
    : "px-2 py-0.5 rounded bg-accent text-primary";
}

// ---------------------------------------------------------------------------
// Issue creation modal
// ---------------------------------------------------------------------------
export async function showCreateForm() {
  const existing = document.getElementById("createModal");
  if (existing) existing.remove();
  const types = (await fetchTypes()) || [];
  const typeOpts = types
    .map(
      (t) =>
        `<option value="${t.type}"${t.type === "task" ? " selected" : ""}>${escHtml(t.display_name)}</option>`,
    )
    .join("");
  const modal = document.createElement("div");
  modal.id = "createModal";
  modal.className = "fixed inset-0 bg-black/50 flex items-center justify-center z-50";
  modal.innerHTML =
    '<div class="rounded-lg p-5 w-96 shadow-xl max-h-[80vh] overflow-y-auto" style="background:var(--surface-raised);border:1px solid var(--border-strong)">' +
    '<div class="text-sm font-semibold mb-3" style="color:var(--text-primary)">Create Issue</div>' +
    '<div class="flex flex-col gap-3">' +
    '<div><label for="createTitle" class="text-xs" style="color:var(--text-secondary)">Title *</label>' +
    '<input id="createTitle" type="text" class="w-full text-xs rounded px-3 py-2 mt-1 focus:outline-none" style="background:var(--surface-overlay);color:var(--text-primary);border:1px solid var(--border-strong)"></div>' +
    '<div class="flex gap-2">' +
    '<div class="flex-1"><label for="createType" class="text-xs" style="color:var(--text-secondary)">Type</label>' +
    `<select id="createType" class="w-full text-xs rounded px-2 py-2 mt-1" style="background:var(--surface-overlay);color:var(--text-primary);border:1px solid var(--border-strong)">${typeOpts}</select></div>` +
    '<div class="w-20"><label for="createPriority" class="text-xs" style="color:var(--text-secondary)">Priority</label>' +
    '<select id="createPriority" class="w-full text-xs rounded px-2 py-2 mt-1" style="background:var(--surface-overlay);color:var(--text-primary);border:1px solid var(--border-strong)">' +
    '<option value="0">P0 \u2014 Critical</option><option value="1">P1 \u2014 High</option><option value="2" selected>P2 \u2014 Medium</option><option value="3">P3 \u2014 Low</option><option value="4">P4 \u2014 Backlog</option>' +
    "</select></div></div>" +
    '<div class="text-xs -mt-1" style="color:var(--text-muted)">Type determines workflow states. Use P0\u2013P1 sparingly.</div>' +
    '<div><label for="createDesc" class="text-xs" style="color:var(--text-secondary)">Description</label>' +
    '<textarea id="createDesc" rows="3" class="w-full text-xs rounded px-3 py-2 mt-1 focus:outline-none resize-none" style="background:var(--surface-overlay);color:var(--text-primary);border:1px solid var(--border-strong)"></textarea></div>' +
    '<div><label for="createAssignee" class="text-xs" style="color:var(--text-secondary)">Assignee</label>' +
    '<input id="createAssignee" type="text" class="w-full text-xs rounded px-3 py-2 mt-1 focus:outline-none" style="background:var(--surface-overlay);color:var(--text-primary);border:1px solid var(--border-strong)"></div>' +
    '<div><label for="createLabels" class="text-xs" style="color:var(--text-secondary)">Labels (comma-separated)</label>' +
    '<input id="createLabels" type="text" class="w-full text-xs rounded px-3 py-2 mt-1 focus:outline-none" style="background:var(--surface-overlay);color:var(--text-primary);border:1px solid var(--border-strong)">' +
    '<div class="text-xs mt-0.5" style="color:var(--text-muted)">Example: ui, backend, urgent</div></div>' +
    "</div>" +
    '<div class="flex justify-end gap-2 mt-4">' +
    `<button onclick="document.getElementById('createModal').remove()" class="text-xs bg-overlay bg-overlay-hover px-3 py-1.5 rounded">Cancel</button>` +
    '<button onclick="submitCreateForm()" class="text-xs px-3 py-1.5 rounded bg-accent-hover" style="background:var(--accent);color:var(--surface-base)">Create</button>' +
    "</div></div>";
  document.body.appendChild(modal);
  modal.onclick = (e) => {
    if (e.target === modal) modal.remove();
  };
  document.getElementById("createTitle").focus();
}

export async function submitCreateForm() {
  const modalEl = document.getElementById("createModal");
  const title = document.getElementById("createTitle").value.trim();
  if (!title) {
    showToast("Title is required", "error");
    return;
  }
  const labelsRaw = document.getElementById("createLabels").value.trim();
  const labels = labelsRaw
    ? labelsRaw
        .split(",")
        .map((l) => l.trim())
        .filter(Boolean)
    : null;
  const body = {
    title,
    type: document.getElementById("createType").value,
    priority: parseInt(document.getElementById("createPriority").value, 10),
    description: document.getElementById("createDesc").value.trim(),
    assignee: document.getElementById("createAssignee").value.trim(),
  };
  if (labels?.length) body.labels = labels;
  const result = await postCreateIssue(body);
  if (!result.ok) {
    showToast(`Error: ${result.error || "Create failed"}`, "error");
    return;
  }
  const created = result.data;
  const findingFileId = modalEl?.dataset?.findingFileId || "";
  const findingId = modalEl?.dataset?.findingId || "";
  if (findingFileId && findingId) {
    const findingResult = await patchFileFinding(findingFileId, findingId, {
      status: "fixed",
      issue_id: created.id,
    });
    if (!findingResult.ok) {
      showToast(
        `Created ${created.id}, but failed to close finding: ${findingResult.error || "Unknown error"}`,
        "warning",
      );
    }
  }
  if (modalEl) modalEl.remove();
  showToast(`Created ${created.id}`, "success");
  if (callbacks.fetchData) await callbacks.fetchData();
  if (callbacks.openDetail) callbacks.openDetail(created.id);
}
