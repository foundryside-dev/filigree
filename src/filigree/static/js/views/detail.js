// ---------------------------------------------------------------------------
// Detail panel — issue detail view, actions, comments, dependencies.
// ---------------------------------------------------------------------------

import {
  deleteIssueDep,
  fetchIssueDetail,
  fetchIssueFiles,
  fetchSearch,
  fetchTransitions,
  patchIssue,
  postAddDependency,
  postClaimIssue,
  postCloseIssue,
  postComment,
  postReleaseIssue,
  postReopenIssue,
} from "../api.js";
import { updateHash } from "../router.js";
import { CATEGORY_COLORS, PRIORITY_COLORS, state, TYPE_ICONS } from "../state.js";
import { escHtml, escJsSingleAttr, issueIdChip, setLoading, showToast, trapFocus } from "../ui.js";

// --- Callbacks for functions not yet available at import time ---

export const callbacks = { fetchData: null, render: null };

export function detailReadinessBadgeHtml(issue, issueMap = state.issueMap) {
  const statusCat = issue.status_category || "open";
  const blockedBy = issue.blocked_by || [];
  const depDetails = issue.dep_details || {};
  const openBlockers = blockedBy.filter((bid) => {
    const blocker = depDetails[bid] || issueMap[bid];
    return !blocker || (blocker.status_category || "open") !== "done";
  });

  return statusCat === "open" && openBlockers.length === 0
    ? '<span class="text-xs bg-emerald-900/50 text-emerald-400 px-2 py-0.5 rounded">Ready</span>'
    : openBlockers.length > 0
      ? `<span class="text-xs bg-red-900/50 text-red-400 px-2 py-0.5 rounded">Blocked by ${openBlockers.length}</span>`
      : "";
}

export function shouldRenderDetailTransitions(issueId, selectedIssueId, container) {
  return Boolean(
    container && selectedIssueId === issueId && container.dataset?.issueId === issueId,
  );
}

// ---------------------------------------------------------------------------
// openDetail — fetch full issue detail and render panel
// ---------------------------------------------------------------------------

