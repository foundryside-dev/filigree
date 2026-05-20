// ---------------------------------------------------------------------------
// Code Health view — hotspots, severity donut, scan coverage, recent scans.
// ---------------------------------------------------------------------------

import { fetchFiles, fetchFileStats, fetchHotspots, fetchScanRuns } from "../api.js";
import { SEVERITY_COLORS, state } from "../state.js";
import { escHtml, escJsSingle, relativeTime } from "../ui.js";

export function healthOverviewUnavailableReason({ hotspots, fileData, stats, scanRunData }) {
  if (stats === null || stats === undefined) return "Code quality statistics are unavailable.";
  if (fileData === null || fileData === undefined) return "Tracked file counts are unavailable.";
  if (hotspots === null || hotspots === undefined) return "Hotspot data is unavailable.";
  if (scanRunData === null || scanRunData === undefined) return "Scan run history is unavailable.";
  return "";
}

/**
 * Render the full health overview into any container (for embedding in Files view).
 * @param {HTMLElement} container - Target container element
 * @param {Object} callbacks - { onClickFile: (id) => onclickExpr, onClickScan: (source) => onclickExpr }
 */
export async function renderHealthOverview(container, { onClickFile, onClickScan } = {}) {
  container.innerHTML =
    '<div style="color:var(--text-muted)" class="text-xs">Loading code quality...</div>';
  try {
    const [hotspots, fileData, stats, scanRunData] = await Promise.all([
      fetchHotspots(10),
      fetchFiles({ limit: 1, offset: 0 }),
      fetchFileStats(),
      fetchScanRuns(10),
    ]);

    const unavailableReason = healthOverviewUnavailableReason({
      hotspots,
      fileData,
      stats,
      scanRunData,
    });
    if (unavailableReason) {
      container.innerHTML =
        '<div class="text-xs text-red-400">Failed to load code quality data: ' +
        escHtml(unavailableReason) +
        "</div>";
      return;
    }

    if (!hotspots && !fileData && !stats) {
      container.innerHTML =
        '<div class="text-xs" style="color:var(--text-muted)">No scan data yet — ingest results to see code health.</div>';
      return;
    }

    state.hotspots = hotspots;
    const agg = {
      critical: stats?.critical || 0,
      high: stats?.high || 0,
      medium: stats?.medium || 0,
      low: stats?.low || 0,
      info: stats?.info || 0,
    };
    const totalFiles = fileData?.total || 0;
    const filesWithFindings = stats?.files_with_findings || 0;
    const scanRuns = scanRunData?.scan_runs || [];

    container.innerHTML =
      '<div class="grid grid-cols-1 md:grid-cols-2 gap-4">' +
      renderHotspotsWidget(hotspots, onClickFile) +
      renderDonutWidget(agg) +
      renderCoverageWidget(filesWithFindings, totalFiles) +
      renderRecentScansWidget(scanRuns, onClickScan) +
      "</div>";
  } catch (err) {
    console.warn("[health] Failed to load health data:", err);
    container.innerHTML = '<div class="text-xs text-red-400">Failed to load health data.</div>';
  }
}

// --- Widget 1: Top 10 Hotspot Files ---

function renderHotspotsWidget(hotspots, onClickFile) {
  if (!hotspots || !hotspots.length) {
    return (
      '<div class="rounded p-4" style="background:var(--surface-raised);border:1px solid var(--border-default)">' +
      '<div class="text-xs font-medium mb-3" style="color:var(--text-secondary)">Top Hotspot Files</div>' +
      '<div style="color:var(--text-muted)" class="text-xs">No hotspots found.</div></div>'
    );
  }

  const maxScore = hotspots[0]?.score || 1;

  const rows = hotspots
    .map((h) => {
      const f = h.file || {};
      const b = h.findings_breakdown || {};
      const total =
        (b.critical || 0) + (b.high || 0) + (b.medium || 0) + (b.low || 0) + (b.info || 0);
      if (total === 0) return "";

      // Stacked bar segments
      const segments = ["critical", "high", "medium", "low", "info"]
        .filter((s) => b[s])
        .map((s) => {
          const pct = ((b[s] / total) * 100).toFixed(1);
          return `<div style="width:${pct}%;background:${SEVERITY_COLORS[s].hex}" class="h-full"></div>`;
        })
        .join("");

      const barWidth = ((h.score / maxScore) * 100).toFixed(1);

      return (
        `<div class="flex items-center gap-2 mb-2 cursor-pointer bg-overlay-hover rounded px-2 py-1" onclick="${onClickFile ? onClickFile(f.id) : `switchView('files');setTimeout(()=>openFileDetail('${escJsSingle(f.id)}'),100)`}" role="button" tabindex="0">` +
        `<span class="text-xs truncate w-48" style="color:var(--text-primary)" title="${escHtml(f.path)}">${escHtml(f.path)}</span>` +
        `<div class="flex-1 h-3 rounded overflow-hidden flex" style="background:var(--surface-base);max-width:${barWidth}%">` +
        segments +
        "</div>" +
        `<span class="text-xs w-8 text-right" style="color:var(--text-muted)">${escHtml(String(h.score))}</span>` +
        "</div>"
      );
    })
    .join("");

  return (
    '<div class="rounded p-4" style="background:var(--surface-raised);border:1px solid var(--border-default)">' +
    '<div class="text-xs font-medium mb-3" style="color:var(--text-secondary)">Top Hotspot Files</div>' +
    rows +
    "</div>"
  );
}

// --- Widget 2: Findings by Severity (CSS donut) ---

