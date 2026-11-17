"""Versioned SQLite schema.

Only additive, checked migrations belong here.  Production tables never use
``ON DELETE CASCADE``: history is evidence and must not disappear because a
parent row is touched.  Cross-entity ownership is enforced with foreign keys,
unique constraints and triggers where the table graph is circular.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Iterable

SCHEMA_VERSION = 4

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    checksum TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    display_id TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'CREATING','READY','RUNNING','PAUSING','PAUSED','BLOCKED','RECOVERY_REQUIRED','COMPLETED')),
    run_intent TEXT NOT NULL CHECK (run_intent IN ('STOPPED','RUN','PAUSE_SAFE','PAUSE_NOW')),
    protection_mode TEXT NOT NULL CHECK (protection_mode IN ('snapshot','reference')),
    workflow_ui_path TEXT NOT NULL,
    workflow_api_path TEXT NOT NULL,
    workflow_ui_sha256 TEXT NOT NULL,
    workflow_api_sha256 TEXT NOT NULL,
    bindings_json TEXT NOT NULL,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    plan_version INTEGER NOT NULL DEFAULT 1,
    row_version INTEGER NOT NULL DEFAULT 1,
    recovery_version INTEGER NOT NULL DEFAULT 0,
    data_root TEXT NOT NULL,
    results_root TEXT NOT NULL,
    job_dir TEXT NOT NULL,
    result_dir TEXT NOT NULL,
    current_error_code TEXT,
    current_error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    last_backup_draw_count INTEGER NOT NULL DEFAULT 0,
    last_backup_at TEXT
);

CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    source_order INTEGER NOT NULL,
    original_name TEXT NOT NULL,
    original_path TEXT,
    snapshot_path TEXT,
    staged_name TEXT,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    image_format TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','REMOVED_BY_USER','MISSING','CHANGED')),
    result_revision INTEGER NOT NULL DEFAULT 0,
    selection_version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_verified_at TEXT,
    removed_at TEXT,
    UNIQUE(job_id, source_order)
);

CREATE TABLE IF NOT EXISTS blocks (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    source_id TEXT NOT NULL REFERENCES sources(id),
    kind TEXT NOT NULL CHECK (kind IN ('INITIAL','APPEND')),
    priority INTEGER NOT NULL CHECK (priority IN (0,1,2)),
    enqueue_seq INTEGER NOT NULL,
    cursor INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','RUNNING','DONE','CANCELLED')),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(job_id, enqueue_seq)
);

CREATE TABLE IF NOT EXISTS draws (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    source_id TEXT NOT NULL REFERENCES sources(id),
    block_id TEXT NOT NULL REFERENCES blocks(id),
    draw_no INTEGER NOT NULL CHECK (draw_no >= 1),
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN (
        'PENDING','CLAIMED','DISPATCHING','QUEUED','RUNNING','VERIFYING',
        'SUCCEEDED','FAILED','INTERRUPTED','UNKNOWN','CANCELLED_BY_USER')),
    accepted_attempt_id TEXT,
    row_version INTEGER NOT NULL DEFAULT 1,
    failure_code TEXT,
    failure_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    succeeded_at TEXT,
    UNIQUE(source_id, draw_no)
);

CREATE TABLE IF NOT EXISTS seed_allocations (
    job_id TEXT NOT NULL REFERENCES jobs(id),
    draw_id TEXT NOT NULL REFERENCES draws(id),
    node_id TEXT NOT NULL,
    input_name TEXT NOT NULL,
    seed INTEGER NOT NULL CHECK (seed >= 0 AND seed <= 9007199254740991),
    created_at TEXT NOT NULL,
    PRIMARY KEY (draw_id, node_id, input_name),
    UNIQUE(job_id, seed)
);

CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY,
    draw_id TEXT NOT NULL REFERENCES draws(id),
    attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
    expected_prompt_id TEXT NOT NULL,
    actual_prompt_id TEXT,
    boot_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'CLAIMED','DISPATCHING','QUEUED','RUNNING','VERIFYING','SUCCEEDED',
        'FAILED','INTERRUPTED','UNKNOWN','RECONCILING','ABANDONED')),
    submission_intent_at TEXT,
    submitted_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    execution_hash TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    input_stage_name TEXT,
    attempt_dir TEXT NOT NULL,
    remote_state TEXT,
    reconcile_json TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(draw_id, attempt_no),
    UNIQUE(expected_prompt_id)
);

CREATE TABLE IF NOT EXISTS execution_slot (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    attempt_id TEXT REFERENCES attempts(id),
    owner TEXT,
    acquired_at TEXT,
    version INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS outputs (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES attempts(id),
    out_no INTEGER NOT NULL CHECK (out_no >= 1),
    rel_path TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    health TEXT NOT NULL CHECK (health IN ('STAGED','VERIFIED','PARTIAL','MISSING','CORRUPT','QUARANTINED')),
    created_at TEXT NOT NULL,
    verified_at TEXT,
    note TEXT,
    UNIQUE(attempt_id, out_no)
);

CREATE TABLE IF NOT EXISTS selections (
    output_id TEXT PRIMARY KEY REFERENCES outputs(id),
    selected INTEGER NOT NULL CHECK (selected IN (0,1)),
    viewed_at TEXT,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS source_reviews (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    result_revision INTEGER NOT NULL,
    selection_version INTEGER NOT NULL,
    plan_version INTEGER NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('SELECTED','SKIPPED')),
    reason TEXT,
    confirmed_at TEXT NOT NULL,
    UNIQUE(source_id, result_revision, selection_version, plan_version)
);

CREATE TABLE IF NOT EXISTS exports (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    kind TEXT NOT NULL CHECK (kind IN ('QUICK','FINAL')),
    layout TEXT NOT NULL CHECK (layout IN ('FLAT','GROUPED')),
    status TEXT NOT NULL CHECK (status IN (
        'DRAFT','FROZEN','COPYING','COMPLETED','PARTIAL_FAILED','RECOVERY_REQUIRED')),
    target_root TEXT NOT NULL,
    batch_dir TEXT,
    batch_name TEXT,
    frozen_plan_version INTEGER,
    cutoff_event_id INTEGER,
    cutoff_at TEXT,
    draft_version INTEGER NOT NULL DEFAULT 1,
    row_version INTEGER NOT NULL DEFAULT 1,
    manifest_hash TEXT,
    preview_json TEXT NOT NULL DEFAULT '{}',
    cutoff_summary_json TEXT NOT NULL DEFAULT '{}',
    item_count INTEGER NOT NULL DEFAULT 0,
    verified_count INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    frozen_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS export_reviews (
    export_id TEXT NOT NULL REFERENCES exports(id),
    source_id TEXT NOT NULL REFERENCES sources(id),
    decision TEXT NOT NULL CHECK (decision IN ('SELECTED','SKIPPED')),
    selection_version INTEGER NOT NULL,
    result_revision INTEGER NOT NULL,
    plan_version INTEGER NOT NULL,
    selected_output_ids_json TEXT NOT NULL DEFAULT '[]',
    pending_draws INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    confirmed_at TEXT NOT NULL,
    PRIMARY KEY (export_id, source_id)
);

CREATE TABLE IF NOT EXISTS export_items (
    id TEXT PRIMARY KEY,
    export_id TEXT NOT NULL REFERENCES exports(id),
    output_id TEXT NOT NULL REFERENCES outputs(id),
    source_sha256 TEXT NOT NULL,
    target_rel_path TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING','COPYING','VERIFIED','FAILED')),
    actual_sha256 TEXT,
    size INTEGER,
    error_code TEXT,
    error_message TEXT,
    copied_at TEXT,
    verified_at TEXT,
    UNIQUE(export_id, output_id),
    UNIQUE(export_id, target_rel_path)
);

CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT,
    entity_type TEXT NOT NULL,
    entity_id TEXT,
    action TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    reason TEXT,
    related_id TEXT,
    actor TEXT NOT NULL DEFAULT 'system',
    payload_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_requests (
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('IN_PROGRESS','COMPLETED','FAILED')),
    response_json TEXT,
    http_status INTEGER,
    entity_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);

CREATE TABLE IF NOT EXISTS backups (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    db_version INTEGER NOT NULL,
    schema_version INTEGER NOT NULL,
    checksum TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('VERIFIED','FAILED')),
    size INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    verified_at TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS runtime_meta (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sources_job ON sources(job_id, source_order);
CREATE INDEX IF NOT EXISTS idx_blocks_job ON blocks(job_id, status, priority, enqueue_seq);
CREATE INDEX IF NOT EXISTS idx_draws_status ON draws(job_id, status);
CREATE INDEX IF NOT EXISTS idx_draws_source ON draws(source_id, draw_no);
CREATE INDEX IF NOT EXISTS idx_attempts_status ON attempts(status);
CREATE INDEX IF NOT EXISTS idx_attempts_draw ON attempts(draw_id, attempt_no);
CREATE INDEX IF NOT EXISTS idx_outputs_attempt ON outputs(attempt_id, out_no);
CREATE INDEX IF NOT EXISTS idx_outputs_health ON outputs(health);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, seq);
CREATE INDEX IF NOT EXISTS idx_exports_job ON exports(job_id, created_at);
CREATE INDEX IF NOT EXISTS idx_export_items_export ON export_items(export_id, status);

CREATE UNIQUE INDEX IF NOT EXISTS uq_attempts_actual_prompt
    ON attempts(actual_prompt_id) WHERE actual_prompt_id IS NOT NULL;

-- A DRAW can accept exactly one successful attempt, and that attempt must
-- belong to the same DRAW and already be SUCCEEDED in this transaction.
CREATE TRIGGER IF NOT EXISTS trg_draw_accepted_attempt_insert
BEFORE UPDATE OF accepted_attempt_id ON draws
WHEN NEW.accepted_attempt_id IS NOT NULL AND (
    NOT EXISTS (
        SELECT 1 FROM attempts a
        WHERE a.id = NEW.accepted_attempt_id
          AND a.draw_id = NEW.id
          AND a.status = 'SUCCEEDED'
    )
)
BEGIN
    SELECT RAISE(ABORT, 'accepted attempt must belong to this draw and be SUCCEEDED');
END;

CREATE TRIGGER IF NOT EXISTS trg_draw_accepted_attempt_immutable
BEFORE UPDATE OF accepted_attempt_id ON draws
WHEN OLD.accepted_attempt_id IS NOT NULL
     AND NEW.accepted_attempt_id IS NOT OLD.accepted_attempt_id
BEGIN
    SELECT RAISE(ABORT, 'accepted attempt is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_draw_success_requires_accept
BEFORE UPDATE OF status ON draws
WHEN NEW.status = 'SUCCEEDED' AND NEW.accepted_attempt_id IS NULL
BEGIN
    SELECT RAISE(ABORT, 'a successful draw requires an accepted attempt');
END;

-- Selections may only target an output of the attempt accepted by its DRAW.
CREATE TRIGGER IF NOT EXISTS trg_selection_requires_success_insert
BEFORE INSERT ON selections
WHEN NOT EXISTS (
    SELECT 1 FROM outputs o
    JOIN attempts a ON a.id = o.attempt_id
    JOIN draws d ON d.id = a.draw_id
    WHERE o.id = NEW.output_id
      AND d.accepted_attempt_id = a.id
      AND d.status = 'SUCCEEDED'
      AND a.status = 'SUCCEEDED'
)
BEGIN
    SELECT RAISE(ABORT, 'selection requires an accepted successful output');
END;

CREATE TRIGGER IF NOT EXISTS trg_selection_requires_success_update
BEFORE UPDATE ON selections
WHEN NOT EXISTS (
    SELECT 1 FROM outputs o
    JOIN attempts a ON a.id = o.attempt_id
    JOIN draws d ON d.id = a.draw_id
    WHERE o.id = NEW.output_id
      AND d.accepted_attempt_id = a.id
      AND d.status = 'SUCCEEDED'
      AND a.status = 'SUCCEEDED'
)
BEGIN
    SELECT RAISE(ABORT, 'selection requires an accepted successful output');
END;

-- Export members are frozen only from healthy outputs of accepted attempts.
CREATE TRIGGER IF NOT EXISTS trg_export_item_requires_verified
BEFORE INSERT ON export_items
WHEN NOT EXISTS (
    SELECT 1 FROM outputs o
    JOIN attempts a ON a.id = o.attempt_id
    JOIN draws d ON d.id = a.draw_id
    WHERE o.id = NEW.output_id
      AND o.health = 'VERIFIED'
      AND d.accepted_attempt_id = a.id
      AND d.status = 'SUCCEEDED'
      AND a.status = 'SUCCEEDED'
)
BEGIN
    SELECT RAISE(ABORT, 'export item requires a verified accepted output');
END;

CREATE TRIGGER IF NOT EXISTS trg_events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

INSERT OR IGNORE INTO execution_slot(singleton_id, attempt_id, owner, acquired_at, version)
VALUES (1, NULL, NULL, NULL, 0);
"""


