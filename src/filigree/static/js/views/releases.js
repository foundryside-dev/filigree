// ---------------------------------------------------------------------------
// Releases view — release roadmap, progress tracking, and tree expansion.
// ---------------------------------------------------------------------------

import { fetchReleases, fetchReleaseTree } from "../api.js";
import { escHtml, escJsSingle } from "../ui.js";

// --- Module-level state ---

let expandedReleaseIds = new Set();
let releaseTreeCache = new Map();
let expandedNodeIds = new Set();
let showReleased = false;
let loadingReleaseIds = new Set();
let errorReleaseIds = new Set();
let _pendingFocusTarget = null;

export function scrollToReleaseCard(cardId) {
  const el = document.getElementById(cardId);
  if (!el) return;
  el.scrollIntoView({ behavior: "smooth", block: "center" });
  el.classList.remove("changed-flash");
  void el.offsetWidth;
  el.classList.add("changed-flash");
}
// scrollToReleaseCard is exported and registered on window by app.js

// --- Color helpers ---

function statusBorderColor(status) {
  switch (status) {
    case "planning":
      return "var(--accent)";
    case "development":
    case "frozen":
    case "testing":
    case "staged":
      return document.body.dataset.theme === "light" ? "#B45309" : "#F59E0B";
    case "released":
      return "#10B981";
    case "cancelled":
    case "rolled_back":
    case "retired":
      return "#EF4444";
    default:
      return "var(--border-default)";
  }
}

function statusBadge(status) {
  let style;
  switch (status) {
    case "planning":
      style = "background:var(--accent);color:var(--surface-base)";
      break;
    case "development":
    case "frozen":
    case "testing":
    case "staged":
      style = "background:#F59E0B;color:#000";
      break;
    case "released":
      style = "background:#10B981;color:#fff";
      break;
    case "cancelled":
    case "rolled_back":
    case "retired":
      style = "background:#EF4444;color:#fff";
      break;
    default:
      style = "background:var(--surface-overlay);color:var(--text-secondary)";
      break;
  }
  return (
    '<span class="text-xs rounded px-1.5 py-0.5" style="' +
    style +
    '">' +
    escHtml(status || "open") +
    "</span>"
  );
}

// --- Progress bar rendering ---

function renderProgressBar(pct, name) {
  const safeName = escHtml(name);
  const clampedPct = Math.max(0, Math.min(100, pct));
  return (
    '<div role="progressbar" aria-valuenow="' +
    clampedPct +
    '" aria-valuemin="0" aria-valuemax="100" ' +
    'aria-label="' +
    safeName +
    " progress: " +
    clampedPct +
    '%" ' +
    'style="background:var(--surface-base);height:8px;border-radius:4px;flex:1;min-width:60px;max-width:120px">' +
    '<div style="width:' +
    clampedPct +
    '%;height:100%;background:var(--accent);border-radius:4px"></div>' +
    "</div>"
  );
}

// --- Tree rendering ---

