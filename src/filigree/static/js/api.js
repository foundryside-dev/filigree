// ---------------------------------------------------------------------------
// Pure API client — all fetch calls to the Filigree backend.
//
// Read operations return parsed JSON on success, or null on any error
// (HTTP error or network failure). Callers must check for null.
//
// Write operations return { ok, data?, error? } via writeRequest().
// ---------------------------------------------------------------------------

import { state } from "./state.js";

function apiUrl(path) {
  return state.API_BASE + path;
}

const JSON_HEADERS = { "Content-Type": "application/json" };

/** Extract a human-readable message from structured or legacy error bodies. */
function extractError(body, fallback) {
  const e = body?.error;
  if (e && typeof e === "object") return e.message || fallback;
  return e || fallback;
}

/** Generic write (POST/PATCH/DELETE) helper — returns { ok, data?, error? }. */
async function writeRequest(path, { method = "POST", body, errorLabel } = {}) {
  try {
    const opts = { method, headers: JSON_HEADERS };
    if (body !== undefined) opts.body = JSON.stringify(body);
    const resp = await fetch(apiUrl(path), opts);
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      return { ok: false, error: extractError(err, errorLabel) };
    }
    return { ok: true, data: await resp.json() };
  } catch (_e) {
    return { ok: false, error: "Network error" };
  }
}

// --- Read operations (return data or null) ---

export async function fetchIssues() {
  try {
    const resp = await fetch(apiUrl("/issues"));
    if (!resp.ok) return null;
    return await resp.json();
  } catch (err) {
    console.warn("[fetchIssues] Network error:", err);
    return null;
  }
}

export async function fetchDeps() {
  try {
    const resp = await fetch(apiUrl("/dependencies"));
    if (!resp.ok) return null;
    return await resp.json();
  } catch (err) {
    console.warn("[fetchDeps] Network error:", err);
    return null;
  }
}

export async function fetchStats() {
  try {
    const resp = await fetch(apiUrl("/stats"));
    if (!resp.ok) return null;
    return await resp.json();
  } catch (err) {
    console.warn("[fetchStats] Network error:", err);
    return null;
  }
}

export async function fetchDashboardConfig() {
  try {
    const resp = await fetch(apiUrl("/config"));
    if (!resp.ok) return null;
    return await resp.json();
  } catch (err) {
    console.warn("[fetchDashboardConfig] Network error:", err);
    return null;
  }
}

export async function fetchAllData() {
  try {
    const results = await Promise.allSettled([
      fetch(apiUrl("/issues")),
      fetch(apiUrl("/dependencies")),
      fetch(apiUrl("/stats")),
    ]);
    const [issuesRes, depsRes, statsRes] = results;
    const labels = ["issues", "dependencies", "stats"];
    for (let i = 0; i < results.length; i++) {
      const r = results[i];
      if (r.status !== "fulfilled") {
        console.error(`[fetchAllData] ${labels[i]} request failed:`, r.reason);
        return null;
      }
      if (!r.value.ok) {
        console.error(`[fetchAllData] ${labels[i]} returned HTTP ${r.value.status}`);
        return null;
      }
    }
    return {
      issues: await issuesRes.value.json(),
      deps: await depsRes.value.json(),
      stats: await statsRes.value.json(),
    };
  } catch (err) {
    console.error("[fetchAllData] Unexpected error:", err);
    return null;
  }
}

export async function fetchIssueDetail(issueId) {
  try {
    const resp = await fetch(apiUrl(`/issue/${issueId}`));
    if (!resp.ok) return null;
    return await resp.json();
  } catch (err) {
    console.warn("[fetchIssueDetail] Network error:", err);
    return null;
  }
}

export async function fetchIssueFiles(issueId) {
  try {
    const resp = await fetch(apiUrl(`/issue/${issueId}/files`));
    if (!resp.ok) return null;
    return await resp.json();
  } catch (err) {
    console.warn("[fetchIssueFiles] Network error:", err);
    return null;
  }
}

export async function fetchIssueEntityAssociations(issueId) {
  try {
    const resp = await fetch(apiUrl(`/issue/${issueId}/entity-associations`));
    if (!resp.ok) return null;
    return await resp.json();
  } catch (err) {
    console.warn("[fetchIssueEntityAssociations] Network error:", err);
    return null;
  }
}

export async function fetchTransitions(issueId) {
  try {
    const resp = await fetch(apiUrl(`/issue/${issueId}/transitions`));
    if (!resp.ok) {
      console.warn(`[fetchTransitions] HTTP ${resp.status} for issue ${issueId}`);
      return null;
    }
    return await resp.json();
  } catch (err) {
    console.error(`[fetchTransitions] Network error for issue ${issueId}:`, err);
    return null;
  }
}

