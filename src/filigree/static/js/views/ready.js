// ---------------------------------------------------------------------------
// Ready view — issues with no open blockers, sorted by priority.
// ---------------------------------------------------------------------------

import { fetchReady } from "../api.js";
import { callbacks } from "../router.js";
import { escHtml, escJsSingleAttr } from "../ui.js";

const PRIORITY_LABELS = ["P0", "P1", "P2", "P3", "P4"];
const PRIORITY_COLORS = [
  "color: var(--status-done); font-weight: bold", // P0
  "color: var(--accent); font-weight: bold", // P1
  "", // P2
  "color: var(--text-secondary)", // P3
  "color: var(--text-secondary); opacity: 0.6", // P4
];

export async function loadReady() {
  const tbody = document.getElementById("readyTableBody");
  const countEl = document.getElementById("readyCount");
  if (!tbody) return;

  tbody.innerHTML =
    '<tr><td colspan="6" class="py-4 text-center text-secondary">Loading...</td></tr>';

  const issues = await fetchReady();
  if (!issues) {
    tbody.innerHTML =
      '<tr><td colspan="6" class="py-4 text-center text-secondary">Failed to load ready issues</td></tr>';
    return;
  }

  if (countEl) countEl.textContent = `${issues.length} issue${issues.length !== 1 ? "s" : ""}`;

  if (issues.length === 0) {
    tbody.innerHTML =
      '<tr><td colspan="6" class="py-4 text-center text-secondary">No ready issues — all blocked or done</td></tr>';
    return;
  }

  const rows = issues.map((issue) => {
    const p = issue.priority ?? 2;
    const pLabel = PRIORITY_LABELS[p] || `P${p}`;
    const pStyle = PRIORITY_COLORS[p] || "";
    const shortId = issue.id.length > 16 ? issue.id.slice(0, 16) : issue.id;
    return `<tr class="border-b border-strong hover:bg-overlay cursor-pointer" onclick="openDetail('${escJsSingleAttr(issue.id)}')">
      <td class="py-1.5 px-2 font-mono" style="${pStyle}">${pLabel}</td>
      <td class="py-1.5 px-2 font-mono text-accent">${escHtml(shortId)}</td>
      <td class="py-1.5 px-2">${escHtml(issue.type || "")}</td>
      <td class="py-1.5 px-2 text-primary">${escHtml(issue.title || "")}</td>
      <td class="py-1.5 px-2 text-secondary">${escHtml(issue.assignee || "")}</td>
      <td class="py-1.5 px-2 text-secondary">${escHtml(issue.status || "")}</td>
    </tr>`;
  });

  tbody.innerHTML = rows.join("");
}

// Expose openDetail for inline onclick
function openDetail(issueId) {
  if (callbacks.openDetail) callbacks.openDetail(issueId);
}
window.openDetail = openDetail;
