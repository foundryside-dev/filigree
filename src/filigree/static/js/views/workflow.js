// ---------------------------------------------------------------------------
// Workflow view — Cytoscape state-machine diagram and milestone plan tree.
// ---------------------------------------------------------------------------

import { fetchPlan, fetchTypeInfo, fetchTypes } from "../api.js";
import { CATEGORY_COLORS, state, THEME_COLORS } from "../state.js";
import { escHtml, escJsSingleAttr } from "../ui.js";

// Types hidden from the workflow dropdown (internal planning types).
const WORKFLOW_HIDDEN = { phase: 1, step: 1, work_package: 1, deliverable: 1 };

/**
 * Prettify a raw identifier for display: replace underscores/hyphens with
 * spaces and title-case each word.
 *   "in_progress" -> "In Progress"
 *   "wont_fix"    -> "Wont Fix"
 */
function prettifyName(raw) {
  if (!raw) return "";
  return raw.replace(/[_-]/g, " ").replace(/\b\w/g, (ch) => ch.toUpperCase());
}

/**
 * Populate the workflow-type dropdown (once) then fetch the selected type's
 * template and render its state-machine graph.
 */
export async function loadWorkflow() {
  const wfSelect = document.getElementById("workflowType");

  // Populate type dropdown if not done
  if (wfSelect && wfSelect.options.length <= 1) {
    try {
      const registered = await fetchTypes();
      if (!registered) return;
      for (const t of registered) {
        if (WORKFLOW_HIDDEN[t.type]) continue;
        const opt = document.createElement("option");
        opt.value = t.type;
        opt.textContent = t.display_name || t.type;
        wfSelect.appendChild(opt);
      }
      // Auto-select first type if none selected
      if (!wfSelect.value && wfSelect.options.length > 1) {
        wfSelect.value = wfSelect.options[1].value;
      }
    } catch (err) {
      console.warn("[workflow] Failed to load type list:", err);
    }
  }

  const typeName = wfSelect ? wfSelect.value : "";
  if (!typeName) return;

  try {
    const tpl = await fetchTypeInfo(typeName);
    if (!tpl) return;
    const stateCounts = {};
    for (const i of state.allIssues) {
      if (i.type === typeName) {
        stateCounts[i.status] = (stateCounts[i.status] || 0) + 1;
      }
    }
    renderWorkflowGraph(tpl, stateCounts);
  } catch (err) {
    console.warn("[workflow] Failed to render workflow graph:", err);
    const cy = document.getElementById("workflowCy");
    if (cy)
      cy.innerHTML = '<div class="p-4 text-xs text-red-400">Failed to load workflow diagram.</div>';
  }
}

/**
 * Render a Cytoscape-powered directed graph of the workflow states and
 * transitions for a given type template.
 *
 * @param {object} template  - Type info object with `states` and `transitions`.
 * @param {object} stateCounts - Map of state-name to current issue count.
 */
export function renderWorkflowGraph(template, stateCounts, targetContainer) {
  const container = targetContainer || document.getElementById("workflowCy");
  const nodes = template.states.map((s) => {
    const count = stateCounts[s.name] || 0;
    return {
      data: {
        id: s.name,
        label: prettifyName(s.name) + (count ? ` (${count})` : ""),
        category: s.category,
        count,
      },
    };
  });
  const edges = template.transitions.map((t, i) => ({
    data: {
      id: `wt${i}`,
      source: t.from,
      target: t.to,
      enforcement: t.enforcement,
    },
  }));

  if (state.workflowCy) state.workflowCy.destroy();

  // cytoscape is loaded via CDN and available on the global scope.
  state.workflowCy = window.cytoscape({
    container,
    elements: nodes.concat(edges),
    layout: {
      name: "dagre",
      rankDir: "LR",
      rankSep: 100,
      nodeSep: 60,
      padding: 30,
    },
    style: [
      {
        selector: "node",
        style: {
          label: "data(label)",
          "font-size": "12px",
          "font-family": "JetBrains Mono, monospace",
          "text-valign": "center",
          "text-halign": "center",
          color: THEME_COLORS.textPrimary,
          "text-outline-color": THEME_COLORS.graphOutline,
          "text-outline-width": 2,
          width: 80,
          height: 40,
          shape: "round-rectangle",
          "background-color": (ele) => CATEGORY_COLORS[ele.data("category")] || "#64748B",
        },
      },
      {
        selector: "edge",
        style: {
          width: 2,
          "line-color": THEME_COLORS.graphEdge,
          "target-arrow-color": THEME_COLORS.graphEdge,
          "target-arrow-shape": "triangle",
          "curve-style": "bezier",
          "arrow-scale": 0.8,
          label: "data(enforcement)",
          "font-size": "9px",
          color: THEME_COLORS.textSecondary,
          "text-rotation": "autorotate",
          "text-margin-y": -10,
        },
      },
    ],
    minZoom: 0.3,
    maxZoom: 3,
  });

  state.workflowCy.fit(undefined, 40);
  if (state.workflowCy.zoom() > 1.5) {
    state.workflowCy.zoom(1.5);
    state.workflowCy.center();
  }
}