function renderDonutWidget(agg) {
  const total = agg.critical + agg.high + agg.medium + agg.low + agg.info;
  if (total === 0) {
    return (
      '<div class="rounded p-4" style="background:var(--surface-raised);border:1px solid var(--border-default)">' +
      '<div class="text-xs font-medium mb-3" style="color:var(--text-secondary)">Findings by Severity</div>' +
      '<div style="color:var(--text-muted)" class="text-xs">No findings to display.</div></div>'
    );
  }

  // Build conic-gradient segments
  const segments = [];
  let cumPct = 0;
  for (const sev of ["critical", "high", "medium", "low", "info"]) {
    if (agg[sev]) {
      const pct = (agg[sev] / total) * 100;
      segments.push(`${SEVERITY_COLORS[sev].hex} ${cumPct}% ${cumPct + pct}%`);
      cumPct += pct;
    }
  }
  const gradient = `conic-gradient(${segments.join(", ")})`;

  const legend = ["critical", "high", "medium", "low", "info"]
    .filter((s) => agg[s])
    .map(
      (s) =>
        `<span class="flex items-center gap-1"><span class="w-2 h-2 rounded-full" style="background:${SEVERITY_COLORS[s].hex}"></span>${s}: ${agg[s]}</span>`,
    )
    .join("");

  return (
    '<div class="rounded p-4" style="background:var(--surface-raised);border:1px solid var(--border-default)">' +
    '<div class="text-xs font-medium mb-3" style="color:var(--text-secondary)">Findings by Severity</div>' +
    '<div class="flex items-center gap-6">' +
    '<div class="relative" style="width:120px;height:120px">' +
    `<div style="width:100%;height:100%;border-radius:50%;background:${gradient}"></div>` +
    '<div class="absolute inset-0 flex items-center justify-center">' +
    '<div class="rounded-full flex items-center justify-center" style="width:64px;height:64px;background:var(--surface-raised)">' +
    `<span class="text-lg font-bold" style="color:var(--text-primary)">${total}</span>` +
    "</div></div></div>" +
    `<div class="flex flex-col gap-1 text-xs" style="color:var(--text-secondary)">${legend}</div>` +
    "</div></div>"
  );
}

// --- Widget 3: Scan Coverage ---

function renderCoverageWidget(withFindings, total) {
  const pct = total > 0 ? ((withFindings / total) * 100).toFixed(0) : 0;

  return (
    '<div class="rounded p-4" style="background:var(--surface-raised);border:1px solid var(--border-default)">' +
    '<div class="text-xs font-medium mb-3" style="color:var(--text-secondary)">Scan Coverage</div>' +
    `<div class="text-2xl font-bold mb-2" style="color:var(--accent)">${pct}%</div>` +
    '<div class="h-3 rounded overflow-hidden mb-2" style="background:var(--surface-base)">' +
    `<div class="h-full rounded" style="width:${pct}%;background:var(--accent)"></div>` +
    "</div>" +
    `<div class="text-xs" style="color:var(--text-muted)">${withFindings} files with findings out of ${total} tracked</div>` +
    "</div>"
  );
}

// --- Widget 4: Recent Scan Activity ---

/** Format an ISO timestamp as a relative time string (e.g. "2h ago"). */
/** Return a Tailwind-ish badge color class per scan_source. */
function _sourceBadge(source) {
  const s = (source || "").toLowerCase();
  const colors = {
    codex: "background:#3b82f6;color:#fff",
    claude: "background:#8b5cf6;color:#fff",
    ruff: "background:#f59e0b;color:#000",
  };
  return colors[s] || "background:var(--surface-overlay);color:var(--text-secondary)";
}

function renderRecentScansWidget(scanRuns, onClickScan) {
  const header =
    '<div class="text-xs font-medium mb-3" style="color:var(--text-secondary)">Recent Scan Activity</div>';
  const wrapper =
    '<div class="rounded p-4" style="background:var(--surface-raised);border:1px solid var(--border-default)">';

  // Empty state — show API guidance
  if (!scanRuns || !scanRuns.length) {
    return (
      wrapper +
      header +
      '<div class="text-xs" style="color:var(--text-muted)">' +
      '<div class="mb-2">No scan runs recorded yet.</div>' +
      '<div class="mb-1">Ingest scan results via the API:</div>' +
      '<code class="block rounded px-2 py-1 text-xs mb-2" style="background:var(--surface-base);color:var(--text-secondary)">POST /api/v1/scan-results</code>' +
      "<div>Example scanners available in <code>scripts/</code></div>" +
      "</div></div>"
    );
  }

  // Scan history rows
  const rows = scanRuns
    .map((run) => {
      const source = escHtml(run.scan_source || "unknown");
      const runId = escHtml(run.scan_run_id || "");
      const time = relativeTime(run.started_at);
      const files = run.files_scanned || 0;
      const findings = run.total_findings || 0;

      return (
        `<div class="flex items-center gap-2 mb-2 rounded px-2 py-1.5 cursor-pointer bg-overlay-hover"${onClickScan ? ` onclick="${onClickScan(run.scan_source || "")}"` : ""} role="button" tabindex="0">` +
        `<span class="text-xs font-medium rounded px-1.5 py-0.5 shrink-0" style="${_sourceBadge(run.scan_source)}">${source}</span>` +
        `<span class="text-xs truncate flex-1" style="color:var(--text-primary)" title="${runId}">${runId}</span>` +
        `<span class="text-xs shrink-0" style="color:var(--text-muted)">${escHtml(String(files))} files</span>` +
        `<span class="text-xs shrink-0" style="color:var(--text-muted)">${escHtml(String(findings))} findings</span>` +
        `<span class="text-xs shrink-0" style="color:var(--text-muted)">${escHtml(time)}</span>` +
        "</div>"
      );
    })
    .join("");

  return wrapper + header + rows + "</div>";
}
