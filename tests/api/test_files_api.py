"""Dashboard API tests — file records, scan results, and scan source filtering."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import AsyncClient

from filigree.registry import RegistryFileNotFoundError, RegistryUnavailableError, ResolvedFile
from tests.conftest import PopulatedDB

_OLD_TS = "2020-01-01T00:00:00+00:00"  # well past any clean-stale cutoff

_CLEAN_STALE_FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "contracts" / "loom" / "findings-clean-stale.json"


class TestFilesSchemaAPI:
    """GET /api/files/_schema — API discovery for file/scan features."""

    async def test_schema_returns_valid_severities(self, client: AsyncClient) -> None:
        resp = await client.get("/api/files/_schema")
        assert resp.status_code == 200
        data = resp.json()
        assert set(data["valid_severities"]) == {"critical", "high", "medium", "low", "info"}

    async def test_schema_returns_valid_finding_statuses(self, client: AsyncClient) -> None:
        resp = await client.get("/api/files/_schema")
        data = resp.json()
        assert "unseen_in_latest" in data["valid_finding_statuses"]

    async def test_schema_returns_valid_association_types(self, client: AsyncClient) -> None:
        resp = await client.get("/api/files/_schema")
        data = resp.json()
        assert "bug_in" in data["valid_association_types"]
        assert "scan_finding" in data["valid_association_types"]

    async def test_schema_returns_valid_sort_fields(self, client: AsyncClient) -> None:
        resp = await client.get("/api/files/_schema")
        data = resp.json()
        assert set(data["valid_file_sort_fields"]) == {"updated_at", "first_seen", "path", "language"}
        assert set(data["valid_finding_sort_fields"]) == {"updated_at", "severity"}

    async def test_schema_returns_endpoints_catalog(self, client: AsyncClient) -> None:
        resp = await client.get("/api/files/_schema")
        data = resp.json()
        assert isinstance(data["endpoints"], list)
        assert len(data["endpoints"]) >= 1
        ep = data["endpoints"][0]
        assert "method" in ep
        assert "path" in ep
        assert "description" in ep

    async def test_schema_has_cache_control(self, client: AsyncClient) -> None:
        resp = await client.get("/api/files/_schema")
        assert resp.headers.get("cache-control") == "max-age=3600"

    async def test_schema_returns_registry_backend_config_flags(self, client: AsyncClient) -> None:
        resp = await client.get("/api/files/_schema")
        data = resp.json()

        assert data["config_flags"] == {
            "registry_backend": "local",
            "registry_backend_features": ["local", "clarion"],
            "allow_local_fallback": False,
            # F-1: probe identity is unset under local-mode; the keys are still
            # emitted so the dashboard JS does not need a separate code path.
            "clarion_instance_id": None,
            "clarion_api_version": None,
            "clarion_instance_rotated": False,
        }

    async def test_schema_config_flags_reflect_project_backend(self, clarion_fallback_client: AsyncClient) -> None:
        resp = await clarion_fallback_client.get("/api/files/_schema")
        data = resp.json()

        assert data["config_flags"]["registry_backend"] == "clarion"
        assert data["config_flags"]["registry_backend_features"] == ["local", "clarion"]
        assert data["config_flags"]["allow_local_fallback"] is True


class TestScanResultsRegistryErrors:
    async def test_scan_results_returns_not_found_for_unknown_clarion_file(
        self,
        client: AsyncClient,
        dashboard_db: PopulatedDB,
    ) -> None:
        class MissingFileRegistry:
            def resolve_file(self, path: str, *, language: str = "", actor: str = "") -> ResolvedFile:
                raise RegistryFileNotFoundError(
                    "Clarion registry could not resolve file at http://clarion.test/api/v1/files?path=missing.py: HTTP 404 not indexed",
                    status_code=404,
                    url="http://clarion.test/api/v1/files?path=missing.py",
                )

            def is_displaced(self) -> bool:
                return True

        dashboard_db.db.registry = MissingFileRegistry()

        resp = await client.post(
            "/api/v1/scan-results",
            json={
                "scan_source": "codex",
                "findings": [{"path": "missing.py", "rule_id": "R1", "severity": "low", "message": "m"}],
            },
        )

        assert resp.status_code == 404
        body = resp.json()
        assert body["code"] == "NOT_FOUND"
        assert "HTTP 404 not indexed" in body["error"]

    @pytest.mark.parametrize("path", ["/api/v1/scan-results", "/api/loom/scan-results", "/api/scan-results"])
    async def test_scan_results_returns_registry_unavailable_code(
        self,
        client: AsyncClient,
        dashboard_db: PopulatedDB,
        path: str,
    ) -> None:
        class UnavailableRegistry:
            def resolve_file(self, path: str, *, language: str = "", actor: str = "") -> ResolvedFile:
                raise RegistryUnavailableError("Clarion registry unavailable for test")

            def is_displaced(self) -> bool:
                return True

        dashboard_db.db.registry = UnavailableRegistry()

        resp = await client.post(
            path,
            json={
                "scan_source": "codex",
                "findings": [{"path": "missing.py", "rule_id": "R1", "severity": "low", "message": "m"}],
            },
        )

        assert resp.status_code == 503
        body = resp.json()
        assert body["code"] == "REGISTRY_UNAVAILABLE"
        assert body["code"] != "IO"


class TestScanRunsAPI:
    """GET /api/scan-runs — scan run history."""

    async def test_empty_table(self, client: AsyncClient) -> None:
        resp = await client.get("/api/scan-runs")
        assert resp.status_code == 200
        assert resp.json() == {"scan_runs": []}

    async def test_single_scan_run(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        dashboard_db.db.process_scan_results(
            scan_source="codex",
            scan_run_id="run-001",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": "low", "message": "m"}],
        )
        resp = await client.get("/api/scan-runs")
        assert resp.status_code == 200
        runs = resp.json()["scan_runs"]
        assert len(runs) == 1
        assert runs[0]["scan_run_id"] == "run-001"
        assert runs[0]["scan_source"] == "codex"
        assert runs[0]["total_findings"] == 1
        assert runs[0]["files_scanned"] == 1

    async def test_multiple_runs_ordered_by_recent(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        dashboard_db.db.process_scan_results(
            scan_source="codex",
            scan_run_id="run-old",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": "low", "message": "m"}],
        )
        dashboard_db.db.process_scan_results(
            scan_source="claude",
            scan_run_id="run-new",
            findings=[{"path": "b.py", "rule_id": "R2", "severity": "high", "message": "m"}],
        )
        resp = await client.get("/api/scan-runs")
        runs = resp.json()["scan_runs"]
        assert len(runs) == 2
        # Most recent first
        assert runs[0]["scan_run_id"] == "run-new"
        assert runs[1]["scan_run_id"] == "run-old"

    async def test_limit_param(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        for i in range(5):
            dashboard_db.db.process_scan_results(
                scan_source="ruff",
                scan_run_id=f"run-{i:03d}",
                findings=[{"path": f"f{i}.py", "rule_id": "R1", "severity": "low", "message": "m"}],
            )
        resp = await client.get("/api/scan-runs?limit=2")
        runs = resp.json()["scan_runs"]
        assert len(runs) == 2

    async def test_empty_run_id_excluded(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        dashboard_db.db.process_scan_results(
            scan_source="ruff",
            scan_run_id="",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": "low", "message": "m"}],
        )
        resp = await client.get("/api/scan-runs")
        assert resp.json() == {"scan_runs": []}

    async def test_no_cache_header(self, client: AsyncClient) -> None:
        resp = await client.get("/api/scan-runs")
        assert resp.headers.get("cache-control") == "no-cache"

    async def test_schema_includes_scan_runs_endpoint(self, client: AsyncClient) -> None:
        resp = await client.get("/api/files/_schema")
        data = resp.json()
        paths = [ep["path"] for ep in data["endpoints"]]
        assert "/api/scan-runs" in paths


class TestUnknownScanRunIdContract:
    """POST scan-results with a client-supplied scan_run_id Filigree has never
    seen — the permanent "tolerate-unknown" intake contract (F6, contracts.md).

    Federation producers (Clarion `clarion analyze`) mint their own run_id and
    POST findings carrying it with NO prior create handshake. Filigree ingests
    the findings and reconstructs the run in GET /api/scan-runs from
    scan_findings.scan_run_id. This is a supported, permanent contract — not a
    transitional leniency — so it is pinned here at the HTTP intake boundary,
    not only at the core-method level (TestScanRunsAPI / TestGetScanRunsCore).

    If this class ever needs to change, the federation contract changed: update
    docs/federation/contracts.md §F6 and notify Loom consumers before merging.
    """

    async def test_unknown_scan_run_id_ingests_with_200(self, client: AsyncClient) -> None:
        """An unknown scan_run_id ingests successfully and is reconstructed in history."""
        resp = await client.post(
            "/api/v1/scan-results",
            json={
                "scan_source": "clarion",
                "scan_run_id": "clarion-run-never-seen-001",
                "findings": [{"path": "a.py", "rule_id": "C1", "severity": "high", "message": "m"}],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["findings_created"] == 1
        # The orphan run is reconstructed from scan_findings in history.
        runs = (await client.get("/api/scan-runs")).json()["scan_runs"]
        assert any(r["scan_run_id"] == "clarion-run-never-seen-001" for r in runs)

    async def test_unknown_run_completion_warning_is_benign(self, client: AsyncClient) -> None:
        """With complete_scan_run=True (default) an unknown run emits a benign
        completion warning in warnings[] — there is no scan_runs row to mark
        'completed'. Consumers MUST NOT treat populated warnings[] as failure;
        findings are still ingested (findings_created reflects the real work)."""
        resp = await client.post(
            "/api/v1/scan-results",
            json={
                "scan_source": "clarion",
                "scan_run_id": "clarion-run-warn-001",
                "findings": [{"path": "b.py", "rule_id": "C2", "severity": "high", "message": "m"}],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["findings_created"] == 1
        assert any("not updated to 'completed'" in w for w in body["warnings"])

    async def test_complete_scan_run_false_suppresses_completion_warning(self, client: AsyncClient) -> None:
        """complete_scan_run=False suppresses the completion attempt entirely,
        so enrich-only producers get a clean warnings[] for an unknown run."""
        resp = await client.post(
            "/api/v1/scan-results",
            json={
                "scan_source": "clarion",
                "scan_run_id": "clarion-run-noverify-001",
                "findings": [{"path": "c.py", "rule_id": "C3", "severity": "high", "message": "m"}],
                "complete_scan_run": False,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["findings_created"] == 1
        assert not any("not updated to 'completed'" in w for w in body["warnings"])


class TestFilesScanSourceFilterAPI:
    """GET /api/files?scan_source=... — filter files by scan source."""

    async def test_scan_source_filters_files(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        dashboard_db.db.process_scan_results(
            scan_source="codex",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": "low", "message": "m"}],
        )
        dashboard_db.db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "b.py", "rule_id": "R2", "severity": "low", "message": "m"}],
        )
        resp = await client.get("/api/files?scan_source=codex")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["results"][0]["path"] == "a.py"

    async def test_no_scan_source_returns_all(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        dashboard_db.db.process_scan_results(
            scan_source="codex",
            findings=[{"path": "a.py", "rule_id": "R1", "severity": "low", "message": "m"}],
        )
        dashboard_db.db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "b.py", "rule_id": "R2", "severity": "low", "message": "m"}],
        )
        resp = await client.get("/api/files")
        assert resp.status_code == 200
        assert resp.json()["total"] == 2


class TestErrorMessagesIncludeValidOptions:
    """Error messages must include valid values to be self-documenting."""

    async def test_unknown_type_lists_valid_types(self, client: AsyncClient) -> None:
        resp = await client.get("/api/type/bogus_type")
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "VALIDATION"
        assert '"bogus_type"' in body["error"]
        # Must include at least some known types
        for expected in ("task", "bug", "feature"):
            assert expected in body["error"], f"Missing valid type '{expected}' in error"

    async def test_create_issue_unknown_type_lists_valid_types(self, client: AsyncClient) -> None:
        resp = await client.post("/api/issues", json={"title": "Bad", "type": "widgets"})
        assert resp.status_code == 400
        body = resp.json()
        assert "widgets" in body["error"]
        assert "task" in body["error"]

    async def test_priority_error_includes_valid_range(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        ids = dashboard_db.ids
        resp = await client.patch(f"/api/issue/{ids['a']}", json={"priority": "high"})
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "VALIDATION"
        assert "0" in body["error"]
        assert "4" in body["error"]

    async def test_issue_not_found_includes_id(self, client: AsyncClient) -> None:
        resp = await client.get("/api/issue/nonexistent-id-xyz")
        assert resp.status_code == 404
        body = resp.json()
        assert body["code"] == "NOT_FOUND"
        assert "nonexistent-id-xyz" in body["error"]


class TestScanResultsFingerprintAPI:
    """Wardline-supplied fingerprint over the wire (Loom §3.B).

    Exercises the native-emitter contract path — POST /api/loom/scan-results —
    rather than only the in-process db call, so the body parser and dedup wire
    end-to-end. The ``client`` fixture uses a local registry, so file_id
    resolution works with no Clarion present (brief §3.C composability).
    """

    async def test_fingerprint_dedup_over_loom_endpoint(self, client: AsyncClient) -> None:
        body = {
            "scan_source": "wardline",
            "findings": [
                {"path": "src/a.py", "rule_id": "WLN-1", "message": "m", "severity": "high", "line_start": 10, "fingerprint": "fp-http"}
            ],
        }
        first = await client.post("/api/loom/scan-results", json=body)
        assert first.status_code == 200

        # Re-emit the "same" finding shifted down — same fingerprint, new line.
        body["findings"][0]["line_start"] = 40
        second = await client.post("/api/loom/scan-results", json=body)
        assert second.status_code == 200

        listing = await client.get("/api/loom/findings?scan_source=wardline")
        assert listing.status_code == 200
        items = listing.json()["items"]
        assert len(items) == 1
        assert items[0]["seen_count"] == 2
        assert items[0]["line_start"] == 40
        assert items[0]["fingerprint"] == "fp-http"

    async def test_non_string_fingerprint_rejected_over_endpoint(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/loom/scan-results",
            json={
                "scan_source": "wardline",
                "findings": [{"path": "src/a.py", "rule_id": "WLN-1", "message": "m", "fingerprint": ["not", "a", "string"]}],
            },
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"


class TestLoomCleanStaleFindingsAPI:
    """POST /api/loom/findings/clean-stale — federation retention surface.

    Thin loom HTTP adapter over the core ``clean_stale_findings`` (ADR-015).
    Soft retention: stale ``unseen_in_latest`` findings move to ``fixed``,
    scoped to a single ``scan_source``. Reuses the existing core method —
    these tests assert the wire contract and the scan_source-isolation /
    enrich-only invariants Clarion depends on.
    """

    def _status_by_rule(self, db: PopulatedDB, path: str) -> dict[str, str]:
        f = db.db.get_file_by_path(path)
        assert f is not None
        return {fi.rule_id: fi.status for fi in db.db.get_findings(f.id)}

    async def test_clean_stale_matrix(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        """One seeded matrix proving scoping + enrich-only in a single sweep.

        | finding                              | after clarion/30d sweep |
        | ------------------------------------ | ----------------------- |
        | clarion + unseen + old               | fixed (cleaned)         |
        | clarion + unseen + recent            | kept unseen_in_latest   |
        | clarion + open (still in latest)     | kept open (enrich-only) |
        | wardline + unseen + old              | kept (scan_source iso)  |
        """
        db = dashboard_db.db
        db.process_scan_results(
            scan_source="clarion",
            findings=[
                {"path": "clar_old.py", "rule_id": "C-OLD", "severity": "high", "message": "m"},
                {"path": "clar_recent.py", "rule_id": "C-RECENT", "severity": "high", "message": "m"},
                {"path": "clar_open.py", "rule_id": "C-OPEN", "severity": "high", "message": "m"},
            ],
        )
        db.process_scan_results(
            scan_source="wardline",
            findings=[{"path": "ward_old.py", "rule_id": "W-OLD", "severity": "high", "message": "m"}],
        )
        # Backdate the two "old" findings to unseen; recent one to unseen but
        # leave its (now) last_seen_at; the open one stays open.
        db.conn.execute(
            "UPDATE scan_findings SET status = 'unseen_in_latest', last_seen_at = ? WHERE rule_id IN ('C-OLD', 'W-OLD')",
            (_OLD_TS,),
        )
        db.conn.execute("UPDATE scan_findings SET status = 'unseen_in_latest' WHERE rule_id = 'C-RECENT'")
        db.conn.commit()

        resp = await client.post(
            "/api/loom/findings/clean-stale",
            json={"scan_source": "clarion", "older_than_days": 30, "actor": "clarion"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"findings_fixed": 1, "scan_source": "clarion", "older_than_days": 30}

        # Only the old clarion unseen finding was swept.
        assert self._status_by_rule(dashboard_db, "clar_old.py")["C-OLD"] == "fixed"
        # Recent clarion unseen finding untouched.
        assert self._status_by_rule(dashboard_db, "clar_recent.py")["C-RECENT"] == "unseen_in_latest"
        # Live (open) clarion finding untouched — enrich-only: still-present
        # findings keep their seen state.
        assert self._status_by_rule(dashboard_db, "clar_open.py")["C-OPEN"] == "open"
        # Wardline finding untouched — scan_source isolation: a clarion-scoped
        # sweep can never affect another tool's findings.
        assert self._status_by_rule(dashboard_db, "ward_old.py")["W-OLD"] == "unseen_in_latest"

    async def test_default_older_than_days_is_30(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        """``older_than_days`` is optional; omitting it defaults to 30 (REQ-FINDING-06)."""
        db = dashboard_db.db
        db.process_scan_results(
            scan_source="clarion",
            findings=[{"path": "a.py", "rule_id": "C1", "severity": "low", "message": "m"}],
        )
        db.conn.execute(
            "UPDATE scan_findings SET status = 'unseen_in_latest', last_seen_at = ? WHERE rule_id = 'C1'",
            (_OLD_TS,),
        )
        db.conn.commit()
        resp = await client.post("/api/loom/findings/clean-stale", json={"scan_source": "clarion"})
        assert resp.status_code == 200
        assert resp.json() == {"findings_fixed": 1, "scan_source": "clarion", "older_than_days": 30}

    async def test_coalesce_fallback_null_last_seen_at(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        """Age gate is coalesce(last_seen_at, updated_at): a NULL last_seen_at
        with an old updated_at is still swept (inherited from the core method)."""
        db = dashboard_db.db
        db.process_scan_results(
            scan_source="clarion",
            findings=[{"path": "a.py", "rule_id": "C1", "severity": "low", "message": "m"}],
        )
        db.conn.execute(
            "UPDATE scan_findings SET status = 'unseen_in_latest', last_seen_at = NULL, updated_at = ? WHERE rule_id = 'C1'",
            (_OLD_TS,),
        )
        db.conn.commit()
        resp = await client.post("/api/loom/findings/clean-stale", json={"scan_source": "clarion", "older_than_days": 30})
        assert resp.status_code == 200
        assert resp.json()["findings_fixed"] == 1
        assert self._status_by_rule(dashboard_db, "a.py")["C1"] == "fixed"

    async def test_older_than_days_zero_sweeps_whole_unseen_backlog(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        """older_than_days=0 is permitted: cutoff = now, so the entire current
        unseen backlog for the source is swept. Bounded blast radius — only
        already-unseen rows, and open (live) findings are still untouched."""
        db = dashboard_db.db
        db.process_scan_results(
            scan_source="clarion",
            findings=[
                {"path": "u.py", "rule_id": "C-UNSEEN", "severity": "low", "message": "m"},
                {"path": "o.py", "rule_id": "C-OPEN", "severity": "low", "message": "m"},
            ],
        )
        # Mark one unseen with a *recent* last_seen_at (would survive a 30-day window).
        db.conn.execute("UPDATE scan_findings SET status = 'unseen_in_latest' WHERE rule_id = 'C-UNSEEN'")
        db.conn.commit()
        resp = await client.post("/api/loom/findings/clean-stale", json={"scan_source": "clarion", "older_than_days": 0})
        assert resp.status_code == 200
        assert resp.json()["findings_fixed"] == 1
        assert self._status_by_rule(dashboard_db, "u.py")["C-UNSEEN"] == "fixed"
        # Live finding untouched even at days=0 — only unseen rows are eligible.
        assert self._status_by_rule(dashboard_db, "o.py")["C-OPEN"] == "open"

    async def test_dismisses_linked_observations_for_swept_findings(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        """The route inherits the core method's observation-dismissal cascade:
        sweeping a finding dismisses the observation linked to it."""
        db = dashboard_db.db
        db.process_scan_results(
            scan_source="clarion",
            findings=[{"path": "a.py", "rule_id": "C1", "severity": "low", "message": "m"}],
            create_observations=True,
        )
        f = db.get_file_by_path("a.py")
        assert f is not None
        finding_id = db.get_findings(f.id)[0].id
        obs_before = db.conn.execute("SELECT COUNT(*) AS c FROM observations WHERE source_finding_id = ?", (finding_id,)).fetchone()["c"]
        assert obs_before == 1
        db.conn.execute(
            "UPDATE scan_findings SET status = 'unseen_in_latest', last_seen_at = ? WHERE id = ?",
            (_OLD_TS, finding_id),
        )
        db.conn.commit()

        resp = await client.post(
            "/api/loom/findings/clean-stale",
            json={"scan_source": "clarion", "older_than_days": 30, "actor": "clarion"},
        )
        assert resp.status_code == 200
        assert resp.json()["findings_fixed"] == 1
        obs_after = db.conn.execute("SELECT COUNT(*) AS c FROM observations WHERE source_finding_id = ?", (finding_id,)).fetchone()["c"]
        assert obs_after == 0  # observation cascade-dismissed

    async def test_missing_scan_source_rejected(self, client: AsyncClient) -> None:
        """scan_source is mandatory on the HTTP surface (accident-guard): the
        core method's None='all sources' mode is deliberately not reachable."""
        resp = await client.post("/api/loom/findings/clean-stale", json={"older_than_days": 30})
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    async def test_empty_scan_source_rejected(self, client: AsyncClient) -> None:
        resp = await client.post("/api/loom/findings/clean-stale", json={"scan_source": "", "older_than_days": 30})
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    @pytest.mark.parametrize("bad_days", [-1, True, "30", 1.5])
    async def test_invalid_older_than_days_rejected(self, client: AsyncClient, bad_days: object) -> None:
        resp = await client.post(
            "/api/loom/findings/clean-stale",
            json={"scan_source": "clarion", "older_than_days": bad_days},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "VALIDATION"

    async def test_fixture_examples_match_live_shape(self, client: AsyncClient, dashboard_db: PopulatedDB) -> None:
        """Pin the published contract fixture against live responses (key set +
        value types), the shape-reference discipline from contracts.md."""
        fixture = json.loads(_CLEAN_STALE_FIXTURE.read_text())
        assert fixture["_meta"]["endpoint"] == "POST /api/loom/findings/clean-stale"

        for example in fixture["examples"]:
            req = example["request"]
            expected = example["response"]
            resp = await client.post(req["path"], json=req["body"])
            assert resp.status_code == expected["status"], example["name"]
            body = resp.json()
            exp_body = expected["body"]
            assert set(body.keys()) == set(exp_body.keys()), example["name"]
            for key, val in exp_body.items():
                assert type(body[key]) is type(val), f"{example['name']}:{key}"
