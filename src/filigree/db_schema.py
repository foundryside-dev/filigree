"""Database schema definitions for the filigree issue tracker.

Contains the canonical SQL schema, the legacy V1 schema (for migration tests),
and the current schema version constant.
"""

from __future__ import annotations

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS issues (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    priority    INTEGER NOT NULL DEFAULT 2,
    type        TEXT NOT NULL DEFAULT 'task',
    parent_id   TEXT REFERENCES issues(id) ON DELETE SET NULL,
    assignee    TEXT DEFAULT '',
    claimed_at  TEXT,
    last_heartbeat_at TEXT,
    claim_expires_at  TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    closed_at   TEXT,
    description TEXT DEFAULT '',
    notes       TEXT DEFAULT '',
    fields      TEXT DEFAULT '{}',

    CHECK (priority BETWEEN 0 AND 4)
);

CREATE INDEX IF NOT EXISTS idx_issues_status ON issues(status);
CREATE INDEX IF NOT EXISTS idx_issues_type ON issues(type);
CREATE INDEX IF NOT EXISTS idx_issues_parent ON issues(parent_id);
CREATE INDEX IF NOT EXISTS idx_issues_priority ON issues(priority);
CREATE INDEX IF NOT EXISTS idx_issues_status_priority ON issues(status, priority, created_at);
CREATE INDEX IF NOT EXISTS idx_issues_assignee_priority ON issues(assignee, priority, created_at);
CREATE INDEX IF NOT EXISTS idx_issues_claim_expires_at ON issues(claim_expires_at);

CREATE TABLE IF NOT EXISTS dependencies (
    issue_id       TEXT NOT NULL REFERENCES issues(id),
    depends_on_id  TEXT NOT NULL REFERENCES issues(id),
    type           TEXT NOT NULL DEFAULT 'blocks',
    created_at     TEXT NOT NULL,
    PRIMARY KEY (issue_id, depends_on_id)
);

