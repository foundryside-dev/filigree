# MCP Shared File Annotations Design

**Date:** 2026-05-07 local time
**Status:** Draft design
**Tracker:** `filigree-360ac7fc4c` under `filigree-ed2ccaf10d`
**Audience:** Filigree maintainers and agents using the MCP surface

## Summary

Add a shared annotation system for durable, provenance-rich file notes. An annotation is not a bug report and not a task. It is project context anchored to a file or line range, created when an agent wants future agents to understand what it was looking at and why that context matters.

The key difference from observations:

- **Observation:** "I noticed something that may deserve triage."
- **Annotation:** "Future agents working near this file or linked work item should know this."

Annotations should be shared by default, DB-backed, line/snippet anchored, linked to any relevant work items, and automatically capture commit/diff/checksum provenance so a later agent can answer: "what exactly were they looking at when they wrote this?"

## Goals

- Let agents leave durable shared notes on files without editing source comments.
- Preserve the viewing context automatically: commit, branch, dirty state, file checksum, anchor snippet, and relevant diff.
- Link one annotation to many targets: issues, epics, observations, findings, sessions, commits, and file records.
- Support a boolean `critical` flag that means "surface this more aggressively."
- Surface annotations from both directions: while reading a file and while working a linked issue or epic.
- Track staleness when files drift from the version where the note was made.

## Non-Goals

- Replace source comments or docstrings.
- Replace observations, findings, comments, or issues.
- Add a full priority system for annotations. Tickets already own scheduling priority; annotations only need attention criticality.
- Build semantic code indexing in the first version. Start with line ranges, snippets, checksums, and diff provenance.

## Core Model

An annotation combines five things:

1. **Note:** what the agent wants future readers to know.
2. **Anchor:** where the agent was looking.
3. **Provenance:** what repository state the agent saw.
4. **Context:** what the agent was doing when it made the note.
5. **Links:** which work items or artifacts the note matters to.

Suggested top-level fields:

```text
annotation_id
file_id
file_path
line_start
line_end
anchor_snippet
note
context_summary
intent
critical
visibility
status
actor
session_id
source_issue_id
created_at
updated_at
resolved_at
```

Suggested enums:

```text
intent:
  explanation
  warning
  breadcrumb
  hypothesis
  decision
  handoff
  gotcha

visibility:
  project
  session

status:
  active
  resolved
  superseded
  stale
  promoted
```

`visibility="project"` should be the default for this shared-context version. `visibility="session"` is useful for temporary notes that should not crowd future agents unless explicitly promoted.

## Provenance Snapshot

Every annotation should automatically capture repository and file state.

Suggested fields:

```text
commit_ref
branch
worktree_dirty
file_checksum
file_size
file_mtime
dirty_diff_hash
dirty_diff_summary
file_diff
worktree_diff_summary
anchor_context_before
anchor_context_after
```

The checksum should be for the file content at annotation time. The diff should be capped:

- Store file-local diff in full when below a size limit.
- Store a hash plus summary when too large.
- Store whole-worktree diff summary and hash, not necessarily the whole patch.

This is the critical trust feature. A later agent should be able to tell whether the note was written against clean `HEAD`, against uncommitted edits, or against a file that no longer matches.

## Links

Annotations need many-to-many links. A file invariant can matter to several issues; an epic can collect important notes from many files.

Suggested link table:

```text
annotation_link_id
annotation_id
target_type
target_id
relationship
created_at
actor
```

Suggested targets:

```text
issue
epic
observation
finding
file
session
commit
pull_request
```

Suggested relationships:

```text
relevant_to
must_consider
evidence_for
explains
contradicts
supersedes
related_to
created_from
promoted_to
```

An annotation linked to an epic with `relationship="must_consider"` and `critical=true` means: when an agent works this epic, this file note should be brought into its working context.

## Critical Flag

Use a boolean:

```text
critical: true | false
```

Do not use P0-P4 for annotations. Priorities schedule work; annotation criticality routes attention.

Critical annotations should:

- appear first in file context;
- appear in linked issue/epic context;
- be called out if they become stale or drifted;
- require explicit resolution, supersession, or carry-forward before closing a linked epic if the relationship is `must_consider`.

## Staleness And Anchor Drift

Annotation reads should compute drift state by comparing stored provenance to current file state.

Suggested states:

```text
current
line_drifted
content_changed_anchor_found
stale
file_missing
commit_unavailable
```

Behavior:

- `current`: checksum still matches.
- `line_drifted`: checksum changed but anchor snippet still matches at a different line.
- `content_changed_anchor_found`: checksum changed and snippet still exists near the original line.
- `stale`: checksum changed and anchor cannot be found.
- `file_missing`: tracked file path no longer exists.
- `commit_unavailable`: original commit is not available locally.