export async function openDetail(issueId) {
  if (state.selectedIssue && state.selectedIssue !== issueId)
    state.detailHistory.push(state.selectedIssue);
  state.selectedIssue = issueId;
  updateHash();
  const panel = document.getElementById("detailPanel");
  const header = document.getElementById("detailHeader");
  const content = document.getElementById("detailContent");

  header.innerHTML = "";
  content.innerHTML = '<div class="text-xs" style="color:var(--text-muted)">Loading...</div>';
  panel.classList.remove("translate-x-full");

  // Fetch full issue detail (includes events, comments, dep_details)
  let d = null;
  let eventsData = [];
  let commentsData = [];
  let issueFilesData = [];
  let staleBanner = "";
  try {
    const [detailData, filesData] = await Promise.all([
      fetchIssueDetail(issueId),
      fetchIssueFiles(issueId),
    ]);
    d = detailData;
    if (!d) throw new Error("Not found");
    eventsData = d.events || [];
    commentsData = d.comments || [];
    issueFilesData = Array.isArray(filesData) ? filesData : [];
  } catch (err) {
    console.warn("[detail] Failed to fetch issue detail, using cached data:", err);
    // Fall back to local data if detail endpoint fails
    d = state.issueMap[issueId];
    if (!d) {
      content.innerHTML = '<div class="text-red-400 text-xs">Issue not found</div>';
      return;
    }
    // Show stale-data banner so user knows they may be viewing outdated info
    staleBanner =
      '<div class="text-xs rounded px-3 py-2 mb-3" style="background:var(--surface-overlay);border:1px dashed var(--border-strong);color:var(--text-secondary)">Showing cached data \u2014 could not reach server.</div>';
  }

  const safeId = escJsSingleAttr(d.id);
  const htmlId = escHtml(d.id);
  const statusCat = d.status_category || "open";
  const statusColor = CATEGORY_COLORS[statusCat] || "#64748B";
  const prioColor = PRIORITY_COLORS[d.priority] || "#6B7280";
  const typeIcon = TYPE_ICONS[d.type] || "";
  const depDetails = d.dep_details || {};
  // Show "state (category)" when they differ, e.g., "fixing (wip)"
  let statusLabel = d.status;
  if (statusCat !== d.status) statusLabel = `${d.status} (${statusCat})`;

  const blockerHtml = (d.blocked_by || [])
    .map((bid) => {
      const det = depDetails[bid] || state.issueMap[bid];
      if (!det) return `<div class="text-xs" style="color:var(--text-muted)">${escHtml(bid)}</div>`;
      const detCat = det.status_category || "open";
      const sc = CATEGORY_COLORS[detCat] || "#64748B";
      const safeBid = escJsSingleAttr(bid);
      const safeIssueId = escJsSingleAttr(d.id);
      return (
        '<div class="flex items-center gap-2 text-xs">' +
        `<span class="w-2 h-2 rounded-full shrink-0" style="background:${sc}"></span>` +
        `<span class="cursor-pointer flex-1" style="color:var(--accent)" role="button" tabindex="0" aria-label="Open blocker ${escHtml(det.title.slice(0, 40))}" onclick="openDetail('${safeBid}')">${escHtml(det.title.slice(0, 40))}</span>` +
        `<span style="color:var(--text-muted)">${escHtml(det.status || "")}</span>` +
        `<button onclick="event.stopPropagation();removeDependency('${safeIssueId}','${safeBid}')" class="text-red-400 hover:text-red-300 ml-1" aria-label="Remove dependency" title="Remove dependency">&times;</button></div>`
      );
    })
    .join("");

  const blocksHtml = (d.blocks || [])
    .map((bid) => {
      const det = depDetails[bid] || state.issueMap[bid];
      if (!det) return `<div class="text-xs" style="color:var(--text-muted)">${escHtml(bid)}</div>`;
      const detCat = det.status_category || "open";
      const sc = CATEGORY_COLORS[detCat] || "#64748B";
      const safeBid = escJsSingleAttr(bid);
      return (
        `<div class="flex items-center gap-2 text-xs cursor-pointer" style="color:var(--accent)" role="button" tabindex="0" aria-label="Open downstream ${escHtml(det.title.slice(0, 40))}" onclick="openDetail('${safeBid}')">` +
        `<span class="w-2 h-2 rounded-full" style="background:${sc}"></span>` +
        `<span>${escHtml(det.title.slice(0, 40))}</span>` +
        `<span style="color:var(--text-muted)">${escHtml(det.status || "")}</span></div>`
      );
    })
    .join("");

  const eventsHtml = eventsData
    .slice(0, 15)
    .map(
      (e) =>
        '<div class="text-xs flex gap-2" style="color:var(--text-muted)">' +
        `<span class="shrink-0" style="color:var(--text-muted)">${e.created_at ? e.created_at.slice(5, 16) : ""}</span>` +
        `<span>${escHtml(e.event_type)}${e.new_value ? `: ${escHtml(e.new_value)}` : ""}</span></div>`,
    )
    .join("");

  const commentsHtml = commentsData
    .map(
      (c) =>
        '<div class="rounded p-2 text-xs mb-1" style="background:var(--surface-base)">' +
        `<div style="color:var(--text-secondary)" class="mb-1">${escHtml(c.author || "anonymous")} &middot; ${c.created_at ? c.created_at.slice(0, 16) : ""}</div>` +
        `<div style="color:var(--text-primary)">${escHtml(c.text)}</div></div>`,
    )
    .join("");
  const issueFilesHtml = issueFilesData
    .map((f) => {
      const safeFileId = escJsSingleAttr(f.file_id);
      const assoc = f.assoc_type
        ? ` <span style="color:var(--text-muted)">(${escHtml(f.assoc_type)})</span>`
        : "";
      const lang = f.file_language
        ? `<span class="ml-2 text-[11px]" style="color:var(--text-muted)">${escHtml(f.file_language)}</span>`
        : "";
      return (
        '<div class="flex items-center gap-2 text-xs rounded px-2 py-1 cursor-pointer bg-overlay-hover mb-1" ' +
        `onclick="switchView('files');setTimeout(()=>openFileDetail('${safeFileId}'),100)" role="button" tabindex="0" aria-label="Open file ${escHtml(f.file_path || f.file_id)}">` +
        `<span class="truncate flex-1" style="color:var(--accent)">${escHtml(f.file_path || f.file_id)}</span>` +
        assoc +
        lang +
        "</div>"
      );
    })
    .join("");

  const readyBadge = detailReadinessBadgeHtml(d);

  header.innerHTML =
    `<span class="text-xs">${issueIdChip(d.id)}</span>` +
    "<div>" +
    (state.detailHistory.length
      ? '<button onclick="detailBack()" class="text-muted text-primary-hover text-xs mr-2">&larr; Back</button>'
      : "") +
    '<button onclick="closeDetail()" class="text-muted text-primary-hover text-lg" aria-label="Close detail panel">&times;</button>' +
    "</div>";

  content.innerHTML =
    staleBanner +
    '<div class="flex items-center gap-2 mb-1">' +
    `<span class="text-lg">${typeIcon}</span>` +
    `<span class="text-lg font-semibold" style="color:var(--text-primary)">${escHtml(d.title)}</span>` +
    (d.type === "milestone" || d.type === "epic"
      ? `<button onclick="loadPlanView('${safeId}')" class="text-xs bg-overlay bg-overlay-hover px-2 py-1 rounded ml-2">View Plan</button>`
      : "") +
    "</div>" +
    '<div class="flex items-center gap-2 mb-4 flex-wrap">' +
    `<span class="text-xs px-2 py-0.5 rounded" style="background:${statusColor};color:white">${escHtml(statusLabel)}</span>` +
    `<span class="w-2 h-2 rounded-full" style="background:${prioColor}" title="P${d.priority}"></span>` +
    `<span class="text-xs" style="color:var(--text-secondary)">P${d.priority}</span>` +
    readyBadge +
    (d.assignee
      ? `<span class="text-xs" style="color:var(--text-secondary)">\u{1F464} ${escHtml(d.assignee)}</span>`
      : "") +
    "</div>" +
    (d.labels?.length
      ? '<div class="flex gap-1 mb-3 flex-wrap">' +
        d.labels
          .map(
            (l) =>
              `<span class="text-xs px-2 py-0.5 rounded" style="background:var(--surface-overlay);color:var(--text-primary)">${escHtml(l)}</span>`,
          )
          .join("") +
        "</div>"
      : "") +
    (d.description
      ? '<div class="mb-4"><div class="text-xs font-medium mb-1" style="color:var(--text-secondary)">Description</div>' +
        `<div class="text-xs leading-relaxed whitespace-pre-wrap" style="color:var(--text-primary)">${escHtml(d.description)}</div></div>`
      : "") +
    (d.notes
      ? '<div class="mb-4"><div class="text-xs font-medium mb-1" style="color:var(--text-secondary)">Notes</div>' +
        `<div class="text-xs leading-relaxed whitespace-pre-wrap" style="color:var(--text-primary)">${escHtml(d.notes)}</div></div>`
      : "") +
    ((d.blocked_by || []).length
      ? '<div class="mb-3"><div class="text-xs font-medium text-red-400 mb-1">Blocked by &larr;</div>' +
        blockerHtml +
        `<button onclick="showAddBlocker('${safeId}')" class="text-xs hover:underline mt-1" style="color:var(--accent)">+ Add blocker</button></div>`
      : `<div class="mb-3"><button onclick="showAddBlocker('${safeId}')" class="text-xs hover:underline" style="color:var(--accent)">+ Add blocker</button></div>`) +
    ((d.blocks || []).length
      ? '<div class="mb-3"><div class="text-xs font-medium mb-1" style="color:var(--accent)">Blocks \u2192</div>' +
        blocksHtml +
        "</div>"
      : "") +
    (issueFilesData.length
      ? '<div class="mb-3"><div class="text-xs font-medium mb-1" style="color:var(--text-secondary)">Associated Files</div>' +
        issueFilesHtml +
        "</div>"
      : "") +
    (commentsData.length
      ? '<div class="mb-3"><div class="text-xs font-medium mb-1" style="color:var(--text-secondary)">Comments</div>' +
        commentsHtml +
        "</div>"
      : "") +
    (eventsData.length
      ? '<div class="mb-3"><div class="text-xs font-medium mb-1" style="color:var(--text-secondary)">Timeline</div>' +
        eventsHtml +
        "</div>"
      : "") +
    // Actions section
    '<div class="mt-4 pt-3" style="border-top:1px solid var(--border-default)">' +
    '<div class="text-xs font-medium mb-2" style="color:var(--text-secondary)">Actions</div>' +
    `<div id="transitionBtns" data-issue-id="${htmlId}" class="flex flex-wrap gap-1 mb-2"></div>` +
    '<div class="flex gap-2 mb-2">' +
    '<label for="prioSelect" class="text-xs" style="color:var(--text-secondary)">Priority</label> ' +
    `<select id="prioSelect" onchange="updateIssue('${safeId}', {priority: parseInt(this.value)})" class="text-xs rounded px-2 py-1" style="background:var(--surface-overlay);color:var(--text-primary);border:1px solid var(--border-strong)">` +
    [0, 1, 2, 3, 4]
      .map((p) => `<option value="${p}"${d.priority === p ? " selected" : ""}>P${p}</option>`)
      .join("") +
    "</select>" +
    '<label for="assigneeInput" class="text-xs" style="color:var(--text-secondary)">Assignee</label> ' +
    `<input id="assigneeInput" type="text" placeholder="Assignee" value="${escHtml(d.assignee || "")}"` +
    ` onkeydown="if(event.key==='Enter')updateIssue('${safeId}',{assignee:this.value})"` +
    ' class="text-xs rounded px-2 py-1 flex-1" style="background:var(--surface-overlay);color:var(--text-primary);border:1px solid var(--border-strong)">' +
    "</div>" +
    (statusCat !== "done"
      ? `<button onclick="closeIssue('${safeId}')" class="text-xs bg-red-900/50 text-red-400 px-3 py-1 rounded border border-red-800 hover:bg-red-900 mb-2">Close</button>`
      : `<button onclick="reopenIssue('${safeId}')" class="text-xs bg-green-900/50 text-green-400 px-3 py-1 rounded border border-green-800 hover:bg-green-900 mb-2">Reopen</button>`) +
    (statusCat !== "done" && !d.assignee
      ? `<button onclick="claimIssue('${safeId}')" class="text-xs bg-emerald-900/50 text-emerald-400 px-3 py-1 rounded border border-emerald-800 hover:bg-emerald-900 mb-2 ml-1">Claim</button>`
      : "") +
    (d.assignee
      ? `<button onclick="releaseIssue('${safeId}')" class="text-xs bg-overlay bg-overlay-hover px-3 py-1.5 rounded mb-2 ml-1" style="color:var(--text-primary);border:1px solid var(--border-strong)">Release</button>`
      : "") +
    "</div>" +
    // Comment input
    '<div class="mt-3 pt-3" style="border-top:1px solid var(--border-default)">' +
    '<label for="commentInput" class="text-xs font-medium mb-1" style="color:var(--text-secondary)">Add Comment</label>' +
    '<div class="flex gap-1">' +
    `<input id="commentInput" type="text" placeholder="Comment..." onkeydown="if(event.key==='Enter')addComment('${safeId}')"` +
    ' class="text-xs rounded px-2 py-1 flex-1 focus:outline-none" style="background:var(--surface-overlay);color:var(--text-primary);border:1px solid var(--border-strong)">' +
    `<button onclick="addComment('${safeId}')" class="text-xs px-2 py-1 rounded bg-accent-hover" style="background:var(--accent);color:var(--surface-base)">Send</button>` +
    "</div>" +
    "</div>" +
    `<div class="mt-3 text-xs select-all" style="color:var(--text-muted)" title="Copy this command to view in terminal">filigree show ${escHtml(d.id)}</div>`;

  // Load transitions async and render buttons
  loadTransitions(issueId)
    .then((transitions) => {
      const container = document.getElementById("transitionBtns");
      if (
        !shouldRenderDetailTransitions(issueId, state.selectedIssue, container) ||
        !transitions ||
        !transitions.length
      )
        return;
      container.innerHTML = transitions
        .map((t) => {
          const btnStyle = t.ready
            ? "background:var(--accent);color:var(--surface-base)"
            : "background:var(--surface-overlay);color:var(--text-muted)";
          const cls = t.ready ? "" : "cursor-not-allowed";
          const missingText = t.missing_fields.length
            ? ` <span style="color:var(--text-muted)">(missing: ${t.missing_fields.map((f) => escHtml(f)).join(", ")})</span>`
            : "";
          return (
            `<button ${t.ready ? `onclick="updateIssue('${safeId}',{status:'${escJsSingleAttr(t.to)}'},this)"` : "disabled"}` +
            ` class="text-xs px-2 py-1 rounded ${cls}" style="${btnStyle}">` +
            `${escHtml(t.to)}${missingText}</button>`
          );
        })
        .join("");
    })
    .catch((err) => {
      const container = document.getElementById("transitionBtns");
      if (shouldRenderDetailTransitions(issueId, state.selectedIssue, container)) {
        console.error("[detail] Failed to load transitions:", err);
        container.innerHTML =
          '<span class="text-xs" style="color:var(--text-muted)">Could not load transitions</span>';
      }
    });

  trapFocus(document.getElementById("detailPanel"));
}

