# Server Install + Ethereal Mode — Design

**Epic:** `filigree-a7f852`
**Date:** 2026-02-22
**Status:** Approved

## Problem

Filigree currently uses a hybrid registration mode: each project's `ensure-dashboard` hook races to grab port 8377, and projects register themselves via POST to `/api/register` on whatever dashboard happens to be running. This is brittle when multiple filigree versions are installed across different project venvs — the first dashboard to start "wins" and other projects get served by a potentially incompatible version.

## Solution

Replace the hybrid registration system with two clean installation modes:

- **Ethereal mode** (default): session-scoped server per project on a unique port
- **Server mode** (opt-in): single persistent daemon serving all projects

## Key Decisions

| Decision | Choice |
|----------|--------|
| Default mode | Ethereal |
| Mode config | `"mode"` field in `.filigree/config.json`, set via `--mode` flag |
| Ethereal port | Deterministic hash with collision fallback |
| Ethereal MCP transport | Stdio (unchanged) |
| Server daemon | Custom PID-based, `filigree server start/stop/status` |
| Server MCP transport | Streamable HTTP |
| Server version policy | Single version, refuse incompatible schemas |
| Server install recommendation | Dedicated venv, not project-affiliated |
| Hook output | Always includes dashboard URL and port |
| Doctor | Mode-aware checks, dead project detection + removal prompts |

---

## Section 1: Mode Configuration

**Config storage:** `.filigree/config.json` gains a `"mode"` field:

```json
{
  "prefix": "filigree",
  "mode": "ethereal"
}
```

**Defaults:**
- `filigree init` — mode defaults to `"ethereal"` (no flag needed)
- `filigree install --mode=server` — switches to server mode and configures accordingly
- `filigree install --mode=ethereal` — explicitly sets ethereal (same as default)
- Omitting `--mode` on an existing project preserves whatever mode is already set

**Mode reading:** A `get_mode()` helper in `core.py` reads from config, returns `"ethereal"` if unset. All downstream code (hooks, doctor, install) branches on this.

---

## Section 2: Ethereal Mode (Default)

**Lifecycle:** SessionStart hook spawns a single-project dashboard on a unique port. Process dies when the session ends (or is cleaned up on next start if orphaned).

**Port selection:**
1. Compute deterministic port: `8400 + (hash(project_path) % 1000)`
2. Check if that port is already listening — if it's a filigree server for *this* project, reuse it
3. If occupied by something else, try up to 5 sequential ports
4. If all occupied, fall back to OS-assigned (port 0)
5. Write chosen port to `.filigree/ephemeral.port`

**PID tracking:** `.filigree/ephemeral.pid` — on startup, check if PID is still alive. If stale, clean up and start fresh.

**MCP transport:** Stays **stdio** — same as today. The MCP server process is per-session, direct SQLite. Only the dashboard is HTTP.

**Hook changes:** `ensure-dashboard` simplifies dramatically:
- No registry import, no `/api/register` POST
- No `/tmp/filigree-dashboard.lock` — lock file moves to `.filigree/ephemeral.lock` (project-scoped)
- Just: check port -> start if needed -> write PID/port files

**Hook output:** The hook prints the dashboard URL so the agent and user always know the address:
- New start: `Started Filigree dashboard on http://localhost:9173`
- Already running: `Filigree dashboard running on http://localhost:9173`

The port is always included since it's no longer a well-known fixed port. The `session-context` hook also includes it in the project snapshot block so it persists in the agent's context window.

**Dashboard changes:** `main()` drops `ProjectManager`/`Registry` entirely. Single project, single DB connection. The `/api/projects`, `/api/register`, `/api/reload` endpoints are removed. `/api/p/{project_key}/` prefix routing goes away — everything is just `/api/`.

**Cleanup:** No explicit teardown hook needed. The orphan-detection on next start is sufficient. If the process is still running from a previous session on the same deterministic port, the new session just reuses it.

---

## Section 3: Server Mode

**Daemon lifecycle:**
- `filigree server start` — spawns uvicorn as a detached process on the configured port (default 8377), writes PID to `~/.config/filigree/server.pid`
- `filigree server stop` — reads PID file, sends SIGTERM, cleans up
- `filigree server status` — reports running/stopped, port, registered projects, filigree version

**Config:** `~/.config/filigree/server.toml`

```toml
port = 8377

[projects]
"/home/user/filigree/.filigree" = { prefix = "filigree" }
"/home/user/other-project/.filigree" = { prefix = "other" }
```

**Project registration:**
- `filigree server register` (from within a project) or `filigree server register /path/to/project`
- Adds the project to `server.toml` and, if the daemon is running, hot-reloads via `POST /api/reload`
- `filigree server unregister` removes it

**MCP transport:** Streamable HTTP. `filigree install --mode=server` writes `.mcp.json` pointing to the daemon:

```json
{
  "mcpServers": {
    "filigree": {
      "type": "streamable-http",
      "url": "http://localhost:8377/mcp/"
    }
  }
}
```

The daemon exposes the MCP endpoint at `/mcp/` alongside the dashboard. Project scoping uses a query parameter or header (e.g. `/mcp/?project=filigree`) so the single endpoint serves all registered projects.

