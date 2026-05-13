"""Dashboard API tests — release endpoints."""

from __future__ import annotations

import json

from httpx import AsyncClient

from filigree.core import FiligreeDB


class TestGetReleasesEndpoint:
    """GET /api/releases — list releases with progress rollups."""

    async def test_returns_200_with_releases_key(self, release_client: AsyncClient, release_dashboard_db: FiligreeDB) -> None:
        resp = await release_client.get("/api/releases")
        assert resp.status_code == 200
        assert "releases" in resp.json()

    async def test_excludes_done_releases_by_default(self, release_client: AsyncClient, release_dashboard_db: FiligreeDB) -> None:
        db = release_dashboard_db
        db.create_issue("Active Release", type="release")
        r2 = db.create_issue("Done Release", type="release")
        # release type only allows planning → cancelled directly; pass status
        # explicitly because validate-transitions rejects planning → released.
        db.close_issue(r2.id, status="cancelled")

        resp = await release_client.get("/api/releases")
        data = resp.json()
        assert len(data["releases"]) == 2  # Active Release + auto-seeded Future (Done excluded)

    async def test_include_released_shows_all(self, release_client: AsyncClient, release_dashboard_db: FiligreeDB) -> None:
        db = release_dashboard_db
        db.create_issue("Active Release", type="release")
        r2 = db.create_issue("Done Release", type="release")
        # release type only allows planning → cancelled directly; pass status
        # explicitly because validate-transitions rejects planning → released.
        db.close_issue(r2.id, status="cancelled")

        resp = await release_client.get("/api/releases?include_released=true")
        data = resp.json()
        assert len(data["releases"]) == 3  # Active + Done + auto-seeded Future

    async def test_include_released_false_is_default(self, release_client: AsyncClient, release_dashboard_db: FiligreeDB) -> None:
        db = release_dashboard_db
        db.create_issue("Active Release", type="release")
        r2 = db.create_issue("Done Release", type="release")
        # release type only allows planning → cancelled directly; pass status
        # explicitly because validate-transitions rejects planning → released.
        db.close_issue(r2.id, status="cancelled")

        resp_default = await release_client.get("/api/releases")
        resp_explicit = await release_client.get("/api/releases?include_released=false")
        assert len(resp_default.json()["releases"]) == len(resp_explicit.json()["releases"])

    async def test_invalid_include_released_returns_400(self, release_client: AsyncClient, release_dashboard_db: FiligreeDB) -> None:
        resp = await release_client.get("/api/releases?include_released=maybe")
        assert resp.status_code == 400

    async def test_response_shape(self, release_client: AsyncClient, release_dashboard_db: FiligreeDB) -> None:
        db = release_dashboard_db
        release = db.create_issue("R1", type="release")
        db.create_issue("T1", type="task", parent_id=release.id)

        resp = await release_client.get("/api/releases")
        data = resp.json()
        assert len(data["releases"]) == 2  # R1 + auto-seeded Future
        entry = next(r for r in data["releases"] if r["id"] == release.id)
        for key in ("id", "title", "status", "progress", "child_summary", "blocks", "blocked_by"):
            assert key in entry, f"Missing key: {key}"

    async def test_progress_shape(self, release_client: AsyncClient, release_dashboard_db: FiligreeDB) -> None:
        db = release_dashboard_db
        release = db.create_issue("R1", type="release")
        db.create_issue("T1", type="task", parent_id=release.id)

        resp = await release_client.get("/api/releases")
        progress = resp.json()["releases"][0]["progress"]
        for key in ("total", "completed", "in_progress", "open", "pct"):
            assert key in progress, f"Missing progress key: {key}"
            assert isinstance(progress[key], int)

    async def test_blocks_are_id_title_objects(self, release_client: AsyncClient, release_dashboard_db: FiligreeDB) -> None:
        db = release_dashboard_db
        r1 = db.create_issue("Blocker", type="release")
        r2 = db.create_issue("Blocked", type="release")
        # r2 depends on r1 -> r1 blocks r2
        db.add_dependency(r2.id, r1.id)

        resp = await release_client.get("/api/releases")
        releases = resp.json()["releases"]
        entry_r1 = next(r for r in releases if r["id"] == r1.id)
        assert len(entry_r1["blocks"]) == 1
        assert "id" in entry_r1["blocks"][0]
        assert "title" in entry_r1["blocks"][0]

    async def test_empty_releases(self, release_client: AsyncClient, release_dashboard_db: FiligreeDB) -> None:
        """With no manually created releases, only the auto-seeded Future release exists."""
        resp = await release_client.get("/api/releases")
        releases = resp.json()["releases"]
        assert len(releases) == 1
        assert releases[0]["title"] == "Future"

    async def test_sort_order_by_semver(self, release_client: AsyncClient, release_dashboard_db: FiligreeDB) -> None:
        db = release_dashboard_db
        r2 = db.create_issue("v2.0.0", type="release", fields={"version": "v2.0.0"})
        r1 = db.create_issue("v1.0.0", type="release", fields={"version": "v1.0.0"})
        r15 = db.create_issue("v1.5.0", type="release", fields={"version": "v1.5.0"})

        resp = await release_client.get("/api/releases")
        releases = resp.json()["releases"]
        ids = [r["id"] for r in releases]
        # Semver ascending, auto-seeded Future release always sorts last
        assert len(releases) == 4
        assert ids[:3] == [r1.id, r15.id, r2.id]
        assert releases[-1]["title"] == "Future"

    async def test_sort_order_future_always_last(self, release_client: AsyncClient, release_dashboard_db: FiligreeDB) -> None:
        db = release_dashboard_db
        # Auto-seeded Future release already exists; create another + a versioned release
        db.create_issue("Future", type="release")
        r1 = db.create_issue("v1.0.0", type="release", fields={"version": "v1.0.0"})

        resp = await release_client.get("/api/releases")
        releases = resp.json()["releases"]
        # v1.0.0 sorts first; both Future releases (auto-seeded + manual) sort after it
        assert len(releases) == 3
        assert releases[0]["id"] == r1.id
        assert all(r["title"] == "Future" for r in releases[1:])

    async def test_sort_order_version_from_title_fallback(self, release_client: AsyncClient, release_dashboard_db: FiligreeDB) -> None:
        db = release_dashboard_db
        # No version field — should parse from title
        r2 = db.create_issue("v2.0.0 — Big Release", type="release")
        r1 = db.create_issue("v1.0.0 — First", type="release")

        resp = await release_client.get("/api/releases")
        releases = resp.json()["releases"]
        ids = [r["id"] for r in releases]
        assert ids.index(r1.id) < ids.index(r2.id)