// ---------------------------------------------------------------------------
// closeDetail — slide panel off-screen
// ---------------------------------------------------------------------------

export function closeDetail() {
  state.selectedIssue = null;
  state.detailHistory = [];
  document.getElementById("detailHeader").innerHTML = "";
  document.getElementById("detailPanel").classList.add("translate-x-full");
  updateHash();
}

// ---------------------------------------------------------------------------
// detailBack — pop navigation history
// ---------------------------------------------------------------------------

export function detailBack() {
  if (state.detailHistory.length) {
    const prev = state.detailHistory.pop();
    state.selectedIssue = null; // prevent pushing to history again
    openDetail(prev);
  }
}

// ---------------------------------------------------------------------------
// updateIssue — PATCH issue with loading state
// ---------------------------------------------------------------------------

export async function updateIssue(issueId, body, btnEl) {
  if (btnEl) setLoading(btnEl, true);
  try {
    const result = await patchIssue(issueId, body);
    if (!result.ok) {
      showToast(`Error: ${result.error || "Update failed"}`, "error");
      return null;
    }
    if (callbacks.fetchData) await callbacks.fetchData();
    if (state.selectedIssue === issueId) openDetail(issueId);
    return result.data;
  } catch (_e) {
    showToast("Network error", "error");
    return null;
  } finally {
    if (btnEl) setLoading(btnEl, false);
  }
}

