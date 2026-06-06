// ---------------------------------------------------------------------------
// Graph view — Cytoscape dependency graph, critical path, scoped subtree rendering.
// ---------------------------------------------------------------------------

import { fetchCriticalPath } from "../api.js";
import { CATEGORY_COLORS, state, THEME_COLORS } from "../state.js";
import { handleGhostClick, resolveGraphScope } from "./graphSidebar.js";

// --- Callbacks for functions not yet available at import time ---

export const callbacks = { openDetail: null, fetchData: null };

const GRAPH_MAX_ZOOM = 4;
const GRAPH_WHEEL_SENSITIVITY = 0.15;
const GRAPH_FIT_ZOOM_CAP = 1.5;

function computeGraphMinZoom(nodeCount) {
  if (nodeCount >= 1000) return 0.45;
  if (nodeCount >= 600) return 0.4;
  if (nodeCount >= 300) return 0.35;
  if (nodeCount >= 150) return 0.3;
  return 0.25;
}

function enforceReadableZoomBounds(nodeCount) {
  if (!state.cy) return;
  const floor = computeGraphMinZoom(nodeCount);
  state.cy.minZoom(floor);
  state.cy.maxZoom(GRAPH_MAX_ZOOM);
  if (state.cy.zoom() < floor) {
    state.cy.zoom(floor);
  }
}

function fitGraphWithCaps() {
  if (!state.cy) return;
  state.cy.fit(undefined, 30);
  enforceReadableZoomBounds(state.cy.nodes().length);
  if (state.cy.zoom() > GRAPH_FIT_ZOOM_CAP) {
    state.cy.zoom(GRAPH_FIT_ZOOM_CAP);
    state.cy.center();
  }
}

function setGraphNotice(text) {
  const el = document.getElementById("graphNotice");
  if (!el) return;
  if (text) {
    el.textContent = "";
    const icon = document.createElement("span");
    icon.textContent = "\u26A0 ";
    icon.setAttribute("aria-hidden", "true");
    const message = document.createElement("span");
    message.textContent = text;
    el.append(icon, message);
    el.classList.remove("hidden");
    return;
  }
  el.textContent = "";
  el.classList.add("hidden");
}

function updateGraphPerfState() {
  const nodeCount = state.cy ? state.cy.nodes().length : 0;
  const edgeCount = state.cy ? state.cy.edges().length : 0;
  const countEl = document.getElementById("graphNodeEdgeCount");
  if (countEl) {
    countEl.textContent = `${nodeCount} nodes, ${edgeCount} edges`;
  }
  const el = document.getElementById("graphPerfState");
  if (!el) return;
  const t = state.graphTelemetry || {};
  const queryMs = t.query_ms ?? "-";
  const renderMs = t.render_ms ?? "-";
  if (queryMs === "-" && renderMs === "-") {
    el.textContent = "";
    return;
  }
  el.textContent = `Query ${queryMs}ms | Render ${renderMs}ms`;
}

function graphStyles() {
  return [
    {
      selector: "node",
      style: {
        label: "data(label)",
        "font-size": "11px",
        "font-family": "JetBrains Mono, monospace",
        "text-valign": "center",
        "text-halign": "center",
        "text-wrap": "wrap",
        "text-max-width": "120px",
        color: THEME_COLORS.textPrimary,
        "text-outline-color": THEME_COLORS.graphOutline,
        "text-outline-width": 2,
        width: "mapData(priority, 0, 4, 60, 35)",
        height: "mapData(priority, 0, 4, 60, 35)",
        opacity: "data(opacity)",
        "background-color": (ele) => CATEGORY_COLORS[ele.data("statusCategory")] || "#64748B",
        "border-width": (ele) => (ele.data("isReady") ? 3 : 0),
        "border-color": "#10B981",
        shape: (ele) => {
          const t = ele.data("type");
          if (t === "epic" || t === "milestone") return "hexagon";
          if (t === "bug") return "diamond";
          if (t === "feature") return "star";
          return "round-rectangle";
        },
      },
    },
    {
      selector: "edge",
      style: {
        width: 1.5,
        "line-color": THEME_COLORS.graphEdge,
        "target-arrow-color": THEME_COLORS.graphEdge,
        "target-arrow-shape": "triangle",
        "curve-style": "bezier",
        "arrow-scale": 0.8,
      },
    },
    {
      selector: "edge[edgeType='hierarchy']",
      style: {
        "line-style": "dashed",
        "line-dash-pattern": [6, 3],
        "line-color": THEME_COLORS.graphEdgeHierarchy || THEME_COLORS.graphEdge,
        "target-arrow-color": THEME_COLORS.graphEdgeHierarchy || THEME_COLORS.graphEdge,
        "target-arrow-shape": "triangle",
        opacity: 0.6,
      },
    },
    {
      selector: "node:selected",
      style: { "border-width": 3, "border-color": THEME_COLORS.accent },
    },
    {
      selector: "node[?isGhost]",
      style: {
        "border-width": 2,
        "border-style": "dashed",
        "border-color": "#8FAAB8",
        "background-opacity": 0.3,
      },
    },
    {
      selector: "node[?isGhost]:active",
      style: {
        "border-color": THEME_COLORS.accent,
        "background-opacity": 0.5,
      },
    },
  ];
}

