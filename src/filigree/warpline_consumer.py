"""Consumer binding — file or link Warpline reverify worklists as Filigree work.

Seam 2A of the frozen warpline interface (``pm/2026-06-13-warpline-interface-lock.md``):
warpline PRODUCES a reverify worklist (``warpline.reverify_worklist.v1``); Filigree
is the write-capable CONSUMER that, on **explicit** action, files new work items
for not-yet-tracked affected entities and reports the already-tracked ones.

Authority split (``post-admission-consumer-tickets.md:32-36``): Filigree owns
work state, issue lifecycle, claims, and close gates. warpline never auto-files —
it only surfaces ``next_actions.filigree[]`` *candidates*. The explicit action
is this consumer call itself, and it is gated ``apply=False`` (preview) by
default so a human or a write-capable tool must opt into the writes.

Every filed work item carries, per the acceptance criteria:

* **producer identity** — the ``warpline`` + ``federation`` labels and a
  provenance line in the issue description.
* **affected-entity key** — an ADR-029 entity association on the entity's SEI.
  This is the same surface warpline reads back via
  ``entity_association_list_by_entity``, so a filed item closes the loop: the
  next reverify worklist sees the entity as tracked (``enrichment.work``
  populated) and this consumer reports it as ``linked`` rather than re-filing.

The worklist entity view (``warpline/refs.py``) carries ``locator`` and ``sei``
but **not** Loomweave's ``content_hash``. The association needs a non-blank hash
(``make_content_hash``), so when an item supplies none we stamp
:data:`UNVERIFIED_CONTENT_HASH`. warpline does not interpret
``content_hash_at_attach`` drift (that is Filigree's read-path concern), so the
sentinel never breaks warpline's reverse-lookup read; it only means a later
freshness comparison reads as ``stale`` until a real hash is attached.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from filigree.types.core import make_content_hash, make_issue_id, make_loomweave_entity_id

if TYPE_CHECKING:
    from filigree.core import FiligreeDB

#: Producer identity stamped on filed work (the ``scan_source``/producer axis).
PRODUCER = "warpline"
#: Labels every warpline-filed issue carries for provenance + federation grouping.
PRODUCER_LABELS = ("warpline", "federation")
#: ``entity_kind`` recorded on the affected-entity association.
ENTITY_KIND = "loomweave-entity"
#: Stamped as ``content_hash_at_attach`` when the worklist item carries no hash.
UNVERIFIED_CONTENT_HASH = "warpline:content-unverified"

#: warpline worklist priority token -> Filigree integer priority (P0..P4).
_PRIORITY_MAP = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4}
_DEFAULT_PRIORITY = 2  # "unknown" / unmapped -> P2 (medium)


def _priority_for(token: Any, override: int | None) -> int:
    """Resolve a Filigree priority from the item's token, honouring an override."""
    if override is not None:
        return override
    if isinstance(token, str) and token in _PRIORITY_MAP:
        return _PRIORITY_MAP[token]
    return _DEFAULT_PRIORITY


def _normalise_worklist(worklist: Mapping[str, Any]) -> Mapping[str, Any]:
    """Accept either the full success envelope or the bare ``data`` payload.

    warpline returns the worklist under the envelope's ``data`` key; callers may
    hand us either the whole envelope or just ``data``. Prefer ``data`` when it
    looks like the worklist (carries ``items``), else treat the input as the
    payload itself.
    """
    data = worklist.get("data")
    if isinstance(data, Mapping) and "items" in data:
        return data
    return worklist


def _build_description(item: Mapping[str, Any], sei: str, locator: Any) -> str:
    """Render the filed issue's body from the worklist item (provenance first)."""
    lines = [
        f"Filed from a warpline reverify worklist (producer: {PRODUCER}).",
        "",
        f"Affected entity: {locator if locator else '(unknown locator)'}",
        f"SEI: {sei}",
    ]
    reason = item.get("reason")
    if reason is not None:
        lines.append(f"Reason: {reason} (depth {item.get('depth', 0)})")
    why = item.get("why")
    if isinstance(why, list) and why:
        lines.append("")
        lines.append("Why (impact path):")
        lines.extend(f"  - {entry}" for entry in why)
    suggested = item.get("suggested_verification")
    if isinstance(suggested, list) and suggested:
        lines.append("")
        lines.append("Suggested verification:")
        for sv in suggested:
            if isinstance(sv, Mapping):
                lines.append(f"  - [{sv.get('kind')}] {sv.get('command')}")
    return "\n".join(lines)