// ---------------------------------------------------------------------------
// closeIssue — close reason modal + API call
// ---------------------------------------------------------------------------

export async function closeIssue(issueId) {
  const existing = document.getElementById("closeReasonModal");
  if (existing) existing.remove();
  const modal = document.createElement("div");
  modal.id = "closeReasonModal";
  modal.className = "fixed inset-0 bg-black/50 flex items-center justify-center z-50";
  modal.innerHTML =
    '<div class="rounded-lg p-4 w-80 shadow-xl" style="background:var(--surface-raised);border:1px solid var(--border-strong)">' +
    '<div class="text-sm mb-2" style="color:var(--text-primary)">Close reason (optional)</div>' +
    '<input id="closeReasonInput" type="text" class="w-full text-xs rounded px-3 py-2 mb-3 focus:outline-none" style="background:var(--surface-overlay);color:var(--text-primary);border:1px solid var(--border-strong)">' +
    '<div class="flex justify-end gap-2">' +
    '<button id="closeReasonCancel" class="text-xs bg-overlay bg-overlay-hover px-3 py-1.5 rounded">Cancel</button>' +
    '<button id="closeReasonConfirm" class="text-xs bg-red-600 text-white px-3 py-1.5 rounded hover:bg-red-700">Close Issue</button>' +
    "</div></div>";
  document.body.appendChild(modal);
  document.getElementById("closeReasonInput").focus();
  document.getElementById("closeReasonCancel").onclick = () => {
    modal.remove();
  };
  modal.onclick = (e) => {
    if (e.target === modal) modal.remove();
  };
  document.getElementById("closeReasonConfirm").onclick = async () => {
    const reason = document.getElementById("closeReasonInput").value || "";
    modal.remove();
    const result = await postCloseIssue(issueId, reason);
    if (!result.ok) {
      showToast(`Error: ${result.error || "Close failed"}`, "error");
      return;
    }
    showToast("Issue closed", "success");
    if (callbacks.fetchData) await callbacks.fetchData();
    if (state.selectedIssue === issueId) openDetail(issueId);
  };
}