export function criticalPathEdgeIds(path) {
  const edgeIds = new Set();
  for (let i = 0; i < path.length - 1; i += 1) {
    const source = path[i]?.id;
    const target = path[i + 1]?.id;
    if (source && target) edgeIds.add(`e-${source}-${target}`);
  }
  return edgeIds;
}

export function setCriticalPathStateFromPath(path, graphState = state) {
  graphState.criticalPathIds = new Set(path.map((p) => p.id));
  graphState.criticalPathEdgeIds = criticalPathEdgeIds(path);
}

export async function refreshCriticalPathState() {
  const data = await fetchCriticalPath();
  setCriticalPathStateFromPath(data?.path || []);
}

export function clearGraphForNoIssues(container, graphState = state) {
  if (graphState.cy) {
    graphState.cy.destroy();
    graphState.cy = null;
  }
  if (container) {
    container.innerHTML =
      '<div data-graph-blank class="flex items-center justify-center h-full text-secondary text-sm">No issues to display for this project.</div>';
  }
}

function applyCriticalPathStyles() {
  if (!state.cy) return;
  if (state.criticalPathActive && state.criticalPathIds.size) {
    state.cy.nodes().forEach((n) => {
      if (!state.criticalPathIds.has(n.id())) n.style("opacity", 0.2);
    });
    state.cy.edges().forEach((e) => {
      if (state.criticalPathEdgeIds.has(e.id())) {
        e.style({
          width: 3,
          "line-color": "#EF4444",
          "target-arrow-color": "#EF4444",
        });
      } else {
        e.style("opacity", 0.1);
      }
    });
  } else {
    state.cy.nodes().forEach((n) => n.style("opacity", n.data("opacity")));
    state.cy.edges().forEach((e) => {
      if (e.data("edgeType") === "hierarchy") {
        e.style({
          width: 1.5,
          "line-style": "dashed",
          "line-color": THEME_COLORS.graphEdgeHierarchy || THEME_COLORS.graphEdge,
          "target-arrow-color": THEME_COLORS.graphEdgeHierarchy || THEME_COLORS.graphEdge,
          opacity: 0.6,
        });
      } else {
        e.style({
          width: 1.5,
          "line-style": "solid",
          "line-color": THEME_COLORS.graphEdge,
          "target-arrow-color": THEME_COLORS.graphEdge,
          opacity: 1,
        });
      }
    });
  }
}

function bindGraphEvents() {
  if (!state.cy) return;
  state.cy.on("tap", "node", (evt) => {
    const nodeId = evt.target.id();
    if (evt.target.data("isGhost")) {
      handleGhostClick(nodeId);
      return;
    }
    if (callbacks.openDetail) callbacks.openDetail(nodeId);
  });
  state.cy.on("mouseover", "node", (evt) => {
    if (state.criticalPathActive) return;
    const hoverNode = evt.target;
    const nodeId = hoverNode.id();
    const downstream = new Set();
    const visited = new Set([nodeId]);
    const queue = [hoverNode];
    for (let i = 0; i < queue.length; i += 1) {
      const curNode = queue[i];
      curNode.outgoers("edge").forEach((edge) => {
        const nextNode = edge.target();
        const nextId = nextNode.id();
        if (visited.has(nextId)) return;
        visited.add(nextId);
        downstream.add(nextId);
        queue.push(nextNode);
      });
    }
    if (downstream.size) {
      state.cy.nodes().forEach((n) => {
        if (n.id() !== nodeId && !downstream.has(n.id())) n.style("opacity", 0.15);
      });
    }
  });
  state.cy.on("mouseout", "node", () => {
    if (state.criticalPathActive) return;
    state.cy.nodes().forEach((n) => {
      n.style("opacity", n.data("opacity"));
    });
  });
}