export async function fetchMetrics(days) {
  try {
    const resp = await fetch(apiUrl(`/metrics?days=${days}`));
    if (!resp.ok) return null;
    return await resp.json();
  } catch (err) {
    console.warn("[fetchMetrics] Network error:", err);
    return null;
  }
}

export async function fetchObservationStats() {
  try {
    const resp = await fetch(apiUrl("/observations/stats"));
    if (!resp.ok) return null;
    return await resp.json();
  } catch (err) {
    console.warn("[fetchObservationStats] Network error:", err);
    return null;
  }
}

export async function fetchActivity(limit, since) {
  try {
    let url = apiUrl("/activity");
    const params = [];
    if (limit) params.push(`limit=${limit}`);
    if (since) params.push(`since=${encodeURIComponent(since)}`);
    if (params.length) url += `?${params.join("&")}`;
    const resp = await fetch(url);
    if (!resp.ok) return null;
    return await resp.json();
  } catch (err) {
    console.warn("[fetchActivity] Network error:", err);
    return null;
  }
}

export async function fetchTypeInfo(typeName) {
  try {
    const resp = await fetch(apiUrl(`/type/${encodeURIComponent(typeName)}`));
    if (!resp.ok) return null;
    return await resp.json();
  } catch (err) {
    console.warn("[fetchTypeInfo] Network error:", err);
    return null;
  }
}

export async function fetchTypes() {
  try {
    const resp = await fetch(apiUrl("/types"));
    if (!resp.ok) return null;
    return await resp.json();
  } catch (err) {
    console.warn("[fetchTypes] Network error:", err);
    return null;
  }
}

export async function fetchPlan(milestoneId) {
  try {
    const resp = await fetch(apiUrl(`/plan/${milestoneId}`));
    if (!resp.ok) return null;
    return await resp.json();
  } catch (err) {
    console.warn("[fetchPlan] Network error:", err);
    return null;
  }
}

export async function fetchCriticalPath() {
  try {
    const resp = await fetch(apiUrl("/critical-path"));
    if (!resp.ok) return null;
    return await resp.json();
  } catch (err) {
    console.warn("[fetchCriticalPath] Network error:", err);
    return null;
  }
}

export async function fetchReady() {
  try {
    const resp = await fetch(apiUrl("/ready"));
    if (!resp.ok) return null;
    return await resp.json();
  } catch (err) {
    console.warn("[fetchReady] Network error:", err);
    return null;
  }
}

export async function fetchReleases(includeReleased = false) {
  try {
    const qs = includeReleased ? "?include_released=true" : "";
    const resp = await fetch(apiUrl(`/releases${qs}`));
    if (!resp.ok) return null;
    return await resp.json();
  } catch (err) {
    console.warn("[fetchReleases] Network error:", err);
    return null;
  }
}

export async function fetchReleaseTree(releaseId) {
  try {
    const resp = await fetch(apiUrl(`/release/${releaseId}/tree`));
    if (!resp.ok) return null;
    return await resp.json();
  } catch (err) {
    console.warn("[fetchReleaseTree] Network error:", err);
    return null;
  }
}

export async function fetchSearch(query, limit) {
  try {
    let url = apiUrl(`/search?q=${encodeURIComponent(query)}`);
    if (limit) url += `&limit=${limit}`;
    const resp = await fetch(url);
    if (!resp.ok) return null;
    return await resp.json();
  } catch (err) {
    console.warn("[fetchSearch] Network error:", err);
    return null;
  }
}

export async function fetchProjects(ttl) {
  try {
    const resp = await fetch(`/api/projects${ttl ? `?ttl=${ttl}` : ""}`);
    if (!resp.ok) return null;
    return await resp.json();
  } catch (err) {
    console.warn("[fetchProjects] Network error:", err);
    return null;
  }
}

// --- Write operations (return { ok, data?, error? }) ---

export function patchIssue(issueId, body) {
  return writeRequest(`/issue/${issueId}`, { method: "PATCH", body, errorLabel: "Update failed" });
}

export function postCloseIssue(issueId, reason) {
  return writeRequest(`/issue/${issueId}/close`, {
    body: { reason: reason || "" },
    errorLabel: "Close failed",
  });
}

export function postReopenIssue(issueId) {
  return writeRequest(`/issue/${issueId}/reopen`, { body: {}, errorLabel: "Reopen failed" });
}

export function postClaimIssue(issueId, assignee) {
  return writeRequest(`/issue/${issueId}/claim`, {
    body: { assignee },
    errorLabel: "Claim failed",
  });
}

export function postReleaseIssue(issueId) {
  return writeRequest(`/issue/${issueId}/release`, { body: {}, errorLabel: "Release failed" });
}

