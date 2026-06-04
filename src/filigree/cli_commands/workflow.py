"""CLI commands for workflow: templates, types, transitions, packs, validate, guide, explain-status."""

from __future__ import annotations

import json as json_mod
import sys
from typing import Any, NoReturn

import click

from filigree.cli_common import get_db, refresh_summary
from filigree.types.api import ErrorCode


def _emit_error(message: str, code: str, *, as_json: bool) -> NoReturn:
    """Emit a 2.0 flat error envelope on --json, plain stderr otherwise. Exits 1."""
    if as_json:
        click.echo(json_mod.dumps({"error": message, "code": code}))
    else:
        click.echo(message, err=True)
    sys.exit(1)


@click.group(invoke_without_command=True)
@click.option("--type", "issue_type", default=None, help="Show specific template")
@click.pass_context
def templates(ctx: click.Context, issue_type: str | None) -> None:
    """Show available issue templates."""
    if ctx.invoked_subcommand is not None:
        return
    with get_db() as db:
        if issue_type:
            tpl = db.get_template(issue_type)
            if not tpl:
                click.echo(f"Unknown template: {issue_type}", err=True)
                sys.exit(1)
            click.echo(f"{tpl['display_name']} ({tpl['type']})")
            click.echo(f"  {tpl['description']}")
            click.echo("\n  Fields:")
            for f in tpl["fields_schema"]:
                required_at = f.get("required_at") or []
                req = f" (required at: {', '.join(required_at)})" if required_at else ""
                click.echo(f"    {f['name']}: {f['type']}{req} — {f['description']}")
        else:
            for item in db.list_templates():
                click.echo(f"  {item['type']:<15} {item['display_name']}")


@templates.command("reload")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def templates_reload(as_json: bool) -> None:
    """Reload workflow templates from disk."""
    with get_db() as db:
        try:
            db.reload_templates()
            # Force the new registry to materialise before regenerating
            # context.md; refresh_summary reads template-derived sections.
            db.templates.list_types()
        except ValueError as exc:
            _emit_error(str(exc), ErrorCode.VALIDATION, as_json=as_json)
        refresh_summary(db)
        if as_json:
            click.echo(json_mod.dumps({"status": "ok"}))
        else:
            click.echo("Templates reloaded")


@click.command("get-template")
@click.argument("type_name")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def get_template_cmd(type_name: str, as_json: bool) -> None:
    """Get the template (states, transitions, fields) for an issue type.

    Verb-noun alias for ``templates --type=<type>`` mirroring the MCP
    ``get_template`` tool.
    """
    _type_info_impl(type_name, as_json)


def _workflow_statuses_impl(as_json: bool) -> None:
    with get_db() as db:
        data = {}
        for category in ("open", "wip", "done"):
            data[category] = list(db._get_states_for_category(category))
        if as_json:
            click.echo(json_mod.dumps({"statuses": data}, indent=2))
            return
        for category, statuses in data.items():
            click.echo(f"{category}: {', '.join(statuses) if statuses else '(none)'}")


@click.command("workflow-statuses")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def workflow_statuses(as_json: bool) -> None:
    """Show workflow statuses by category from enabled templates."""
    _workflow_statuses_impl(as_json)


@click.command("get-workflow-statuses")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def get_workflow_statuses(as_json: bool) -> None:
    """Show workflow statuses by category from enabled templates. Alias for `workflow-statuses`."""
    _workflow_statuses_impl(as_json)


def _types_impl(as_json: bool) -> None:
    with get_db() as db:
        types_list: list[dict[str, Any]] = []
        for tpl in db.templates.list_types():
            types_list.append(
                {
                    "type": tpl.type,
                    "display_name": tpl.display_name,
                    "description": tpl.description,
                    "pack": tpl.pack,
                    "initial_state": tpl.initial_state,
                    "states": [{"name": s.name, "category": s.category} for s in tpl.states],
                }
            )
        types_list.sort(key=lambda t: str(t["type"]))

        if as_json:
            click.echo(json_mod.dumps({"items": types_list, "has_more": False}, indent=2))
            return

        for t in types_list:
            states = " → ".join(s["name"] for s in t["states"])
            click.echo(f"  {t['type']:<15} [{t['pack']}] {states}")


