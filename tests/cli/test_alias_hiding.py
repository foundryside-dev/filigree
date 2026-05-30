"""Surface consolidation (filigree-c73c75b652).

The long-form MCP-mirror verbs (``get-ready``, ``update-issue``, …) are hidden
from ``--help`` but remain fully functional. Their canonical short siblings
(``ready``, ``update``, …) stay visible. This guards both halves of that
contract so the consolidation can't silently regress (an un-hidden alias
re-cluttering help, or a hidden alias becoming a dead command).
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from filigree.cli import _HIDDEN_ALIAS_VERBS, cli

# (hidden long form, canonical short form that must stay visible)
_ALIAS_PAIRS = [
    ("get-issue", "show"),
    ("get-ready", "ready"),
    ("get-blocked", "blocked"),
    ("get-changes", "changes"),
    ("get-plan", "plan"),
    ("get-critical-path", "critical-path"),
    ("get-type-info", "type-info"),
    ("get-valid-transitions", "transitions"),
    ("get-workflow-statuses", "workflow-statuses"),
    ("get-workflow-guide", "guide"),
    ("get-label-taxonomy", "taxonomy"),
    ("get-issue-events", "events"),
    ("get-stale-claims", "stale-claims"),
    ("list-issues", "list"),
    ("list-labels", "labels"),
    ("list-types", "types"),
    ("list-packs", "packs"),
    ("update-issue", "update"),
    ("validate-issue", "validate"),
    ("reclaim-issue", "reclaim"),
    ("release-claim", "release"),
    ("undo-last", "undo"),
]


def test_alias_pairs_cover_the_hidden_list() -> None:
    """The test's pair table and the source hide-list stay in lockstep."""
    assert {long for long, _ in _ALIAS_PAIRS} == set(_HIDDEN_ALIAS_VERBS)


@pytest.mark.parametrize(("long", "short"), _ALIAS_PAIRS)
def test_long_form_hidden_short_form_visible(long: str, short: str) -> None:
    long_cmd = cli.commands.get(long)
    short_cmd = cli.commands.get(short)
    assert long_cmd is not None, f"{long} should still be registered (hidden, not removed)"
    assert short_cmd is not None, f"canonical {short} must exist"
    assert long_cmd.hidden is True, f"{long} should be hidden from --help"
    assert short_cmd.hidden is False, f"canonical {short} must stay visible"


def test_hidden_aliases_absent_from_help_but_short_forms_present() -> None:
    help_text = CliRunner().invoke(cli, ["--help"]).output
    for long, short in _ALIAS_PAIRS:
        assert f"\n  {long} " not in help_text, f"{long} should not appear in --help"
        assert short in help_text, f"canonical {short} should appear in --help"


def test_hidden_alias_still_invokable() -> None:
    """Hidden != removed: a hidden alias must still resolve and run."""
    runner = CliRunner()
    # --help on the hidden command itself still works (command exists)...
    assert runner.invoke(cli, ["get-ready", "--help"]).exit_code == 0
    # ...and it is not advertised in the group listing.
    assert "get-ready" not in runner.invoke(cli, ["--help"]).output
