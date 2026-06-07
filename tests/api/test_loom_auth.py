"""Opt-in bearer-token auth for the loom federation surface.

Design: docs/superpowers/specs/2026-06-03-loom-bearer-token-auth-design.md
Issue: filigree-30cd35bcb9 (option b — loom routes honour the bearer token).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import filigree.dashboard as dash_module
from filigree.dashboard import ProjectStore, create_app
from filigree.dashboard_auth import _token_matches, is_loom_scoped_path
from tests.conftest import PopulatedDB

TOKEN = "s3cret-federation-token"  # noqa: S105 — test fixture


class TestIsLoomScopedPath:
    """The pure path predicate that decides which routes auth gates."""

    @pytest.mark.parametrize(
        "path",
        [
            "/api/weft/issues",
            "/api/weft/scan-results",
            "/api/weft/findings/clean-stale",
            "/api/weft/issues/filigree-abc123/close",
            "/api/p/acme/weft/issues",  # server-mode project mount
            "/api/scan-results",  # living-surface federation alias
            "/api/p/acme/scan-results",  # alias under server-mode mount
            "/api/observations",  # living-surface observation ingest alias
            "/api/p/acme/observations",  # observation alias under server-mode mount
            "/api/v1/scan-results",  # classic scanner callback alias
            "/api/v1/observations",  # classic observation-write alias (B1)
            "/api/p/acme/v1/observations",  # classic observation alias under server-mode mount
            "/mcp",  # dashboard-mounted MCP streamable HTTP endpoint
            "/mcp/session",  # MCP subpaths inherit the same bearer boundary
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
            "/api/p/acme/issue/x",  # classic under server-mode mount
            "/api",  # no trailing segment
            "/api/weftish/x",  # must not prefix-match "weft" loosely
        ],
    )
    def test_non_loom_paths_are_false(self, path: str) -> None:
        assert is_loom_scoped_path(path) is False

    def test_every_living_surface_route_is_loom_scoped(self) -> None:
        """Drift guard: the LIVING_FEDERATION_ALIASES allowlist must cover every
        route on the living-surface router. Without this, the next off-/api/weft/
        federation alias added to ``create_living_surface_router`` would ship
        UNAUTHENTICATED when a token is set — and the hardcoded-path tests above
        would not catch it. Derives the expectation from the router itself.
        """
        from fastapi.routing import APIRoute

        from filigree.dashboard_routes import analytics, files

        routes = [
            r
            for router in (files.create_living_surface_router(), analytics.create_living_surface_router())
            for r in router.routes
            if isinstance(r, APIRoute)
        ]
        assert len(routes) >= 2  # an empty/missing router must not pass vacuously
        for route in routes:
            # The living-surface router mounts under /api (and /api/p/{key} in
            # server mode) — both forms must be gated.
            assert is_loom_scoped_path(f"/api{route.path}") is True, route.path
            assert is_loom_scoped_path(f"/api/p/acme{route.path}") is True, route.path

    def test_classic_v1_observations_is_loom_scoped(self) -> None:
        """Regression (B1): the classic observation-write alias must be gated.

        ``/api/v1/observations`` dispatches the same ``_create_observation_handler``
        as the gated ``/api/observations`` and ``/api/weft/observations``; it was
        the lone ungated sibling because it sits on the *classic* router, which
        the living-only drift guard above never iterated.
        """
        assert is_loom_scoped_path("/api/v1/observations") is True
        assert is_loom_scoped_path("/api/p/acme/v1/observations") is True

    def test_every_federation_write_alias_is_loom_scoped(self) -> None:
        """Cross-generation drift guard — the one the v1/observations hole needed.

        A federation-write op (observation / scan-results ingest) is exposed
        across generations: weft (``/api/weft/...``), living (``/api/...``), and a
        versioned classic alias (``/api/v1/...``). Every alias of such an op must
        be gated; a divergence (one gated, a sibling open) is exactly the
        ``/api/v1/observations`` defect. The existing living-only guard missed it
        because the hole was on the classic router — so this iterates the classic,
        living, AND weft routers.

        Soundness: the federation-write op set is derived from
        ``LIVING_FEDERATION_ALIASES``. This is complete only because every classic
        federation-write alias is a version-prefixed variant of an op that *also*
        has a living-surface route (so its bare name is forced into
        ``LIVING_FEDERATION_ALIASES`` by the living guard above). A brand-new
        federation op with NO living-surface route would escape both this guard
        and the gate — add it to the alias sets explicitly.
        """
        from fastapi.routing import APIRoute

        from filigree.dashboard_auth import LIVING_FEDERATION_ALIASES
        from filigree.dashboard_routes import analytics, files

        ungenerationed = [
            analytics.create_classic_router(),
            files.create_classic_router(),
            analytics.create_living_surface_router(),
            files.create_living_surface_router(),
        ]
        weft = [analytics.create_weft_router(), files.create_weft_router()]

        checked = 0
        for router in ungenerationed:
            for r in router.routes:
                if isinstance(r, APIRoute) and r.path.rsplit("/", 1)[-1] in LIVING_FEDERATION_ALIASES:
                    full = f"/api{r.path}"
                    assert is_loom_scoped_path(full) is True, full
                    # server-mode mount /api/p/{key}/... must gate identically
                    assert is_loom_scoped_path(full.replace("/api/", "/api/p/acme/", 1)) is True, full
                    checked += 1
        for router in weft:
            for r in router.routes:
                if isinstance(r, APIRoute) and r.path.rsplit("/", 1)[-1] in LIVING_FEDERATION_ALIASES:
                    assert is_loom_scoped_path(f"/api/weft{r.path}") is True, r.path
                    checked += 1
        # weft + living + classic aliases for both ops — must not pass vacuously.
        assert checked >= 5


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
        monkeypatch.delenv("FILIGREE_FEDERATION_API_TOKEN", raising=False)
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
            resp = await c.get("/api/weft/issues")
        assert resp.status_code == 200

    async def test_loom_route_correct_token(self, app_factory: Callable[[str | None], FastAPI]) -> None:
        app = app_factory(TOKEN)
        async with _client(app) as c:
            resp = await c.get("/api/weft/issues", headers={"Authorization": f"Bearer {TOKEN}"})
        assert resp.status_code == 200

    async def test_mcp_endpoint_not_mounted_when_token_unset(self, app_factory: Callable[[str | None], FastAPI]) -> None:
        """MCP HTTP is high privilege and must not be exposed without auth."""
        app = app_factory(None)
        async with _client(app) as c:
            resp = await c.post("/mcp", json={})
        assert resp.status_code == 404

    async def test_loom_route_absent_header_rejected(self, app_factory: Callable[[str | None], FastAPI]) -> None:
        app = app_factory(TOKEN)
        async with _client(app) as c:
            resp = await c.get("/api/weft/issues")
        assert resp.status_code == 401
        assert resp.json()["code"] == "PERMISSION"
        assert resp.headers.get("WWW-Authenticate") == "Bearer"

    async def test_loom_route_wrong_token_rejected(self, app_factory: Callable[[str | None], FastAPI]) -> None:
        app = app_factory(TOKEN)
        async with _client(app) as c:
            resp = await c.get("/api/weft/issues", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401

    async def test_loom_route_malformed_header_rejected(self, app_factory: Callable[[str | None], FastAPI]) -> None:
        """A token without the ``Bearer`` scheme is not honoured."""
        app = app_factory(TOKEN)
        async with _client(app) as c:
            resp = await c.get("/api/weft/issues", headers={"Authorization": TOKEN})
        assert resp.status_code == 401

    async def test_living_alias_scan_results_enforced(self, app_factory: Callable[[str | None], FastAPI]) -> None:
        """The living federation alias POST /api/scan-results is gated."""
        app = app_factory(TOKEN)
        async with _client(app) as c:
            resp = await c.post("/api/scan-results", json={})
        assert resp.status_code == 401

    async def test_living_alias_observations_enforced(self, app_factory: Callable[[str | None], FastAPI]) -> None:
        """The living federation alias POST /api/observations is gated."""
        app = app_factory(TOKEN)
        async with _client(app) as c:
            resp = await c.post("/api/observations", json={})
        assert resp.status_code == 401

    async def test_living_alias_observations_correct_token(self, app_factory: Callable[[str | None], FastAPI]) -> None:
        app = app_factory(TOKEN)
        async with _client(app) as c:
            resp = await c.post(
                "/api/observations",
                headers={"Authorization": f"Bearer {TOKEN}"},
                json={"summary": "auth-protected living observation"},
            )
        assert resp.status_code == 201

    async def test_classic_v1_scan_results_enforced(self, app_factory: Callable[[str | None], FastAPI]) -> None:
        """The legacy scanner callback alias must share scan-ingest auth."""
        app = app_factory(TOKEN)
        async with _client(app) as c:
            resp = await c.post("/api/v1/scan-results", json={})
        assert resp.status_code == 401

    async def test_classic_v1_observations_enforced(self, app_factory: Callable[[str | None], FastAPI]) -> None:
        """B1: the classic observation-write alias must require the bearer token,
        like its living (/api/observations) and weft (/api/weft/observations)
        siblings — 401 without the token, 201 (first create) with it."""
        app = app_factory(TOKEN)
        async with _client(app) as c:
            unauth = await c.post("/api/v1/observations", json={"summary": "must be gated"})
            authed = await c.post(
                "/api/v1/observations",
                headers={"Authorization": f"Bearer {TOKEN}"},
                json={"summary": "classic alias accepted"},
            )
        assert unauth.status_code == 401
        assert authed.status_code == 201

    async def test_dashboard_mounted_mcp_endpoint_enforced(self, app_factory: Callable[[str | None], FastAPI]) -> None:
        """The dashboard-mounted HTTP MCP surface shares the bearer boundary."""
        app = app_factory(TOKEN)
        async with _client(app) as c:
            resp = await c.post("/mcp", json={})
        assert resp.status_code == 401

    async def test_specific_federation_token_env_var_enforces_scope(
        self,
        dashboard_db: PopulatedDB,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("FILIGREE_API_TOKEN", raising=False)
        monkeypatch.setenv("FILIGREE_FEDERATION_API_TOKEN", TOKEN)
        dash_module._db = dashboard_db.db
        try:
            app = create_app()
            async with _client(app) as c:
                unauth = await c.post("/api/observations", json={"summary": "must be gated"})
                authed = await c.post(
                    "/api/observations",
                    headers={"Authorization": f"Bearer {TOKEN}"},
                    json={"summary": "specific env token accepted"},
                )
        finally:
            dash_module._db = None
        assert unauth.status_code == 401
        assert authed.status_code == 201

    async def test_whitespace_token_leaves_surface_open_and_warns(
        self, app_factory: Callable[[str | None], FastAPI], caplog: pytest.LogCaptureFixture
    ) -> None:
        """A whitespace-only FILIGREE_API_TOKEN cannot be a real secret, so auth
        is NOT installed and the loom surface stays open — but an operator who
        exported a blank token must not be left believing auth is on. The fix is
        the warning; assert both the open route AND the log line so a regression
        in either is caught.
        """
        import logging

        with caplog.at_level(logging.WARNING):
            app = app_factory("   ")  # create_app reads the env var here
        async with _client(app) as c:
            resp = await c.get("/api/weft/issues")
        assert resp.status_code == 200  # open: a blank token gates nothing
        assert any("FILIGREE_API_TOKEN is set but empty" in r.message for r in caplog.records)


@pytest.fixture
def scoped_auth_app(project_store: ProjectStore, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[], FastAPI]]:
    """Server-mode app with a home/daemon token + per-project federation tokens.

    Exercises strict scope-aware auth (weft-23574069a1): a project-scoped request
    authenticates against THAT project's own token (``tok-<key>``) — not the
    daemon home-store token (``home-daemon-token``). The ``project_store`` fixture
    (alpha, bravo) patches ``SERVER_CONFIG_DIR`` to a temp dir.
    """
    import filigree.server as server_mod

    for v in ("WEFT_FEDERATION_TOKEN", "FILIGREE_FEDERATION_API_TOKEN", "FILIGREE_API_TOKEN"):
        monkeypatch.delenv(v, raising=False)

    (server_mod.SERVER_CONFIG_DIR / "federation_token").write_text("home-daemon-token\n")
    for proj in project_store.list_projects():
        (Path(proj["path"]) / "federation_token").write_text(f"tok-{proj['key']}\n")

    def _make() -> FastAPI:
        dash_module._project_store = project_store
        return create_app(server_mode=True)

    yield _make
    dash_module._project_store = None


class TestServerModeScopedAuth:
    """Strict per-project scoped auth: the scoped project's token OR a tier-1 env
    pin — never the daemon home-store token on a scoped request."""

    async def test_scoped_request_accepts_own_project_token(self, scoped_auth_app: Callable[[], FastAPI]) -> None:
        app = scoped_auth_app()
        async with _client(app) as c:
            resp = await c.get("/api/p/bravo/weft/issues", headers={"Authorization": "Bearer tok-bravo"})
        assert resp.status_code == 200, resp.text

    async def test_scoped_request_rejects_other_project_token(self, scoped_auth_app: Callable[[], FastAPI]) -> None:
        app = scoped_auth_app()
        async with _client(app) as c:
            resp = await c.get("/api/p/bravo/weft/issues", headers={"Authorization": "Bearer tok-alpha"})
        assert resp.status_code == 401

    async def test_scoped_request_rejects_home_token(self, scoped_auth_app: Callable[[], FastAPI]) -> None:
        """The daemon home-store token is NOT a valid credential for a scoped route."""
        app = scoped_auth_app()
        async with _client(app) as c:
            resp = await c.get("/api/p/bravo/weft/issues", headers={"Authorization": "Bearer home-daemon-token"})
        assert resp.status_code == 401

    async def test_query_scope_accepts_project_token(self, scoped_auth_app: Callable[[], FastAPI]) -> None:
        app = scoped_auth_app()
        async with _client(app) as c:
            resp = await c.get("/api/weft/issues?project=alpha", headers={"Authorization": "Bearer tok-alpha"})
        assert resp.status_code == 200, resp.text

    async def test_unscoped_read_accepts_daemon_token(self, scoped_auth_app: Callable[[], FastAPI]) -> None:
        """An unscoped read still authenticates against the daemon token (lenient)."""
        app = scoped_auth_app()
        async with _client(app) as c:
            resp = await c.get("/api/weft/issues", headers={"Authorization": "Bearer home-daemon-token"})
        assert resp.status_code == 200, resp.text

    async def test_unscoped_write_fails_closed_even_with_token(self, scoped_auth_app: Callable[[], FastAPI]) -> None:
        """A federation write with no scope is rejected as ambiguous (400) — never a
        silent home write — regardless of a valid daemon token."""
        app = scoped_auth_app()
        async with _client(app) as c:
            resp = await c.post(
                "/api/weft/scan-results",
                headers={"Authorization": "Bearer home-daemon-token"},
                json={"scan_source": "wardline", "findings": []},
            )
        assert resp.status_code == 400, resp.text

    async def test_env_pin_accepted_across_scopes(self, project_store: ProjectStore, monkeypatch: pytest.MonkeyPatch) -> None:
        """A tier-1 WEFT_FEDERATION_TOKEN env pin authenticates any project scope,
        and the per-project token still works alongside it."""
        monkeypatch.delenv("FILIGREE_FEDERATION_API_TOKEN", raising=False)
        monkeypatch.delenv("FILIGREE_API_TOKEN", raising=False)
        monkeypatch.setenv("WEFT_FEDERATION_TOKEN", "operator-pin")
        for proj in project_store.list_projects():
            (Path(proj["path"]) / "federation_token").write_text(f"tok-{proj['key']}\n")
        dash_module._project_store = project_store
        try:
            app = create_app(server_mode=True)
            async with _client(app) as c:
                pin_alpha = await c.get("/api/p/alpha/weft/issues", headers={"Authorization": "Bearer operator-pin"})
                pin_bravo = await c.get("/api/p/bravo/weft/issues", headers={"Authorization": "Bearer operator-pin"})
                own_token = await c.get("/api/p/bravo/weft/issues", headers={"Authorization": "Bearer tok-bravo"})
        finally:
            dash_module._project_store = None
        assert pin_alpha.status_code == 200
        assert pin_bravo.status_code == 200
        assert own_token.status_code == 200


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
        data = resp.json()
        assert data["auth"]["federation"]["enabled"] is True
        assert data["auth"]["federation"]["token_env"] == dash_module.LEGACY_API_ENV_VAR
        assert data["auth"]["classic_api"]["enabled"] is False
        assert data["auth"]["dashboard_ui"]["enabled"] is False
        assert data["auth"]["mcp_http"]["enabled"] is True

    async def test_health_reports_specific_federation_token_source(
        self,
        dashboard_db: PopulatedDB,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("FILIGREE_API_TOKEN", raising=False)
        monkeypatch.setenv("FILIGREE_FEDERATION_API_TOKEN", TOKEN)
        dash_module._db = dashboard_db.db
        try:
            app = create_app()
            async with _client(app) as c:
                resp = await c.get("/api/health")
        finally:
            dash_module._db = None
        assert resp.status_code == 200
        data = resp.json()
        assert data["auth"]["federation"]["enabled"] is True
        assert data["auth"]["federation"]["token_env"] == dash_module.FEDERATION_API_ENV_VAR

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
                "/api/weft/issues",
                headers={
                    "Origin": "http://localhost:8377",
                    "Access-Control-Request-Method": "GET",
                },
            )
        assert resp.status_code != 401