@click.command("types")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def types_cmd(as_json: bool) -> None:
    """List all registered issue types with pack info."""
    _types_impl(as_json)


@click.command("list-types")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def list_types_cmd(as_json: bool) -> None:
    """List all registered issue types with pack info. Alias for `types`."""
    _types_impl(as_json)


def _type_info_impl(type_name: str, as_json: bool) -> None:
    with get_db() as db:
        tpl = db.get_template(type_name)
        if tpl is None:
            _emit_error(f"Unknown type: {type_name}", ErrorCode.NOT_FOUND, as_json=as_json)

        if as_json:
            click.echo(json_mod.dumps(dict(tpl), indent=2))
            return

        click.echo(f"{tpl['display_name']} ({tpl['type']}) — {tpl['pack']} pack")
        click.echo(f"  {tpl['description']}")
        click.echo("\n  States:")
        for s in tpl["states"]:
            initial = " (initial)" if s["name"] == tpl["initial_state"] else ""
            click.echo(f"    {s['name']:<20} [{s['category']}]{initial}")
        click.echo("\n  Transitions:")
        for t in tpl["transitions"]:
            requires_fields = t["requires_fields"]
            fields_note = f" (requires: {', '.join(requires_fields)})" if requires_fields else ""
            click.echo(f"    {t['from']} → {t['to']}  [{t['enforcement']}]{fields_note}")
        if tpl["fields_schema"]:
            click.echo("\n  Fields:")
            for f in tpl["fields_schema"]:
                notes: list[str] = []
                if f.get("required_at"):
                    notes.append(f"required at: {', '.join(f['required_at'])}")
                if f.get("pattern"):
                    notes.append(f"pattern: {f['pattern']}")
                if f.get("unique"):
                    notes.append("unique")
                suffix = f" ({'; '.join(notes)})" if notes else ""
                click.echo(f"    {f['name']}: {f['type']} — {f['description']}{suffix}")


@click.command("type-info")
@click.argument("type_name")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def type_info(type_name: str, as_json: bool) -> None:
    """Show full workflow definition for an issue type."""
    _type_info_impl(type_name, as_json)


@click.command("get-type-info")
@click.argument("type_name")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def get_type_info(type_name: str, as_json: bool) -> None:
    """Show full workflow definition for an issue type. Alias for `type-info`."""
    _type_info_impl(type_name, as_json)


def _transitions_impl(issue_id: str, as_json: bool) -> None:
    with get_db() as db:
        try:
            transitions = db.get_valid_transitions(issue_id)
        except KeyError:
            _emit_error(f"Not found: {issue_id}", ErrorCode.NOT_FOUND, as_json=as_json)

        if as_json:
            items = [
                {
                    "to": t.to,
                    "category": t.category,
                    "enforcement": t.enforcement,
                    "requires_fields": list(t.requires_fields),
                    "missing_fields": list(t.missing_fields),
                    "ready": t.ready,
                }
                for t in transitions
            ]
            click.echo(
                json_mod.dumps(
                    {"items": items, "has_more": False},
                    indent=2,
                )
            )
            return

        if not transitions:
            click.echo("No transitions available (unknown type or terminal state)")
            return

        issue = db.get_issue(issue_id)
        click.echo(f"Transitions from '{issue.status}' ({issue.type}):")
        for t in transitions:
            ready_mark = " READY" if t.ready else ""
            missing = f" (missing: {', '.join(t.missing_fields)})" if t.missing_fields else ""
            click.echo(f"  → {t.to:<20} [{t.category}] {t.enforcement}{missing}{ready_mark}")