**Version enforcement:** On startup and project registration, the daemon checks each project's schema version against its own. If a project's schema is newer, it refuses to serve it and logs: `Project "foo" requires filigree >= 1.4 (daemon running 1.3). Upgrade the daemon or run in ethereal mode.`

**Hook behavior in server mode:** The `ensure-dashboard` hook skips spawning a process entirely — it just verifies the daemon is reachable on the configured port and registers the project if needed. Output: `Filigree server running on http://localhost:8377 (3 projects)`

### Recommended Setup

The preferred way to install filigree in server mode is to create a dedicated directory with its own virtualenv, rather than installing it into any project's venv. This ensures all projects are served by a single, consistent filigree version and avoids version drift when individual project venvs are updated independently.

```bash
mkdir ~/.filigree-server && cd ~/.filigree-server
python -m venv .venv && .venv/bin/pip install "filigree[dashboard]"
.venv/bin/filigree server start
```

---

## Section 4: Doctor Checks Per Mode

**Ethereal mode doctor checks:**
1. `.filigree/config.json` has `"mode": "ethereal"`
2. If `.filigree/ephemeral.pid` exists, verify the process is alive — warn if stale
3. If `.filigree/ephemeral.port` exists, verify the port is listening — warn if not
4. All existing checks (DB, schema version, MCP config, hooks, etc.) remain

**Server mode doctor checks:**
1. `.filigree/config.json` has `"mode": "server"`
2. `~/.config/filigree/server.toml` exists and is valid TOML
3. Daemon is running — check PID file and port reachability
4. Daemon version matches this CLI's version — warn on mismatch
5. **Project health sweep** — for each registered project in `server.toml`:
   - `.filigree/` directory still exists
   - DB is accessible and schema version is compatible
   - Flag **dead projects** (directory gone, DB corrupt, or schema too new)
6. MCP config points to the daemon URL (not stale stdio config)

**Dead project UX:** When doctor finds dead projects, it reports them clearly and prompts for action:

```
!! Project "old-thing" (/home/user/old-thing/.filigree)
   Directory no longer exists
   Fix: filigree server unregister /home/user/old-thing

!! Project "experiment" (/home/user/experiment/.filigree)
   Schema v5 (daemon supports v4)
   Fix: upgrade daemon or switch project to ethereal mode
```

`filigree doctor --fix` in server mode auto-unregisters projects whose directories no longer exist (with confirmation).

`filigree server status` also shows project health inline — healthy projects get a checkmark, dead ones get a warning with the removal hint.

---

## Section 5: Install Command Changes

**Current:** `filigree install` has flags like `--claude-code`, `--codex`, `--hooks`, `--claude-md`, `--gitignore`, `--skills`. No mode concept.

**New:** Add `--mode=ethereal|server` (default: `ethereal`).

**Behavior by mode:**

`filigree install` (ethereal, default):
- Writes `"mode": "ethereal"` to `.filigree/config.json`
- MCP config (`.mcp.json`) uses stdio transport — same as today
- Hooks: registers `session-context` and `ensure-dashboard` (ethereal variant)
- Everything else unchanged (CLAUDE.md injection, gitignore, skills, etc.)

`filigree install --mode=server`:
- Writes `"mode": "server"` to `.filigree/config.json`
- Creates `~/.config/filigree/server.toml` if it doesn't exist
- Registers the current project in `server.toml`
- MCP config (`.mcp.json`) uses streamable HTTP transport pointing to daemon URL
- Hooks: registers `session-context` and a lighter `ensure-dashboard` that just verifies the daemon is up (no spawning)
- If daemon isn't running, prints: `Note: start the daemon with "filigree server start"`

**Mode switching:** Running `filigree install --mode=server` on a project that was previously ethereal overwrites the MCP config and hooks. Running `filigree install --mode=ethereal` on a server-mode project switches back (but doesn't unregister from `server.toml` — that's explicit via `filigree server unregister`).

**`filigree init`:** Gains the same `--mode` flag. `filigree init` is effectively `init` + `install`, so it configures the mode at project creation time. Default remains ethereal.

---

## Section 6: Testing & Documentation

**Testing:**
- Ethereal mode: test port selection (deterministic, collision fallback, OS-assigned), PID lifecycle (stale cleanup, reuse), hook output includes URL
- Server mode: test daemon start/stop/status, project register/unregister, `server.toml` read/write, version check rejection, dead project detection
- Mode switching: test install flipping between modes, MCP config correctly rewritten
- Doctor: test each mode's check suite, dead project reporting, `--fix` auto-unregister

**Documentation:**
- `filigree install` help text updated with `--mode` flag
- README/CLAUDE.md instructions updated with mode recommendations
- Server mode gets the dedicated venv recommendation (see Section 3)

---

## Removal: Hybrid Registration

After both modes are functional, remove the hybrid registration system (tracked as `filigree-4b4a68`, blocked by the two feature issues):

- `src/filigree/registry.py` — entire module
- Dashboard multi-project scaffolding (`/api/register`, `/api/projects`, `/api/reload`, `ProjectManager`, project-key routing)
- Hook registration logic (`_try_register_with_server`, `Registry().register()` calls)
- `~/.filigree/registry.json` and `~/.filigree/registry.lock` conventions
- `/tmp/filigree-dashboard.lock` and `/tmp/filigree-dashboard.pid` conventions
- `tests/test_registry.py`