// ---------------------------------------------------------------------------
// renderGraph — build Cytoscape graph from sidebar-scoped subtree
// ---------------------------------------------------------------------------

export function renderGraph() {
  const renderStarted = performance.now();
  const container = document.getElementById("cy");
  if (!state.allIssues.length) {
    clearGraphForNoIssues(container);
    setGraphNotice("");
    updateGraphPerfState();
    return;
  }

  // --- Scoped subtree rendering ---
  const { nodes: scopeNodes, edges: scopeEdges, ghostIds } = resolveGraphScope();

  if (scopeNodes.length === 0 && state.graphSidebarSelections.size === 0) {
    // Blank state — no selections
    if (state.cy) {
      state.cy.destroy();
      state.cy = null;
    }
    container.innerHTML =
      '<div data-graph-blank class="flex items-center justify-center h-full text-secondary text-sm">Select items from the sidebar to explore their dependency graph.</div>';
    setGraphNotice("");
    updateGraphPerfState();
    return;
  }

  // Restore container if it had the blank prompt (not Cytoscape's own DOM)
  if (container.querySelector("[data-graph-blank]")) {
    container.innerHTML = "";
  }

  // Apply status pill filters
  const showOpen = state.statusPills.open;
  const showActive = state.statusPills.active;
  const showClosed = state.statusPills.done;

  const filteredNodes = scopeNodes.filter((n) => {
    const cat = n.status_category || "open";
    if (cat === "open" && !showOpen) return false;
    if (cat === "wip" && !showActive) return false;
    if (cat === "done" && !showClosed) return false;
    return true;
  });

  const filteredIds = new Set(filteredNodes.map((n) => n.id));
  const search = document.getElementById("filterSearch")?.value?.toLowerCase().trim() || "";

  const cyNodes = filteredNodes.map((n) => {
    const title = n.title || n.id;
    const isGhost = ghostIds.has(n.id);
    const matchesSearch =
      !search || title.toLowerCase().includes(search) || n.id.toLowerCase().includes(search);
    return {
      data: {
        id: n.id,
        label: title.length > 30 ? `${title.slice(0, 28)}..` : title,
        status: n.status,
        statusCategory: n.status_category || "open",
        priority: n.priority,
        type: n.type,
        isReady: !!n.is_ready,
        childCount: (n.children || []).length,
        isGhost: isGhost,
        opacity: isGhost ? 0.45 : matchesSearch ? 1 : 0.2,
      },
    };
  });

  const cyEdges = scopeEdges
    .filter((e) => filteredIds.has(e.source) && filteredIds.has(e.target))
    .map((e) => ({
      data: {
        id: `e-${e.source}-${e.target}`,
        source: e.source,
        target: e.target,
        edgeType: e.edgeType || "dependency",
      },
    }));

  const nextNodesById = new Map(cyNodes.map((n) => [n.data.id, n]));
  const nextEdgesById = new Map(cyEdges.map((e) => [e.data.id, e]));
  let created = false;

  if (!state.cy) {
    const graphMinZoom = computeGraphMinZoom(cyNodes.length);
    /* global cytoscape */
    state.cy = cytoscape({
      container,
      elements: cyNodes.concat(cyEdges),
      layout: {
        name: "dagre",
        rankDir: "TB",
        rankSep: 80,
        nodeSep: 40,
        padding: 20,
      },
      style: graphStyles(),
      minZoom: graphMinZoom,
      maxZoom: GRAPH_MAX_ZOOM,
      wheelSensitivity: GRAPH_WHEEL_SENSITIVITY,
    });
    created = true;
    fitGraphWithCaps();
  } else {
    const currentNodeIds = new Set(state.cy.nodes().map((n) => n.id()));
    const currentEdgeIds = new Set(state.cy.edges().map((e) => e.id()));
    const topologyChanged =
      currentNodeIds.size !== nextNodesById.size ||
      currentEdgeIds.size !== nextEdgesById.size ||
      Array.from(nextNodesById.keys()).some((id) => !currentNodeIds.has(id)) ||
      Array.from(nextEdgesById.keys()).some((id) => !currentEdgeIds.has(id));

    if (!topologyChanged) {
      state.cy.batch(() => {
        state.cy.nodes().forEach((n) => {
          const next = nextNodesById.get(n.id());
          if (next) n.data(next.data);
        });
        state.cy.edges().forEach((e) => {
          const next = nextEdgesById.get(e.id());
          if (next) e.data(next.data);
        });
      });
      enforceReadableZoomBounds(cyNodes.length);
    } else {
      const previousPositions = {};
      const selectedNodeId = state.cy.$("node:selected").id();
      const previousZoom = state.cy.zoom();
      const previousPan = state.cy.pan();
      state.cy.nodes().forEach((n) => {
        previousPositions[n.id()] = n.position();
      });
      state.cy.destroy();

      const canReusePositions =
        cyNodes.length > 0 && cyNodes.every((n) => Object.hasOwn(previousPositions, n.data.id));
      const graphMinZoom = computeGraphMinZoom(cyNodes.length);
      state.cy = cytoscape({
        container,
        elements: cyNodes.concat(cyEdges),
        layout: canReusePositions
          ? {
              name: "preset",
              fit: false,
              padding: 20,
              positions: (node) => previousPositions[node.id()],
            }
          : {
              name: "dagre",
              rankDir: "TB",
              rankSep: 80,
              nodeSep: 40,
              padding: 20,
            },
        style: graphStyles(),
        minZoom: graphMinZoom,
        maxZoom: GRAPH_MAX_ZOOM,
        wheelSensitivity: GRAPH_WHEEL_SENSITIVITY,
      });
      created = true;
      if (selectedNodeId && state.cy.$id(selectedNodeId).length) {
        state.cy.$id(selectedNodeId).select();
      }
      if (canReusePositions) {
        state.cy.zoom(previousZoom);
        state.cy.pan(previousPan);
        enforceReadableZoomBounds(cyNodes.length);
      } else {
        fitGraphWithCaps();
      }
    }
  }

  if (created) bindGraphEvents();
  applyCriticalPathStyles();

  const renderMs = Math.round(performance.now() - renderStarted);
  state.graphTelemetry = { ...(state.graphTelemetry || {}), render_ms: renderMs };
  updateGraphPerfState();
}