@click.command("transitions")
@click.argument("issue_id")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def transitions_cmd(issue_id: str, as_json: bool) -> None:
    """Show valid next states for an issue."""
    _transitions_impl(issue_id, as_json)


@click.command("get-valid-transitions")
@click.argument("issue_id")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def get_valid_transitions(issue_id: str, as_json: bool) -> None:
    """Show valid next states for an issue. Alias for `transitions`."""
    _transitions_impl(issue_id, as_json)


def _packs_impl(as_json: bool) -> None:
    with get_db() as db:
        packs = db.templates.list_packs()

        if as_json:
            click.echo(
                json_mod.dumps(
                    {
                        "items": [
                            {
                                "pack": p.pack,
                                "version": p.version,
                                "display_name": p.display_name,
                                "description": p.description,
                                "types": sorted(p.types.keys()),
                                "requires_packs": list(p.requires_packs),
                            }
                            for p in sorted(packs, key=lambda p: p.pack)
                        ],
                        "has_more": False,
                    },
                    indent=2,
                )
            )
            return

        for p in sorted(packs, key=lambda p: p.pack):
            type_names = ", ".join(sorted(p.types.keys()))
            click.echo(f"  {p.pack:<15} v{p.version}  {type_names}")


@click.command("packs")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def packs_cmd(as_json: bool) -> None:
    """List enabled workflow packs."""
    _packs_impl(as_json)


@click.command("list-packs")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def list_packs_cmd(as_json: bool) -> None:
    """List enabled workflow packs. Alias for `packs`."""
    _packs_impl(as_json)


def _validate_impl(issue_id: str, as_json: bool) -> None:
    with get_db() as db:
        try:
            result = db.validate_issue(issue_id)
        except KeyError:
            _emit_error(f"Not found: {issue_id}", ErrorCode.NOT_FOUND, as_json=as_json)

        if as_json:
            click.echo(
                json_mod.dumps(
                    {
                        "valid": result.valid,
                        "warnings": list(result.warnings),
                        "errors": list(result.errors),
                    },
                    indent=2,
                )
            )
            return

        if result.valid and not result.warnings:
            click.echo(f"{issue_id}: valid (no warnings)")
        elif result.valid:
            click.echo(f"{issue_id}: valid with warnings:")
            for w in result.warnings:
                click.echo(f"  ! {w}")
        else:
            click.echo(f"{issue_id}: INVALID")
            for e in result.errors:
                click.echo(f"  X {e}")


@click.command("validate")
@click.argument("issue_id")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def validate_cmd(issue_id: str, as_json: bool) -> None:
    """Validate an issue against its type template."""
    _validate_impl(issue_id, as_json)


@click.command("validate-issue")
@click.argument("issue_id")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def validate_issue_cmd(issue_id: str, as_json: bool) -> None:
    """Validate an issue against its type template. Alias for `validate`."""
    _validate_impl(issue_id, as_json)


def _guide_impl(pack_name: str, as_json: bool) -> None:
    with get_db() as db:
        pack = db.templates.get_pack(pack_name)
        if pack is None:
            available_packs = sorted(p.pack for p in db.templates.list_packs())
            msg = f"Unknown pack: {pack_name}."
            if available_packs:
                msg += f" Available packs: {', '.join(available_packs)}."
            _emit_error(msg, ErrorCode.NOT_FOUND, as_json=as_json)

        if as_json:
            guide_obj = None if pack.guide is None else dict(pack.guide)
            click.echo(json_mod.dumps({"pack": pack.pack, "guide": guide_obj}, indent=2))
            return

        if pack.guide is None:
            click.echo(f"No guide available for pack '{pack_name}'")
            return

        guide = pack.guide
        if "overview" in guide:
            click.echo(f"# {pack.display_name} Guide\n")
            click.echo(guide["overview"])
        if "state_diagram" in guide:
            click.echo(f"\n## State Diagram\n{guide['state_diagram']}")
        if "when_to_use" in guide:
            click.echo(f"\n## When to Use\n{guide['when_to_use']}")
        if "tips" in guide:
            click.echo("\n## Tips")
            for tip in guide["tips"]:
                click.echo(f"  - {tip}")
        if "common_mistakes" in guide:
            click.echo("\n## Common Mistakes")
            for mistake in guide["common_mistakes"]:
                click.echo(f"  - {mistake}")


