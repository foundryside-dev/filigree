// ---------------------------------------------------------------------------
// Graph Sidebar — scoped subtree explorer for the graph tab.
// ---------------------------------------------------------------------------

import { state, TYPE_COLORS } from "../state.js";
import { escHtml } from "../ui.js";

// Type display order for sidebar groups
const TYPE_ORDER = ["milestone", "epic", "release", "feature", "task", "bug"];

// --- Tree index (rebuilt when allIssues changes) ---

let rootIssues = []; // issues with parent_id === null
let childIndex = {}; // parentId -> [childIssue, ...]
let ancestorIndex = {}; // issueId -> top-level ancestor issueId
let subtreeIndex = {}; // rootId -> Set<issueId> (all descendants including root)
let crossTreeDeps = {}; // rootId -> count of cross-tree dependency edges

export function rebuildTreeIndex() {
  rootIssues = [];
  childIndex = {};
  ancestorIndex = {};
  subtreeIndex = {};
  crossTreeDeps = {};

  for (const issue of state.allIssues) {
    if (!issue.parent_id) {
      rootIssues.push(issue);
    }
    if (issue.parent_id) {
      if (!childIndex[issue.parent_id]) childIndex[issue.parent_id] = [];
      childIndex[issue.parent_id].push(issue);
    }
  }

  // Build subtree and ancestor indices
  for (const root of rootIssues) {
    const subtree = new Set();
    const stack = [root.id];
    const visited = new Set();
    while (stack.length) {
      const id = stack.pop();
      if (visited.has(id)) continue;
      visited.add(id);
      subtree.add(id);
      ancestorIndex[id] = root.id;
      for (const child of childIndex[id] || []) {
        stack.push(child.id);
      }
    }
    subtreeIndex[root.id] = subtree;
  }

  // Prune stale selections — issues deleted between data refreshes
  for (const [id] of state.graphSidebarSelections) {
    if (!ancestorIndex[id] && !rootIssues.some((r) => r.id === id)) {
      state.graphSidebarSelections.delete(id);
    }
  }

  // Count cross-tree deps per root
  for (const root of rootIssues) {
    let count = 0;
    const subtree = subtreeIndex[root.id];
    for (const id of subtree) {
      const issue = state.issueMap[id];
      if (!issue) continue;
      for (const depId of issue.blocks || []) {
        if (!subtree.has(depId)) count++;
      }
      for (const depId of issue.blocked_by || []) {
        if (!subtree.has(depId)) count++;
      }
    }
    crossTreeDeps[root.id] = count;
  }
}

export function getAncestorId(issueId) {
  return ancestorIndex[issueId] || issueId;
}

export function getSubtreeIds(rootId) {
  return subtreeIndex[rootId] || new Set([rootId]);
}

// --- Type filter ---

export function renderTypeFilter() {
  const container = document.getElementById("graphSidebarTypeFilter");
  if (!container) return;

  const typesPresent = new Set(rootIssues.map((i) => i.type));
  const ordered = TYPE_ORDER.filter((t) => typesPresent.has(t));
  for (const t of typesPresent) {
    if (!ordered.includes(t)) ordered.push(t);
  }

  container.innerHTML = ordered
    .map((t) => {
      const active = state.graphSidebarTypeFilter.size === 0 || state.graphSidebarTypeFilter.has(t);
      const color = TYPE_COLORS[t] || "#6B7280";
      return `<button data-sidebar-type="${escHtml(t)}"
        class="px-1.5 py-0.5 rounded text-[10px] font-medium ${active ? "text-primary" : "text-muted opacity-50"}"
        style="border:1px solid ${active ? color : "var(--border-default)"};${active ? `background:${color}22` : ""}"
        aria-pressed="${active}">${escHtml(t)}</button>`;
    })
    .join("");
}

export function toggleGraphSidebarType(type) {
  if (state.graphSidebarTypeFilter.has(type)) {
    state.graphSidebarTypeFilter.delete(type);
  } else {
    state.graphSidebarTypeFilter.add(type);
  }
  // If all types are selected, clear the filter (= show all)
  const allTypes = new Set(rootIssues.map((i) => i.type));
  if (allTypes.size === state.graphSidebarTypeFilter.size) {
    state.graphSidebarTypeFilter.clear();
  }
  renderGraphSidebar();
}