// ---------------------------------------------------------------------------
// graphFit — reset zoom and center
// ---------------------------------------------------------------------------

export function graphFit() {
  if (state.cy) {
    fitGraphWithCaps();
  }
}

// ---------------------------------------------------------------------------
// toggleCriticalPath — toggle critical path highlight
// ---------------------------------------------------------------------------

export async function toggleCriticalPath() {
  state.criticalPathActive = !state.criticalPathActive;
  const btn = document.getElementById("btnCritPath");
  if (state.criticalPathActive) {
    btn.className = "px-2 py-0.5 rounded bg-red-600 text-white";
    await refreshCriticalPathState();
  } else {
    btn.className = "px-2 py-0.5 rounded bg-overlay bg-overlay-hover";
    setCriticalPathStateFromPath([]);
  }
  renderGraph();
}

// ---------------------------------------------------------------------------
// showHealthBreakdown — health breakdown modal in detail panel
// ---------------------------------------------------------------------------

export function showHealthBreakdown() {
  const b = state._healthBreakdown;
  if (!b) return;
  const panel = document.getElementById("detailContent");
  const dp = document.getElementById("detailPanel");
  dp.classList.remove("translate-x-full");
  state.selectedIssue = null;
  panel.innerHTML =
    '<div class="flex items-center justify-between mb-3">' +
    `<span class="text-base font-semibold" style="color:var(--text-primary)">System Health: ${b.score}/100</span>` +
    '<button onclick="closeDetail()" class="text-muted text-primary-hover text-lg">&times;</button></div>' +
    ["blocked", "freshness", "ready", "balance"]
      .map((k) => {
        const f = b[k];
        const pct = Math.round((f.score / f.max) * 100);
        return (
          '<div class="mb-3"><div class="flex justify-between text-xs mb-1">' +
          `<span class="capitalize" style="color:var(--text-primary)">${k}</span>` +
          `<span style="color:var(--text-secondary)">${f.score}/${f.max}</span></div>` +
          '<div class="w-full h-2 rounded-full overflow-hidden" style="background:var(--surface-base)">' +
          `<div class="h-full rounded-full ${pct >= 75 ? "bg-emerald-500" : pct >= 50 ? "bg-amber-500" : "bg-red-500"}" style="width:${pct}%"></div></div>` +
          `<div class="text-xs mt-0.5" style="color:var(--text-muted)">${f.detail}</div></div>`
        );
      })
      .join("");
}
