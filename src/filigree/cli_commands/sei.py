"""``filigree sei-backfill`` — the locator→SEI value migration (ADR-038 §7).

Operator-invoked CLI surface over :mod:`filigree.sei_backfill`. Default is a
dry-run plan; ``--execute`` applies. The production run is owner-scheduled (a
coordinated cross-tool freeze, per Clarion's migration playbook) — this verb is
the machinery, not the trigger.
"""

from __future__ import annotations

import json as json_mod
import sqlite3
import sys

import click

from filigree.cli_common import get_db
from filigree.registry import RegistryResolutionError, RegistryUnavailableError
from filigree.sei_backfill import SeiBackfillError, SeiBackfillReport, run_sei_backfill
from filigree.types.api import ErrorCode


@click.command("sei-backfill")
@click.option("--execute", "execute", is_flag=True, help="Apply the migration (default is a dry-run plan)")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def sei_backfill_cmd(ctx: click.Context, execute: bool, as_json: bool) -> None:
    """Rewrite stored Clarion entity ids from locators to SEIs (ADR-038 §7).

    Resolves every opaque ``clarion_entity_id`` — and every historical
    deleted-issue tombstone id — through Clarion's identity/resolve endpoint and
    rewrites it in place. The column name and wire shape are unchanged; only the
    value format changes (locator → ``clarion:eid:<hex>``).

    Default is a dry-run that reports exactly what an applied run would do; pass
    ``--execute`` to write. Idempotent and resumable: values already carrying the
    SEI prefix are skipped, so a partial run re-runs to convergence. Locators
    Clarion can no longer resolve are flagged ORPHAN for review, never dropped.
    """
    dry_run = not execute
    with get_db() as db:
        try:
            report = run_sei_backfill(db, dry_run=dry_run, actor=ctx.obj["actor"])
        except SeiBackfillError as e:
            # Clean precondition refusal (not Clarion-backed / SEI unsupported).
            _emit_error(str(e), ErrorCode.VALIDATION, as_json=as_json)
        except (RegistryUnavailableError, RegistryResolutionError) as e:
            _emit_error(f"Clarion unavailable: {e}", ErrorCode.REGISTRY_UNAVAILABLE, as_json=as_json)
        except sqlite3.Error as e:
            _emit_error(str(e), ErrorCode.IO, as_json=as_json)

    if as_json:
        click.echo(json_mod.dumps(report.to_dict(), indent=2))
        return
    _emit_human(report)


def _emit_error(message: str, code: ErrorCode, *, as_json: bool) -> None:
    if as_json:
        click.echo(json_mod.dumps({"error": message, "code": code}))
    else:
        click.echo(f"Error: {message}", err=True)
    sys.exit(1)


def _emit_human(report: SeiBackfillReport) -> None:
    mode = "DRY-RUN (no changes written)" if report.dry_run else "APPLIED"
    click.echo(f"SEI backfill — {mode}")
    click.echo(
        f"  associations: {report.associations_migrated} migrated, "
        f"{report.associations_already_sei} already-SEI, "
        f"{report.associations_orphaned} orphaned, "
        f"{report.associations_merged} merged"
    )
    click.echo(
        f"  tombstones:   {report.tombstone_locators_migrated} ids migrated, "
        f"{report.tombstone_locators_orphaned} orphaned "
        f"(across {report.tombstones_scanned} tombstone(s))"
    )
    if report.orphans:
        click.echo(f"  ORPHANS NEEDING REVIEW ({len(report.orphans)}):")
        for o in report.orphans:
            click.echo(f"    [{o.source}/{o.reason}] issue {o.issue_id}: {o.locator}")


def register(cli: click.Group) -> None:
    cli.add_command(sei_backfill_cmd, "sei-backfill")
