// ---------------------------------------------------------------------------
// Insights view — throughput, cycle/lead time, agent workload, sparkline,
// and embedded activity feed.
// ---------------------------------------------------------------------------

import { fetchActivity, fetchMetrics, fetchObservationStats } from "../api.js";
import { state, THEME_COLORS } from "../state.js";
import { escHtml, escJsSingleAttr } from "../ui.js";
import { renderActivitySection } from "./activity.js";

/**
 * Load and render the metrics dashboard (throughput, cycle time, lead time,
 * per-type breakdown, and agent workload bars).
 */
export async function loadMetrics() {
  const days = document.getElementById("insightsDays")?.value || "30";
  const container = document.getElementById("insightsContent");
  container.innerHTML = '<div style="color:var(--text-muted)">Loading...</div>';
  try {
    const [m, obsStats] = await Promise.all([fetchMetrics(days), fetchObservationStats()]);
    if (!m) {
      container.innerHTML = '<div class="text-red-400">Failed to load metrics.</div>';
      return;
    }
    const byTypeHtml = Object.keys(m.by_type || {})
      .map((t) => {
        const d = m.by_type[t];
        return (
          '<tr><td class="py-1 pr-4" style="color:var(--text-primary)">' +
          escHtml(t) +
          "</td>" +
          '<td class="py-1 pr-4">' +
          (d.avg_cycle_time_hours !== null ? `${d.avg_cycle_time_hours}h` : "\u2014") +
          "</td>" +
          '<td class="py-1">' +
          d.count +
          "</td></tr>"
        );
      })
      .join("");

    container.innerHTML =
      '<div class="grid grid-cols-3 gap-4 mb-6">' +
      '<div class="rounded p-4" style="background:var(--surface-raised);border:1px solid var(--border-default)">' +
      '<div class="text-xs mb-1" style="color:var(--text-muted)">Throughput</div>' +
      '<div class="text-2xl font-bold" style="color:var(--accent)">' +
      m.throughput +
      "</div>" +
      '<div class="text-xs" style="color:var(--text-muted)">issues closed</div></div>' +
      '<div class="rounded p-4" style="background:var(--surface-raised);border:1px solid var(--border-default)">' +
      '<div class="text-xs mb-1" style="color:var(--text-muted)">Avg Cycle Time</div>' +
      '<div class="text-2xl font-bold text-emerald-400">' +
      (m.avg_cycle_time_hours !== null ? `${m.avg_cycle_time_hours}h` : "\u2014") +
      "</div>" +
      '<div class="text-xs" style="color:var(--text-muted)">first WIP to done</div></div>' +
      '<div class="rounded p-4" style="background:var(--surface-raised);border:1px solid var(--border-default)">' +
      '<div class="text-xs mb-1" style="color:var(--text-muted)">Avg Lead Time</div>' +
      '<div class="text-2xl font-bold text-amber-400">' +
      (m.avg_lead_time_hours !== null ? `${m.avg_lead_time_hours}h` : "\u2014") +
      "</div>" +
      '<div class="text-xs" style="color:var(--text-muted)">creation to done</div></div>' +
      "</div>" +
      (byTypeHtml
        ? '<div class="rounded p-4" style="background:var(--surface-raised);border:1px solid var(--border-default)">' +
          '<div class="text-xs font-medium mb-2" style="color:var(--text-secondary)">By Type</div>' +
          '<table class="text-xs w-full"><thead><tr style="color:var(--text-muted)">' +
          '<th class="text-left py-1 pr-4">Type</th><th class="text-left py-1 pr-4">Avg Cycle</th><th class="text-left py-1">Count</th>' +
          "</tr></thead><tbody>" +
          byTypeHtml +
          "</tbody></table></div>"
        : '<div class="p-6 text-center" style="color:var(--text-muted)">' +
          '<div class="font-medium mb-2" style="color:var(--text-primary)">No completed issues in this period</div>' +
          '<div style="color:var(--text-muted)">Metrics track throughput, cycle time, and lead time.</div>' +
          '<div style="color:var(--text-muted)" class="mt-1">Close issues to see flow data here.</div>' +
          '<div class="mt-3"><button onclick="var d=document.getElementById(\'insightsDays\');if(d)d.value=\'90\';loadMetrics()" style="color:var(--accent)" class="hover:underline text-xs">Try 90-day window</button></div></div>');

    renderAgentWorkload(container);
    renderObservationStats(container, obsStats);
    await renderActivityEmbed(container);
  } catch (_e) {
    container.innerHTML = '<div class="text-red-400">Failed to load metrics.</div>';
  }
}