# ---------------------------------------------------------------------------
# Migration 2: persistent, resumable creation progress.
#
# Creation is a separate state machine from inference.  The row is written in
# the same transaction as the facts it counts (SOURCE / DRAW+seed), so a crash
# can never leave a counter that claims more work than is actually committed.
# ---------------------------------------------------------------------------
SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS creation_progress (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id),
    request_hash TEXT NOT NULL,
    stage TEXT NOT NULL CHECK (stage IN (
        'WAITING','COPYING','PLANNING','FINALIZING','PAUSING','PAUSED','DONE','FAILED','ABANDONED')),
    control_intent TEXT NOT NULL DEFAULT 'CONTINUE' CHECK (control_intent IN (
        'CONTINUE','STOP','ABANDON')),
    sources_total INTEGER NOT NULL DEFAULT 0 CHECK (sources_total >= 0),
    sources_committed INTEGER NOT NULL DEFAULT 0
        CHECK (sources_committed >= 0 AND sources_committed <= sources_total),
    draws_total INTEGER NOT NULL DEFAULT 0 CHECK (draws_total >= 0),
    draws_committed INTEGER NOT NULL DEFAULT 0
        CHECK (draws_committed >= 0 AND draws_committed <= draws_total),
    generation INTEGER NOT NULL DEFAULT 1 CHECK (generation >= 0),
    progress_version INTEGER NOT NULL DEFAULT 1 CHECK (progress_version >= 0),
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_creation_progress_stage
    ON creation_progress(stage, control_intent, created_at);
