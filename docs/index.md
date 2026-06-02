---
template: home.html
hide:
  - navigation
  - toc
---

## Install

```bash
pip install filigree
```

Filigree is just Python + SQLite + click — no framework, no service to run. To
wire it into Claude Code (session hooks + the bundled workflow skill pack):

```bash
filigree install
```

## 30-second example

Initialize a project, then drive the work loop — the same loop whether you run
it yourself or an agent runs it through MCP tools:

```bash
# Create a .filigree/ directory (like .git/) in the current project
filigree init

# Orient: ready / in-progress / critical path
filigree session-context

# Atomically claim the next startable issue and move it into its working status
filigree start-next-work --assignee me

# ...do the work, commit...

filigree close <id>
```

Every mutation regenerates `context.md`, so the next orientation — for you or
for an agent — is already waiting. Agents skip the CLI entirely and call the
same operations as native [MCP tools](mcp.md); background subagents use
`--json` and `--actor` for machine-readable output and audit trails.

## Next steps

- [Getting Started](getting-started.md) — install, initialize a project, and run the work loop.
- [Workflow templates](workflows.md) — the 24 issue types and their enforced state machines.
- [Agent integration](agent-integration.md) — wiring Filigree into a coding agent.
- [MCP server reference](mcp.md) — the tool surface agents call directly.