function renderAgentWorkload(container) {
  const agentLoad = {};
  for (const i of state.allIssues) {
    if (i.assignee && (i.status_category || "open") === "wip") {
      agentLoad[i.assignee] = (agentLoad[i.assignee] || 0) + 1;
    }
  }
  const agents = Object.keys(agentLoad).sort((a, b) => agentLoad[b] - agentLoad[a]);
  if (!agents.length) return;
  const maxLoad = Math.max(...agents.map((a) => agentLoad[a]));
  const agentHtml = agents
    .map((a) => {
      const pct = (agentLoad[a] / maxLoad) * 100;
      return (
        '<div class="flex items-center gap-2 mb-1">' +
        '<span class="text-xs w-24 truncate" style="color:var(--text-primary)">' +
        escHtml(a) +
        "</span>" +
        '<div class="flex-1 h-4 rounded overflow-hidden" style="background:var(--surface-base)">' +
        '<div class="h-full rounded" style="width:' +
        pct +
        '%;background:var(--accent)"></div></div>' +
        '<span class="text-xs w-6 text-right" style="color:var(--text-secondary)">' +
        agentLoad[a] +
        "</span></div>"
      );
    })
    .join("");
  container.innerHTML +=
    '<div class="rounded p-4 mt-4" style="background:var(--surface-raised);border:1px solid var(--border-default)">' +
    '<div class="text-xs font-medium mb-2" style="color:var(--text-secondary)">Agent Workload (Active WIP)</div>' +
    agentHtml +
    "</div>";
}

export function renderObservationStats(container, obsStats) {
  if (!obsStats || obsStats.count <= 0) return;
  const staleColor = obsStats.stale_count > 0 ? "text-amber-400" : "text-emerald-400";
  const expiringColor = obsStats.expiring_soon_count > 0 ? "text-red-400" : "text-emerald-400";
  container.innerHTML +=
    '<div class="rounded p-4 mt-4" style="background:var(--surface-raised);border:1px solid var(--border-default)">' +
    '<div class="text-xs font-medium mb-3" style="color:var(--text-secondary)">Observations</div>' +
    '<div class="grid grid-cols-4 gap-3">' +
    '<div><div class="text-lg font-bold" style="color:var(--accent)">' +
    escHtml(String(obsStats.count)) +
    "</div>" +
    '<div class="text-[10px]" style="color:var(--text-muted)">pending</div></div>' +
    '<div><div class="text-lg font-bold ' +
    staleColor +
    '">' +
    escHtml(String(obsStats.stale_count)) +
    "</div>" +
    '<div class="text-[10px]" style="color:var(--text-muted)">stale (&gt;48h)</div></div>' +
    '<div><div class="text-lg font-bold ' +
    expiringColor +
    '">' +
    escHtml(String(obsStats.expiring_soon_count)) +
    "</div>" +
    '<div class="text-[10px]" style="color:var(--text-muted)">expiring &lt;24h</div></div>' +
    '<div><div class="text-lg font-bold" style="color:var(--text-primary)">' +
    escHtml(obsStats.oldest_hours != null ? obsStats.oldest_hours + "h" : "?") +
    "</div>" +
    '<div class="text-[10px]" style="color:var(--text-muted)">oldest</div></div>' +
    "</div></div>";
}

async function renderActivityEmbed(container) {
  const activityBox = document.createElement("div");
  activityBox.className = "rounded mt-4";
  activityBox.style.cssText =
    "background:var(--surface-raised);border:1px solid var(--border-default)";
  activityBox.innerHTML =
    '<div class="text-xs font-medium px-4 py-3" style="color:var(--text-secondary)">Recent Activity</div>' +
    '<div id="activityEmbedContent" class="px-4 pb-3"></div>';
  container.appendChild(activityBox);

  const actContent = document.getElementById("activityEmbedContent");
  if (!actContent) return;
  const count = await renderActivitySection(actContent, 15);
  if (count > 4) {
    const rows = actContent.querySelectorAll(":scope > div");
    let visible = 0;
    rows.forEach((row) => {
      if (row.hasAttribute("data-day-separator")) return;
      visible++;
      if (visible > 4) row.classList.add("hidden");
    });
    const more = document.createElement("button");
    more.className = "text-xs mt-2 hover:underline";
    more.style.color = "var(--accent)";
    more.textContent = `Show ${count - 4} more events`;
    more.onclick = () => {
      actContent.querySelectorAll(".hidden").forEach((el) => el.classList.remove("hidden"));
      more.remove();
    };
    actContent.appendChild(more);
  }
  const header = activityBox.querySelector(".text-xs.font-medium");
  if (header && count) header.textContent = `Recent Activity (${count} events)`;
}