function renderTreeNode(node, level, releaseId) {
  const maxLevel = Math.min(level, 3);
  const indent = maxLevel * 24; // 24px per level (ml-6 equivalent)
  const nodeId = node.issue.id;
  const safeId = escJsSingle(nodeId);
  const isLeaf = !node.children || node.children.length === 0;
  const isCollapsed = !expandedNodeIds.has(nodeId);
  const hasChildren = !isLeaf;
  const pct = node.progress?.pct ?? 0;

  let html = "";
  html += '<li role="treeitem" aria-level="' + (maxLevel + 1) + '"';
  if (hasChildren) {
    html += ' aria-expanded="' + !isCollapsed + '"';
  }
  html += ' tabindex="-1"';
  html += ' data-node-id="' + escHtml(nodeId) + '"';
  html += ' data-release-id="' + escHtml(releaseId) + '"';
  html += ' data-has-children="' + hasChildren + '"';
  html += ' style="list-style:none;padding-left:' + indent + "px;";
  if (level > 0) {
    html +=
      "border-left:1px solid var(--border-default);margin-left:" + (maxLevel - 1) * 24 + "px;";
  }
  html += '"';
  html += ' class="py-1 flex items-center gap-2">';

  // Toggle or leaf indicator
  if (hasChildren) {
    const arrow = isCollapsed ? "\u25B6" : "\u25BC";
    html +=
      '<button class="text-xs flex items-center justify-center cursor-pointer" ' +
      'style="width:44px;height:44px;min-width:44px;min-height:44px;background:none;border:none;color:var(--text-secondary)" ' +
      "onclick=\"event.stopPropagation();window._toggleReleaseTreeNode('" +
      safeId +
      "','" +
      escJsSingle(releaseId) +
      "')\" " +
      'aria-label="' +
      (isCollapsed ? "Expand" : "Collapse") +
      " " +
      escHtml(node.issue.title || nodeId) +
      '">' +
      arrow +
      "</button>";
  } else {
    // Leaf — status badge inline
    html +=
      '<span style="width:44px;min-width:44px;display:inline-flex;align-items:center;justify-content:center">' +
      statusBadge(node.issue.status || "") +
      "</span>";
  }

  // Title (clickable)
  html +=
    '<button class="cursor-pointer hover:underline text-xs text-left" ' +
    'style="color:var(--text-primary);background:none;border:none;padding:0" ' +
    "onclick=\"window.openDetail('" +
    safeId +
    "')\">" +
    escHtml(node.issue.title || nodeId) +
    "</button>";

  // Progress bar for non-leaf nodes
  if (hasChildren) {
    html += " " + renderProgressBar(pct, node.issue.title || nodeId);
    html += ' <span class="text-xs" style="color:var(--text-muted)">' + pct + "%</span>";
  }

  // Render children if expanded (INSIDE the li)
  if (hasChildren && !isCollapsed) {
    html += '<ul role="group">';
    for (const child of node.children) {
      html += renderTreeNode(child, level + 1, releaseId);
    }
    html += "</ul>";
  }

  html += "</li>";

  return html;
}

function collectTreeNodeIds(node, ids) {
  ids.add(node.issue.id);
  if (node.children) {
    for (const child of node.children) {
      collectTreeNodeIds(child, ids);
    }
  }
}

function drainStaleExpandedIds(tree) {
  const validIds = new Set();
  if (tree.children) {
    for (const child of tree.children) {
      collectTreeNodeIds(child, validIds);
    }
  }
  for (const id of expandedNodeIds) {
    if (!validIds.has(id)) expandedNodeIds.delete(id);
  }
}

function renderTree(tree, releaseId) {
  if (!tree || !tree.children || tree.children.length === 0) {
    return '<div class="text-xs py-2" style="color:var(--text-muted)">No child items.</div>';
  }

  let html = "";
  html += '<div class="flex items-center gap-2 mb-2">';
  html +=
    '<button class="text-xs px-2 py-1 rounded bg-overlay bg-overlay-hover" ' +
    'style="min-height:44px" ' +
    "onclick=\"window._collapseAllReleaseTree('" +
    escJsSingle(releaseId) +
    "')\">Collapse all</button>";
  html += "</div>";

  html += '<ul role="tree" data-release-tree="' + escHtml(releaseId) + '">';
  for (const child of tree.children) {
    html += renderTreeNode(child, 0, releaseId);
  }
  html += "</ul>";

  return html;
}

// --- Keyboard navigation for tree ---

function setupTreeKeyboard(container) {
  const treeRoots = container.querySelectorAll('[role="tree"]');
  treeRoots.forEach((tree) => {
    tree.addEventListener("keydown", handleTreeKeydown);
    // Initialize roving tabindex — first item is the tab entry point
    const firstItem = tree.querySelector('[role="treeitem"]');
    if (firstItem) {
      firstItem.setAttribute("tabindex", "0");
    }
  });
}

function getVisibleTreeItems(tree) {
  return Array.from(tree.querySelectorAll('[role="treeitem"]')).filter(
    (el) => el.offsetParent !== null,
  );
}

