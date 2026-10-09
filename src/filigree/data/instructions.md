## Filigree Issue Tracker

`filigree` tracks this project's work: `filigree session-context` at session
start, then `filigree start-next-work --assignee <name>`. Name yourself on
writes: `filigree --actor <name>` (CLI) or `actor=<name>` (MCP);
`session-context` also reads `FILIGREE_ACTOR`.

Reference: the **filigree-workflow** skill, `filigree --help`, and the
`mcp__filigree__*` tool schemas. Prefer MCP tools; fall back to the CLI.

Two rules `--help` will not tell you:

1. Claim atomically: `work_start` / `work_start_next` (MCP) or `start-work` /
   `start-next-work` (CLI). Never chain a claim with a separate status update;
   that two-step form races other agents.
2. On `SCHEMA_MISMATCH` the installed filigree is older than the project
   database. Surface it to the user; do not retry.