"""

# ---------------------------------------------------------------------------
# Migration 3: resumable result-review sessions.
#
# The browser is only a cache.  One durable session per job stores the review
# scope, the current source/result/page position, the last confirmed boundary
# and a bounded navigation history.  A group decision and the new boundary are
# written in the same transaction; a response retry is answered from the
# action receipt so a lost HTTP response cannot advance two groups.
# ---------------------------------------------------------------------------
SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS review_sessions (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
    scope TEXT NOT NULL DEFAULT 'pending' CHECK (scope IN ('pending','all')),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    break_type TEXT NOT NULL DEFAULT 'IN_GROUP'
        CHECK (break_type IN ('IN_GROUP','AFTER_CONFIRMED')),
    current_source_id TEXT REFERENCES sources(id),
    current_source_order INTEGER,
    current_output_id TEXT REFERENCES outputs(id),
    result_offset INTEGER NOT NULL DEFAULT 0 CHECK (result_offset >= 0),
    history_json TEXT NOT NULL DEFAULT '[]',
    confirmed_source_id TEXT REFERENCES sources(id),
    confirmed_source_order INTEGER,
    confirmed_decision TEXT CHECK (confirmed_decision IN ('SELECTED','SKIPPED')),
    confirmed_result_revision INTEGER,
    confirmed_selection_version INTEGER,
    confirmed_plan_version INTEGER,
    confirmed_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_session_actions (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES review_sessions(id),
    job_id TEXT NOT NULL REFERENCES jobs(id),
    action_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('DECISION')),
    source_id TEXT NOT NULL REFERENCES sources(id),
    decision TEXT NOT NULL CHECK (decision IN ('SELECTED','SKIPPED')),
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, action_id)
);

CREATE INDEX IF NOT EXISTS idx_review_sessions_job ON review_sessions(job_id);
CREATE INDEX IF NOT EXISTS idx_review_actions_session
    ON review_session_actions(session_id, created_at);
"""