function handleTreeKeydown(e) {
  const tree = e.currentTarget;
  const items = getVisibleTreeItems(tree);
  if (!items.length) return;

  const current = document.activeElement;
  let idx = items.indexOf(current);
  if (idx < 0) idx = 0;

  switch (e.key) {
    case "ArrowDown": {
      e.preventDefault();
      const next = Math.min(idx + 1, items.length - 1);
      setTreeFocus(items, next);
      break;
    }
    case "ArrowUp": {
      e.preventDefault();
      const prev = Math.max(idx - 1, 0);
      setTreeFocus(items, prev);
      break;
    }
    case "ArrowRight": {
      e.preventDefault();
      const item = items[idx];
      if (!item) break;
      const hasChildren = item.dataset.hasChildren === "true";
      const expanded = item.getAttribute("aria-expanded") === "true";
      if (hasChildren && !expanded) {
        // Expand
        const nodeId = item.dataset.nodeId;
        const releaseId = item.dataset.releaseId;
        if (nodeId && releaseId) {
          window._toggleReleaseTreeNode(nodeId, releaseId);
        }
      } else if (hasChildren && expanded) {
        // Move to first child
        const group = item.querySelector('[role="group"]');
        if (group) {
          const firstChild = group.querySelector('[role="treeitem"]');
          if (firstChild) {
            const newItems = getVisibleTreeItems(tree);
            const newIdx = newItems.indexOf(firstChild);
            if (newIdx >= 0) setTreeFocus(newItems, newIdx);
          }
        }
      }
      break;
    }
    case "ArrowLeft": {
      e.preventDefault();
      const item = items[idx];
      if (!item) break;
      const hasChildren = item.dataset.hasChildren === "true";
      const expanded = item.getAttribute("aria-expanded") === "true";
      if (hasChildren && expanded) {
        // Collapse
        const nodeId = item.dataset.nodeId;
        const releaseId = item.dataset.releaseId;
        if (nodeId && releaseId) {
          window._toggleReleaseTreeNode(nodeId, releaseId);
        }
      } else {
        // Go to parent
        const level = parseInt(item.getAttribute("aria-level"), 10);
        if (level > 1) {
          for (let i = idx - 1; i >= 0; i--) {
            const parentLevel = parseInt(items[i].getAttribute("aria-level"), 10);
            if (parentLevel < level) {
              setTreeFocus(items, i);
              break;
            }
          }
        }
      }
      break;
    }
    case "Home": {
      e.preventDefault();
      setTreeFocus(items, 0);
      break;
    }
    case "End": {
      e.preventDefault();
      setTreeFocus(items, items.length - 1);
      break;
    }
    case "Enter":
    case " ": {
      e.preventDefault();
      const item = items[idx];
      if (!item) break;
      const hasChildren = item.dataset.hasChildren === "true";
      if (hasChildren) {
        const nodeId = item.dataset.nodeId;
        const releaseId = item.dataset.releaseId;
        if (nodeId && releaseId) {
          window._toggleReleaseTreeNode(nodeId, releaseId);
        }
      } else {
        const nodeId = item.dataset.nodeId;
        if (nodeId) window.openDetail(nodeId);
      }
      break;
    }
  }
}

function setTreeFocus(items, idx) {
  items.forEach((el) => el.setAttribute("tabindex", "-1"));
  if (items[idx]) {
    items[idx].setAttribute("tabindex", "0");
    items[idx].focus();
  }
}

// --- Child summary formatting ---

function formatChildSummary(summary) {
  if (!summary) return "";
  const parts = [];
  if (summary.epics) parts.push(summary.epics + (summary.epics === 1 ? " epic" : " epics"));
  if (summary.milestones)
    parts.push(summary.milestones + (summary.milestones === 1 ? " milestone" : " milestones"));
  if (summary.tasks) parts.push(summary.tasks + (summary.tasks === 1 ? " task" : " tasks"));
  if (summary.bugs) parts.push(summary.bugs + (summary.bugs === 1 ? " bug" : " bugs"));
  if (summary.other) parts.push(summary.other + " other");
  return parts.join(", ");
}