// --- Sidebar rendering ---

function getVisibleRootIssues() {
  let items = rootIssues;
  if (state.graphSidebarTypeFilter.size > 0) {
    items = items.filter((i) => state.graphSidebarTypeFilter.has(i.type));
  }
  return items;
}

function groupByType(issues) {
  const groups = {};
  for (const issue of issues) {
    const type = issue.type || "other";
    if (!groups[type]) groups[type] = [];
    groups[type].push(issue);
  }
  // Sort within groups by priority then title
  for (const g of Object.values(groups)) {
    g.sort((a, b) => a.priority - b.priority || a.title.localeCompare(b.title));
  }
  // Order groups by TYPE_ORDER
  const ordered = [];
  for (const t of TYPE_ORDER) {
    if (groups[t]) ordered.push([t, groups[t]]);
  }
  for (const [t, items] of Object.entries(groups)) {
    if (!TYPE_ORDER.includes(t)) ordered.push([t, items]);
  }
  return ordered;
}

export function renderGraphSidebar() {
  const list = document.getElementById("graphSidebarList");
  if (!list) return;

  const scrollTop = list.scrollTop;
  const visible = getVisibleRootIssues();
  const groups = groupByType(visible);

  // Update hidden selections badge
  const hiddenBadge = document.getElementById("graphSidebarHiddenBadge");
  if (hiddenBadge) {
    let hiddenCount = 0;
    for (const [id] of state.graphSidebarSelections) {
      const issue = state.issueMap[id];
      if (
        issue &&
        state.graphSidebarTypeFilter.size > 0 &&
        !state.graphSidebarTypeFilter.has(issue.type)
      ) {
        hiddenCount++;
      }
    }
    if (hiddenCount > 0) {
      hiddenBadge.textContent = `${hiddenCount} hidden`;
      hiddenBadge.classList.remove("hidden");
    } else {
      hiddenBadge.classList.add("hidden");
    }
  }

  if (rootIssues.length === 0) {
    list.innerHTML =
      '<div class="px-2 py-4 text-muted text-center">No top-level issues found. Issues must have no parent to appear here.</div>';
    renderTypeFilter();
    return;
  }

  if (visible.length === 0) {
    list.innerHTML =
      '<div class="px-2 py-4 text-muted text-center">No issues match the current type filter.</div>';
    renderTypeFilter();
    return;
  }

  const html = [];
  for (const [type, items] of groups) {
    const color = TYPE_COLORS[type] || "#6B7280";
    html.push(`<div class="mt-2 mb-1 flex items-center gap-1">
      <span class="inline-block w-2 h-2 rounded-full" style="background:${color}"></span>
      <span class="font-medium text-secondary uppercase tracking-wider text-[10px]">${escHtml(type)}s (${items.length})</span>
    </div>`);
    for (const issue of items) {
      const sel = state.graphSidebarSelections.get(issue.id);
      const selState = sel ? sel.state : null;
      const crossDeps = crossTreeDeps[issue.id] || 0;

      let bgClass = "bg-transparent hover:bg-overlay";
      let textClass = "text-secondary";
      let indicator = "";
      if (selState === "explicit") {
        bgClass = "bg-accent/20 hover:bg-accent/30";
        textClass = "text-primary";
      } else if (selState === "clicked-in") {
        bgClass = "bg-amber-500/15 hover:bg-amber-500/25";
        textClass = "text-primary";
        indicator =
          '<span class="text-amber-400 text-[10px] ml-auto shrink-0" title="Explored via dependency">dep</span>';
      }

      const depBadge =
        crossDeps > 0
          ? `<span class="text-muted text-[10px]" title="${crossDeps} cross-tree dependencies">${crossDeps}x</span>`
          : "";

      const title = issue.title.length > 40 ? issue.title.slice(0, 38) + ".." : issue.title;

      html.push(`<div role="option" aria-selected="${!!selState}"
        class="flex items-center gap-1.5 px-2 py-1 rounded cursor-pointer ${bgClass} ${textClass}"
        data-sidebar-item="${escHtml(issue.id)}"
        tabindex="0"
        title="${escHtml(issue.title)}">
        <span class="truncate">${escHtml(title)}</span>
        ${depBadge}
        ${indicator}
      </div>`);
    }
  }

  list.innerHTML = html.join("");
  renderTypeFilter();
  list.scrollTop = scrollTop;
}