class TestGetReleaseTreeEndpoint:
    """GET /api/release/{release_id}/tree — release hierarchy with progress."""

    async def test_returns_200_with_tree(self, release_client: AsyncClient, release_dashboard_db: FiligreeDB) -> None:
        db = release_dashboard_db
        release = db.create_issue("R1", type="release")

        resp = await release_client.get(f"/api/release/{release.id}/tree")
        assert resp.status_code == 200
        data = resp.json()
        assert "release" in data
        assert "children" in data

    async def test_nonexistent_id_returns_404(self, release_client: AsyncClient, release_dashboard_db: FiligreeDB) -> None:
        resp = await release_client.get("/api/release/nonexistent-abc123/tree")
        assert resp.status_code == 404
        assert resp.json()["code"] == "NOT_FOUND"

    async def test_non_release_type_returns_404(self, release_client: AsyncClient, release_dashboard_db: FiligreeDB) -> None:
        db = release_dashboard_db
        epic = db.create_issue("E1", type="epic")

        resp = await release_client.get(f"/api/release/{epic.id}/tree")
        assert resp.status_code == 404
        # Code matches status — asking for the release tree of an
        # id-that-exists-but-is-not-a-release is a "release not found at this
        # id" from the caller's perspective.
        assert resp.json()["code"] == "NOT_FOUND"

    async def test_tree_structure_shape(self, release_client: AsyncClient, release_dashboard_db: FiligreeDB) -> None:
        db = release_dashboard_db
        release = db.create_issue("R1", type="release")
        epic = db.create_issue("E1", type="epic", parent_id=release.id)
        db.create_issue("T1", type="task", parent_id=epic.id)

        resp = await release_client.get(f"/api/release/{release.id}/tree")
        data = resp.json()
        assert len(data["children"]) == 1
        child = data["children"][0]
        assert "issue" in child
        assert "progress" in child
        assert "children" in child

    async def test_leaf_has_null_progress(self, release_client: AsyncClient, release_dashboard_db: FiligreeDB) -> None:
        db = release_dashboard_db
        release = db.create_issue("R1", type="release")
        db.create_issue("T1", type="task", parent_id=release.id)

        resp = await release_client.get(f"/api/release/{release.id}/tree")
        data = resp.json()
        child = data["children"][0]
        assert child["progress"] is None

    async def test_non_leaf_has_progress_dict(self, release_client: AsyncClient, release_dashboard_db: FiligreeDB) -> None:
        db = release_dashboard_db
        release = db.create_issue("R1", type="release")
        epic = db.create_issue("E1", type="epic", parent_id=release.id)
        db.create_issue("T1", type="task", parent_id=epic.id)

        resp = await release_client.get(f"/api/release/{release.id}/tree")
        data = resp.json()
        epic_child = data["children"][0]
        assert epic_child["progress"] is not None
        for key in ("total", "completed", "pct"):
            assert key in epic_child["progress"]

    async def test_empty_release_returns_empty_children(self, release_client: AsyncClient, release_dashboard_db: FiligreeDB) -> None:
        db = release_dashboard_db
        release = db.create_issue("R1", type="release")

        resp = await release_client.get(f"/api/release/{release.id}/tree")
        data = resp.json()
        assert data["children"] == []

    async def test_release_with_only_direct_tasks(self, release_client: AsyncClient, release_dashboard_db: FiligreeDB) -> None:
        db = release_dashboard_db
        release = db.create_issue("R1", type="release")
        db.create_issue("T1", type="task", parent_id=release.id)
        db.create_issue("T2", type="task", parent_id=release.id)
        db.create_issue("T3", type="task", parent_id=release.id)

        resp = await release_client.get(f"/api/release/{release.id}/tree")
        data = resp.json()
        assert len(data["children"]) == 3
        for child in data["children"]:
            assert child["progress"] is None


