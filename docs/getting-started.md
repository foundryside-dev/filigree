# Getting Started

Get up and running with Filigree in 5 minutes.

## Prerequisites

- Python 3.11 or later

## Install

### From PyPI

```bash
pip install filigree
```

### With uv

```bash
uv add filigree
```

### From source

```bash
git clone https://github.com/foundryside-dev/filigree.git
cd filigree
uv sync
```

## Initialize a Project

Navigate to your project root and run:

```bash
cd my-project
filigree init
```

```
Initialized filigree store at .weft/filigree/ in /path/to/my-project
  Prefix: my-project
  Mode: ethereal
  Database: /path/to/my-project/.weft/filigree/filigree.db
  Anchor: .weft/filigree/ (store-dir presence; confless — no .filigree.conf)
  Scanners: /path/to/my-project/.weft/filigree/scanners/ (add .toml files to register scanners)

Next: filigree install
```

This creates a `.weft/filigree/` store directory containing:

- `filigree.db` — SQLite database (WAL mode)
- `config.json` — project prefix, install mode, enabled packs
- `context.md` — auto-generated project summary
- `scanners/` — drop `.toml` files here to register scanners

`.weft/` is the canonical Weft store root since 3.0.0; `filigree` is its sole
writer under `.weft/filigree/`. Legacy `.filigree/` stores keep working and are
auto-migrated forward on the next `filigree init`.

Issue IDs use the format `{prefix}-{10hex}` (e.g., `myproj-a3f9b2e1c0`). The prefix defaults to your directory name.

## Set Up Integrations

```bash
filigree install
```

This command:

- Writes `.mcp.json` for Claude Code (MCP server config)
- Injects usage instructions into `CLAUDE.md`
- Adds `.filigree/` entries to `.gitignore`

For specific integrations:

```bash
filigree install --claude-code    # Claude Code only
filigree install --codex          # OpenAI Codex only (runtime folder autodiscovery)
filigree install --hooks          # Claude Code hooks only
filigree install --skills         # Claude Code skills only
filigree install --codex-skills   # Codex skills only
```

To configure operating mode explicitly:

```bash
filigree init --mode=ethereal     # Default mode (single-project local process)
filigree install --mode=server    # Persistent daemon / multi-project mode
```

## Create Your First Issue

```bash
filigree create "Set up CI pipeline" --type=task --priority=1
```

```
Created task myproj-a3f9b2e1c0: Set up CI pipeline (P1)
```

## View the Ready Queue

```bash
filigree ready
```

```
P1  myproj-a3f9b2e1c0  task  Set up CI pipeline
```

Shows all unblocked issues sorted by priority. This is what agents check first to find work.

## Work on an Issue

```bash
filigree update myproj-a3f9b2e1c0 --status=in_progress
```

## Close an Issue

```bash
filigree close myproj-a3f9b2e1c0
```

Or with a reason:

```bash
filigree close myproj-a3f9b2e1c0 --reason="Implemented in commit abc123"
```

## Optional Extras

### MCP Server

The MCP server is included in the base install — no extra needed. It exposes 118 tools so agents interact with filigree without parsing CLI output. See [MCP Server Reference](mcp.md).

### Web Dashboard

```bash
filigree dashboard --port=8377
```

The dashboard is included in the base install — no extra needed.

## Entry Points

| Command | Purpose |
|---------|---------|
| `filigree` | CLI interface |
| `filigree-mcp` | MCP server (stdio transport) |
| `filigree-dashboard` | Web UI (port 8377) |

## What Next?

- [CLI Reference](cli.md) — full command reference with parameter docs
- [MCP Server Reference](mcp.md) — 118 tools for agent-native interaction
- [Workflow Templates](workflows.md) — state machines, packs, and field schemas
- [Agent Integration](agent-integration.md) — multi-agent patterns and session resumption
- [Architecture](architecture.md) — source layout, DB schema, design decisions
