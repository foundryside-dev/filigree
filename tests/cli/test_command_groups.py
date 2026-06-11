"""CLI sub-command grouping (filigree-03303d6c5a).

Four niche subsystems (findings, files, annotations, observations) plus the
pre-existing ``scanner`` group are exposed as Click sub-command GROUPS:
``filigree <group> <subverb>`` is the canonical, visible invocation. Every
pre-existing flat verb (``get-finding``, ``trigger-scan``, …) still resolves
but is HIDDEN from top-level ``--help`` — zero breakage.

This guards every half of that contract:
  * each group is visible in top-level ``--help``;
  * each grouped subverb resolves (``filigree <group> <subverb> --help`` → 0);
  * each old flat name still resolves but is absent from top-level ``--help``;
  * ``observe`` stays a flat, visible top-level verb.

The mapping table below is the single source of truth and is kept in lockstep
with the source registration via ``test_mapping_matches_source``.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from filigree.cli import cli

# group -> list of (grouped subverb, flat back-compat name).
# The grouped form is the canonical/visible one; the flat name is the hidden
# back-compat alias that must keep resolving.
_GROUP_MAP: dict[str, list[tuple[str, str]]] = {
    "finding": [
        ("report", "report-finding"),
        ("list", "list-findings"),
        ("get", "get-finding"),
        ("dismiss", "dismiss-finding"),
        ("promote", "promote-finding"),
        ("update", "update-finding"),
        ("batch-update", "batch-update-findings"),
        ("clean-stale", "clean-stale-findings"),
    ],
    "file": [
        ("register", "register-file"),
        ("get", "get-file"),
        ("list", "list-files"),
        ("timeline", "get-file-timeline"),
        ("issue-files", "get-issue-files"),
        ("add-association", "add-file-association"),
        ("delete-record", "delete-file-record"),
        ("migrate-registry", "migrate-registry"),
    ],
    "annotation": [
        ("get", "get-annotation"),
        ("list", "list-annotations"),
        ("resolve", "resolve-annotation"),
        ("carry-forward", "carry-forward-annotation"),
        ("annotate-file", "annotate-file"),
    ],
    "scanner": [
        ("enable", "enable-scanner"),
        ("disable", "disable-scanner"),
        ("status", "get-scan-status"),
        ("list", "list-scanners"),
        ("list-available", "list-available-scanners"),
        ("preview", "preview-scan"),
        ("trigger", "trigger-scan"),
        ("trigger-batch", "trigger-scan-batch"),
        ("prompt-packs", "list-prompt-packs"),
    ],
    "observation": [
        ("list", "list-observations"),
        ("dismiss", "dismiss-observation"),
        ("promote", "promote-observation"),
        ("link", "link-observation"),
        ("promote-to-issue", "promote-observations-to-issue"),
        ("batch-dismiss", "batch-dismiss-observations"),
        ("batch-link", "batch-link-observations"),
        ("batch-promote", "batch-promote-observations"),
    ],
}

# Visible in-group aliases whose flat counterpart is itself visible — they do
# NOT belong in _GROUP_MAP, whose rows also drive the flat-name-must-be-hidden
# assertions (which would wrongly require the visible ``observe`` to be hidden).
# ``observation create`` is a visible alias of ``observe`` (filigree-ce3bfae865).
_VISIBLE_GROUP_ALIASES: dict[str, set[str]] = {"observation": {"create"}}

_FLAT_NAMES = [(g, flat) for g, members in _GROUP_MAP.items() for _sub, flat in members]
_GROUPED = [(g, sub) for g, members in _GROUP_MAP.items() for sub, _flat in members]


def _top_help() -> str:
    return CliRunner().invoke(cli, ["--help"]).output


@pytest.mark.parametrize("group", sorted(_GROUP_MAP))
def test_group_visible_in_top_level_help(group: str) -> None:
    grp = cli.commands.get(group)
    assert grp is not None, f"{group} group must be registered"
    assert grp.hidden is False, f"{group} group must be visible in --help"
    assert f"\n  {group} " in _top_help(), f"{group} should appear in top-level --help"


@pytest.mark.parametrize(("group", "subverb"), _GROUPED)
def test_grouped_subverb_invokable(group: str, subverb: str) -> None:
    result = CliRunner().invoke(cli, [group, subverb, "--help"])
    assert result.exit_code == 0, f"filigree {group} {subverb} --help failed: {result.output}"


@pytest.mark.parametrize(("group", "flat"), _FLAT_NAMES)
def test_flat_alias_still_resolves(group: str, flat: str) -> None:
    result = CliRunner().invoke(cli, [flat, "--help"])
    assert result.exit_code == 0, f"flat alias {flat} should still resolve: {result.output}"


@pytest.mark.parametrize(("group", "flat"), _FLAT_NAMES)
def test_flat_alias_hidden_from_top_level_help(group: str, flat: str) -> None:
    assert f"\n  {flat} " not in _top_help(), f"{flat} should be hidden from top-level --help"


@pytest.mark.parametrize(("group", "flat"), _FLAT_NAMES)
def test_flat_alias_registered_but_hidden(group: str, flat: str) -> None:
    cmd = cli.commands.get(flat)
    assert cmd is not None, f"{flat} must remain registered (hidden, not removed)"
    assert cmd.hidden is True, f"{flat} must be hidden"


# Two scanner subverbs were renamed for consistency (available->list-available,
# prompts->prompt-packs) and report-finding was relocated to ``finding report``.
# The previously-shipped in-group spellings are preserved as HIDDEN in-group
# aliases so ``filigree scanner available`` (etc.) does not break.
_SCANNER_LEGACY_INGROUP = ["available", "prompts", "report-finding"]


@pytest.mark.parametrize("subverb", _SCANNER_LEGACY_INGROUP)
def test_legacy_scanner_ingroup_alias_resolves_but_hidden(subverb: str) -> None:
    result = CliRunner().invoke(cli, ["scanner", subverb, "--help"])
    assert result.exit_code == 0, f"scanner {subverb} must still resolve: {result.output}"
    scanner_help = CliRunner().invoke(cli, ["scanner", "--help"]).output
    assert f"\n  {subverb} " not in scanner_help, f"scanner {subverb} must be hidden from 'scanner --help'"


def test_observe_stays_flat_and_visible() -> None:
    cmd = cli.commands.get("observe")
    assert cmd is not None, "observe must stay a flat top-level verb"
    assert cmd.hidden is False, "observe must stay visible"
    assert "\n  observe " in _top_help()
    assert CliRunner().invoke(cli, ["observe", "--help"]).exit_code == 0


def test_mapping_matches_source() -> None:
    """The mapping table and the actual registered group subcommands stay in sync."""
    for group, members in _GROUP_MAP.items():
        grp = cli.commands.get(group)
        assert grp is not None
        registered = {n for n, c in grp.commands.items() if not c.hidden}  # type: ignore[attr-defined]
        expected = {sub for sub, _flat in members} | _VISIBLE_GROUP_ALIASES.get(group, set())
        assert registered == expected, f"{group}: source has {registered}, table has {expected}"


def test_observation_create_alias_visible_and_shares_callback() -> None:
    """``observation create`` is a visible alias of ``observe`` (filigree-ce3bfae865).

    Same callback (no fork), distinct Command object (alias-specific help), and
    visible in ``filigree observation --help`` with alias-of-observe wording.
    """
    result = CliRunner().invoke(cli, ["observation", "create", "--help"])
    assert result.exit_code == 0, f"filigree observation create --help failed: {result.output}"
    assert "observe" in result.output, "alias help should mention the canonical observe verb"

    group_help = CliRunner().invoke(cli, ["observation", "--help"]).output
    assert "\n  create " in group_help, "create should be visible in 'observation --help'"
    create_line = next(line for line in group_help.splitlines() if line.startswith("  create "))
    assert "observe" in create_line, f"create's short help should mark it as an alias of observe: {create_line!r}"

    observe = cli.commands["observe"]
    grp = cli.commands["observation"]
    create = grp.commands["create"]  # type: ignore[attr-defined]
    assert create.callback is observe.callback, "alias must share observe's callback — no fork"
    assert create is not observe, "alias must be a clone, not the shared observe object (help differs)"