// ---------------------------------------------------------------------------
// Workflow modal — accessible via Settings gear after tab demotion.
// ---------------------------------------------------------------------------

export function showWorkflowModal(type) {
  const existing = document.getElementById("workflowModal");
  if (existing) existing.remove();

  const modal = document.createElement("div");
  modal.id = "workflowModal";
  modal.className = "fixed inset-0 bg-black/50 flex items-center justify-center z-50";
  // Shared cleanup: remove modal and keydown listener from all dismissal paths.
  const onKey = (ev) => {
    if (ev.key === "Escape") closeModal();
  };
  const closeModal = () => {
    modal.remove();
    document.removeEventListener("keydown", onKey);
  };

  modal.onclick = (ev) => {
    if (ev.target === modal) closeModal();
  };

  modal.innerHTML =
    '<div class="rounded-lg shadow-xl" style="background:var(--surface-raised);border:1px solid var(--border-strong);width:90vw;max-width:800px;height:70vh;display:flex;flex-direction:column">' +
    '<div class="flex items-center justify-between px-4 py-3" style="border-bottom:1px solid var(--border-default)">' +
    '<div class="flex items-center gap-3">' +
    '<span class="text-sm font-semibold" style="color:var(--text-primary)">Workflow Diagram</span>' +
    '<select id="workflowModalType" onchange="loadWorkflowInModal()" class="bg-overlay text-primary text-xs rounded px-2 py-1 border border-strong">' +
    '<option value="">Select type...</option></select>' +
    "</div>" +
    '<button id="workflowModalClose" class="text-muted text-primary-hover text-lg">&times;</button>' +
    "</div>" +
    '<div id="workflowModalCy" class="flex-1" style="min-height:0"></div>' +
    "</div>";

  document.body.appendChild(modal);

  document.getElementById("workflowModalClose").onclick = closeModal;
  document.addEventListener("keydown", onKey);

  // Focus trap: keep Tab cycling within modal
  modal.addEventListener("keydown", (ev) => {
    if (ev.key !== "Tab") return;
    const focusable = modal.querySelectorAll('button, select, [tabindex]:not([tabindex="-1"])');
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (ev.shiftKey && document.activeElement === first) {
      ev.preventDefault();
      last.focus();
    } else if (!ev.shiftKey && document.activeElement === last) {
      ev.preventDefault();
      first.focus();
    }
  });

  // Move focus into modal
  const closeBtn = document.getElementById("workflowModalClose");
  if (closeBtn) closeBtn.focus();

  populateWorkflowModalTypes(type);
}

async function populateWorkflowModalTypes(preselect) {
  const select = document.getElementById("workflowModalType");
  if (!select) return;
  try {
    const registered = await fetchTypes();
    if (!registered) return;
    for (const t of registered) {
      if (WORKFLOW_HIDDEN[t.type]) continue;
      const opt = document.createElement("option");
      opt.value = t.type;
      opt.textContent = t.display_name || t.type;
      select.appendChild(opt);
    }
    if (preselect) {
      select.value = preselect;
      loadWorkflowInModal();
    } else if (select.options.length > 1) {
      select.value = select.options[1].value;
      loadWorkflowInModal();
    }
  } catch (err) {
    console.warn("[workflow] Failed to load modal type list:", err);
  }
}