The first version can compute this lazily in read tools instead of storing it eagerly.

## MCP Tools

Minimum useful MCP surface:

```text
annotate_file(
  file_path,
  note,
  line_start?,
  line_end?,
  context_summary?,
  intent?,
  critical?,
  visibility?,
  source_issue_id?,
  links?
)

list_annotations(
  file_path?,
  file_id?,
  issue_id?,
  target_type?,
  target_id?,
  actor?,
  intent?,
  critical?,
  status?,
  include_stale?
)

get_annotation(annotation_id)

update_annotation(
  annotation_id,
  note?,
  context_summary?,
  intent?,
  critical?,
  visibility?,
  status?
)

resolve_annotation(annotation_id, reason)

link_annotation(annotation_id, target_type, target_id, relationship)

unlink_annotation(annotation_id, target_type, target_id, relationship?)
```

High-leverage context tools:

```text
get_file_context(file_path, include_annotations=true)
get_issue_context(issue_id, include_annotations=true)
```

`get_file_context` should return file record, findings, observations, associated issues, and annotations. `get_issue_context` should return issue detail, comments, associated files, linked findings/observations, and linked annotations, with critical annotations elevated.

## CLI Shape

The CLI should exist for background agents and humans:

```bash
filigree annotate-file src/foo.py \
  --line 42 \
  --intent warning \
  --critical \
  --link issue:filigree-abc123:must_consider \
  "This validation protects the legacy CLI path."

filigree list-annotations --file src/foo.py --json
filigree resolve-annotation filigree-ann-abc123 --reason "Invariant moved to docs"
```

CLI JSON should use the same envelopes as MCP.

## Example

```text
annotate_file(
  file_path="src/filigree/mcp_tools/scanners.py",
  line_start=301,
  line_end=306,
  note="report_finding creates observations implicitly here; any cleanup of finding/observation flow must preserve or make explicit this dual-write behavior.",
  context_summary="Noticed while designing shared file annotations after the MCP agent-systems review.",
  intent="warning",
  critical=true,
  links=[
    {
      target_type: "issue",
      target_id: "filigree-ed2ccaf10d",
      relationship: "must_consider"
    },
    {
      target_type: "issue",
      target_id: "filigree-42e0aa3c89",
      relationship: "evidence_for"
    }
  ]
)
```

Future file context might show:

```text
CRITICAL annotation filigree-ann-123
intent: warning
drift: line_drifted, snippet found at line 318
made_at: commit abc123, dirty worktree true
linked: filigree-ed2ccaf10d (must_consider), filigree-42e0aa3c89 (evidence_for)
note: report_finding creates observations implicitly here...
```

## Relationship To Existing Concepts

Annotations should coexist with existing Filigree primitives:

- **Comments** are issue conversation and handoff.
- **Observations** are triage candidates that may become work.
- **Findings** are scanner or agent-discovered structured code concerns.
- **Issues/epics** are scheduled work.
- **Annotations** are durable file context with provenance.

Promotion paths:

- annotation -> observation when the note becomes a triage candidate;
- annotation -> issue when it becomes work;
- observation -> annotation when triage decides the item is useful context but not a defect;
- finding -> annotation when a finding is intentionally accepted or documented as context.

## Data Storage Sketch

Tables:

```text
annotations
annotation_links
annotation_provenance
annotation_resolutions
```

`annotations` stores the note, anchor, lifecycle, and display fields.
`annotation_provenance` stores commit/diff/checksum fields, separated so large diff metadata does not crowd normal list queries.
`annotation_links` stores many-to-many typed relationships.
`annotation_resolutions` stores audit records for resolve/supersede/promote operations.

## Acceptance Criteria

- Agents can create a shared annotation on a file through MCP in one call.
- The annotation automatically records current commit, branch, dirty state, file checksum, anchor snippet, and file-local diff metadata.
- An annotation can be linked to multiple issues or epics with typed relationships.
- A boolean `critical` flag changes surfacing behavior without introducing priority semantics.
- `get_file_context` shows active annotations for a file and computes drift status.
- `get_issue_context` shows linked critical annotations for an issue or epic.
- Resolving an annotation preserves an audit trail.
- Existing observations, findings, comments, and issues remain distinct; annotations do not replace them.

## Open Questions

- Should `visibility="session"` annotations expire automatically, or remain hidden until promoted/shared?
- What diff size cap is appropriate for inline storage?
- Should closing an epic with unresolved critical `must_consider` annotations warn, block, or simply list them?
- Should annotations be allowed on non-file targets directly, or should the first version require a file/file-range anchor?
