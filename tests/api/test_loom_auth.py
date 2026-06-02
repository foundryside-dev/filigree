"""Opt-in bearer-token auth for the loom federation surface.

Design: docs/superpowers/specs/2026-06-03-loom-bearer-token-auth-design.md
Issue: filigree-30cd35bcb9 (option b — loom routes honour the bearer token).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import filigree.dashboard as dash_module
from filigree.dashboard import create_app
from filigree.dashboard_auth import _token_matches, is_loom_scoped_path
from tests.conftest import PopulatedDB

TOKEN = "s3cret-federation-token"  # noqa: S105 — test fixture


class TestIsLoomScopedPath:
    """The pure path predicate that decides which routes auth gates."""

    @pytest.mark.parametrize(
        "path",
        [
            "/api/loom/issues",
            "/api/loom/scan-results",
            "/api/loom/findings/clean-stale",
            "/api/loom/issues/filigree-abc123/close",
            "/api/p/acme/loom/issues",  # server-mode project mount
            "/api/scan-results",  # living-surface federation alias
            "/api/p/acme/scan-results",  # alias under server-mode mount
        ],
    )
    def test_loom_scoped_paths_are_true(self, path: str) -> None:
        assert is_loom_scoped_path(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "/api/issue/filigree-abc123",  # classic singular
            "/api/issues",  # classic list
            "/api/ready",
            "/api/health",
            "/",
            "/api/v1/scan-results",  # classic outlier — NOT loom-scoped
            "/api/p/acme/issue/x",  # classic under server-mode mount
            "/api",  # no trailing segment
            "/api/loomish/x",  # must not prefix-match "loom" loosely
        ],
    )
    def test_non_loom_paths_are_false(self, path: str) -> None:
        assert is_loom_scoped_path(path) is False


class TestTokenMatches:
    """Constant-time token comparison, robust to non-ASCII input."""

    def test_equal_tokens_match(self) -> None:
        assert _token_matches("abc123", "abc123") is True

    def test_unequal_tokens_do_not_match(self) -> None:
        assert _token_matches("abc123", "different") is False

    def test_non_ascii_provided_token_does_not_raise(self) -> None:
        """A latin-1 header byte decodes to a non-ASCII str server-side;
        the comparison must return False, not raise (would 500 otherwise)."""
        assert _token_matches("café-\xe9", "abc123") is False


@pytest.fixture
def app_factory(dashboard_db: PopulatedDB, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str | None], FastAPI]]:
    """Build a dashboard app with FILIGREE_API_TOKEN set (or unset).

    ``token=None`` clears the env var (today's no-auth behaviour); a string
    sets it before ``create_app`` reads it. Resets the module-global DB on
    teardown so apps built here do not leak into other tests.
    """

    def _make(token: str | None) -> FastAPI:
        if token is None:
            monkeypatch.delenv("FILIGREE_API_TOKEN", raising=False)
        else:
            monkeypatch.setenv("FILIGREE_API_TOKEN", token)
        dash_module._db = dashboard_db.db
        return create_app()

    yield _make
    dash_module._db = None


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class TestLoomAuthEnforcement:
    async def test_loom_route_open_when_token_unset(self, app_factory: Callable[[str | None], FastAPI]) -> None:
        """Back-compat: with no token configured, loom routes need no auth."""
        app = app_factory(None)
        async with _client(app) as c:
            resp = await c.get("/api/loom/issues")
        assert resp.status_code == 200

    async def test_loom_route_correct_token(self, app_factory: Callable[[str | None], FastAPI]) -> None:
        app = app_factory(TOKEN)
        async with _client(app) as c:
            resp = await c.get("/api/loom/issues", headers={"Authorization": f"Bearer {TOKEN}"})
        assert resp.status_code == 200

    async def test_loom_route_absent_header_rejected(self, app_factory: Callable[[str | None], FastAPI]) -> None:
        app = app_factory(TOKEN)
        async with _client(app) as c:
            resp = await c.get("/api/loom/issues")
        assert resp.status_code == 401
        assert resp.json()["code"] == "PERMISSION"
        assert resp.headers.get("WWW-Authenticate") == "Bearer"

    async def test_loom_route_wrong_token_rejected(self, app_factory: Callable[[str | None], FastAPI]) -> None:
        app = app_factory(TOKEN)
        async with _client(app) as c:
            resp = await c.get("/api/loom/issues", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401

    async def test_loom_route_malformed_header_rejected(self, app_factory: Callable[[str | None], FastAPI]) -> None:
        """A token without the ``Bearer`` scheme is not honoured."""
        app = app_factory(TOKEN)
        async with _client(app) as c:
            resp = await c.get("/api/loom/issues", headers={"Authorization": TOKEN})
        assert resp.status_code == 401

    async def test_living_alias_scan_results_enforced(self, app_factory: Callable[[str | None], FastAPI]) -> None:
        """The living federation alias POST /api/scan-results is gated."""
        app = app_factory(TOKEN)
        async with _client(app) as c:
            resp = await c.post("/api/scan-results", json={})
        assert resp.status_code == 401


class TestLoomAuthScopeBoundary:
    async def test_classic_route_open_when_token_set(self, app_factory: Callable[[str | None], FastAPI], dashboard_db: PopulatedDB) -> None:
        """Classic surface stays open even with a token configured."""
        app = app_factory(TOKEN)
        issue_id = dashboard_db.ids["a"]
        async with _client(app) as c:
            resp = await c.get(f"/api/issue/{issue_id}")
        assert resp.status_code == 200

    async def test_health_open_when_token_set(self, app_factory: Callable[[str | None], FastAPI]) -> None:
        app = app_factory(TOKEN)
        async with _client(app) as c:
            resp = await c.get("/api/health")
        assert resp.status_code == 200

    async def test_root_open_when_token_set(self, app_factory: Callable[[str | None], FastAPI]) -> None:
        app = app_factory(TOKEN)
        async with _client(app) as c:
            resp = await c.get("/")
        assert resp.status_code == 200

    async def test_options_preflight_not_blocked(self, app_factory: Callable[[str | None], FastAPI]) -> None:
        """CORS preflight (OPTIONS) carries no auth and must pass through."""
        app = app_factory(TOKEN)
        async with _client(app) as c:
            resp = await c.options(
                "/api/loom/issues",
                headers={
                    "Origin": "http://localhost:8377",
                    "Access-Control-Request-Method": "GET",
                },
            )
        assert resp.status_code != 401