@click.command("guide")
@click.argument("pack_name")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def guide_cmd(pack_name: str, as_json: bool) -> None:
    """Display workflow guide for a pack."""
    _guide_impl(pack_name, as_json)


@click.command("get-workflow-guide")
@click.argument("pack_name")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def get_workflow_guide(pack_name: str, as_json: bool) -> None:
    """Display workflow guide for a pack. Alias for `guide`."""
    _guide_impl(pack_name, as_json)


@click.command("explain-status")
@click.argument("type_name")
@click.argument("status_name")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def explain_status(type_name: str, status_name: str, as_json: bool) -> None:
    """Explain a status's transitions and required fields."""
    with get_db() as db:
        tpl = db.templates.get_type(type_name)
        if tpl is None:
            _emit_error(f"Unknown type: {type_name}", ErrorCode.NOT_FOUND, as_json=as_json)

        status_def = None
        for s in tpl.states:
            if s.name == status_name:
                status_def = s
                break
        if status_def is None:
            _emit_error(
                f"Unknown status '{status_name}' for type '{type_name}'",
                ErrorCode.NOT_FOUND,
                as_json=as_json,
            )

        inbound = [{"from": t.from_state, "enforcement": t.enforcement} for t in tpl.transitions if t.to_state == status_name]
        outbound: list[dict[str, Any]] = [
            {"to": t.to_state, "enforcement": t.enforcement, "requires_fields": list(t.requires_fields)}
            for t in tpl.transitions
            if t.from_state == status_name
        ]
        required_fields = [f.name for f in tpl.fields_schema if status_name in f.required_at]

        if as_json:
            click.echo(
                json_mod.dumps(
                    {
                        "status": status_name,
                        "category": status_def.category,
                        "type": type_name,
                        "inbound_transitions": inbound,
                        "outbound_transitions": outbound,
                        "required_fields": required_fields,
                    },
                    indent=2,
                )
            )
            return

        click.echo(f"Status: {status_name} [{status_def.category}] (type: {type_name})")
        if inbound:
            click.echo("\nInbound transitions:")
            for t in inbound:
                click.echo(f"  <- {t['from']} [{t['enforcement']}]")
        else:
            click.echo("\nNo inbound transitions (initial status)")
        if outbound:
            click.echo("\nOutbound transitions:")
            for ot in outbound:
                req_fields = ot["requires_fields"]
                fields_note = f" (requires: {', '.join(req_fields)})" if req_fields else ""
                click.echo(f"  -> {ot['to']} [{ot['enforcement']}]{fields_note}")
        else:
            click.echo("\nNo outbound transitions (terminal status)")
        if required_fields:
            click.echo(f"\nRequired fields at this status: {', '.join(required_fields)}")


def register(cli: click.Group) -> None:
    """Register workflow commands with the CLI group."""
    cli.add_command(templates)
    cli.add_command(get_template_cmd)
    cli.add_command(workflow_statuses)
    cli.add_command(get_workflow_statuses)
    cli.add_command(types_cmd)
    cli.add_command(list_types_cmd)
    cli.add_command(type_info)
    cli.add_command(get_type_info)
    cli.add_command(transitions_cmd)
    cli.add_command(get_valid_transitions)
    cli.add_command(packs_cmd)
    cli.add_command(list_packs_cmd)
    cli.add_command(validate_cmd)
    cli.add_command(validate_issue_cmd)
    cli.add_command(guide_cmd)
    cli.add_command(get_workflow_guide)
    cli.add_command(explain_status)