function formatTargetDate(isoDate) {
  if (!isoDate) return "";
  const target = new Date(isoDate);
  if (isNaN(target.getTime())) return escHtml(isoDate);
  const now = new Date();
  const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const targetStart = new Date(target.getFullYear(), target.getMonth(), target.getDate());
  const diffDays = Math.round((targetStart - todayStart) / (1000 * 60 * 60 * 24));

  const formatted = target.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
  let relative;
  let color = "var(--text-muted)";

  if (diffDays < 0) {
    const absDays = Math.abs(diffDays);
    relative = absDays === 1 ? "1 day overdue" : absDays + " days overdue";
    color = "#EF4444";
  } else if (diffDays === 0) {
    relative = "today";
    color = "#F59E0B";
  } else if (diffDays <= 7) {
    relative = diffDays === 1 ? "tomorrow" : "in " + diffDays + " days";
    color = "#F59E0B";
  } else {
    relative = "in " + diffDays + " days";
  }

  return (
    '<span style="color:' +
    color +
    '">Target: ' +
    escHtml(formatted) +
    " (" +
    escHtml(relative) +
    ")</span>"
  );
}

// --- Blocker link rendering ---

function renderBlockerLink(ref) {
  const safeId = escJsSingle(ref.id);
  if (ref.type === "release") {
    // Release blockers scroll to the card on this page, falling back to detail view
    return (
      '<a href="#" class="hover:underline" style="color:var(--accent)" onclick="event.preventDefault();' +
      "var el=document.getElementById('release-card-" +
      safeId +
      "');" +
      "if(el){window._scrollToReleaseCard('release-card-" +
      safeId +
      "')}" +
      "else{window.openDetail('" +
      safeId +
      "')}\">" +
      escHtml(ref.title) +
      "</a>"
    );
  }
  // Non-release blockers (milestones, tasks, etc.) open the issue detail panel
  return (
    '<a href="#" class="hover:underline" style="color:var(--accent)" onclick="event.preventDefault();window.openDetail(\'' +
    safeId +
    "')\">" +
    escHtml(ref.title) +
    "</a>"
  );
}

// --- Card rendering ---