def ingest_reverify_worklist(
    db: FiligreeDB,
    worklist: Mapping[str, Any],
    *,
    apply: bool = False,
    actor: str = PRODUCER,
    priority_override: int | None = None,
    default_content_hash: str | None = None,
) -> dict[str, Any]:
    """File-or-link a warpline reverify worklist as Filigree work.

    For each worklist item, keyed on the entity's SEI:

    * **no SEI** — ``skipped``: an unresolved entity has no affected-entity key
      to bind, so it cannot participate in the tracked-entity loop.
    * **SEI already bound to an open issue** — ``linked``: report the existing
      issue id(s); never duplicate-file.
    * **SEI not tracked (or only closed bindings)** — ``filed``: in ``apply``
      mode create a task and attach the SEI association; in preview mode record
      what *would* be filed without writing.

    Args:
        db: open Filigree store.
        worklist: a ``warpline.reverify_worklist.v1`` envelope or its ``data``.
        apply: ``False`` (default) previews — pure reads, no writes. ``True``
            performs the file/link writes (the explicit user/tool action).
        actor: identity recorded as issue creator / association ``attached_by``.
        priority_override: force this Filigree priority on every filed item.
        default_content_hash: hash stamped on filed associations when the item
            carries none (else :data:`UNVERIFIED_CONTENT_HASH`).

    Returns:
        ``{"applied", "producer", "summary": {filed, linked, skipped, total},
        "results": [...]}`` — one per-item result dict, order-preserving.
    """
    data = _normalise_worklist(worklist)
    raw_items = data.get("items")
    items = raw_items if isinstance(raw_items, list) else []

    results: list[dict[str, Any]] = []
    filed = linked = skipped = 0

    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        entity = raw.get("entity")
        entity = entity if isinstance(entity, Mapping) else {}
        sei = entity.get("sei")
        locator = entity.get("locator")

        if not (isinstance(sei, str) and sei):
            skipped += 1
            results.append(
                {
                    "action": "skipped",
                    "sei": None,
                    "locator": locator,
                    "reason": "no SEI — entity unresolved; no affected-entity key to bind",
                }
            )
            continue

        entity_id = make_loomweave_entity_id(sei)
        open_issue_ids: list[str] = []
        closed_issue_ids: list[str] = []
        for row in db.list_associations_by_entity(entity_id):
            issue_id = str(row["issue_id"])
            try:
                category = db.get_issue(issue_id).status_category
            except KeyError:
                continue  # binding outlived its issue; ignore
            (closed_issue_ids if category == "done" else open_issue_ids).append(issue_id)

        if open_issue_ids:
            linked += 1
            results.append(
                {
                    "action": "linked",
                    "sei": sei,
                    "locator": locator,
                    "linked_issue_ids": open_issue_ids,
                    "reason": "entity already tracked by an open issue",
                }
            )
            continue

        priority = _priority_for(raw.get("priority"), priority_override)
        supplied_hash = entity.get("content_hash") or default_content_hash
        content_hash = supplied_hash if isinstance(supplied_hash, str) and supplied_hash else UNVERIFIED_CONTENT_HASH
        result: dict[str, Any] = {
            "action": "filed",
            "sei": sei,
            "locator": locator,
            "priority": priority,
            "content_hash_source": "provided" if content_hash != UNVERIFIED_CONTENT_HASH else "sentinel",
        }
        if closed_issue_ids:
            result["prior_closed_issue_ids"] = closed_issue_ids

        if apply:
            issue = db.create_issue(
                f"Reverify: {locator if locator else sei}",
                type="task",
                priority=priority,
                description=_build_description(raw, sei, locator),
                labels=list(PRODUCER_LABELS),
                actor=actor,
            )
            db.add_entity_association(
                make_issue_id(issue.id),
                entity_id,
                make_content_hash(content_hash),
                actor=actor,
                entity_kind=ENTITY_KIND,
            )
            result["issue_id"] = issue.id

        filed += 1
        results.append(result)

    return {
        "applied": apply,
        "producer": PRODUCER,
        "summary": {"filed": filed, "linked": linked, "skipped": skipped, "total": len(results)},
        "results": results,
    }