export async function loadWorkflowInModal() {
  const select = document.getElementById("workflowModalType");
  const container = document.getElementById("workflowModalCy");
  if (!select || !container) return;
  const typeName = select.value;
  if (!typeName) return;

  try {
    const tpl = await fetchTypeInfo(typeName);
    if (!tpl) return;
    const stateCounts = {};
    for (const i of state.allIssues) {
      if (i.type === typeName) {
        stateCounts[i.status] = (stateCounts[i.status] || 0) + 1;
      }
    }
    renderWorkflowGraph(tpl, stateCounts, container);
  } catch (_e) {
    container.innerHTML = '<div class="p-4 text-xs text-red-400">Failed to load workflow.</div>';
  }
}

/**
 * Fetch and render a milestone plan tree in the detail panel.
 *
 * @param {string} milestoneId - The milestone issue ID.
 */
export async function loadPlanView(milestoneId) {
  const panel = document.getElementById("detailContent");
  panel.innerHTML = '<div class="text-xs" style="color:var(--text-muted)">Loading plan...</div>';
  try {
    const plan = await fetchPlan(milestoneId);
    if (!plan) {
      panel.innerHTML = '<div class="text-red-400 text-xs">No plan found for this issue.</div>';
      return;
    }
    const m = plan.milestone || {};
    const phases = plan.phases || [];
    const totalSteps = plan.total_steps || 0;
    const completedSteps = plan.completed_steps || 0;
    const pct = totalSteps ? Math.round((completedSteps / totalSteps) * 100) : 0;

    let html =
      '<div class="flex items-center justify-between mb-3">' +
      '<span class="text-xs" style="color:var(--text-muted)">' +
      escHtml(m.id || milestoneId) +
      "</span>" +
      "<button onclick=\"openDetail('" +
      milestoneId +
      '\')" class="text-xs hover:underline" style="color:var(--accent)">Back to detail</button></div>' +
      '<div class="text-lg font-semibold mb-2" style="color:var(--text-primary)">' +
      escHtml(m.title || "Plan") +
      "</div>" +
      '<div class="w-full h-3 rounded-full mb-1 overflow-hidden" style="background:var(--surface-base)">' +
      '<div class="h-full bg-emerald-500 rounded-full" style="width:' +
      pct +
      '%"></div></div>' +
      '<div class="text-xs mb-4" style="color:var(--text-muted)">' +
      completedSteps +
      "/" +
      totalSteps +
      " steps (" +
      pct +
      "%)</div>";

    for (const p of phases) {
      const phase = p.phase || {};
      const steps = p.steps || [];
      const pDone = steps.filter((s) => (s.status_category || "open") === "done").length;
      const pPct = steps.length ? Math.round((pDone / steps.length) * 100) : 0;
      html +=
        '<div class="mb-3 rounded p-3" style="background:var(--surface-raised);border:1px solid var(--border-default)">' +
        '<div class="flex items-center justify-between mb-1">' +
        '<span class="text-xs font-medium" style="color:var(--text-primary)">' +
        escHtml(phase.title || "Phase") +
        "</span>" +
        '<span class="text-xs" style="color:var(--text-muted)">' +
        pDone +
        "/" +
        steps.length +
        "</span></div>" +
        '<div class="w-full h-1.5 rounded-full mb-2 overflow-hidden" style="background:var(--surface-base)">' +
        '<div class="h-full rounded-full" style="width:' +
        pPct +
        '%;background:var(--accent)"></div></div>' +
        steps
          .map((s) => {
            const catColor = CATEGORY_COLORS[s.status_category || "open"] || "#64748B";
            const safeId = escJsSingleAttr(s.id);
            return (
              '<div class="flex items-center gap-2 py-1 ml-4 cursor-pointer" onclick="openDetail(\'' +
              safeId +
              "')\">" +
              '<span class="w-2 h-2 rounded-full shrink-0" style="background:' +
              catColor +
              '"></span>' +
              '<span class="text-xs" style="color:var(--text-primary)">' +
              escHtml(s.title) +
              "</span>" +
              '<span class="text-xs" style="color:var(--text-muted)">' +
              escHtml(s.status || "") +
              "</span></div>"
            );
          })
          .join("") +
        "</div>";
    }

    panel.innerHTML = html;
  } catch (_e) {
    panel.innerHTML = '<div class="text-red-400 text-xs">Failed to load plan.</div>';
  }
}