function renderReleaseCard(release) {
  const borderColor = statusBorderColor(release.status);
  const isExpanded = expandedReleaseIds.has(release.id);
  const isLoading = loadingReleaseIds.has(release.id);
  const isBlocked = release.blocked_by && release.blocked_by.length > 0;
  const safeId = escJsSingle(release.id);
  const pct = release.progress?.pct ?? 0;
  const textColor = isBlocked ? "color:var(--text-secondary)" : "color:var(--text-primary)";

  let html = "";
  html +=
    '<div class="rounded mb-3" role="article" aria-label="' +
    escHtml(release.title || release.id) +
    '" style="background:var(--surface-raised);border:1px solid var(--border-default);border-left:4px solid ' +
    borderColor +
    '" id="release-card-' +
    escHtml(release.id) +
    '">';
  html += '<div class="p-4">';

  // Header row: toggle + title + status badge
  html += '<div class="flex items-center gap-2 mb-2">';

  // Expand toggle
  const arrow = isExpanded ? "\u25BC" : "\u25B6";
  html +=
    '<button class="text-xs flex items-center justify-center cursor-pointer" ' +
    'style="width:44px;height:44px;min-width:44px;min-height:44px;background:none;border:none;color:var(--text-secondary)" ' +
    "onclick=\"window._toggleReleaseExpand('" +
    safeId +
    "')\" " +
    'aria-label="' +
    (isExpanded ? "Collapse" : "Expand") +
    " release " +
    escHtml(release.title || release.id) +
    '">' +
    arrow +
    "</button>";

  // Title (clickable)
  html +=
    '<button class="cursor-pointer hover:underline text-sm font-medium flex-1 text-left" style="' +
    textColor +
    ';background:none;border:none;padding:0" ' +
    "onclick=\"window.openDetail('" +
    safeId +
    "')\">" +
    escHtml(release.title || release.id) +
    "</button>";

  // Status badge
  html += statusBadge(release.status);

  // Blocked badge
  if (isBlocked) {
    html +=
      ' <span class="text-xs rounded px-1.5 py-0.5 shrink-0" style="background:#EF4444;color:#fff" aria-label="blocked">blocked</span>';
  }

  html += "</div>";

  // Stats row
  html += '<div class="flex flex-wrap gap-2 items-center text-xs" style="color:var(--text-muted)">';
  html += "<span>P" + (release.priority != null ? release.priority : "?") + "</span>";

  const summaryText = formatChildSummary(release.child_summary);
  if (summaryText) {
    html += "<span>" + escHtml(summaryText) + "</span>";
  }

  html += renderProgressBar(pct, release.title || release.id);
  html += "<span>" + pct + "%</span>";
  html += "</div>";

  // Target date
  if (release.target_date) {
    html += '<div class="text-xs mt-1">' + formatTargetDate(release.target_date) + "</div>";
  }

  // Blocks / Blocked by links
  if (release.blocks && release.blocks.length > 0) {
    html += '<div class="text-xs mt-1" style="color:var(--text-muted)">Blocks: ';
    html += release.blocks.map((b) => renderBlockerLink(b)).join(", ");
    html += "</div>";
  }

  if (release.blocked_by && release.blocked_by.length > 0) {
    html += '<div class="text-xs mt-1" style="color:var(--text-muted)">Blocked by: ';
    html += release.blocked_by.map((b) => renderBlockerLink(b)).join(", ");
    html += "</div>";
  }

  // Expanded tree area
  if (isExpanded) {
    html += '<div class="mt-3 pt-3" style="border-top:1px solid var(--border-default)">';
    if (isLoading) {
      html +=
        '<div class="text-xs py-2" style="color:var(--text-muted)" role="status">Loading tree...</div>';
    } else if (errorReleaseIds.has(release.id)) {
      html +=
        '<div class="text-xs py-2 flex items-center gap-2" style="color:var(--text-muted)">' +
        "Failed to load release tree." +
        ' <button class="text-xs px-2 py-1 rounded cursor-pointer" ' +
        'style="background:var(--surface-overlay);color:var(--accent);border:1px solid var(--border-default);min-height:28px" ' +
        "onclick=\"window._retryReleaseTree('" +
        safeId +
        "')\">Retry</button>" +
        "</div>";
    } else {
      const tree = releaseTreeCache.get(release.id);
      if (tree) {
        html += renderTree(tree, release.id);
      } else {
        html +=
          '<div class="text-xs py-2" style="color:var(--text-muted)">No tree data available.</div>';
      }
    }
    html += "</div>";
  }

  html += "</div>";
  html += "</div>";

  return html;
}

// --- Expand/collapse handlers (exported, registered on window by app.js) ---

export async function toggleReleaseExpand(releaseId) {
  if (expandedReleaseIds.has(releaseId)) {
    expandedReleaseIds.delete(releaseId);
    // Collapse doesn't need focus management, so fire-and-forget is fine.
    loadReleases();
    return;
  }

  expandedReleaseIds.add(releaseId);
  loadingReleaseIds.add(releaseId);
  loadReleases(); // Re-render to show loading state

  try {
    const tree = await fetchReleaseTree(releaseId);
    if (tree) {
      releaseTreeCache.set(releaseId, tree);
      errorReleaseIds.delete(releaseId);
      drainStaleExpandedIds(tree);
    }
  } catch (_e) {
    errorReleaseIds.add(releaseId);
  } finally {
    loadingReleaseIds.delete(releaseId);
    _pendingFocusTarget = releaseId;
    await loadReleases();

    // Focus the first treeitem inside the expanded release card
    const card = document.getElementById("release-card-" + releaseId);
    if (card) {
      const firstItem = card.querySelector('[role="treeitem"]');
      if (firstItem) {
        firstItem.setAttribute("tabindex", "0");
        firstItem.focus();
      }
    }
    _pendingFocusTarget = null;
  }
}