// ---------------------------------------------------------------------------
// reopenIssue — reopen API call
// ---------------------------------------------------------------------------

export async function reopenIssue(issueId) {
  const result = await postReopenIssue(issueId);
  if (!result.ok) {
    showToast(`Error: ${result.error || "Reopen failed"}`, "error");
    return;
  }
  if (callbacks.fetchData) await callbacks.fetchData();
  if (state.selectedIssue === issueId) openDetail(issueId);
}

// ---------------------------------------------------------------------------
// claimIssue — claim modal
// ---------------------------------------------------------------------------

export async function claimIssue(issueId) {
  const safeIssueId = escJsSingleAttr(issueId);
  const existing = document.getElementById("claimModal");
  if (existing) existing.remove();
  const saved = localStorage.getItem("filigree_claim_name") || "";
  const modal = document.createElement("div");
  modal.id = "claimModal";
  modal.className = "fixed inset-0 bg-black/50 flex items-center justify-center z-50";
  modal.innerHTML =
    '<div class="rounded-lg p-4 w-72 shadow-xl" style="background:var(--surface-raised);border:1px solid var(--border-strong)">' +
    '<div class="text-sm mb-2" style="color:var(--text-primary)">Claim issue</div>' +
    `<input id="claimNameInput" type="text" value="${escHtml(saved)}" placeholder="Your name..." class="w-full text-xs rounded px-3 py-2 mb-1 focus:outline-none" style="background:var(--surface-overlay);color:var(--text-primary);border:1px solid var(--border-strong)">` +
    (saved
      ? `<div class="text-xs mb-2" style="color:var(--text-muted)">Remembered as "${escHtml(saved)}" \u2014 <button onclick="document.getElementById('claimNameInput').value='';localStorage.removeItem('filigree_claim_name');this.parentElement.remove();" style="color:var(--accent)" class="hover:underline">not you?</button></div>`
      : '<div class="mb-2"></div>') +
    '<div class="flex justify-end gap-2">' +
    `<button onclick="document.getElementById('claimModal').remove()" class="text-xs bg-overlay bg-overlay-hover px-3 py-1.5 rounded">Cancel</button>` +
    `<button onclick="confirmClaim('${safeIssueId}')" class="text-xs bg-emerald-600 text-white px-3 py-1.5 rounded hover:bg-emerald-700">Claim</button>` +
    "</div></div>";
  document.body.appendChild(modal);
  modal.onclick = (e) => {
    if (e.target === modal) modal.remove();
  };
  const inp = document.getElementById("claimNameInput");
  inp.focus();
  inp.addEventListener("keydown", (e) => {
    if (e.key === "Enter") confirmClaim(issueId);
  });
}

