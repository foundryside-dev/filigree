"""CLI tests for workflow commands (types, transitions, validate, packs, guide, plan, batch, events, explain-state)."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from filigree.cli import cli
from filigree.cli_common import get_db
from tests.cli.conftest import _extract_id


class TestWorkflowCli:
    """Tests for workflow template CLI commands (types, type-info, transitions, packs, validate, guide)."""

    def test_types_lists_registered_types(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["types"])
        assert result.exit_code == 0
        # Core pack types should appear
        assert "task" in result.output
        assert "bug" in result.output

    def test_types_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["types", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "items" in data
        assert not data["has_more"]
        type_names = {t["type"] for t in data["items"]}
        assert "task" in type_names
        # Each item must include initial_state and states as {name, category} dicts.
        for t in data["items"]:
            assert "initial_state" in t, f"initial_state missing from type {t['type']}"
            assert "states" in t
            assert len(t["states"]) > 0
            assert isinstance(t["states"][0], dict), "states[0] must be a dict with name/category"
            assert {"name", "category"} <= set(t["states"][0].keys())

    def test_type_info_shows_workflow(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["type-info", "task"])
        assert result.exit_code == 0
        assert "States:" in result.output
        assert "Transitions:" in result.output

    def test_type_info_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["type-info", "task", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["type"] == "task"
        assert data["pack"] == "core"
        assert "states" in data
        assert "transitions" in data
        assert "initial_state" in data

    def test_type_info_json_includes_field_schema_metadata(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """--json must include options/default/required_at for fields that define them.

        The built-in `bug` type's `severity` field has all three, so it anchors this test.
        """
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["type-info", "bug", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        fields = {f["name"]: f for f in data["fields_schema"]}
        assert "severity" in fields, "bug.severity field missing from --json output"
        severity = fields["severity"]
        assert severity.get("options") == ["critical", "major", "minor", "cosmetic"]
        assert severity.get("default") == "major"
        assert severity.get("required_at") == ["confirmed"]

    def test_type_info_unknown(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["type-info", "nonexistent_type"])
        assert result.exit_code == 1
        assert "Unknown type" in result.output

    def test_blocked_json_include_blockers_hydrates_blocker_context(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        with get_db() as db:
            blocked = db.create_issue("Blocked")
            blocker = db.create_issue("Blocker", priority=1, type="bug")
            db.add_dependency(blocked.id, blocker.id)

        result = runner.invoke(cli, ["blocked", "--json", "--include-blockers"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        item = data["items"][0]
        assert item["issue_id"] == blocked.id
        assert item["blocked_by"] == [blocker.id]
        assert item["blockers"] == [
            {
                "issue_id": blocker.id,
                "title": "Blocker",
                "status": "triage",
                "priority": 1,
                "type": "bug",
            }
        ]

    def test_transitions_shows_valid_states(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Transitions test"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["transitions", issue_id])
        assert result.exit_code == 0
        # An open task should have at least one transition
        assert "→" in result.output or "Transitions from" in result.output

    def test_transitions_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Transitions JSON"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["transitions", issue_id, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert set(data) == {"items", "has_more"}
        assert data["has_more"] is False
        if data["items"]:
            assert "to" in data["items"][0]
            assert "ready" in data["items"][0]

    def test_transitions_not_found(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["transitions", "nonexistent-abc"])
        assert result.exit_code == 1
        assert "Not found" in result.output

    def test_packs_lists_enabled(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["packs"])
        assert result.exit_code == 0
        assert "core" in result.output

    def test_packs_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["packs", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "items" in data
        assert not data["has_more"]
        pack_names = {p["pack"] for p in data["items"]}
        assert "core" in pack_names
        # Each item must include requires_packs (matching PackListItem TypedDict).
        for p in data["items"]:
            assert "requires_packs" in p, f"requires_packs missing from pack {p['pack']}"
            assert isinstance(p["requires_packs"], list)

    def test_validate_clean_issue(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Valid issue"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["validate", issue_id])
        assert result.exit_code == 0
        assert "valid" in result.output.lower()

    def test_validate_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Validate JSON"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["validate", issue_id, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "valid" in data
        assert "warnings" in data
        assert "errors" in data

    def test_validate_not_found(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["validate", "nonexistent-abc"])
        assert result.exit_code == 1
        assert "Not found" in result.output

    def test_guide_core_pack(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["guide", "core"])
        assert result.exit_code == 0
        # Guide should have some content (overview, tips, etc.)
        assert len(result.output) > 20

    def test_guide_unknown_pack(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["guide", "nonexistent_pack"])
        assert result.exit_code == 1
        assert "Unknown pack" in result.output

    def test_guide_json_returns_object(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """--json must emit guide as an object, not a stringified MappingProxyType."""
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["guide", "core", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["pack"] == "core"
        assert isinstance(data["guide"], dict), f"guide must be dict, got {type(data['guide']).__name__}"
        assert "overview" in data["guide"]

    def test_templates_group_default(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """filigree templates (no subcommand) still lists templates."""
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["templates"])
        assert result.exit_code == 0
        assert "task" in result.output

    def test_templates_reload(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["templates", "reload"])
        assert result.exit_code == 0
        assert "reloaded" in result.output.lower()

    def test_templates_type_renders_required_at(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Bug filigree-36c0699c5d: CLI must annotate fields from required_at, not nonexistent 'required'."""
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["templates", "--type", "bug"])
        assert result.exit_code == 0
        # `severity` is required at the `confirmed` state in the built-in bug pack
        severity_line = next((line for line in result.output.splitlines() if line.lstrip().startswith("severity:")), None)
        assert severity_line is not None, f"severity field missing from output:\n{result.output}"
        assert "confirmed" in severity_line, f"severity annotation must mention the required_at state 'confirmed', got: {severity_line!r}"

    def test_templates_type_omits_annotation_when_not_required(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Bug filigree-36c0699c5d: fields without required_at must not carry a required annotation."""
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["templates", "--type", "bug"])
        assert result.exit_code == 0
        # `component` has no required_at in the built-in bug pack
        component_line = next((line for line in result.output.splitlines() if line.lstrip().startswith("component:")), None)
        assert component_line is not None
        assert "required" not in component_line

    def test_templates_reload_corrupt_config_emits_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Bug filigree-259e5b58ef: corrupt config.json must surface as a structured error, not a traceback.

        Mirrors tests/mcp/test_tools.py::test_reload_templates_corrupt_config_returns_structured_error.
        """
        from filigree.types.api import ErrorCode

        runner, project_root = cli_in_project
        config_path = project_root / ".filigree" / "config.json"
        config_path.write_text("{not valid json")

        result = runner.invoke(cli, ["templates", "reload", "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["code"] == ErrorCode.VALIDATION
        assert "config.json" in payload["error"]

    def test_templates_reload_corrupt_config_plain_text(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Bug filigree-259e5b58ef: without --json, corrupt config still exits non-zero with a clean message."""
        runner, project_root = cli_in_project
        config_path = project_root / ".filigree" / "config.json"
        config_path.write_text("{not valid json")

        result = runner.invoke(cli, ["templates", "reload"])
        assert result.exit_code == 1
        assert "config.json" in result.output
        assert "Traceback" not in result.output

    def test_templates_reload_refreshes_context_md(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Bug filigree-00359c8498: reload must regenerate context.md, not just invalidate the cache.

        Mirrors tests/mcp/test_tools.py::test_reload_templates_refreshes_context_md.
        """
        runner, project_root = cli_in_project
        summary_path = project_root / ".filigree" / "context.md"
        summary_path.write_text("STALE-MARKER-BEFORE-RELOAD")

        result = runner.invoke(cli, ["templates", "reload"])
        assert result.exit_code == 0
        assert summary_path.read_text() != "STALE-MARKER-BEFORE-RELOAD"

    def test_templates_reload_json_success(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Bug filigree-dfbcc84687: templates reload --json emits a structured success body."""
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["templates", "reload", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload == {"status": "ok"}

    def test_type_info_unknown_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Bug filigree-dfbcc84687: type-info --json on unknown type emits the 2.0 envelope."""
        from filigree.types.api import ErrorCode

        runner, _ = cli_in_project
        result = runner.invoke(cli, ["type-info", "nonexistent_type", "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["code"] == ErrorCode.NOT_FOUND
        assert "nonexistent_type" in payload["error"]

    def test_transitions_not_found_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Bug filigree-dfbcc84687: transitions --json on missing issue emits the envelope."""
        from filigree.types.api import ErrorCode

        runner, _ = cli_in_project
        result = runner.invoke(cli, ["transitions", "nonexistent-abc", "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["code"] == ErrorCode.NOT_FOUND

    def test_validate_not_found_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Bug filigree-dfbcc84687: validate --json on missing issue emits the envelope."""
        from filigree.types.api import ErrorCode

        runner, _ = cli_in_project
        result = runner.invoke(cli, ["validate", "nonexistent-abc", "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["code"] == ErrorCode.NOT_FOUND

    def test_explain_status_unknown_type_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Bug filigree-dfbcc84687: explain-status --json on unknown type emits the envelope."""
        from filigree.types.api import ErrorCode

        runner, _ = cli_in_project
        result = runner.invoke(cli, ["explain-status", "nonexistent_type", "open", "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["code"] == ErrorCode.NOT_FOUND

    def test_explain_status_unknown_status_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Bug filigree-dfbcc84687: explain-status --json on unknown status emits the envelope."""
        from filigree.types.api import ErrorCode

        runner, _ = cli_in_project
        result = runner.invoke(cli, ["explain-status", "task", "nonexistent_status", "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["code"] == ErrorCode.NOT_FOUND

    def test_get_template_alias_known_type(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Bug filigree-6213766f9b: get-template <type> mirrors the MCP get_template tool."""
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["get-template", "bug", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["type"] == "bug"
        assert payload["pack"] == "core"
        assert "fields_schema" in payload
        assert "states" in payload
        assert "initial_state" in payload

    def test_get_template_human_output_shows_full_workflow(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["get-template", "task"])
        assert result.exit_code == 0
        assert "States:" in result.output
        assert "Transitions:" in result.output

    def test_type_info_json_matches_get_template_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project

        template = runner.invoke(cli, ["get-template", "task", "--json"])
        type_info = runner.invoke(cli, ["type-info", "task", "--json"])

        assert template.exit_code == type_info.exit_code == 0
        assert json.loads(type_info.output) == json.loads(template.output)

    def test_get_template_alias_unknown_type_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Bug filigree-6213766f9b: get-template --json on unknown type emits the 2.0 envelope."""
        from filigree.types.api import ErrorCode

        runner, _ = cli_in_project
        result = runner.invoke(cli, ["get-template", "nonexistent_type", "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["code"] == ErrorCode.NOT_FOUND
        assert "nonexistent_type" in payload["error"]


class TestCreatePlanCli:
    def test_create_plan_from_stdin(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        plan_json = json.dumps(
            {
                "milestone": {"title": "v1.0 Release"},
                "phases": [
                    {
                        "title": "Phase 1",
                        "steps": [
                            {"title": "Step A"},
                            {"title": "Step B", "deps": [0]},
                        ],
                    }
                ],
            }
        )
        result = runner.invoke(cli, ["create-plan"], input=plan_json)
        assert result.exit_code == 0
        assert "v1.0 Release" in result.output

    def test_create_plan_from_file(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, project_root = cli_in_project
        plan_file = project_root / "plan.json"
        plan_file.write_text(
            json.dumps(
                {
                    "milestone": {"title": "File Plan"},
                    "phases": [{"title": "Phase 1", "steps": [{"title": "Step 1"}]}],
                }
            )
        )
        result = runner.invoke(cli, ["create-plan", "--file", str(plan_file)])
        assert result.exit_code == 0
        assert "File Plan" in result.output

    def test_create_plan_json_output(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        plan_json = json.dumps(
            {
                "milestone": {"title": "JSON Plan"},
                "phases": [{"title": "P1", "steps": [{"title": "S1"}]}],
            }
        )
        result = runner.invoke(cli, ["create-plan", "--json"], input=plan_json)
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "milestone" in data
        assert "phases" in data

    def test_create_plan_invalid_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["create-plan"], input="not json")
        assert result.exit_code == 1
        assert "Invalid JSON" in result.output or "error" in result.output.lower()

    def test_create_plan_validation_error_exits_1(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Backend ValueError (e.g. empty title) should exit 1, not crash."""
        runner, _ = cli_in_project
        plan_json = json.dumps({"milestone": {"title": ""}, "phases": [{"title": "P1"}]})
        result = runner.invoke(cli, ["create-plan"], input=plan_json)
        assert result.exit_code == 1
        assert "error" in result.output.lower()

    def test_create_plan_bad_dep_ref_exits_1(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """IndexError from bad dep refs should exit 1, not crash."""
        runner, _ = cli_in_project
        plan_json = json.dumps(
            {
                "milestone": {"title": "MS"},
                "phases": [{"title": "P1", "steps": [{"title": "S1", "deps": [99]}]}],
            }
        )
        result = runner.invoke(cli, ["create-plan"], input=plan_json)
        assert result.exit_code == 1
        assert "error" in result.output.lower()

    def test_create_plan_step_not_object_clean_error(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Regression: a non-dict step (e.g. a JSON string) used to surface as
        an uncaught ``AttributeError`` inside ``db.create_plan``. CLI must
        reject it up front.
        """
        runner, _ = cli_in_project
        plan_json = json.dumps(
            {
                "milestone": {"title": "MS"},
                "phases": [{"title": "P1", "steps": ["bad-step"]}],
            }
        )
        result = runner.invoke(cli, ["create-plan"], input=plan_json)
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "Step 1" in result.output
        assert "object" in result.output.lower()

    def test_create_plan_dep_float_rejected(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Regression: JSON float dep like 0.1 used to be ``str()``ed to
        ``"0.1"`` and silently interpreted as cross-phase ref ``phase 0,
        step 1``. CLI must reject non-int/non-string dep types.
        """
        runner, _ = cli_in_project
        plan_json = json.dumps(
            {
                "milestone": {"title": "MS"},
                "phases": [
                    {
                        "title": "P1",
                        "steps": [{"title": "S0"}, {"title": "S1", "deps": [0.1]}],
                    }
                ],
            }
        )
        result = runner.invoke(cli, ["create-plan"], input=plan_json)
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "dep" in result.output.lower()

    def test_create_plan_dep_bool_rejected(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """``True``/``False`` are ``int`` subclasses in Python; without an
        explicit bool check ``str(True)`` becomes ``"True"`` and ``int()`` raises.
        """
        runner, _ = cli_in_project
        plan_json = json.dumps(
            {
                "milestone": {"title": "MS"},
                "phases": [{"title": "P1", "steps": [{"title": "S1", "deps": [True]}]}],
            }
        )
        result = runner.invoke(cli, ["create-plan"], input=plan_json)
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "bool" in result.output.lower()

    def test_create_plan_steps_not_list_clean_error(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """A non-list ``steps`` value (e.g. a dict) must produce a clean error."""
        runner, _ = cli_in_project
        plan_json = json.dumps(
            {
                "milestone": {"title": "MS"},
                "phases": [{"title": "P1", "steps": {"oops": "not a list"}}],
            }
        )
        result = runner.invoke(cli, ["create-plan"], input=plan_json)
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "steps" in result.output.lower()
        assert "list" in result.output.lower()


class TestPlanCli:
    def test_plan_not_found(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["plan", "nonexistent-abc"])
        assert result.exit_code == 1
        assert "Not found" in result.output

    def test_plan_display(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        plan_json = json.dumps(
            {
                "milestone": {"title": "v1.0"},
                "phases": [{"title": "Phase 1", "steps": [{"title": "Step 1"}]}],
            }
        )
        r = runner.invoke(cli, ["create-plan"], input=plan_json)
        assert r.exit_code == 0
        # Extract milestone ID from output
        milestone_line = next(line for line in r.output.splitlines() if "v1.0" in line)
        # Parse ID from "Created plan: v1.0 (test-xxx)"
        ms_id = milestone_line.split("(")[1].rstrip(")")
        result = runner.invoke(cli, ["plan", ms_id])
        assert result.exit_code == 0
        assert "Milestone" in result.output
        assert "Phase 1" in result.output

    def test_plan_json_output(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        plan_json = json.dumps(
            {
                "milestone": {"title": "v2.0"},
                "phases": [{"title": "Phase 1", "steps": [{"title": "Step 1"}]}],
            }
        )
        r = runner.invoke(cli, ["create-plan"], input=plan_json)
        assert r.exit_code == 0
        milestone_line = next(line for line in r.output.splitlines() if "v2.0" in line)
        ms_id = milestone_line.split("(")[1].rstrip(")")
        result = runner.invoke(cli, ["plan", ms_id, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "milestone" in data
        assert "issue_id" in data["milestone"]
        assert "fields" not in data["milestone"]

        full_result = runner.invoke(cli, ["get-plan", ms_id, "--json", "--detail", "full"])
        assert full_result.exit_code == 0
        full_data = json.loads(full_result.output)
        assert "fields" in full_data["milestone"]

    def test_plan_step_marker_uses_status_category(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Bug fix: filigree-6b0f8cfb49 — step icons must be derived from

        status_category, not raw status names. Built-in planning states use
        ``pending`` (open) and ``in_progress`` (wip), not the legacy
        ``open``/``closed`` names the CLI hardcodes.
        """
        runner, _ = cli_in_project
        plan_json = json.dumps(
            {
                "milestone": {"title": "Markers MS"},
                "phases": [{"title": "Phase 1", "steps": [{"title": "Step 1"}]}],
            }
        )
        r = runner.invoke(cli, ["create-plan"], input=plan_json)
        assert r.exit_code == 0
        milestone_line = next(line for line in r.output.splitlines() if "Markers MS" in line)
        ms_id = milestone_line.split("(")[1].rstrip(")")
        result = runner.invoke(cli, ["plan", ms_id])
        assert result.exit_code == 0
        # A pending step is "open" category → space marker, NOT "?" (unknown)
        step_lines = [ln for ln in result.output.splitlines() if "Step 1" in ln]
        assert step_lines, "Step 1 missing from plan output"
        assert "[?]" not in step_lines[0], f"Unknown marker for pending step: {step_lines[0]!r}"
        assert "[ ]" in step_lines[0], f"Expected open marker for pending step: {step_lines[0]!r}"

    def test_plan_phase_marker_uses_status_category(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Phase WIP marker must use status_category=='wip', not raw 'in_progress'.

        The built-in planning phase workflow uses ``active`` (wip category),
        not ``in_progress`` — so the old equality check never matched.
        """
        runner, _ = cli_in_project
        plan_json = json.dumps(
            {
                "milestone": {"title": "PhaseMarkers MS"},
                "phases": [{"title": "Phase A", "steps": [{"title": "Step 1"}]}],
            }
        )
        r = runner.invoke(cli, ["create-plan"], input=plan_json)
        assert r.exit_code == 0
        milestone_line = next(line for line in r.output.splitlines() if "PhaseMarkers MS" in line)
        ms_id = milestone_line.split("(")[1].rstrip(")")

        # Advance phase to wip state (built-in planning uses 'active' as wip)
        phase_line = next(line for line in r.output.splitlines() if "Phase A" in line)
        # "Created plan: ...(ms_id)" then "  Phase: Phase A (1 steps)"
        # Find phase id via JSON call
        result_json = runner.invoke(cli, ["plan", ms_id, "--json", "--detail", "full"])
        data = json.loads(result_json.output)
        phase_id = data["phases"][0]["phase"]["issue_id"]
        upd = runner.invoke(cli, ["update", phase_id, "--status", "active"])
        assert upd.exit_code == 0, f"update to active failed: {upd.output}"

        result = runner.invoke(cli, ["plan", ms_id])
        assert result.exit_code == 0
        phase_lines = [ln for ln in result.output.splitlines() if "Phase A" in ln]
        assert phase_lines, "Phase A missing from plan output"
        # WIP marker should appear, not the empty [    ] marker
        assert "[WIP]" in phase_lines[0], f"Expected WIP marker for active phase: {phase_lines[0]!r}"
        # Silence unused-variable warning for phase_line sanity output
        assert "Phase A" in phase_line

    def test_create_plan_bad_priority_exits_1(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Bug fix: filigree-a5e7090f76 — out-of-range priority must exit 1

        with a clean error message, not crash with sqlite3.IntegrityError
        traceback.
        """
        runner, _ = cli_in_project
        plan_json = json.dumps(
            {
                "milestone": {"title": "MS"},
                "phases": [{"title": "P1", "steps": [{"title": "S1", "priority": 99}]}],
            }
        )
        result = runner.invoke(cli, ["create-plan"], input=plan_json)
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "priority" in result.output.lower()


class TestCreatePlanMissingKeys:
    def test_missing_milestone_key(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["create-plan"], input=json.dumps({"phases": []}))
        assert result.exit_code == 1


class TestCreatePlanMalformedInput:
    """Bug filigree-802ab8: wrong value types should exit 1, not crash with traceback."""

    def test_milestone_as_list(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """milestone as a list instead of dict should give clean error, not AttributeError."""
        runner, _ = cli_in_project
        plan_json = json.dumps({"milestone": ["not", "a", "dict"], "phases": []})
        result = runner.invoke(cli, ["create-plan"], input=plan_json)
        assert result.exit_code == 1
        assert "milestone" in result.output.lower()
        assert "object" in result.output.lower()
        assert "Traceback" not in result.output

    def test_phases_as_string(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """phases as a string instead of list should give clean error, not TypeError."""
        runner, _ = cli_in_project
        plan_json = json.dumps({"milestone": {"title": "MS"}, "phases": "not a list"})
        result = runner.invoke(cli, ["create-plan"], input=plan_json)
        assert result.exit_code == 1
        assert "phases" in result.output.lower()
        assert "list" in result.output.lower()
        assert "Traceback" not in result.output

    def test_phase_entry_as_string(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Non-dict phase entries should give clean error, not AttributeError."""
        runner, _ = cli_in_project
        plan_json = json.dumps({"milestone": {"title": "MS"}, "phases": ["not a dict"]})
        result = runner.invoke(cli, ["create-plan"], input=plan_json)
        assert result.exit_code == 1
        assert "phase 1" in result.output.lower()
        assert "object" in result.output.lower()
        assert "Traceback" not in result.output

    def test_data_as_list(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Top-level JSON as a list should give clean error, not crash."""
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["create-plan"], input=json.dumps([1, 2, 3]))
        assert result.exit_code == 1
        # Should produce a user-visible error message (not empty from unhandled exception)
        assert result.output.strip()


class TestCreatePlanFileErrors:
    """Bug filigree-5cc1de: file I/O errors should give clean error, not unhandled traceback."""

    def test_directory_as_file_path(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Passing a directory instead of a file should exit cleanly."""
        runner, project_root = cli_in_project
        dir_path = project_root / "somedir"
        dir_path.mkdir()
        result = runner.invoke(cli, ["create-plan", "--file", str(dir_path)])
        assert result.exit_code != 0
        # Exception must be handled (SystemExit from sys.exit), not leaked raw
        assert result.exception is None or isinstance(result.exception, SystemExit)

    def test_binary_file(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Binary file that can't be decoded as UTF-8 should give clean error."""
        runner, project_root = cli_in_project
        bin_file = project_root / "plan.bin"
        bin_file.write_bytes(b"\x80\x81\x82\xff\xfe")
        result = runner.invoke(cli, ["create-plan", "--file", str(bin_file)])
        assert result.exit_code != 0
        # Exception must be handled (SystemExit from sys.exit), not leaked raw
        assert result.exception is None or isinstance(result.exception, SystemExit)


class TestCreatePlanTitleTypeValidation:
    """Bug filigree-401a96653b: non-string title raised AttributeError from db_planning's
    ``.strip()`` call instead of producing a clean validation error.
    """

    def test_milestone_title_int_clean_error(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        plan_json = json.dumps({"milestone": {"title": 123}, "phases": []})
        result = runner.invoke(cli, ["create-plan"], input=plan_json)
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "title" in result.output.lower()
        assert "string" in result.output.lower() or "str" in result.output.lower()

    def test_milestone_title_int_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        plan_json = json.dumps({"milestone": {"title": 123}, "phases": []})
        result = runner.invoke(cli, ["create-plan", "--json"], input=plan_json)
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "VALIDATION"
        assert "title" in data["error"].lower()

    def test_phase_title_bool_clean_error(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        plan_json = json.dumps(
            {
                "milestone": {"title": "MS"},
                "phases": [{"title": True, "steps": []}],
            }
        )
        result = runner.invoke(cli, ["create-plan"], input=plan_json)
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "phase 1" in result.output.lower()
        assert "title" in result.output.lower()

    def test_step_title_none_clean_error(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        plan_json = json.dumps(
            {
                "milestone": {"title": "MS"},
                "phases": [{"title": "P1", "steps": [{"title": None}]}],
            }
        )
        result = runner.invoke(cli, ["create-plan"], input=plan_json)
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "step 1" in result.output.lower()
        assert "title" in result.output.lower()


class TestPlanningCliJsonErrorEnvelope:
    """Bug filigree-f099dedc5d: --json validation/not-found paths must emit the
    flat ``{error, code}`` envelope, not plain text.
    """

    def test_plan_not_found_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["plan", "demo-nope", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "NOT_FOUND"
        assert "demo-nope" in data["error"]

    def test_create_plan_invalid_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["create-plan", "--json"], input="not json")
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "VALIDATION"
        assert "JSON" in data["error"] or "json" in data["error"].lower()

    def test_create_plan_missing_file_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, project_root = cli_in_project
        missing_path = project_root / "definitely-missing-plan.json"
        result = runner.invoke(cli, ["create-plan", "--file", str(missing_path), "--json"])
        assert result.exit_code == 1, result.output
        data = json.loads(result.output)
        assert data["code"] == "IO"
        assert str(missing_path) in data["error"]

    def test_create_plan_top_level_list_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["create-plan", "--json"], input=json.dumps([1, 2, 3]))
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "VALIDATION"

    def test_create_plan_missing_keys_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["create-plan", "--json"], input=json.dumps({"phases": []}))
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "VALIDATION"

    def test_create_plan_phase_not_object_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        plan_json = json.dumps({"milestone": {"title": "MS"}, "phases": ["bad"]})
        result = runner.invoke(cli, ["create-plan", "--json"], input=plan_json)
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "VALIDATION"
        assert "phase 1" in data["error"].lower()

    def test_changes_invalid_timestamp_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["changes", "--since", "not-a-timestamp", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "VALIDATION"
        assert "timestamp" in data["error"].lower() or "iso" in data["error"].lower()


class TestBatchCli:
    def test_batch_update(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r1 = runner.invoke(cli, ["create", "A"])
        id1 = _extract_id(r1.output)
        r2 = runner.invoke(cli, ["create", "B"])
        id2 = _extract_id(r2.output)
        result = runner.invoke(cli, ["batch-update", id1, id2, "--priority", "0"])
        assert result.exit_code == 0
        assert "Updated 2" in result.output

    def test_batch_update_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r1 = runner.invoke(cli, ["create", "A"])
        id1 = _extract_id(r1.output)
        result = runner.invoke(cli, ["batch-update", id1, "--priority", "1", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, dict)
        assert "succeeded" in data
        assert "failed" in data

    def test_batch_update_json_malformed_field_returns_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """batch-update --json with bad --field must emit JSON error, not plain text."""
        runner, _ = cli_in_project
        r1 = runner.invoke(cli, ["create", "A"])
        id1 = _extract_id(r1.output)
        result = runner.invoke(cli, ["batch-update", id1, "--field", "no-equals-sign", "--json"])
        data = json.loads(result.output)
        assert "error" in data

    def test_batch_update_partial_failure_exits_nonzero(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r1 = runner.invoke(cli, ["create", "A"])
        id1 = _extract_id(r1.output)
        result = runner.invoke(cli, ["batch-update", id1, "test-deadbeef00", "--priority", "1", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert len(data["succeeded"]) == 1
        assert len(data["failed"]) == 1

    def test_batch_close(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r1 = runner.invoke(cli, ["create", "A"])
        id1 = _extract_id(r1.output)
        r2 = runner.invoke(cli, ["create", "B"])
        id2 = _extract_id(r2.output)
        result = runner.invoke(cli, ["batch-close", id1, id2])
        assert result.exit_code == 0
        assert "Closed 2" in result.output

    def test_batch_close_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r1 = runner.invoke(cli, ["create", "A"])
        id1 = _extract_id(r1.output)
        result = runner.invoke(cli, ["batch-close", id1, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "succeeded" in data
        assert "failed" in data

    def test_batch_close_partial_failure(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r1 = runner.invoke(cli, ["create", "A"])
        id1 = _extract_id(r1.output)
        result = runner.invoke(cli, ["batch-close", id1, "test-deadbeef00", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert len(data["succeeded"]) == 1
        assert len(data["failed"]) == 1

    def test_batch_close_omits_newly_unblocked_when_empty(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        # filigree-893edb553a: BatchResponse contract — newly_unblocked is
        # NotRequired and must be OMITTED entirely when empty (not emitted as []).
        runner, _ = cli_in_project
        r1 = runner.invoke(cli, ["create", "A"])
        id1 = _extract_id(r1.output)
        result = runner.invoke(cli, ["batch-close", id1, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "newly_unblocked" not in data, f"expected omission, got {data!r}"

    def test_batch_close_emits_newly_unblocked_when_present(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        # filigree-893edb553a: when closing a blocker actually unblocks something,
        # newly_unblocked MUST be present in the payload.
        runner, _ = cli_in_project
        r1 = runner.invoke(cli, ["create", "Blocker"])
        blocker_id = _extract_id(r1.output)
        r2 = runner.invoke(cli, ["create", "Dependent"])
        dep_id = _extract_id(r2.output)
        runner.invoke(cli, ["add-dep", dep_id, blocker_id])
        result = runner.invoke(cli, ["batch-close", blocker_id, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "newly_unblocked" in data, f"expected presence, got {data!r}"
        assert any(i["issue_id"] == dep_id for i in data["newly_unblocked"])

    def test_batch_add_label_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r1 = runner.invoke(cli, ["create", "A"])
        id1 = _extract_id(r1.output)
        r2 = runner.invoke(cli, ["create", "B"])
        id2 = _extract_id(r2.output)

        result = runner.invoke(cli, ["batch-add-label", "security", id1, id2, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["succeeded"]) == 2
        assert data["failed"] == []

        listed = runner.invoke(cli, ["list", "--label", "security", "--json"])
        listed_data = json.loads(listed.output)
        listed_ids = {row["issue_id"] for row in listed_data["items"]}
        assert id1 in listed_ids
        assert id2 in listed_ids

    def test_batch_add_label_partial_failure(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r1 = runner.invoke(cli, ["create", "A"])
        id1 = _extract_id(r1.output)
        result = runner.invoke(cli, ["batch-add-label", "security", id1, "test-deadbeef00", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert len(data["succeeded"]) == 1
        assert len(data["failed"]) == 1
        assert data["failed"][0]["id"] == "test-deadbeef00"

    def test_batch_add_label_rejects_priority_like_label_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r1 = runner.invoke(cli, ["create", "A"])
        id1 = _extract_id(r1.output)

        result = runner.invoke(cli, ["batch-add-label", "priority:1", id1, "--json"])

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["succeeded"] == []
        assert len(data["failed"]) == 1
        assert data["failed"][0]["code"] == "VALIDATION"
        assert "priority field" in data["failed"][0]["error"]

    def test_batch_remove_label_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r1 = runner.invoke(cli, ["create", "A"])
        id1 = _extract_id(r1.output)
        r2 = runner.invoke(cli, ["create", "B"])
        id2 = _extract_id(r2.output)
        runner.invoke(cli, ["batch-add-label", "security", id1, id2, "--json"])

        result = runner.invoke(cli, ["batch-remove-label", "security", id1, id2, "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert set(data["succeeded"]) == {id1, id2}
        assert data["failed"] == []

        listed = runner.invoke(cli, ["list", "--label", "security", "--json"])
        listed_data = json.loads(listed.output)
        listed_ids = {row["issue_id"] for row in listed_data["items"]}
        assert id1 not in listed_ids
        assert id2 not in listed_ids

    def test_batch_remove_label_partial_failure(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r1 = runner.invoke(cli, ["create", "A"])
        id1 = _extract_id(r1.output)
        runner.invoke(cli, ["add-label", "security", id1])

        result = runner.invoke(cli, ["batch-remove-label", "security", id1, "test-deadbeef00", "--json"])

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["succeeded"] == [id1]
        assert len(data["failed"]) == 1
        assert data["failed"][0]["id"] == "test-deadbeef00"

    def test_batch_add_comment_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r1 = runner.invoke(cli, ["create", "A"])
        id1 = _extract_id(r1.output)
        r2 = runner.invoke(cli, ["create", "B"])
        id2 = _extract_id(r2.output)

        result = runner.invoke(cli, ["batch-add-comment", "triage-complete", id1, id2, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["succeeded"]) == 2
        # succeeded must contain issue_ids (not comment_ids) to match MCP shape.
        assert set(data["succeeded"]) == {id1, id2}
        assert data["failed"] == []

        comments = runner.invoke(cli, ["get-comments", id1, "--json"])
        comments_data = json.loads(comments.output)
        # filigree-d2263e721d: ListResponse envelope, not a bare list.
        assert any(c["text"] == "triage-complete" for c in comments_data["items"])

    def test_batch_add_comment_partial_failure(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r1 = runner.invoke(cli, ["create", "A"])
        id1 = _extract_id(r1.output)
        result = runner.invoke(cli, ["batch-add-comment", "triage-complete", id1, "test-deadbeef00", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert len(data["succeeded"]) == 1
        assert len(data["failed"]) == 1
        assert data["failed"][0]["id"] == "test-deadbeef00"


class TestEventsCli:
    def test_changes_since(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        runner.invoke(cli, ["create", "Event test"])
        result = runner.invoke(cli, ["changes", "--since", "2020-01-01T00:00:00"])
        assert result.exit_code == 0
        assert "created" in result.output.lower()

    def test_changes_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        runner.invoke(cli, ["create", "Event JSON"])
        result = runner.invoke(cli, ["changes", "--since", "2020-01-01T00:00:00", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "items" in data
        assert len(data["items"]) >= 1

    def test_changes_json_has_next_since_cursor(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        runner.invoke(cli, ["create", "Cursor event A"])
        runner.invoke(cli, ["create", "Cursor event B"])

        result = runner.invoke(cli, ["changes", "--since", "2020-01-01T00:00:00", "--limit", "1", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["has_more"] is True
        assert data["next_since"] == data["items"][-1]["created_at"]

    def test_changes_json_filters_by_actor_type_and_label(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        with get_db() as db:
            target = db.create_issue("Target catch-up", actor="agent-alpha", labels=["cluster:focus"])
            db.create_issue("Other actor", actor="agent-beta", labels=["cluster:focus"])
            db.create_issue("Other label", actor="agent-alpha", labels=["cluster:other"])

        result = runner.invoke(
            cli,
            [
                "changes",
                "--since",
                "2000-01-01T00:00:00+00:00",
                "--actor",
                "agent-alpha",
                "--type",
                "created",
                "--label",
                "cluster:focus",
                "--json",
            ],
        )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert [item["issue_id"] for item in data["items"]] == [target.id]
        assert {item["event_type"] for item in data["items"]} == {"created"}
        assert {item["actor"] for item in data["items"]} == {"agent-alpha"}

    def test_changes_json_excludes_heartbeats_by_default_with_opt_in(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        with get_db() as db:
            issue = db.create_issue("Heartbeat catch-up")
            db.conn.execute(
                "INSERT INTO events (issue_id, event_type, actor, created_at) VALUES (?, ?, ?, ?)",
                (issue.id, "heartbeat", "agent-alpha", "2026-01-01T00:00:00+00:00"),
            )
            db.conn.commit()

        base_args = ["changes", "--since", "2000-01-01T00:00:00+00:00", "--json"]
        default_result = runner.invoke(cli, base_args)
        include_result = runner.invoke(cli, [*base_args, "--include-heartbeats"])

        assert default_result.exit_code == 0, default_result.output
        default_data = json.loads(default_result.output)
        assert "heartbeat" not in {item["event_type"] for item in default_data["items"]}

        assert include_result.exit_code == 0, include_result.output
        include_data = json.loads(include_result.output)
        assert "heartbeat" in {item["event_type"] for item in include_data["items"]}

    def test_changes_json_can_resume_inside_same_timestamp_group(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        tied_timestamp = "2026-01-01T00:00:00+00:00"
        with get_db() as db:
            issue = db.create_issue("CLI same timestamp cursor")
            cursor = db.conn.execute(
                "INSERT INTO events (issue_id, event_type, actor, created_at) VALUES (?, ?, ?, ?)",
                (issue.id, "title_changed", "import", tied_timestamp),
            ).lastrowid
            expected = db.conn.execute(
                "INSERT INTO events (issue_id, event_type, actor, created_at) VALUES (?, ?, ?, ?)",
                (issue.id, "status_changed", "import", tied_timestamp),
            ).lastrowid
            db.conn.commit()

        result = runner.invoke(
            cli,
            [
                "changes",
                "--since",
                tied_timestamp,
                "--after-event-id",
                str(cursor),
                "--limit",
                "1",
                "--json",
            ],
        )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert [item["id"] for item in data["items"]] == [expected]
        assert data["next_since"] == tied_timestamp
        assert data["next_event_id"] == expected

    def test_changes_json_empty_next_since_echoes_normalized_since(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project

        result = runner.invoke(cli, ["changes", "--since", "2099-01-01T00:00:00Z", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["items"] == []
        assert data["has_more"] is False
        assert data["next_since"] == "2099-01-01T00:00:00+00:00"

    def test_changes_empty(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["changes", "--since", "2099-01-01T00:00:00"])
        assert result.exit_code == 0

    def test_changes_z_suffix_normalized(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """'Z' suffix (Zulu) is idiomatic ISO-8601; must be accepted and normalized
        to +00:00 before comparison, matching stored timestamps.
        """
        runner, _ = cli_in_project
        runner.invoke(cli, ["create", "Z test"])
        result = runner.invoke(cli, ["changes", "--since", "2020-01-01T00:00:00Z", "--json"])
        assert result.exit_code == 0, f"Z-suffix must be accepted: {result.output}"
        data = json.loads(result.output)
        assert len(data["items"]) >= 1, "Z-suffixed --since should match stored +00:00 timestamps"

    def test_changes_z_suffix_boundary_matches_plus_zero(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Z-suffix at a precision boundary must behave as +00:00 would.

        Regression: stored rows carry microseconds (``...51.147689+00:00``).
        Raw lexical SQLite compare of ``...51Z`` against ``...51.147689+00:00``
        places the Z-suffix string AFTER the stored value (Z=90 > .=46), so
        ``created_at > ?`` would silently drop matching rows. Normalizing to
        ``+00:00`` first keeps comparison correct.
        """
        runner, _ = cli_in_project
        # Manually insert event with fractional-second +00:00 timestamp (matches
        # how filigree actually writes timestamps via _now_iso).
        from filigree.core import FiligreeDB

        _, project_root = cli_in_project
        db = FiligreeDB.from_filigree_dir(project_root / ".filigree")
        issue = db.create_issue("boundary test")
        db.conn.execute(
            "UPDATE events SET created_at = ? WHERE issue_id = ?",
            ("2026-06-15T12:00:00.123456+00:00", issue.id),
        )
        db.conn.commit()
        db.close()

        # Query with Z-suffix at the same second: ...12:00:00Z should match
        # ...12:00:00.123456+00:00 because the stored event is 0.123s later.
        result = runner.invoke(cli, ["changes", "--since", "2026-06-15T12:00:00Z", "--json"])
        assert result.exit_code == 0, f"CLI error: {result.output}"
        data = json.loads(result.output)
        # The boundary event must be returned (it's 0.123s after --since).
        issue_ids = {e["issue_id"] for e in data["items"]}
        assert issue.id in issue_ids, f"Z-suffix boundary must match fractional +00:00 timestamp; got {data}"

    def test_changes_non_utc_offset_normalized(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Non-UTC offset --since must be converted to UTC before SQLite text compare.

        Regression: ``_normalize_iso_timestamp`` previously returned the offset
        unchanged. A cursor like ``2026-06-15T13:00:00+01:00`` (chronologically
        12:00 UTC) is lexically AFTER a stored event ``2026-06-15T12:30:00+00:00``,
        so ``WHERE created_at > ?`` silently dropped events that were actually
        chronologically after the cursor.
        """
        from filigree.core import FiligreeDB

        runner, project_root = cli_in_project
        db = FiligreeDB.from_filigree_dir(project_root / ".filigree")
        issue = db.create_issue("non-utc test")
        # Event at 12:30 UTC, half an hour after the cursor's chronological time.
        db.conn.execute(
            "UPDATE events SET created_at = ? WHERE issue_id = ?",
            ("2026-06-15T12:30:00+00:00", issue.id),
        )
        db.conn.commit()
        db.close()

        # Cursor at 13:00 +01:00 == 12:00 UTC, 30min before the event.
        result = runner.invoke(cli, ["changes", "--since", "2026-06-15T13:00:00+01:00", "--json"])
        assert result.exit_code == 0, f"CLI error: {result.output}"
        data = json.loads(result.output)
        issue_ids = {e["issue_id"] for e in data["items"]}
        assert issue.id in issue_ids, f"non-UTC offset cursor must be normalized to UTC before lexical compare; got {data}"

    def test_changes_negative_limit_rejected(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """``--limit`` declares a positive-events maximum; SQLite treats
        ``LIMIT -1`` as unbounded, contradicting the contract.
        """
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["changes", "--since", "2020-01-01T00:00:00", "--limit", "-1"])
        assert result.exit_code != 0
        # Click's IntRange emits "Invalid value for '--limit'..."
        assert "limit" in result.output.lower() or "limit" in (result.stderr or "").lower()

    def test_changes_zero_limit_rejected(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """``--limit=0`` is a degenerate query; reject as invalid."""
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["changes", "--since", "2020-01-01T00:00:00", "--limit", "0"])
        assert result.exit_code != 0

    def test_changes_malformed_since_rejected(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Malformed --since input must produce a clean error, not silent empty result."""
        runner, _ = cli_in_project
        runner.invoke(cli, ["create", "Malformed test"])
        result = runner.invoke(cli, ["changes", "--since", "not-a-date"])
        assert result.exit_code != 0, "malformed timestamp must error"
        assert "invalid" in result.output.lower() or "invalid" in (result.stderr or "").lower()

    def test_events_for_issue(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Track events"])
        issue_id = _extract_id(r.output)
        runner.invoke(cli, ["update", issue_id, "--title", "Changed"])
        result = runner.invoke(cli, ["events", issue_id])
        assert result.exit_code == 0

    def test_events_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Track JSON"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["events", issue_id, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "items" in data
        assert data["items"][0]["event_id"]
        assert "id" not in data["items"][0]

    def test_events_not_found(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["events", "nonexistent-abc"])
        assert result.exit_code == 1


class TestExplainStatusCli:
    def test_explain_status_basic(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["explain-status", "task", "open"])
        assert result.exit_code == 0
        assert "open" in result.output

    def test_explain_status_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["explain-status", "task", "open", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "open"
        assert "category" in data
        assert "inbound_transitions" in data
        assert "outbound_transitions" in data

    def test_explain_status_unknown_type(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["explain-status", "nonexistent", "open"])
        assert result.exit_code == 1
        assert "Unknown type" in result.output

    def test_explain_status_unknown_status(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["explain-status", "task", "nonexistent"])
        assert result.exit_code == 1
        assert "Unknown status" in result.output


class TestLabelsCommand:
    def test_labels_command(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        runner.invoke(cli, ["create", "Issue A", "-l", "cluster:broad-except"])
        result = runner.invoke(cli, ["labels"])
        assert result.exit_code == 0
        assert "cluster" in result.output
        assert "broad-except" in result.output

    def test_labels_json_output(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["labels", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "items" in data

    def test_labels_json_renames_bare_namespace(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        runner.invoke(cli, ["create", "Issue A", "-l", "tech-debt"])
        result = runner.invoke(cli, ["labels", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        namespaces = {item["namespace"] for item in data["items"]}
        assert "unnamespaced" in namespaces
        assert "_bare" not in namespaces

    def test_labels_public_unnamespaced_filter(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        runner.invoke(cli, ["create", "Issue A", "-l", "tech-debt", "-l", "cluster:x"])
        result = runner.invoke(cli, ["labels", "--namespace", "unnamespaced", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert [item["namespace"] for item in data["items"]] == ["unnamespaced"]

    def test_labels_namespace_filter(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        runner.invoke(cli, ["create", "Issue A", "-l", "cluster:x", "-l", "effort:m"])
        result = runner.invoke(cli, ["labels", "--namespace", "cluster"])
        assert result.exit_code == 0
        assert "cluster" in result.output
        assert "effort" not in result.output


class TestTaxonomyCommand:
    def test_taxonomy_command(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["taxonomy"])
        assert result.exit_code == 0
        assert "auto" in result.output
        assert "virtual" in result.output

    def test_taxonomy_json_output(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["taxonomy", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "manual_suggested" in data
        assert data["bare_labels"]["reserved"]["patterns"] == ["P[0-4]", "priority:*"]
        assert data["bare_labels"]["reserved"]["writable"] is False
