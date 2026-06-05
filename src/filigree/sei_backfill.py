"""Locator→SEI value migration for entity associations (ADR-038 §7).

The Loom suite moves every cross-tool binding off the mutable **locator**
(``{plugin}:{kind}:{qualname}``) onto the durable, opaque **SEI**
(``clarion:eid:<hex>``). Filigree stores the binding id opaquely in
``entity_associations.clarion_entity_id`` (and, for hard-deleted issues, in the
``deleted_issues.entity_ids`` tombstone JSON array). This module rewrites those
stored values *in place* — the column name, wire shape, and storage mechanism
are unchanged; only the value format changes.

Why this is NOT a schema migration and NOT auto-run
---------------------------------------------------
The rewrite resolves each locator through Loomweave's
``POST /api/v1/identity/resolve:batch`` endpoint — an **outbound network call**.
It therefore must never live in ``apply_pending_migrations`` or in the
``db_entity_associations`` layer (whose federation sentinel test forbids any
outbound socket). It is an **operator-invoked** step, driven by the
``filigree sei-backfill`` CLI verb. The production run is owner-scheduled (a
coordinated cross-tool freeze, see Loomweave's ``sei-migration-playbook.md``);
this module is the machinery, not the trigger.

Safety properties (the no-false-green ethos)
--------------------------------------------
- **Idempotent / resumable.** A value already carrying the ``clarion:eid:``
  prefix is skipped without a network call; Loomweave additionally rejects
  SEI-shaped input with HTTP 400 (REQ-F-02), so a partially-run backfill simply
  re-runs to convergence.
- **Never silently drops an orphan.** A locator Loomweave can no longer resolve
  (``alive:false``) or rejects as invalid keeps its locator value and is stamped
  ``migration_orphaned_at`` (associations) or kept verbatim and reported
  (tombstones) for human review.
- **Opacity preserved.** The only inspection performed on a stored id is the
  sanctioned ``clarion:eid:`` prefix check.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from filigree.db_base import _now_iso
from filigree.registry import LoomweaveRegistry, RegistryResolutionError, RegistryUnavailableError, SeiResolution

if TYPE_CHECKING:
    from filigree.core import FiligreeDB

logger = logging.getLogger(__name__)

# The reserved SEI prefix. The ONLY substring of a stored id the backfill is
# permitted to inspect (ADR-038 / the migration playbook sanction this single
# check); everything else about the id stays opaque.
SEI_PREFIX = "clarion:eid:"


class SeiBackfillError(RuntimeError):
    """The backfill cannot run because its precondition is unmet.

    Raised for a clean, actionable refusal — not a partial write. The two
    cases: the project is not in ``clarion`` registry mode (no authority to
    resolve against), or the reachable Loomweave has not shipped SEI
    (``_capabilities.sei.supported`` false/absent). In the latter case the
    honest answer is "identity unavailable; nothing to migrate", per the
    oracle's ``capability_absent`` scenario.
    """


class LoomweaveOutOfSyncError(SeiBackfillError):
    """The local Loomweave database is not synchronized or online."""


@dataclass(frozen=True, slots=True)
class OrphanRecord:
    """One binding whose locator did not resolve to an alive SEI.

    Kept verbatim (never dropped) and surfaced for human review. ``source`` is
    ``"association"`` (a live ``entity_associations`` row, now stamped
    ``migration_orphaned_at``) or ``"tombstone"`` (a locator inside a
    ``deleted_issues.entity_ids`` array, left as a locator). ``reason`` is
    ``"unresolved"`` (Loomweave answered ``alive:false``) or ``"invalid"``
    (Loomweave rejected it as a malformed locator).
    """

    source: Literal["association", "tombstone"]
    issue_id: str
    locator: str
    reason: Literal["unresolved", "invalid"]


@dataclass(slots=True)
class SeiBackfillReport:
    """Outcome of a backfill pass (dry-run or applied)."""

    dry_run: bool
    associations_scanned: int = 0
    associations_migrated: int = 0
    associations_already_sei: int = 0
    associations_orphaned: int = 0
    associations_merged: int = 0
    tombstones_scanned: int = 0
    tombstones_corrupt: int = 0
    tombstone_locators_migrated: int = 0
    tombstone_locators_orphaned: int = 0
    tombstone_locators_already_sei: int = 0
    orphans: list[OrphanRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        # ``asdict`` recurses into the nested ``OrphanRecord`` dataclasses, so the
        # JSON shape stays in lockstep with the field declarations above — no
        # hand-mirrored key list to drift when a counter is added or renamed.
        return asdict(self)


def run_sei_backfill(db: FiligreeDB, *, dry_run: bool = True, actor: str = "") -> SeiBackfillReport:
    """Resolve every stored locator to its SEI and rewrite it in place.

    Covers both surfaces that carry an opaque entity id: live
    ``entity_associations`` rows and the historical ``deleted_issues.entity_ids``
    tombstone arrays (REQ-F-01 — so the ``affected_entities`` change feed emits
    only SEIs after cutover, never a locator/SEI mix).

    With ``dry_run`` (the default) nothing is written; the returned report
    describes exactly what an applied run would do. Raises
    :class:`SeiBackfillError` if the project cannot resolve against a
    SEI-capable Loomweave.
    """
    _require_sei_capable(db)
    registry = _build_loomweave_registry(db)
    try:
        assoc_rows = db.conn.execute(
            "SELECT issue_id, clarion_entity_id FROM entity_associations WHERE migration_orphaned_at IS NULL"
        ).fetchall()
        tomb_rows = db.conn.execute("SELECT seq, issue_id, entity_ids FROM deleted_issues").fetchall()

        locators = _collect_locators(assoc_rows, tomb_rows)
        resolution = registry.resolve_locators_batch(sorted(locators))
    finally:
        registry.close()

    report = SeiBackfillReport(dry_run=dry_run)
    if dry_run:
        _plan(report, assoc_rows, tomb_rows, resolution)
        return report

    _apply(db, report, assoc_rows, tomb_rows, resolution)
    return report


# ---------------------------------------------------------------------------
# Preconditions + registry construction
# ---------------------------------------------------------------------------


def _require_sei_capable(db: FiligreeDB) -> None:
    if db.registry_backend != "clarion":
        msg = (
            f"SEI backfill requires Loomweave as the registry backend (project is {db.registry_backend!r}). "
            "There is no authority to resolve locators against; nothing to migrate."
        )
        raise SeiBackfillError(msg)

    # 1. Reachability & capabilities checks
    try:
        capabilities = db.loomweave_capabilities
        if capabilities is None:
            capabilities = db.reprobe_loomweave_capabilities()
    except (RegistryUnavailableError, RegistryResolutionError) as e:
        raise LoomweaveOutOfSyncError(f"Loomweave server is unreachable: {e}") from e

    if capabilities is None:
        raise LoomweaveOutOfSyncError("Loomweave server returned empty capabilities or is offline.")

    # 2. Local database sync checks (only run if db.project_root is not None and .git directory exists)
    if db.project_root is not None and (db.project_root / ".git").exists():
        loomweave_db_path = db.project_root / ".clarion" / "clarion.db"
        if not loomweave_db_path.is_file():
            raise LoomweaveOutOfSyncError("Local Loomweave database not found.")

        try:
            res = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=db.project_root,
                capture_output=True,
                text=True,
                check=True,
            )
            git_head = res.stdout.strip()
        except (subprocess.SubprocessError, FileNotFoundError) as e:
            raise LoomweaveOutOfSyncError(f"Failed to resolve git HEAD commit: {e}") from e

        try:
            loomweave_conn = sqlite3.connect(f"file:{loomweave_db_path}?mode=ro", uri=True)
            loomweave_conn.row_factory = sqlite3.Row
            row = loomweave_conn.execute(
                "SELECT analyzed_at_commit FROM runs WHERE status = 'completed' ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            loomweave_conn.close()
        except sqlite3.Error as e:
            raise LoomweaveOutOfSyncError(f"Failed to query local Loomweave database: {e}") from e

        if row is None or not row["analyzed_at_commit"] or row["analyzed_at_commit"] != git_head:
            last_commit = row["analyzed_at_commit"] if (row and row["analyzed_at_commit"]) else "none"
            raise LoomweaveOutOfSyncError(
                f"Loomweave database is out of sync with git HEAD (latest run commit: {last_commit}, git HEAD: {git_head})."
            )

    if not capabilities.get("sei_supported", False):
        msg = (
            "Connected Loomweave has not shipped SEI (_capabilities.sei.supported is false/absent). "
            "Identity is unavailable; keep working on locators — nothing to migrate yet."
        )
        raise SeiBackfillError(msg)


def _build_loomweave_registry(db: FiligreeDB) -> LoomweaveRegistry:
    """Construct a dedicated resolve client from the project's Loomweave config.

    A fresh ``LoomweaveRegistry`` (rather than reusing ``db.registry``, which may
    be a local-fallback wrapper) keeps the resolve path explicit and decoupled;
    the one-shot client is closed by the caller.
    """
    base_url = db._loomweave_base_url()
    if base_url is None:  # pragma: no cover — guarded by _require_sei_capable
        msg = "clarion.base_url is not configured"
        raise SeiBackfillError(msg)
    return LoomweaveRegistry(
        base_url,
        timeout_seconds=db._loomweave_timeout_seconds(),
        auth_token=db._resolve_loomweave_auth_token(),
    )


def _collect_locators(assoc_rows: list[Any], tomb_rows: list[Any]) -> set[str]:
    """Gather the distinct, not-already-SEI locators across both surfaces."""
    locators: set[str] = set()
    for row in assoc_rows:
        eid = row["clarion_entity_id"]
        if not eid.startswith(SEI_PREFIX):
            locators.add(eid)
    for row in tomb_rows:
        # Malformed rows are surfaced by the report-owning pass (_plan / _apply);
        # here we only need the salvageable locators to resolve.
        locs, _malformed = _decode_entity_ids(row["entity_ids"])
        for loc in locs:
            if not loc.startswith(SEI_PREFIX):
                locators.add(loc)
    return locators


def _decode_entity_ids(raw: str | None) -> tuple[list[str], bool]:
    """Decode a ``deleted_issues.entity_ids`` blob into its locator strings.

    Returns ``(locators, malformed)``. ``malformed`` is True for shapes that
    cannot be rewritten losslessly — corrupt JSON, a non-array value, or an
    array containing non-string values — so a report-owning caller can surface
    it (warn + counter) rather than silently drop data, honouring the module's
    "never silently drops an orphan" contract. A legitimately empty tombstone
    (NULL / ``"[]"``) is *not* malformed. Only Filigree writes this column, so
    corruption signals external tampering, not a normal path."""
    if not raw:
        return [], False
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return [], True
    if not isinstance(decoded, list):
        return [], True
    if not all(isinstance(item, str) for item in decoded):
        return [], True
    return decoded, False


def _warn_malformed_tombstone(report: SeiBackfillReport, row: Any) -> None:
    """Surface a tombstone whose ``entity_ids`` could not be parsed as a string
    array: count it and emit a breadcrumb so it is traceable, not invisible."""
    report.tombstones_corrupt += 1
    logger.warning(
        "deleted_issues.entity_ids for issue %r (seq=%s) is not a JSON array of strings; "
        "its locators cannot be migrated or orphaned and the row is left verbatim for manual "
        "inspection (only Filigree writes this column, so this indicates corruption or tampering)",
        row["issue_id"],
        row["seq"],
    )


# ---------------------------------------------------------------------------
# Dry-run planning (no writes)
# ---------------------------------------------------------------------------


def _plan(
    report: SeiBackfillReport,
    assoc_rows: list[Any],
    tomb_rows: list[Any],
    resolution: SeiResolution,
) -> None:
    sei_by_locator = resolution["resolved"]
    # Pre-tally which SEIs more than one of an issue's locators would collapse to,
    # so the dry-run reports merges the applied run will perform.
    targets_per_issue: dict[str, dict[str, int]] = {}
    already_sei_per_issue: dict[str, set[str]] = {}
    for row in assoc_rows:
        eid = row["clarion_entity_id"]
        if eid.startswith(SEI_PREFIX):
            report.associations_already_sei += 1
            already_sei_per_issue.setdefault(row["issue_id"], set()).add(eid)
            continue
        report.associations_scanned += 1
        sei = sei_by_locator.get(eid)
        if sei is None:
            report.associations_orphaned += 1
            report.orphans.append(OrphanRecord("association", row["issue_id"], eid, _orphan_reason(eid, resolution)))
            continue
        report.associations_migrated += 1
        counts = targets_per_issue.setdefault(row["issue_id"], {})
        counts[sei] = counts.get(sei, 0) + 1
    # Among the rows of an issue that end at one SEI, all but one survive — the
    # rest are deleted as merges. ``n`` locators alone leave ``n - 1`` merges; an
    # already-SEI row at that target is a pre-existing extra survivor, so every
    # one of the ``n`` locators collides and is merged (``n`` merges). Counting it
    # keeps the dry-run preview faithful to the destructive applied run.
    for issue_id, counts in targets_per_issue.items():
        present = already_sei_per_issue.get(issue_id, set())
        for sei, n in counts.items():
            report.associations_merged += n - 1 + (1 if sei in present else 0)

    for row in tomb_rows:
        locs, malformed = _decode_entity_ids(row["entity_ids"])
        if malformed:
            _warn_malformed_tombstone(report, row)
            continue
        if not locs:
            continue
        report.tombstones_scanned += 1
        for loc in locs:
            if loc.startswith(SEI_PREFIX):
                report.tombstone_locators_already_sei += 1
                continue
            if sei_by_locator.get(loc) is not None:
                report.tombstone_locators_migrated += 1
            else:
                report.tombstone_locators_orphaned += 1
                report.orphans.append(OrphanRecord("tombstone", row["issue_id"], loc, _orphan_reason(loc, resolution)))


# ---------------------------------------------------------------------------
# Applied run (transactional)
# ---------------------------------------------------------------------------


def _apply(
    db: FiligreeDB,
    report: SeiBackfillReport,
    assoc_rows: list[Any],
    tomb_rows: list[Any],
    resolution: SeiResolution,
) -> None:
    sei_by_locator = resolution["resolved"]
    now = _now_iso()
    db.conn.commit()
    try:
        db.conn.execute("BEGIN IMMEDIATE")
        for row in assoc_rows:
            _apply_association(db.conn, report, row, sei_by_locator, resolution, now)
        for row in tomb_rows:
            _apply_tombstone(db.conn, report, row, sei_by_locator, resolution)
        db.conn.commit()
    except Exception:
        db.conn.rollback()
        raise


def _apply_association(
    conn: sqlite3.Connection,
    report: SeiBackfillReport,
    row: Any,
    sei_by_locator: dict[str, str],
    resolution: SeiResolution,
    now: str,
) -> None:
    issue_id = row["issue_id"]
    eid = row["clarion_entity_id"]
    if eid.startswith(SEI_PREFIX):
        report.associations_already_sei += 1
        return
    report.associations_scanned += 1
    sei = sei_by_locator.get(eid)
    if sei is None:
        conn.execute(
            "UPDATE entity_associations SET migration_orphaned_at = ? WHERE issue_id = ? AND clarion_entity_id = ?",
            (now, issue_id, eid),
        )
        report.associations_orphaned += 1
        report.orphans.append(OrphanRecord("association", issue_id, eid, _orphan_reason(eid, resolution)))
        return
    try:
        conn.execute(
            "UPDATE entity_associations SET clarion_entity_id = ? WHERE issue_id = ? AND clarion_entity_id = ?",
            (sei, issue_id, eid),
        )
        report.associations_migrated += 1
    except sqlite3.IntegrityError:
        # (issue_id, sei) already exists — two locators on this issue collapse to
        # the same entity. Collapse to a single row: keep the survivor, fold the
        # newest attach metadata into it, drop this duplicate.
        _merge_into_survivor(conn, issue_id, old_locator=eid, sei=sei)
        report.associations_migrated += 1
        report.associations_merged += 1


def _merge_into_survivor(
    conn: sqlite3.Connection,
    issue_id: str,
    *,
    old_locator: str,
    sei: str,
) -> None:
    """Collapse a duplicate binding into the existing (issue_id, sei) survivor.

    Preference rule: the survivor keeps its identity and ``attached_by``, but
    adopts the newest ``attached_at`` and the matching ``content_hash_at_attach``
    so freshness reflects the most recent attach across the merged pair.
    """
    survivor = conn.execute(
        "SELECT content_hash_at_attach, attached_at, attached_by FROM entity_associations WHERE issue_id = ? AND clarion_entity_id = ?",
        (issue_id, sei),
    ).fetchone()
    # ``incoming`` from the outer scan carries only (issue_id, clarion_entity_id);
    # fetch its full attach metadata before we delete the duplicate row.
    incoming_full = conn.execute(
        "SELECT content_hash_at_attach, attached_at FROM entity_associations WHERE issue_id = ? AND clarion_entity_id = ?",
        (issue_id, old_locator),
    ).fetchone()
    conn.execute(
        "DELETE FROM entity_associations WHERE issue_id = ? AND clarion_entity_id = ?",
        (issue_id, old_locator),
    )
    if survivor is not None and incoming_full is not None and incoming_full["attached_at"] > survivor["attached_at"]:
        conn.execute(
            "UPDATE entity_associations SET content_hash_at_attach = ?, attached_at = ? WHERE issue_id = ? AND clarion_entity_id = ?",
            (incoming_full["content_hash_at_attach"], incoming_full["attached_at"], issue_id, sei),
        )


def _apply_tombstone(
    conn: sqlite3.Connection,
    report: SeiBackfillReport,
    row: Any,
    sei_by_locator: dict[str, str],
    resolution: SeiResolution,
) -> None:
    locs, malformed = _decode_entity_ids(row["entity_ids"])
    if malformed:
        _warn_malformed_tombstone(report, row)
        return
    if not locs:
        return
    report.tombstones_scanned += 1
    issue_id = row["issue_id"]
    rewritten: list[str] = []
    changed = False
    for loc in locs:
        if loc.startswith(SEI_PREFIX):
            report.tombstone_locators_already_sei += 1
            rewritten.append(loc)
            continue
        sei = sei_by_locator.get(loc)
        if sei is not None:
            rewritten.append(sei)
            changed = True
            report.tombstone_locators_migrated += 1
        else:
            rewritten.append(loc)
            report.tombstone_locators_orphaned += 1
            report.orphans.append(OrphanRecord("tombstone", issue_id, loc, _orphan_reason(loc, resolution)))
    if changed:
        deduped = _dedupe_preserve_order(rewritten)
        conn.execute(
            "UPDATE deleted_issues SET entity_ids = ? WHERE seq = ?",
            (json.dumps(deduped), row["seq"]),
        )


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _orphan_reason(locator: str, resolution: SeiResolution) -> Literal["unresolved", "invalid"]:
    """Classify why a locator did not migrate, for the operator's review list."""
    if locator in resolution["already_migrated"]:
        return "invalid"
    return "unresolved"