// ---------------------------------------------------------------------------
// confirmClaim — confirm claim after modal input
// ---------------------------------------------------------------------------

export async function confirmClaim(issueId) {
  const name = document.getElementById("claimNameInput").value.trim();
  if (!name) {
    showToast("Name is required", "error");
    return;
  }
  localStorage.setItem("filigree_claim_name", name);
  const modal = document.getElementById("claimModal");
  if (modal) modal.remove();
  const result = await postClaimIssue(issueId, name);
  if (!result.ok) {
    showToast(`Error: ${result.error || "Claim failed"}`, "error");
    return;
  }
  showToast(`Claimed by ${name}`, "success");
  if (callbacks.fetchData) await callbacks.fetchData();
  if (state.selectedIssue === issueId) openDetail(issueId);
}

// ---------------------------------------------------------------------------
// releaseIssue — release API call
// ---------------------------------------------------------------------------

export async function releaseIssue(issueId) {
  const result = await postReleaseIssue(issueId);
  if (!result.ok) {
    showToast(`Error: ${result.error || "Release failed"}`, "error");
    return;
  }
  showToast("Issue released", "success");
  if (callbacks.fetchData) await callbacks.fetchData();
  if (state.selectedIssue === issueId) openDetail(issueId);
}

// ---------------------------------------------------------------------------
// moveIssueTo — move via drag/transition
// ---------------------------------------------------------------------------

export async function moveIssueTo(issueId, targetStatus) {
  const modal = document.getElementById("moveModal");
  if (modal) modal.remove();
  const result = await patchIssue(issueId, { status: targetStatus });
  if (!result.ok) {
    showToast(`Error: ${result.error || "Move failed"}`, "error");
    return;
  }
  showToast(`Moved to ${targetStatus}`, "success");
  if (callbacks.fetchData) await callbacks.fetchData();
  if (state.selectedIssue === issueId) openDetail(issueId);
}

// ---------------------------------------------------------------------------
// removeDependency — remove dep
// ---------------------------------------------------------------------------