// --- Selection logic ---

const SOFT_NODE_CAP = 300;

export function toggleGraphSidebarItem(issueId) {
  const existing = state.graphSidebarSelections.get(issueId);
  if (!existing) {
    // Unselected -> explicit (check node cap first)
    const { exceedsCap, total } = checkNodeCap(issueId);
    if (exceedsCap && !confirmNodeCap(total)) return;
    state.graphSidebarSelections.set(issueId, { state: "explicit", causedBy: new Set() });
  } else if (existing.state === "clicked-in") {
    // Clicked-in -> promote to explicit (already rendered, no cap check needed)
    state.graphSidebarSelections.set(issueId, { state: "explicit", causedBy: new Set() });
  } else {
    // Explicit -> unselected (with cascade)
    deselectWithCascade(issueId);
  }
  renderGraphSidebar();
  if (callbacks.renderGraph) callbacks.renderGraph();
}

function deselectWithCascade(issueId) {
  state.graphSidebarSelections.delete(issueId);
  // Collect clicked-in items whose causedBy set becomes empty, then delete
  const toRemove = [];
  for (const [id, sel] of state.graphSidebarSelections) {
    if (sel.state === "clicked-in") {
      sel.causedBy.delete(issueId);
      if (sel.causedBy.size === 0) {
        toRemove.push(id);
      }
    }
  }
  for (const id of toRemove) {
    state.graphSidebarSelections.delete(id);
  }
}

export function graphSidebarSelectAll() {
  // Estimate total node count if we select all visible roots
  let estimatedTotal = 0;
  for (const [rootId] of state.graphSidebarSelections) {
    estimatedTotal += getSubtreeIds(rootId).size;
  }
  const visible = getVisibleRootIssues();
  for (const issue of visible) {
    if (!state.graphSidebarSelections.has(issue.id)) {
      estimatedTotal += getSubtreeIds(issue.id).size;
    }
  }
  if (estimatedTotal > SOFT_NODE_CAP && !confirmNodeCap(estimatedTotal)) return;

  for (const issue of visible) {
    if (!state.graphSidebarSelections.has(issue.id)) {
      state.graphSidebarSelections.set(issue.id, { state: "explicit", causedBy: new Set() });
    }
  }
  renderGraphSidebar();
  if (callbacks.renderGraph) callbacks.renderGraph();
}

export function graphSidebarClearAll() {
  state.graphSidebarSelections.clear();
  renderGraphSidebar();
  if (callbacks.renderGraph) callbacks.renderGraph();
}

// --- Ghost click handler (called from graph.js when a ghost node is tapped) ---

export function handleGhostClick(nodeId) {
  const ancestorId = getAncestorId(nodeId);
  if (!ancestorId || state.graphSidebarSelections.has(ancestorId)) return;

  // Check node cap before expanding
  const { exceedsCap, total } = checkNodeCap(ancestorId);
  if (exceedsCap && !confirmNodeCap(total)) return;

  // Find which explicit selections reference this ghost via cross-tree deps
  const causedBy = new Set();
  for (const [selId, sel] of state.graphSidebarSelections) {
    if (sel.state !== "explicit") continue;
    const subtree = getSubtreeIds(selId);
    for (const id of subtree) {
      const issue = state.issueMap[id];
      if (!issue) continue;
      const deps = [...(issue.blocks || []), ...(issue.blocked_by || [])];
      const targetSubtree = getSubtreeIds(ancestorId);
      if (deps.some((d) => targetSubtree.has(d))) {
        causedBy.add(selId);
        break;
      }
    }
  }

  state.graphSidebarSelections.set(ancestorId, { state: "clicked-in", causedBy });
  const statusEl = document.getElementById("graphSidebarStatus");
  const issue = state.issueMap[ancestorId];
  if (statusEl && issue) {
    statusEl.textContent = `Added: ${issue.title.slice(0, 40)}`;
  }
  renderGraphSidebar();
  if (callbacks.renderGraph) callbacks.renderGraph();
}

