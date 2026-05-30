// ---------------------------------------------------------------------------
// Activity feed view — chronological event timeline.
// ---------------------------------------------------------------------------

import { fetchActivity } from "../api.js";
import { escHtml, escJsSingle } from "../ui.js";

// Render a list of activity events into HTML.
function renderEventList(events) {
  let lastDay = "";
  return events
    .map((e) => {
      const time = e.created_at ? e.created_at.slice(5, 16) : "";
      const day = e.created_at ? e.created_at.slice(0, 10) : "";
      let separator = "";
      if (day && day !== lastDay) {
        lastDay = day;
        separator =
          '<div data-day-separator class="flex items-center gap-2 py-2 mt-2 first:mt-0">' +
          '<div class="flex-1" style="border-top:1px solid var(--border-default)"></div>' +
          '<span class="text-xs font-medium shrink-0" style="color:var(--text-muted)">' +
          day +
          "</span>" +
          '<div class="flex-1" style="border-top:1px solid var(--border-default)"></div>' +
          "</div>";
      }
      const title = e.issue_title ? escHtml(e.issue_title.slice(0, 50)) : escHtml(e.issue_id);
      let detail = "";
      if (e.event_type === "status_changed") detail = `${e.old_value} \u2192 ${e.new_value}`;
      else if (e.new_value) detail = e.new_value;
      const fullTitle = e.issue_title ? escHtml(e.issue_title) : escHtml(e.issue_id);
      return (
        separator +
        '<div class="flex items-center gap-3 py-2 cursor-pointer bg-overlay-hover overflow-hidden" style="border-bottom:1px solid var(--surface-raised);flex-wrap:nowrap" onclick="openDetail(\'' +
        escJsSingle(e.issue_id) +
        "')" +
        ">" +
        '<span class="shrink-0 w-24" style="color:var(--text-muted)">' +
        time +
        "</span>" +
        '<span class="shrink-0 w-32" style="color:var(--text-secondary)">' +
        escHtml(e.event_type) +
        "</span>" +
        '<span class="truncate min-w-0" style="color:var(--text-primary)" title="' +
        fullTitle +
        '">' +
        title +
        "</span>" +
        (detail
          ? '<span class="shrink-0 max-w-[10rem] truncate" style="color:var(--text-muted)" title="' +
            escHtml(detail) +
            '">' +
            escHtml(detail) +
            "</span>"
          : "") +
        (e.actor
          ? '<span class="shrink-0 max-w-[8rem] truncate" style="color:var(--text-muted)" title="' +
            escHtml(e.actor) +
            '">' +
            escHtml(e.actor) +
            "</span>"
          : "") +
        "</div>"
      );
    })
    .join("");
}

/**
 * Render activity events into any container (for embedding in Insights view).
 * Returns the number of events rendered.
 */
export async function renderActivitySection(container, limit = 15) {
  container.innerHTML =
    '<div style="color:var(--text-muted)" class="text-xs">Loading activity...</div>';
  try {
    const events = await fetchActivity(limit);
    if (!events || !events.length) {
      container.innerHTML =
        '<div class="text-xs" style="color:var(--text-muted)">No recent activity.</div>';
      return 0;
    }
    container.innerHTML = renderEventList(events);
    return events.length;
  } catch (err) {
    console.error("[renderActivitySection] Failed:", err);
    container.innerHTML = '<div class="text-xs text-red-400">Failed to load activity.</div>';
    return 0;
  }
}