export async function removeDependency(issueId, depId) {
  const result = await deleteIssueDep(issueId, depId);
  if (!result.ok) {
    showToast(`Error: ${result.error || "Remove failed"}`, "error");
    return;
  }
  showToast("Dependency removed", "success");
  if (callbacks.fetchData) await callbacks.fetchData();
  if (state.selectedIssue === issueId) openDetail(issueId);
}

// ---------------------------------------------------------------------------
// showAddBlocker — add blocker modal with search
// ---------------------------------------------------------------------------

export async function showAddBlocker(issueId) {
  const safeIssueId = escJsSingleAttr(issueId);
  const existing = document.getElementById("addBlockerModal");
  if (existing) existing.remove();
  const modal = document.createElement("div");
  modal.id = "addBlockerModal";
  modal.className = "fixed inset-0 bg-black/50 flex items-center justify-center z-50";
  modal.innerHTML =
    '<div class="rounded-lg p-4 w-80 shadow-xl" style="background:var(--surface-raised);border:1px solid var(--border-strong)">' +
    '<div class="text-sm mb-2" style="color:var(--text-primary)">Add Blocker</div>' +
    '<input id="blockerSearchInput" type="text" placeholder="Search issues..." ' +
    'class="w-full text-xs rounded px-3 py-2 mb-2 focus:outline-none" style="background:var(--surface-overlay);color:var(--text-primary);border:1px solid var(--border-strong)">' +
    '<div id="blockerResults" class="max-h-48 overflow-y-auto text-xs"></div>' +
    `<button onclick="document.getElementById('addBlockerModal').remove()" class="text-xs text-muted text-primary-hover mt-2">Cancel</button>` +
    "</div>";
  document.body.appendChild(modal);
  modal.onclick = (e) => {
    if (e.target === modal) modal.remove();
  };
  const input = document.getElementById("blockerSearchInput");
  input.focus();
  let searchTimeout = null;
  input.oninput = () => {
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(async () => {
      const q = input.value.trim();
      const results = document.getElementById("blockerResults");
      if (!q) {
        results.innerHTML = "";
        return;
      }
      try {
        const data = await fetchSearch(q, 10);
        if (!data) {
          results.innerHTML = '<div class="text-red-400">Search failed</div>';
          return;
        }
        results.innerHTML =
          data.results
            .filter((r) => r.id !== issueId)
            .map(
              (r) =>
                `<div class="flex items-center gap-2 py-1 px-1 cursor-pointer bg-overlay-hover rounded" onclick="addDependency('${safeIssueId}','${escJsSingleAttr(r.id)}')">` +
                `<span style="color:var(--text-secondary)">${escHtml(r.id)}</span>` +
                `<span style="color:var(--text-primary)" class="truncate">${escHtml(r.title.slice(0, 30))}</span></div>`,
            )
            .join("") || '<div style="color:var(--text-muted)">No results</div>';
      } catch (_e) {
        results.innerHTML = '<div class="text-red-400">Search failed</div>';
      }
    }, 200);
  };
}

// ---------------------------------------------------------------------------
// addDependency — add dep
// ---------------------------------------------------------------------------

export async function addDependency(issueId, dependsOnId) {
  const modal = document.getElementById("addBlockerModal");
  if (modal) modal.remove();
  const result = await postAddDependency(issueId, dependsOnId);
  if (!result.ok) {
    showToast(`Error: ${result.error || "Add failed"}`, "error");
    return;
  }
  showToast("Dependency added", "success");
  if (callbacks.fetchData) await callbacks.fetchData();
  if (state.selectedIssue === issueId) openDetail(issueId);
}

// ---------------------------------------------------------------------------
// addComment — post comment
// ---------------------------------------------------------------------------

export async function addComment(issueId) {
  const input = document.getElementById("commentInput");
  const text = input ? input.value.trim() : "";
  if (!text) return;
  const result = await postComment(issueId, text);
  if (result.ok) {
    input.value = "";
    openDetail(issueId);
  } else {
    showToast(`Error: ${result.error || "Comment failed"}`, "error");
  }
}

// ---------------------------------------------------------------------------
// loadTransitions — wrapper around fetchTransitions
// ---------------------------------------------------------------------------

export async function loadTransitions(issueId) {
  return fetchTransitions(issueId);
}