// --- Resolve visible nodes and edges for Cytoscape ---

export function resolveGraphScope() {
  if (state.graphSidebarSelections.size === 0) {
    return { nodes: [], edges: [], ghostIds: new Set() };
  }

  // Collect all selected subtree node IDs
  const selectedIds = new Set();
  for (const [rootId] of state.graphSidebarSelections) {
    const subtree = getSubtreeIds(rootId);
    for (const id of subtree) selectedIds.add(id);
  }

  // Identify ghost nodes: nodes referenced by deps from selected set but not in selected set
  const ghostIds = new Set();
  for (const id of selectedIds) {
    const issue = state.issueMap[id];
    if (!issue) continue;
    for (const depId of [...(issue.blocks || []), ...(issue.blocked_by || [])]) {
      if (!selectedIds.has(depId) && state.issueMap[depId]) {
        ghostIds.add(depId);
      }
    }
  }

  // Build node list (selected + ghosts)
  const allVisibleIds = new Set([...selectedIds, ...ghostIds]);
  const nodes = [];
  for (const id of allVisibleIds) {
    const issue = state.issueMap[id];
    if (!issue) continue;
    nodes.push(issue);
  }

  // Build edge list from deps and parent-child hierarchy
  const edges = [];
  const edgeSeen = new Set();
  for (const id of allVisibleIds) {
    const issue = state.issueMap[id];
    if (!issue) continue;
    // Dependency edges (blocks -> blocked)
    for (const blockedId of issue.blocks || []) {
      if (allVisibleIds.has(blockedId)) {
        const key = `${id}->${blockedId}`;
        if (!edgeSeen.has(key)) {
          edgeSeen.add(key);
          edges.push({ source: id, target: blockedId, edgeType: "dependency" });
        }
      }
    }
    // Parent-child edges (parent -> child)
    if (issue.parent_id && allVisibleIds.has(issue.parent_id)) {
      const key = `${issue.parent_id}->${id}`;
      if (!edgeSeen.has(key)) {
        edgeSeen.add(key);
        edges.push({ source: issue.parent_id, target: id, edgeType: "hierarchy" });
      }
    }
  }

  return { nodes, edges, ghostIds };
}

// --- Soft node cap check ---

export function checkNodeCap(additionalRootId) {
  // Estimate total nodes if we add this root's subtree
  let total = 0;
  for (const [rootId] of state.graphSidebarSelections) {
    total += getSubtreeIds(rootId).size;
  }
  if (additionalRootId) {
    total += getSubtreeIds(additionalRootId).size;
  }
  return { total, exceedsCap: total > SOFT_NODE_CAP };
}

function confirmNodeCap(total) {
  const statusEl = document.getElementById("graphSidebarStatus");
  const ok = confirm(`This would display ~${total} nodes. Large graphs may be slow. Continue?`);
  if (!ok && statusEl) {
    statusEl.textContent = `Cancelled — would have shown ${total} nodes`;
  }
  return ok;
}

// --- Delegated event listeners ---

let listenersAttached = false;

export function attachSidebarListeners() {
  if (listenersAttached) return;
  listenersAttached = true;

  // Type filter buttons (delegated on container)
  const typeContainer = document.getElementById("graphSidebarTypeFilter");
  if (typeContainer) {
    typeContainer.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-sidebar-type]");
      if (btn) toggleGraphSidebarType(btn.dataset.sidebarType);
    });
  }

  // Sidebar item click + keyboard (delegated on list)
  const list = document.getElementById("graphSidebarList");
  if (list) {
    list.addEventListener("click", (e) => {
      const el = e.target.closest("[data-sidebar-item]");
      if (el) toggleGraphSidebarItem(el.dataset.sidebarItem);
    });
    list.addEventListener("keydown", (e) => {
      if (e.key === " " || e.key === "Enter") {
        const el = e.target.closest("[data-sidebar-item]");
        if (el) {
          e.preventDefault();
          toggleGraphSidebarItem(el.dataset.sidebarItem);
        }
      }
    });
  }
}

// --- Callbacks ---

export const callbacks = { renderGraph: null };
