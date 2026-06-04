# Architecture Scanner: A Deterministic, Graph-Based Codebase Representation for AI Agents

> **Status:** Concept — reviewed by 6-person design panel (2026-02-28).
> See [architecture_scanner_synthesis.md](https://github.com/tachyon-beep/filigree/blob/main/docs/concepts/architecture_scanner_synthesis.md) for the full debate record.

## 1. System Objective

To provide a deterministic, statically analyzed, graph-based representation of a Python codebase via the Model Context Protocol (MCP). This allows AI agents to perform high-level architectural reasoning, dependency tracking, and targeted code retrieval without reading unparsed text files or hallucinating structural connections.

**Product model:** Archscan is a **peer product** to Filigree — a separate codebase, separate MCP server, and independent release cycle. It integrates with Filigree through well-defined contracts but operates standalone. Agents mount both MCP servers to get structural awareness (archscan) alongside project management (filigree).

## 2. Core Architecture Components

The system is divided into three layers:

### A. The Transport & Protocol Layer (MCP Interface)

* **Responsibility:** Handles the JSON-RPC communication between the AI Client and the Server via standard input/output (stdio).
* **Function:** Registers the available tools, their JSON schemas, and their descriptive prompts. Acts as the router, passing agent requests to the Query Engine.

### B. The Static Analysis Engine (The Crawler)

* **Responsibility:** Parses raw Python files into Abstract Syntax Trees (AST) without executing them.
* **Function:** Implements custom `ast.NodeVisitor` classes. Sweeps through directories, stripping out function bodies and runtime logic, extracting only structural metadata: class names, method signatures, type hints, docstrings, and explicit import paths.
* **Limitations:** Cannot resolve dynamic imports (`importlib.import_module`), star imports (`from x import *`), monkey-patching, metaclass-generated attributes, or runtime type information. Produces static-analysis coverage, not runtime truth.
* **Target:** Python 3.11+ (uses `ast.Constant` exclusively, avoids deprecated node types).

### C. The Architectural Graph Store (In-Memory State)

* **Responsibility:** Maintains the cross-file relationship map. Single-file AST parsing isn't enough; the system needs to know how subsystems connect.
* **Function:** Uses a simple directed adjacency-dict implementation (`dict[str, set[str]]`) with BFS/DFS traversal functions. As the Crawler parses files, it populates this graph:
  * **Nodes:** Modules, Classes, and Functions.
  * **Edges:** `imports_from`, `inherits_from`, `calls_method`. All edges carry explicit directionality (A→B vs B→A).
* This graph powers the "Blast Radius" and "Subsystem Boundary" queries.
* **Why not NetworkX:** For MVP, the graph operations needed (BFS, DFS, cycle detection via Tarjan's) are trivially implementable with a raw adjacency dict. NetworkX adds a significant dependency for algorithms the MVP doesn't use. Upgrade to NetworkX in v2 if advanced graph algorithms (centrality, community detection) prove necessary.

### Removed: Quality Gatekeeper

The original concept included a "fail fast" engine that refused to analyze code exceeding complexity thresholds. This was **unanimously cut** by the review panel. The code most in need of structural analysis is precisely the code that's most complex. A complexity-gated refusal creates a perverse incentive where the tool's utility decreases exactly when demand increases. If complexity metrics are desired, expose them as advisory data or as a separate scanner that POSTs findings to Filigree.

---

## 3. The API / Tool Registry

Three MCP tools, following a progressive disclosure pattern (wide-to-narrow, cheap-to-expensive) by default. All tools support a `detail` parameter that controls response depth:

- **Default (no flag):** Progressive mode — returns the minimal, token-efficient response appropriate to the tool's level of abstraction.
- **`detail="full"`:** Returns enriched output with cross-cutting context (dependency counts, related symbols, blast radius summaries) in a single call. For agents that already have context and want everything without walking the funnel step by step.

The principle: **context is the most precious resource agents have.** The default protects it; the flag spends it deliberately.

### Tools

1. **`map_subsystem_boundaries(target_dir, detail="summary")`:**
   - **Default:** Module names and directed import edges — the lay of the land.
   - **`detail="full"`:** Includes per-module symbol counts, fan-in/fan-out metrics, and circular dependency warnings inline.
   - Response includes `last_validated_at` staleness timestamp.

2. **`get_symbol_skeleton(target_path, detail="signatures")`:**
   - **Default:** AST-parsed signatures, type hints, docstrings, and line numbers — no function bodies. The primary workhorse tool.
   - **`detail="full"`:** Includes per-symbol direct caller/callee counts and dependency direction, so agents can spot high-impact symbols without a separate blast radius call.
   - Response includes `last_validated_at` staleness timestamp.

3. **`trace_blast_radius(target_symbol, detail="direct")`:**
   - **Default:** Direct callers and callees (1-hop) with explicit directionality (`imports` vs `imported_by`, `calls` vs `called_by`).
   - **`detail="full"`:** Includes skeleton snippets (signatures only) for each caller/callee, so agents can assess impact without follow-up skeleton calls.
   - Response includes a confidence indicator and `last_validated_at` staleness timestamp.
   - v1.0 scope is limited to direct callers within the analyzed directory; transitive multi-hop traversal and `__init__.py` re-export resolution are v2.0.

### Removed: `fetch_surgical_context`

The original concept included a tool for extracting raw code by AST node reference. This was **unanimously cut**. `get_symbol_skeleton` returns line numbers per symbol, and agents already have `Read` with offset/limit for targeted code extraction. Adding a second code-retrieval tool creates decision fatigue without adding new capability.

### Tool Description Cross-References

All tool descriptions include navigation hints referencing the peer product:
- Archscan tools mention: "Tip: use filigree's `get_issue_files` to find files associated with your current task."
- Filigree tools mention: "Tip: use archscan's `get_symbol_skeleton` for structural analysis of source files."

These are zero-coupling hints — neither product depends on the other being present.

---

## 4. Data Flow: The Agentic Loop

### Progressive mode (default) — agent is exploring

1. **Orientation:** The agent calls `map_subsystem_boundaries(dir)` to understand how modules relate. Returns directed import edges.
2. **Skeleton Retrieval:** The agent calls `get_symbol_skeleton(path)` on a specific module to understand its interface contracts.
3. **Impact Assessment:** Before making changes, the agent calls `trace_blast_radius(symbol)` to discover direct callers and assess risk.
4. **Targeted Reading:** Using line numbers from the skeleton, the agent uses its built-in `Read` tool with offset/limit to examine specific function bodies.

### Full mode — agent already has context

1. **Direct skeleton:** The agent already knows which file it needs (from filigree issue, stack trace, or prior session). It calls `get_symbol_skeleton(path, detail="full")` to get signatures AND per-symbol caller/callee counts in one call.
2. **Targeted blast radius:** The agent calls `trace_blast_radius(symbol, detail="full")` to get callers with their signatures inline — no follow-up skeleton calls needed.
3. **Targeted Reading:** Same as progressive — `Read` with offset/limit for function bodies.

The full mode collapses what would be 3-4 progressive tool calls into 1-2, at the cost of a larger response. Agents that arrive with context (most daily work) should prefer full mode. Agents exploring an unfamiliar codebase should use progressive mode to minimize wasted tokens.

---

## 5. Caching & State Management

Because codebases change during an agentic workflow, the MCP server cannot rely on a stale graph.

* **File Hashing:** The Graph Store tracks the SHA-256 hash of every file it crawls.
* **Lazy Revalidation:** When an agent queries a specific module, the server checks the current file hash against the stored hash. If it differs, the Static Analysis Engine re-runs the AST parser *only* for that file and updates the specific nodes and edges in the Graph Store before returning the result.
* **Staleness Signals:** Every response includes a `last_validated_at` timestamp and a freshness indicator. Agents use this to decide whether to trust results or cross-verify with grep. Stale data that looks authoritative is worse than no data — this is a trust-critical feature, not optional metadata.
* **Memory Management:** AST nodes are parsed, structural data extracted into the graph model, and raw ASTs discarded. The graph store holds only the structural metadata, not full AST trees.

---

## 6. Integration with Filigree

Archscan is a peer product to Filigree. Integration is **optional and opportunistic** — archscan works standalone with full functionality.

### Integration Points

1. **Scan Results API** (`POST /api/v1/scan-results`): Archscan detects structural anomalies and POSTs them as findings to Filigree's scan API. Fire-and-forget with graceful failure if Filigree is unreachable.

2. **File Path as Shared Key:** Both products reference files by project-relative path. No shared IDs, no shared schemas. When archscan POSTs findings, Filigree creates/upserts file records as needed.

3. **Scanner TOML Registration** (v1.1): Archscan can register itself via `.filigree/scanners/archscan.toml`, enabling agents to trigger structural analysis through Filigree's `trigger_scan` MCP tool.

### What Becomes a Finding

Only structural **anomalies** — things an agent or human would act on:

| Finding | Rule ID | Severity |
|---------|---------|----------|
| Circular import dependency | `circular-dependency` | medium |
| Orphan module (zero importers) | `orphan-module` | low |
| Extreme fan-in (>N dependents) | `high-fan-in` | medium |
| Extreme fan-out (>N imports) | `high-fan-out` | medium |

Raw graph edges ("A imports B") are **never** POSTed as findings. They are ephemeral query data owned by archscan's graph store.

### Data Ownership

* **Archscan owns:** The dependency graph, structural analysis results, cache state
* **Filigree owns:** Issues, workflows, file-to-issue associations, scan findings
* **Neither duplicates the other's data.** If Filigree wants to display dependency info, it queries archscan at render time (v2 consideration).

### The Principle

> Two independent products that are *aware* of each other, not *dependent* on each other. Integration should be discoverable, not required. Agents that use both get a richer experience; agents that use one still get full value.

---

## 7. Release Plan

### v1.0 — MVP

**Tools:** `get_symbol_skeleton`, `trace_blast_radius` (1-hop), `map_subsystem_boundaries`
**Infrastructure:** Adjacency-dict graph, SHA-256 cache, lazy revalidation, standalone MCP server (stdio), staleness signals, dependency directionality
**Findings:** Circular dependency detection (fire-and-forget POST)
**Polish:** Cross-reference hints in tool descriptions

### v1.1 — Fast Follow

- Optional filigree detection + fire-and-forget POSTs (robust retry)
- Scanner TOML registration with filigree
- Orphan module detection, fan-in/fan-out hotspot detection
- Public API view (`get_symbol_skeleton(path, public_only=True)`)

### v2.0 — Next Major

- Transitive blast radius (multi-hop, `__init__.py` re-export resolution)
- Diff-aware view ("what changed since commit X" — git integration)
- Type context enrichment (cross-file type resolution)
- NetworkX upgrade (if advanced graph algorithms needed)