# ---------------------------------------------------------------------------
# Migration 4: result retention and storage cleanup.
#
# ``outputs.health`` keeps recording what happened when the image was
# produced.  A deliberate user deletion is a separate retention fact: no row
# means RETAINED, and only CLEANING/PURGED hide an OUT from normal reads.
# The external per-job cleanup journal (``jobs/<job>/cleanup/...``) is the
# durable authority that survives restoring an older database; these tables
# are the indexed projection used by queries and the UI.
# ---------------------------------------------------------------------------
SCHEMA_V4 = """
CREATE TABLE IF NOT EXISTS output_retention (
    output_id TEXT PRIMARY KEY REFERENCES outputs(id),
    state TEXT NOT NULL CHECK (state IN ('RETAINED','CLEANING','PURGED')),
    cleanup_id TEXT,
    original_rel_path TEXT,
    sha256 TEXT,
    size INTEGER,
    reason TEXT,
    updated_at TEXT NOT NULL,
    purged_at TEXT
);

CREATE TABLE IF NOT EXISTS cleanup_runs (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    kind TEXT NOT NULL CHECK (kind IN ('UNSELECTED_RESULTS')),
    status TEXT NOT NULL CHECK (status IN (
        'PREVIEW','RUNNING','COMPLETED','PARTIAL_FAILED','RECOVERY_REQUIRED','EXPIRED','CANCELLED')),
    storage_instance_id TEXT,
    plan_version INTEGER NOT NULL,
    storage_revision INTEGER NOT NULL,
    plan_token_hash TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    protection_json TEXT NOT NULL DEFAULT '{}',
    candidate_count INTEGER NOT NULL DEFAULT 0,
    estimated_bytes INTEGER NOT NULL DEFAULT 0,
    estimated_thumbnail_bytes INTEGER NOT NULL DEFAULT 0,
    protected_selected_count INTEGER NOT NULL DEFAULT 0,
    protected_export_count INTEGER NOT NULL DEFAULT 0,
    deleted_files INTEGER NOT NULL DEFAULT 0,
    deleted_bytes INTEGER NOT NULL DEFAULT 0,
    deleted_thumbnail_bytes INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    all_skipped INTEGER NOT NULL DEFAULT 0 CHECK (all_skipped IN (0,1)),
    confirmation_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT,
    error_message TEXT,
    confirmed_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cleanup_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cleanup_id TEXT NOT NULL REFERENCES cleanup_runs(id),
    output_id TEXT NOT NULL REFERENCES outputs(id),
    source_id TEXT NOT NULL REFERENCES sources(id),
    draw_id TEXT NOT NULL REFERENCES draws(id),
    attempt_id TEXT NOT NULL REFERENCES attempts(id),
    source_order INTEGER NOT NULL,
    draw_no INTEGER NOT NULL,
    out_no INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    file_identity_json TEXT NOT NULL DEFAULT '{}',
    thumbnail_rel_path TEXT,
    thumbnail_size INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK (status IN ('PENDING','INTENT','DELETED','FAILED','SKIPPED')),
    error_code TEXT,
    error_message TEXT,
    intended_at TEXT,
    completed_at TEXT,
    UNIQUE(cleanup_id, output_id)
);

CREATE TABLE IF NOT EXISTS job_storage_state (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id),
    storage_revision INTEGER NOT NULL DEFAULT 0 CHECK (storage_revision >= 0),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS maintenance_state (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    holder TEXT,
    kind TEXT,
    cleanup_id TEXT,
    job_id TEXT,
    boot_id TEXT,
    acquired_at TEXT,
    version INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_output_retention_state ON output_retention(state);
CREATE INDEX IF NOT EXISTS idx_cleanup_runs_job ON cleanup_runs(job_id, created_at);
CREATE INDEX IF NOT EXISTS idx_cleanup_runs_status ON cleanup_runs(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_cleanup_items_run ON cleanup_items(cleanup_id, source_order, draw_no, out_no);
CREATE INDEX IF NOT EXISTS idx_cleanup_items_output ON cleanup_items(output_id);

INSERT OR IGNORE INTO maintenance_state(singleton_id, holder, kind, cleanup_id, job_id, boot_id, acquired_at, version)
VALUES (1, NULL, NULL, NULL, NULL, NULL, NULL, 0);
"""