export async function retryReleaseTree(releaseId) {
  errorReleaseIds.delete(releaseId);
  loadingReleaseIds.add(releaseId);
  loadReleases(); // Re-render to show loading state

  try {
    const tree = await fetchReleaseTree(releaseId);
    if (tree) {
      releaseTreeCache.set(releaseId, tree);
      drainStaleExpandedIds(tree);
    }
  } catch (_e) {
    errorReleaseIds.add(releaseId);
  } finally {
    loadingReleaseIds.delete(releaseId);
    loadReleases();
  }
}

export function toggleReleaseTreeNode(nodeId, releaseId) {
  if (expandedNodeIds.has(nodeId)) {
    expandedNodeIds.delete(nodeId);
  } else {
    expandedNodeIds.add(nodeId);
  }
  loadReleases();
}

export function collapseAllReleaseTree(releaseId) {
  const tree = releaseTreeCache.get(releaseId);
  if (!tree || !tree.children) return;

  function removeAll(node) {
    expandedNodeIds.delete(node.issue.id);
    if (node.children) {
      for (const child of node.children) {
        removeAll(child);
      }
    }
  }

  for (const child of tree.children) {
    removeAll(child);
  }
  loadReleases();
}

// --- Main loader ---

export async function loadReleases() {
  const container = document.getElementById("releasesContent");
  if (!container) return;

  // Sync checkbox state
  const checkbox = document.getElementById("showReleased");
  if (checkbox) {
    checkbox.checked = showReleased;
    // Wire up onchange (idempotent)
    checkbox.onchange = function () {
      showReleased = this.checked;
      loadReleases();
    };
  }

  // Save scroll position
  const scrollParent = container.closest(".overflow-y-auto");
  const scrollTop = scrollParent ? scrollParent.scrollTop : 0;

  // If we already have data in the cache for expanded releases, render immediately
  // but still fetch fresh data in the background
  const data = await fetchReleases(showReleased);

  if (!data) {
    container.innerHTML = '<div class="text-red-400">Failed to load releases.</div>';
    return;
  }

  const releases = data.releases || [];

  if (!releases.length) {
    container.innerHTML =
      '<div class="p-6 text-center" style="color:var(--text-muted)">' +
      '<div class="font-medium mb-2" style="color:var(--text-primary)">No active releases.</div>' +
      "<div>Use the \u201CShow released &amp; cancelled\u201D checkbox above to view release history.</div></div>";
    return;
  }

  // Re-fetch trees for expanded releases (in parallel)
  const expandedFetches = [];
  for (const id of expandedReleaseIds) {
    if (!loadingReleaseIds.has(id)) {
      expandedFetches.push(
        fetchReleaseTree(id)
          .then((tree) => {
            if (tree) {
              releaseTreeCache.set(id, tree);
              drainStaleExpandedIds(tree);
            }
          })
          .catch(() => {
            /* best-effort */
          }),
      );
    }
  }

  if (expandedFetches.length > 0) {
    await Promise.all(expandedFetches);
  }

  // Render cards
  let html = "";
  for (const release of releases) {
    html += renderReleaseCard(release);
  }

  // Capture focused element identity before DOM replacement
  const focusNodeId = document.activeElement?.dataset?.nodeId || null;
  let focusCardId = document.activeElement?.closest?.('[id^="release-card-"]')?.id || null;

  container.innerHTML = html;

  // Restore scroll position
  if (scrollParent) {
    scrollParent.scrollTop = scrollTop;
  }

  // Set up keyboard navigation on any rendered trees
  setupTreeKeyboard(container);

  // Restore focus after re-render (skip if caller will handle focus)
  if (!_pendingFocusTarget) {
    if (focusNodeId) {
      const targetNode = container.querySelector('[data-node-id="' + focusNodeId + '"]');
      if (targetNode) {
        targetNode.setAttribute("tabindex", "0");
        targetNode.focus();
        focusCardId = null; // prevent fallback
      }
    }
    if (focusCardId) {
      const card = document.getElementById(focusCardId);
      const btn = card?.querySelector("button");
      if (btn) btn.focus();
    }
  }
}