/**
 * Render a 14-day sparkline of closed-issue activity onto the #sparkline canvas.
 */
export async function renderSparkline() {
  try {
    const events = await fetchActivity(500);
    if (!events) return;
    const closedByDay = {};
    const now = Date.now();
    for (const e of events) {
      if (
        e.event_type !== "closed" &&
        !(e.event_type === "status_changed" && e.new_value === "closed")
      )
        continue;
      const dayAgo = Math.floor((now - new Date(e.created_at).getTime()) / 86400000);
      if (dayAgo < 14) closedByDay[dayAgo] = (closedByDay[dayAgo] || 0) + 1;
    }
    const data = [];
    for (let i = 13; i >= 0; i--) data.push(closedByDay[i] || 0);
    const canvas = document.getElementById("sparkline");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const w = canvas.width;
    const h = canvas.height;
    ctx.clearRect(0, 0, w, h);
    const max = Math.max(...data, 1);
    ctx.strokeStyle = THEME_COLORS.accent;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    data.forEach((v, i) => {
      const x = (i / (data.length - 1)) * w;
      const y = h - (v / max) * (h - 4) - 2;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  } catch (err) {
    console.debug("[renderSparkline] Non-critical sparkline error:", err);
  }
}

/**
 * Scan WIP issues for staleness (>2 h since last update) and update the
 * #staleBadge element.  Stores the stale list on state._staleIssues for
 * the modal.
 */
export function updateStaleBadge() {
  const stale = state.allIssues.filter(
    (i) =>
      (i.status_category || "open") === "wip" &&
      i.updated_at &&
      Date.now() - new Date(i.updated_at).getTime() > 2 * 3600000,
  );
  const badge = document.getElementById("staleBadge");
  if (!badge) return;
  if (stale.length) {
    badge.textContent = `${stale.length} stale`;
    badge.classList.remove("hidden");
  } else {
    badge.classList.add("hidden");
  }
  state._staleIssues = stale;
}

/**
 * Show a modal listing all stale WIP issues.  If only one, jump straight to
 * its detail panel.
 */
export function showStaleIssues() {
  const stale = state._staleIssues || [];
  if (!stale.length) return;
  if (stale.length === 1) {
    // openDetail is still on the global scope during migration
    window.openDetail(stale[0].id);
    return;
  }
  const modal = document.createElement("div");
  modal.id = "staleModal";
  modal.className = "fixed inset-0 bg-black/50 flex items-center justify-center z-50";
  modal.onclick = (ev) => {
    if (ev.target === modal) modal.remove();
  };
  modal.innerHTML =
    '<div class="rounded-lg p-4 w-80 shadow-xl max-h-96 overflow-y-auto" style="background:var(--surface-raised);border:1px solid var(--border-strong)">' +
    '<div class="text-sm mb-3" style="color:var(--text-primary)">' +
    stale.length +
    " stale issues</div>" +
    '<div class="flex flex-col gap-2">' +
    stale
      .map((i) => {
        const hrs = Math.floor((Date.now() - new Date(i.updated_at).getTime()) / 3600000);
        return (
          "<button onclick=\"document.getElementById('staleModal').remove();openDetail('" +
          escJsSingleAttr(i.id) +
          "')\" " +
          'class="text-xs text-left bg-overlay bg-overlay-hover px-3 py-2 rounded" style="color:var(--text-primary)">' +
          escHtml(i.title.slice(0, 40)) +
          ' <span class="text-red-400">(' +
          hrs +
          "h)</span></button>"
        );
      })
      .join("") +
    "</div>" +
    '<button onclick="document.getElementById(\'staleModal\').remove()" class="text-xs text-muted text-primary-hover mt-3">Close</button>' +
    "</div>";
  document.body.appendChild(modal);
}