class TestReleasesRobustnessAgainstCorruptData:
    """Regressions for the release-API hardening cluster.

    The ingest path (``import_jsonl``) can persist non-string version values
    and invalid priorities (no post-deserialisation validation). The release
    endpoints must degrade gracefully rather than crash or misreport.
    """

    async def test_non_string_version_does_not_crash_list(self, release_client: AsyncClient, release_dashboard_db: FiligreeDB) -> None:
        """Regression: filigree-b592adfe89. /api/releases must not raise
        TypeError when a release row has a non-string ``version`` field.
        """
        db = release_dashboard_db
        r = db.create_issue("R1", type="release", fields={"version": "v1.0.0"})
        # Simulate imported corruption: non-string version stored verbatim
        db.conn.execute(
            "UPDATE issues SET fields = ? WHERE id = ?",
            (json.dumps({"version": 123}), r.id),
        )
        db.conn.commit()

        resp = await release_client.get("/api/releases")
        # Before fix: uncaught TypeError propagates → 500 from FastAPI default handler,
        # but without our structured error code. After fix: 200 (corrupt release sorts
        # as non-semver) OR 500 with structured RELEASES_LOAD_ERROR.
        assert resp.status_code == 200
        releases = resp.json()["releases"]
        # The corrupt release should appear, just without ordering preference
        assert any(entry["id"] == r.id for entry in releases)

    async def test_overlong_semver_digits_do_not_crash_list(
        self, release_client: AsyncClient, release_dashboard_db: FiligreeDB
    ) -> None:
        db = release_dashboard_db
        r = db.create_issue("Huge Semver", type="release", fields={"version": "v1.0.0"})
        overlong_version = "v" + ("9" * 5000) + ".0.0"
        db.conn.execute(
            "UPDATE issues SET fields = ? WHERE id = ?",
            (json.dumps({"version": overlong_version}), r.id),
        )
        db.conn.commit()

        resp = await release_client.get("/api/releases")

        assert resp.status_code == 200
        releases = resp.json()["releases"]
        assert any(entry["id"] == r.id for entry in releases)

    async def test_unrelated_valueerror_returns_500_not_false_not_a_release(
        self, release_client: AsyncClient, release_dashboard_db: FiligreeDB
    ) -> None:
        """Regression: filigree-27ed472d6c. ``get_release_tree`` can raise
        ValueError from two distinct causes: the intended "issue exists but
        is not a release" path, AND any future data-invariant failure
        (e.g. ``Issue.__post_init__`` rejecting corrupt imported rows).
        The route must only map the former to 404 NOT_A_RELEASE; unrelated
        ValueErrors reach the ``except Exception`` branch and surface as
        500 INTERNAL (bug/data-corruption) rather than IO (transient), so
        clients don't retry what can only be fixed by inspection.
        """
        from unittest.mock import patch

        db = release_dashboard_db
        r = db.create_issue("R1", type="release")
        with patch.object(
            db,
            "get_release_tree",
            side_effect=ValueError("unrelated data corruption"),
        ):
            resp = await release_client.get(f"/api/release/{r.id}/tree")
        assert resp.status_code == 500
        body = resp.json()
        assert body["code"] == "INTERNAL"

    async def test_high_semver_sorts_before_non_semver(self, release_client: AsyncClient, release_dashboard_db: FiligreeDB) -> None:
        """Regression: filigree-2fc4203a63. Version ``v999999.0.1`` is a
        valid semver and must sort before any non-semver release; the
        previous 3-tuple sentinel ``(999_999, 0, 0)`` collided with
        high-numbered semver tuples.
        """
        db = release_dashboard_db
        # A valid semver with an extreme major number
        r_hi = db.create_issue("High Semver", type="release", fields={"version": "v999999.0.1"})
        # A release whose title has no semver-like substring → non-semver bucket
        r_plain = db.create_issue("Plain Title Release", type="release")

        resp = await release_client.get("/api/releases")
        releases = resp.json()["releases"]
        ids = [entry["id"] for entry in releases]
        assert ids.index(r_hi.id) < ids.index(r_plain.id), (
            f"High semver must sort before non-semver; got titles {[entry['title'] for entry in releases]}"
        )

    async def test_exact_collision_value_still_sorts_as_semver(self, release_client: AsyncClient, release_dashboard_db: FiligreeDB) -> None:
        """Regression: filigree-2fc4203a63 edge case. ``v999999.0.0``
        matched the previous ``_NON_SEMVER_KEY`` exactly — after fix it
        must still be treated as semver and ordered before non-semver.
        """
        db = release_dashboard_db
        r_collide = db.create_issue("Collision Semver", type="release", fields={"version": "v999999.0.0"})
        r_plain = db.create_issue("Plain Title Release", type="release")

        resp = await release_client.get("/api/releases")
        releases = resp.json()["releases"]
        ids = [entry["id"] for entry in releases]
        assert ids.index(r_collide.id) < ids.index(r_plain.id)