CREATE INDEX IF NOT EXISTS idx_deps_depends_on ON dependencies(depends_on_id);
CREATE INDEX IF NOT EXISTS idx_deps_issue_depends ON dependencies(issue_id, depends_on_id);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id   TEXT NOT NULL REFERENCES issues(id),
    event_type TEXT NOT NULL,
    actor      TEXT DEFAULT '',
    old_value  TEXT,
    new_value  TEXT,
    comment    TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    -- v16: same-second emissions get distinct event_seq values. This
    -- per-issue event ordering key is part of the legacy-named unique event
    -- index, so ordinary bursts persist as separate audit rows. _record_event
    -- computes the next value inline under the caller-held writer transaction via
    -- COALESCE((SELECT MAX(event_seq) FROM events WHERE issue_id = ?),
    -- -1) + 1; legacy rows default to 0.
    event_seq  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_events_issue ON events(issue_id);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_events_issue_time ON events(issue_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_dedup
  ON events(issue_id, event_type, actor,
    coalesce(old_value,''), coalesce(new_value,''), created_at, event_seq);

CREATE TABLE IF NOT EXISTS comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id   TEXT NOT NULL REFERENCES issues(id),
    author     TEXT DEFAULT '',
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_comments_issue ON comments(issue_id, created_at);

CREATE TABLE IF NOT EXISTS labels (
    issue_id TEXT NOT NULL REFERENCES issues(id),
    label    TEXT NOT NULL,
    PRIMARY KEY (issue_id, label)
);

CREATE INDEX IF NOT EXISTS idx_labels_label_issue ON labels(label, issue_id);

CREATE TABLE IF NOT EXISTS type_templates (
    type          TEXT PRIMARY KEY,
    pack          TEXT NOT NULL DEFAULT 'core',
    definition    TEXT NOT NULL,
    is_builtin    BOOLEAN NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS packs (
    name          TEXT PRIMARY KEY,
    version       TEXT NOT NULL,
    definition    TEXT NOT NULL,
    is_builtin    BOOLEAN NOT NULL DEFAULT 0,
    enabled       BOOLEAN NOT NULL DEFAULT 1
);

-- FTS5 full-text search with sync triggers
CREATE VIRTUAL TABLE IF NOT EXISTS issues_fts USING fts5(
    title, description, content='issues', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS issues_fts_insert AFTER INSERT ON issues BEGIN
    INSERT INTO issues_fts(rowid, title, description) VALUES (new.rowid, new.title, new.description);
END;
CREATE TRIGGER IF NOT EXISTS issues_fts_update AFTER UPDATE OF title, description ON issues BEGIN
    INSERT INTO issues_fts(issues_fts, rowid, title, description)
        VALUES('delete', old.rowid, old.title, old.description);
    INSERT INTO issues_fts(rowid, title, description) VALUES (new.rowid, new.title, new.description);
END;
CREATE TRIGGER IF NOT EXISTS issues_fts_delete AFTER DELETE ON issues BEGIN
    INSERT INTO issues_fts(issues_fts, rowid, title, description)
        VALUES('delete', old.rowid, old.title, old.description);
END;

-- ---- File records & scan findings (v2) -----------------------------------

CREATE TABLE IF NOT EXISTS file_records (
    id          TEXT PRIMARY KEY,
    path        TEXT NOT NULL UNIQUE,
    language    TEXT DEFAULT '',
    file_type   TEXT DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    registry_backend TEXT NOT NULL DEFAULT 'local',
    created_by  TEXT DEFAULT '',
    updated_by  TEXT DEFAULT '',
    first_seen  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    metadata    TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_file_records_path ON file_records(path);
CREATE INDEX IF NOT EXISTS idx_file_records_language ON file_records(language);

CREATE TABLE IF NOT EXISTS scan_findings (
    id            TEXT PRIMARY KEY,
    file_id       TEXT NOT NULL REFERENCES file_records(id),
    issue_id      TEXT REFERENCES issues(id) ON DELETE SET NULL,
    scan_source   TEXT NOT NULL DEFAULT '',
    rule_id       TEXT DEFAULT '',
    severity      TEXT NOT NULL DEFAULT 'info',
    status        TEXT NOT NULL DEFAULT 'open',
    message       TEXT DEFAULT '',
    suggestion    TEXT DEFAULT '',
    scan_run_id   TEXT DEFAULT '',
    line_start    INTEGER,
    line_end      INTEGER,
    seen_count    INTEGER DEFAULT 1,
    created_by    TEXT DEFAULT '',
    updated_by    TEXT DEFAULT '',
    first_seen    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    last_seen_at  TEXT,
    metadata      TEXT DEFAULT '{}',
    CHECK (severity IN ('critical', 'high', 'medium', 'low', 'info')),
    CHECK (status IN ('open', 'acknowledged', 'fixed', 'false_positive', 'unseen_in_latest'))
);

CREATE INDEX IF NOT EXISTS idx_scan_findings_file ON scan_findings(file_id);
CREATE INDEX IF NOT EXISTS idx_scan_findings_issue ON scan_findings(issue_id);
CREATE INDEX IF NOT EXISTS idx_scan_findings_severity ON scan_findings(severity);
CREATE INDEX IF NOT EXISTS idx_scan_findings_status ON scan_findings(status);
CREATE INDEX IF NOT EXISTS idx_scan_findings_run ON scan_findings(scan_run_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_scan_findings_dedup
  ON scan_findings(file_id, scan_source, rule_id, coalesce(line_start, -1));

CREATE TABLE IF NOT EXISTS file_associations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id     TEXT NOT NULL REFERENCES file_records(id),
    issue_id    TEXT NOT NULL REFERENCES issues(id),
    assoc_type  TEXT NOT NULL,
    actor       TEXT DEFAULT '',
    created_at  TEXT NOT NULL,
    UNIQUE(file_id, issue_id, assoc_type),
    CHECK (assoc_type IN ('bug_in', 'task_for', 'scan_finding', 'mentioned_in'))
);

CREATE INDEX IF NOT EXISTS idx_file_assoc_file ON file_associations(file_id);
CREATE INDEX IF NOT EXISTS idx_file_assoc_issue ON file_associations(issue_id);

-- ---- Scan run lifecycle tracking ------------------------------------------

CREATE TABLE IF NOT EXISTS scan_runs (
    id            TEXT PRIMARY KEY,
    scanner_name  TEXT NOT NULL,
    scan_source   TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'pending',
    file_paths    TEXT NOT NULL DEFAULT '[]',
    file_ids      TEXT NOT NULL DEFAULT '[]',
    pid           INTEGER,
    api_url       TEXT DEFAULT '',
    log_path      TEXT DEFAULT '',
    started_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    completed_at  TEXT,
    exit_code     INTEGER,
    findings_count INTEGER DEFAULT 0,
    error_message TEXT DEFAULT '',
    CHECK (status IN ('pending', 'running', 'completed', 'failed', 'timeout'))
);

CREATE INDEX IF NOT EXISTS idx_scan_runs_status ON scan_runs(status);
CREATE INDEX IF NOT EXISTS idx_scan_runs_scanner ON scan_runs(scanner_name);

CREATE TABLE IF NOT EXISTS file_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id     TEXT NOT NULL REFERENCES file_records(id),
    event_type  TEXT NOT NULL DEFAULT 'file_metadata_update',
    field       TEXT NOT NULL,
    old_value   TEXT DEFAULT '',
    new_value   TEXT DEFAULT '',
    actor       TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_file_events_file ON file_events(file_id);

-- ---- Observations (agent scratchpad) ------------------------------------

CREATE TABLE IF NOT EXISTS observations (
    id                TEXT PRIMARY KEY,
    summary           TEXT NOT NULL,
    detail            TEXT DEFAULT '',
    file_id           TEXT REFERENCES file_records(id) ON DELETE SET NULL,
    file_path         TEXT DEFAULT '',
    line              INTEGER,
    source_issue_id   TEXT DEFAULT '',
    source_finding_id TEXT DEFAULT '',
    priority          INTEGER DEFAULT 3 CHECK (priority BETWEEN 0 AND 4),
    actor             TEXT DEFAULT '',
    created_at        TEXT NOT NULL,
    expires_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_observations_priority ON observations(priority, created_at);
CREATE INDEX IF NOT EXISTS idx_observations_expires ON observations(expires_at);
CREATE INDEX IF NOT EXISTS idx_observations_file_id ON observations(file_id);
CREATE INDEX IF NOT EXISTS idx_observations_source_finding ON observations(source_finding_id);
-- Dedup contract: coalesce(line, -1) means NULL lines map to -1.
-- An observation with line=NULL and line=-1 are considered duplicates.
-- This is intentional — line=-1 is not a valid line number.
CREATE UNIQUE INDEX IF NOT EXISTS idx_observations_dedup
  ON observations(summary, file_path, coalesce(line, -1));

CREATE TABLE IF NOT EXISTS dismissed_observations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    obs_id       TEXT NOT NULL,
    summary      TEXT NOT NULL,
    actor        TEXT DEFAULT '',
    reason       TEXT DEFAULT '',
    dismissed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dismissed_obs_id ON dismissed_observations(obs_id);

CREATE TABLE IF NOT EXISTS observation_links (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    obs_id             TEXT NOT NULL,
    issue_id           TEXT NOT NULL REFERENCES issues(id),
    disposition        TEXT NOT NULL DEFAULT 'evidence',
    summary            TEXT NOT NULL,
    detail             TEXT DEFAULT '',
    file_id            TEXT REFERENCES file_records(id) ON DELETE SET NULL,
    file_path          TEXT DEFAULT '',
    line               INTEGER,
    source_issue_id    TEXT DEFAULT '',
    source_finding_id  TEXT DEFAULT '',
    priority           INTEGER DEFAULT 3 CHECK (priority BETWEEN 0 AND 4),
    observation_actor  TEXT DEFAULT '',
    actor              TEXT DEFAULT '',
    reason             TEXT DEFAULT '',
    linked_at          TEXT NOT NULL,
    CHECK (disposition IN ('evidence', 'duplicate', 'superseded', 'related'))
);

CREATE INDEX IF NOT EXISTS idx_observation_links_obs ON observation_links(obs_id);
CREATE INDEX IF NOT EXISTS idx_observation_links_issue ON observation_links(issue_id, linked_at);

-- ---- Shared file annotations (v10) --------------------------------------

CREATE TABLE IF NOT EXISTS annotations (
    id              TEXT PRIMARY KEY,
    file_id         TEXT REFERENCES file_records(id) ON DELETE SET NULL,
    file_path       TEXT NOT NULL,
    line_start      INTEGER,
    line_end        INTEGER,
    anchor_snippet  TEXT DEFAULT '',
    note            TEXT NOT NULL,
    context_summary TEXT DEFAULT '',
    intent          TEXT NOT NULL DEFAULT 'breadcrumb',
    critical        BOOLEAN NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'active',
    actor           TEXT DEFAULT '',
    session_ref     TEXT DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    resolved_at     TEXT,
    CHECK (intent IN ('explanation', 'warning', 'breadcrumb', 'hypothesis', 'decision', 'handoff', 'gotcha')),
    CHECK (status IN ('active', 'resolved', 'superseded', 'promoted')),
    CHECK (line_start IS NULL OR line_start >= 1),
    CHECK (line_end IS NULL OR (line_start IS NOT NULL AND line_end >= line_start))
);

CREATE INDEX IF NOT EXISTS idx_annotations_file ON annotations(file_id, status, critical, created_at);
CREATE INDEX IF NOT EXISTS idx_annotations_path ON annotations(file_path, status, critical, created_at);
CREATE INDEX IF NOT EXISTS idx_annotations_status ON annotations(status, critical, created_at);

CREATE TABLE IF NOT EXISTS annotation_provenance (
    annotation_id          TEXT PRIMARY KEY REFERENCES annotations(id) ON DELETE CASCADE,
    commit_ref            TEXT DEFAULT '',
    branch                TEXT DEFAULT '',
    repo_root             TEXT DEFAULT '',
    worktree_root         TEXT DEFAULT '',
    git_state             TEXT DEFAULT '',
    worktree_dirty        BOOLEAN NOT NULL DEFAULT 0,
    file_checksum         TEXT DEFAULT '',
    file_size             INTEGER DEFAULT 0,
    file_mtime            TEXT DEFAULT '',
    dirty_diff_hash       TEXT DEFAULT '',
    dirty_diff_summary    TEXT DEFAULT '',
    file_diff             TEXT DEFAULT '',
    worktree_diff_summary TEXT DEFAULT '',
    anchor_context_before TEXT DEFAULT '',
    anchor_context_after  TEXT DEFAULT '',
    provenance_trust_level TEXT NOT NULL DEFAULT 'minimal',
    provenance_flags      TEXT NOT NULL DEFAULT '[]',
    provenance_warnings   TEXT NOT NULL DEFAULT '[]',
    CHECK (provenance_trust_level IN ('complete', 'partial', 'minimal'))
);

CREATE TABLE IF NOT EXISTS annotation_links (
    id             TEXT PRIMARY KEY,
    annotation_id  TEXT NOT NULL REFERENCES annotations(id) ON DELETE CASCADE,
    target_type    TEXT NOT NULL,
    target_id      TEXT NOT NULL,
    relationship   TEXT NOT NULL,
    actor          TEXT DEFAULT '',
    created_at     TEXT NOT NULL,
    UNIQUE(annotation_id, target_type, target_id, relationship),
    CHECK (target_type IN ('issue', 'file', 'finding', 'observation')),
    CHECK (relationship IN ('relevant_to', 'must_consider', 'evidence_for', 'explains', 'created_from', 'promoted_to'))
);

CREATE INDEX IF NOT EXISTS idx_annotation_links_annotation ON annotation_links(annotation_id);
CREATE INDEX IF NOT EXISTS idx_annotation_links_target ON annotation_links(target_type, target_id, relationship);

CREATE TABLE IF NOT EXISTS annotation_events (
    id            TEXT PRIMARY KEY,
    annotation_id TEXT NOT NULL REFERENCES annotations(id) ON DELETE CASCADE,
    event_type    TEXT NOT NULL,
    actor         TEXT DEFAULT '',
    reason        TEXT DEFAULT '',
    old_value     TEXT,
    new_value     TEXT,
    target_type   TEXT DEFAULT '',
    target_id     TEXT DEFAULT '',
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_annotation_events_annotation ON annotation_events(annotation_id, created_at);

CREATE TABLE IF NOT EXISTS annotation_closeout_acknowledgements (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    annotation_id         TEXT NOT NULL REFERENCES annotations(id) ON DELETE CASCADE,
    target_type           TEXT NOT NULL DEFAULT 'issue',
    target_id             TEXT NOT NULL,
    carried_to_target_id  TEXT DEFAULT '',
    actor                 TEXT DEFAULT '',
    reason                TEXT DEFAULT '',
    acknowledged_at       TEXT NOT NULL,
    UNIQUE(annotation_id, target_type, target_id),
    CHECK (target_type IN ('issue'))
);

CREATE INDEX IF NOT EXISTS idx_annotation_closeout_ack_target
  ON annotation_closeout_acknowledgements(target_type, target_id);

-- ---- Cross-product entity associations (ADR-029) --------------------------
-- Binds Filigree issues to Clarion entities (functions, classes, modules).
-- The clarion_entity_id is OPAQUE to Filigree — no grammar parsing, no
-- CHECK constraint on its shape. Filigree does not know what a "Clarion
-- entity" is; it stores the ID as a string and hands content_hash back at
-- query time so Clarion can detect drift. This preserves the federation
-- enrich-only rule (loom.md §5).

CREATE TABLE IF NOT EXISTS entity_associations (
    issue_id                TEXT NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    clarion_entity_id       TEXT NOT NULL,
    content_hash_at_attach  TEXT NOT NULL,
    attached_at             TEXT NOT NULL,
    attached_by             TEXT NOT NULL,
    PRIMARY KEY (issue_id, clarion_entity_id)
);

CREATE INDEX IF NOT EXISTS ix_entity_assoc_entity
  ON entity_associations(clarion_entity_id);
"""

# V1 schema (without file tables) — kept for migration tests.
# Defined as a standalone constant to avoid brittle string-split coupling.
SCHEMA_V1_SQL = """\
CREATE TABLE IF NOT EXISTS issues (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    priority    INTEGER NOT NULL DEFAULT 2,
    type        TEXT NOT NULL DEFAULT 'task',
    parent_id   TEXT REFERENCES issues(id) ON DELETE SET NULL,
    assignee    TEXT DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    closed_at   TEXT,
    description TEXT DEFAULT '',
    notes       TEXT DEFAULT '',
    fields      TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_issues_status ON issues(status);
CREATE INDEX IF NOT EXISTS idx_issues_type ON issues(type);
CREATE INDEX IF NOT EXISTS idx_issues_parent ON issues(parent_id);
CREATE INDEX IF NOT EXISTS idx_issues_priority ON issues(priority);
CREATE INDEX IF NOT EXISTS idx_issues_status_priority ON issues(status, priority, created_at);

CREATE TABLE IF NOT EXISTS dependencies (
    issue_id       TEXT NOT NULL REFERENCES issues(id),
    depends_on_id  TEXT NOT NULL REFERENCES issues(id),
    type           TEXT NOT NULL DEFAULT 'blocks',
    created_at     TEXT NOT NULL,
    PRIMARY KEY (issue_id, depends_on_id)
);

CREATE INDEX IF NOT EXISTS idx_deps_depends_on ON dependencies(depends_on_id);
CREATE INDEX IF NOT EXISTS idx_deps_issue_depends ON dependencies(issue_id, depends_on_id);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id   TEXT NOT NULL REFERENCES issues(id),
    event_type TEXT NOT NULL,
    actor      TEXT DEFAULT '',
    old_value  TEXT,
    new_value  TEXT,
    comment    TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_issue ON events(issue_id);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_events_issue_time ON events(issue_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_dedup
  ON events(issue_id, event_type, actor,
    coalesce(old_value,''), coalesce(new_value,''), created_at);

CREATE TABLE IF NOT EXISTS comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id   TEXT NOT NULL REFERENCES issues(id),
    author     TEXT DEFAULT '',
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_comments_issue ON comments(issue_id, created_at);

CREATE TABLE IF NOT EXISTS labels (
    issue_id TEXT NOT NULL REFERENCES issues(id),
    label    TEXT NOT NULL,
    PRIMARY KEY (issue_id, label)
);

CREATE TABLE IF NOT EXISTS type_templates (
    type          TEXT PRIMARY KEY,
    pack          TEXT NOT NULL DEFAULT 'core',
    definition    TEXT NOT NULL,
    is_builtin    BOOLEAN NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS packs (
    name          TEXT PRIMARY KEY,
    version       TEXT NOT NULL,
    definition    TEXT NOT NULL,
    is_builtin    BOOLEAN NOT NULL DEFAULT 0,
    enabled       BOOLEAN NOT NULL DEFAULT 1
);

-- FTS5 full-text search with sync triggers
CREATE VIRTUAL TABLE IF NOT EXISTS issues_fts USING fts5(
    title, description, content='issues', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS issues_fts_insert AFTER INSERT ON issues BEGIN
    INSERT INTO issues_fts(rowid, title, description) VALUES (new.rowid, new.title, new.description);
END;
CREATE TRIGGER IF NOT EXISTS issues_fts_update AFTER UPDATE OF title, description ON issues BEGIN
    INSERT INTO issues_fts(issues_fts, rowid, title, description)
        VALUES('delete', old.rowid, old.title, old.description);
    INSERT INTO issues_fts(rowid, title, description) VALUES (new.rowid, new.title, new.description);
END;
CREATE TRIGGER IF NOT EXISTS issues_fts_delete AFTER DELETE ON issues BEGIN
    INSERT INTO issues_fts(issues_fts, rowid, title, description)
        VALUES('delete', old.rowid, old.title, old.description);
END;
"""

CURRENT_SCHEMA_VERSION = 17