export function postAddDependency(issueId, dependsOnId) {
  return writeRequest(`/issue/${issueId}/dependencies`, {
    body: { depends_on: dependsOnId },
    errorLabel: "Add failed",
  });
}

export function deleteIssueDep(issueId, depId) {
  return writeRequest(`/issue/${issueId}/dependencies/${depId}`, {
    method: "DELETE",
    errorLabel: "Remove failed",
  });
}

export function postComment(issueId, text) {
  return writeRequest(`/issue/${issueId}/comments`, {
    body: { text },
    errorLabel: "Comment failed",
  });
}

export function postCreateIssue(body) {
  return writeRequest("/issues", { body, errorLabel: "Create failed" });
}

export function postBatchUpdate(issueIds, fields) {
  return writeRequest("/batch/update", {
    body: { issue_ids: issueIds, ...fields },
    errorLabel: "Batch update failed",
  });
}

export function postBatchClose(issueIds, reason, actor) {
  const body = { issue_ids: issueIds };
  if (reason) body.reason = reason;
  if (actor) body.actor = actor;
  return writeRequest("/batch/close", { body, errorLabel: "Batch close failed" });
}

export async function postReload() {
  try {
    const resp = await fetch("/api/reload", { method: "POST" });
    if (!resp.ok) {
      console.error("[postReload] HTTP", resp.status);
      return { ok: false };
    }
    return resp.json();
  } catch (err) {
    console.error("[postReload] Network error:", err);
    return { ok: false };
  }
}

// --- File & Findings API ---

export async function fetchFiles(params) {
  try {
    const qs = params ? "?" + new URLSearchParams(params) : "";
    const resp = await fetch(apiUrl("/files" + qs));
    if (!resp.ok) return null;
    return await resp.json();
  } catch (err) {
    console.warn("[fetchFiles] Network error:", err);
    return null;
  }
}

export async function fetchFileDetail(fileId) {
  try {
    const resp = await fetch(apiUrl(`/files/${encodeURIComponent(fileId)}`));
    if (!resp.ok) return null;
    return await resp.json();
  } catch (err) {
    console.warn("[fetchFileDetail] Network error:", err);
    return null;
  }
}

export async function fetchFileFindings(fileId, params) {
  try {
    const qs = params ? "?" + new URLSearchParams(params) : "";
    const resp = await fetch(apiUrl(`/files/${encodeURIComponent(fileId)}/findings` + qs));
    if (!resp.ok) return null;
    return await resp.json();
  } catch (err) {
    console.warn("[fetchFileFindings] Network error:", err);
    return null;
  }
}

export async function fetchFileTimeline(fileId, params) {
  try {
    const qs = params ? "?" + new URLSearchParams(params) : "";
    const resp = await fetch(apiUrl(`/files/${encodeURIComponent(fileId)}/timeline` + qs));
    if (!resp.ok) return null;
    return await resp.json();
  } catch (err) {
    console.warn("[fetchFileTimeline] Network error:", err);
    return null;
  }
}

export async function fetchHotspots(limit) {
  try {
    const qs = limit ? `?limit=${limit}` : "";
    const resp = await fetch(apiUrl("/files/hotspots" + qs));
    if (!resp.ok) return null;
    return await resp.json();
  } catch (err) {
    console.warn("[fetchHotspots] Network error:", err);
    return null;
  }
}

export async function fetchFileStats() {
  try {
    const resp = await fetch(apiUrl("/files/stats"));
    if (!resp.ok) return null;
    return await resp.json();
  } catch (err) {
    console.warn("[fetchFileStats] Network error:", err);
    return null;
  }
}

export async function fetchFileSchema() {
  try {
    const resp = await fetch(apiUrl("/files/_schema"));
    if (!resp.ok) return null;
    return await resp.json();
  } catch (err) {
    console.warn("[fetchFileSchema] Network error:", err);
    return null;
  }
}

export async function fetchScanRuns(limit) {
  try {
    const qs = limit ? `?limit=${limit}` : "";
    const resp = await fetch(apiUrl("/scan-runs" + qs));
    if (!resp.ok) {
      console.warn("[fetchScanRuns] HTTP", resp.status);
      return null;
    }
    return await resp.json();
  } catch (err) {
    console.error("[fetchScanRuns] Network error:", err);
    return null;
  }
}

export function postFileAssociation(fileId, body) {
  return writeRequest(`/files/${encodeURIComponent(fileId)}/associations`, {
    body,
    errorLabel: "Association failed",
  });
}

export function patchFileFinding(fileId, findingId, body) {
  return writeRequest(
    `/files/${encodeURIComponent(fileId)}/findings/${encodeURIComponent(findingId)}`,
    { method: "PATCH", body, errorLabel: "Finding update failed" },
  );
}