SCHEMA_SCRIPTS: dict[int, str] = {1: SCHEMA_V1, 2: SCHEMA_V2, 3: SCHEMA_V3, 4: SCHEMA_V4}


def schema_checksum(version: int = 1) -> str:
    script = SCHEMA_SCRIPTS.get(int(version))
    if script is None:
        raise ValueError(f"unknown schema version {version}")
    return hashlib.sha256(script.encode("utf-8")).hexdigest()


def _migration_rows(conn: sqlite3.Connection) -> list[tuple[int, str, str]]:
    try:
        return list(conn.execute("SELECT version, applied_at, checksum FROM schema_migrations ORDER BY version"))
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return []
        raise


def current_schema_version(conn: sqlite3.Connection) -> int:
    rows = _migration_rows(conn)
    return max((row[0] for row in rows), default=0)


def _sql_statements(script: str) -> list[str]:
    """Split a SQL script into complete statements for transactional apply."""
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                statements.append(statement)
            buffer = ""
    if buffer.strip():
        statements.append(buffer.strip())
    return statements


def apply_migrations(conn: sqlite3.Connection, now: str) -> int:
    """Apply all known migrations.  Returns the final schema version.

    Existing migration rows are verified against the exact script checksum
    before any new migration is attempted: a modified old definition must stop
    startup instead of silently diverging.  Each version is applied in its own
    transaction; a failure leaves the previous version intact.
    """
    rows = _migration_rows(conn)
    existing = {row[0]: row for row in rows}
    highest = max(existing, default=0)
    if highest > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema {highest} is newer than code schema {SCHEMA_VERSION}"
        )
    for version, row in existing.items():
        if version in SCHEMA_SCRIPTS and row[2] != schema_checksum(version):
            raise RuntimeError(
                f"database migration {version} checksum does not match this code"
            )
    for version in sorted(SCHEMA_SCRIPTS):
        if version in existing:
            continue
        try:
            conn.execute("BEGIN IMMEDIATE")
            for statement in _sql_statements(SCHEMA_SCRIPTS[version]):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at, checksum) VALUES (?, ?, ?)",
                (version, now, schema_checksum(version)),
            )
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
    return current_schema_version(conn)


def table_names(conn: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
    ]
