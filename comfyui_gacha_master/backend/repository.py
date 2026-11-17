"""Parameterized SQL and transaction-level invariants.

The repository never opens its own transactions around filesystem work.  The
service prepares input, seeds and attempt directories first, then calls the
short transactional methods here.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from typing import Any, Iterable, Mapping, Optional, Sequence

from .db import Database
from .errors import GachaError, conflict, not_found
from .states import (
    AttemptStatus,
    DrawStatus,
    JobStatus,
    OutputHealth,
    RunIntent,
    SourceStatus,
    check_attempt,
    check_draw,
    check_job,
)
from .util import natural_sort_key, parse_int, utc_now


def _dict(row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
    return dict(row) if row is not None else None


# A retained OUT is still present by evidence.  CLEANING is a short window
# where a durable delete intent exists; PURGED is a completed, journaled
# deletion.  Both are unavailable for review, export, selection and images.
RETENTION_AVAILABLE = (
    "NOT EXISTS (SELECT 1 FROM output_retention ret "
    "WHERE ret.output_id=o.id AND ret.state IN ('CLEANING','PURGED'))"
)
RETENTION_STATE_SQL = "COALESCE(ret.state,'RETAINED')"


class Repository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ---------------------------------------------------------------- jobs
    def create_job(self, record: Mapping[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self.db.tx() as conn:
            conn.execute(
                """
                INSERT INTO jobs(
                    id, display_id, display_name, status, run_intent, protection_mode,
                    workflow_ui_path, workflow_api_path, workflow_ui_sha256, workflow_api_sha256,
                    bindings_json, warnings_json, plan_version, row_version, recovery_version,
                    data_root, results_root, job_dir, result_dir,
                    created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record["id"],
                    record["display_id"],
                    record["display_name"],
                    record.get("status", JobStatus.CREATING),
                    record.get("run_intent", RunIntent.STOPPED),
                    record["protection_mode"],
                    record["workflow_ui_path"],
                    record["workflow_api_path"],
                    record["workflow_ui_sha256"],
                    record["workflow_api_sha256"],
                    json.dumps(record["bindings"], ensure_ascii=False, sort_keys=True),
                    json.dumps(record.get("warnings", []), ensure_ascii=False),
                    int(record.get("plan_version", 1)),
                    1,
                    int(record.get("recovery_version", 0)),
                    record["data_root"],
                    record["results_root"],
                    record["job_dir"],
                    record["result_dir"],
                    now,
                    now,
                ),
            )
            self._event(
                conn,
                job_id=record["id"],
                entity_type="JOB",
                entity_id=record["id"],
                action="JOB_CREATED",
                to_state=record.get("status", JobStatus.CREATING),
                actor="user",
            )
        job = self.get_job(record["id"])
        assert job is not None
        return job

    def get_job(self, job_id: str) -> Optional[dict[str, Any]]:
        row = self.db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return self._decode_job(_dict(row))

    def all_jobs(self) -> list[dict[str, Any]]:
        rows = self.db.query_all("SELECT * FROM jobs ORDER BY created_at")
        return [self._decode_job(dict(row)) for row in rows]

    def get_job_by_display_id(self, display_id: str) -> Optional[dict[str, Any]]:
        row = self.db.query_one("SELECT * FROM jobs WHERE display_id = ?", (display_id,))
        return self._decode_job(_dict(row))

    def list_jobs(
        self,
        *,
        limit: int = 50,
        cursor_created_at: Optional[str] = None,
        cursor_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        if cursor_created_at and cursor_id:
            rows = self.db.query_all(
                """
                SELECT * FROM jobs
                WHERE (created_at < ?) OR (created_at = ? AND id < ?)
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                (cursor_created_at, cursor_created_at, cursor_id, limit),
            )
        else:
            rows = self.db.query_all(
                "SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
            )
        return [self._decode_job(dict(row)) for row in rows]

    def update_job(
        self,
        job_id: str,
        *,
        expected_version: Optional[int] = None,
        actor: str = "system",
        reason: Optional[str] = None,
        **fields: Any,
    ) -> dict[str, Any]:
        allowed = {
            "display_name",
            "status",
            "run_intent",
            "plan_version",
            "recovery_version",
            "current_error_code",
            "current_error_message",
            "started_at",
            "completed_at",
            "last_backup_draw_count",
            "last_backup_at",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise GachaError("INTERNAL_ERROR", f"不允许更新 JOB 字段: {sorted(unknown)}")
        with self.db.tx() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise not_found("任务不存在。", job_id=job_id)
            current = dict(row)
            if expected_version is not None and int(current["row_version"]) != int(expected_version):
                raise conflict(
                    "任务版本已变化，请刷新后重试。",
                    job_id=job_id,
                    current_version=current["row_version"],
                    expected_version=expected_version,
                )
            if "status" in fields and fields["status"] != current["status"]:
                check_job(current["status"], fields["status"])
            if "run_intent" in fields and fields["run_intent"] not in RunIntent.ALL:
                raise GachaError("BAD_REQUEST", "run_intent 无效。")
            assignments = []
            params: list[Any] = []
            for key, value in fields.items():
                assignments.append(f"{key} = ?")
                params.append(value)
            assignments.append("row_version = row_version + 1")
            assignments.append("updated_at = ?")
            params.append(utc_now())
            params.append(job_id)
            conn.execute(f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?", params)
            if "status" in fields and fields["status"] != current["status"]:
                self._event(
                    conn,
                    job_id=job_id,
                    entity_type="JOB",
                    entity_id=job_id,
                    action="JOB_STATUS",
                    from_state=current["status"],
                    to_state=fields["status"],
                    reason=reason,
                    actor=actor,
                )
            if "run_intent" in fields and fields["run_intent"] != current["run_intent"]:
                self._event(
                    conn,
                    job_id=job_id,
                    entity_type="JOB",
                    entity_id=job_id,
                    action="RUN_INTENT",
                    from_state=current["run_intent"],
                    to_state=fields["run_intent"],
                    reason=reason,
                    actor=actor,
                )
        job = self.get_job(job_id)
        assert job is not None
        return job

    def set_job_error(
        self, job_id: str, code: Optional[str], message: Optional[str], *, actor: str = "system"
    ) -> None:
        self.update_job(
            job_id,
            current_error_code=code,
            current_error_message=message,
            actor=actor,
            reason=code,
        )

    def active_run_job(self) -> Optional[dict[str, Any]]:
        row = self.db.query_one(
            "SELECT * FROM jobs WHERE run_intent = ? ORDER BY updated_at DESC LIMIT 1",
            (RunIntent.RUN,),
        )
        return self._decode_job(_dict(row))

    # ------------------------------------------------------- creation state
    def get_creation(self, job_id: str) -> Optional[dict[str, Any]]:
        return _dict(self.db.query_one("SELECT * FROM creation_progress WHERE job_id = ?", (job_id,)))

    def insert_creation(
        self,
        *,
        job_id: str,
        request_hash: str,
        sources_total: int,
        draws_total: int,
        stage: str = "WAITING",
        generation: int = 1,
        conn: Optional[sqlite3.Connection] = None,
    ) -> dict[str, Any]:
        now = utc_now()
        sql = """
            INSERT INTO creation_progress(
                job_id, request_hash, stage, control_intent,
                sources_total, sources_committed, draws_total, draws_committed,
                generation, progress_version, created_at, updated_at
            ) VALUES (?,?,?,'CONTINUE',?,0,?,0,?,1,?,?)
        """
        params = (job_id, request_hash, stage, int(sources_total), int(draws_total), int(generation), now, now)
        if conn is None:
            with self.db.tx() as tx:
                tx.execute(sql, params)
        else:
            conn.execute(sql, params)
        return _dict(self.db.query_one("SELECT * FROM creation_progress WHERE job_id = ?", (job_id,))) or {}

    def list_creation_active(self) -> list[dict[str, Any]]:
        rows = self.db.query_all(
            "SELECT p.*, j.status AS job_status FROM creation_progress p "
            "JOIN jobs j ON j.id = p.job_id "
            "WHERE j.status = 'CREATING' AND p.control_intent = 'CONTINUE' "
            "AND p.stage IN ('WAITING','COPYING','PLANNING','FINALIZING') "
            "ORDER BY p.created_at LIMIT 1"
        )
        return [dict(row) for row in rows]

    def list_creation_for_creating_jobs(self) -> list[dict[str, Any]]:
        rows = self.db.query_all(
            "SELECT p.*, j.status AS job_status FROM creation_progress p "
            "JOIN jobs j ON j.id = p.job_id WHERE j.status = 'CREATING' ORDER BY p.created_at"
        )
        return [dict(row) for row in rows]

    # ------------------------------------------------------------- sources
    def insert_source(
        self,
        record: Mapping[str, Any],
        conn: Optional[sqlite3.Connection] = None,
    ) -> dict[str, Any]:
        now = utc_now()
        if conn is not None:
            self._insert_source(conn, record, now)
            row = conn.execute("SELECT * FROM sources WHERE id = ?", (record["id"],)).fetchone()
            return dict(row) if row else {}
        with self.db.tx() as tx:
            self._insert_source(tx, record, now)
        return self.get_source(record["id"])  # type: ignore[return-value]

    def _insert_source(self, conn: sqlite3.Connection, record: Mapping[str, Any], now: str) -> None:
        conn.execute(
            """
            INSERT INTO sources(
                id, job_id, source_order, original_name, original_path, snapshot_path,
                staged_name, size, sha256, width, height, image_format, status,
                result_revision, selection_version, created_at, last_verified_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                record["id"],
                record["job_id"],
                int(record["source_order"]),
                record["original_name"],
                record.get("original_path"),
                record.get("snapshot_path"),
                record.get("staged_name"),
                int(record["size"]),
                record["sha256"],
                int(record["width"]),
                int(record["height"]),
                record["image_format"],
                record.get("status", SourceStatus.ACTIVE),
                int(record.get("result_revision", 0)),
                int(record.get("selection_version", 0)),
                now,
                now,
            ),
        )
        self._event(
            conn,
            job_id=record["job_id"],
            entity_type="SRC",
            entity_id=record["id"],
            action="SOURCE_ADDED",
            to_state=record.get("status", SourceStatus.ACTIVE),
            actor="user",
            payload={"source_order": record["source_order"]},
        )

    def get_source(self, source_id: str) -> Optional[dict[str, Any]]:
        return _dict(self.db.query_one("SELECT * FROM sources WHERE id = ?", (source_id,)))

    def get_source_for_update(self, conn: sqlite3.Connection, source_id: str) -> Optional[dict[str, Any]]:
        return _dict(conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone())

    def list_sources(
        self, job_id: str, *, limit: int = 200, after_order: int = 0, include_removed: bool = False
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        if include_removed:
            rows = self.db.query_all(
                "SELECT * FROM sources WHERE job_id = ? AND source_order > ? ORDER BY source_order LIMIT ?",
                (job_id, int(after_order), limit),
            )
        else:
            rows = self.db.query_all(
                "SELECT * FROM sources WHERE job_id = ? AND source_order > ? AND status != 'REMOVED_BY_USER' "
                "ORDER BY source_order LIMIT ?",
                (job_id, int(after_order), limit),
            )
        return [dict(row) for row in rows]

    def all_sources(self, job_id: str, *, include_removed: bool = False) -> list[dict[str, Any]]:
        if include_removed:
            rows = self.db.query_all("SELECT * FROM sources WHERE job_id = ? ORDER BY source_order", (job_id,))
        else:
            rows = self.db.query_all(
                "SELECT * FROM sources WHERE job_id = ? AND status != ? ORDER BY source_order",
                (job_id, SourceStatus.REMOVED_BY_USER),
            )
        return [dict(row) for row in rows]

    def update_source(
        self,
        source_id: str,
        *,
        expected_revision: Optional[int] = None,
        **fields: Any,
    ) -> dict[str, Any]:
        allowed = {
            "status",
            "original_path",
            "last_verified_at",
            "removed_at",
            "result_revision",
            "selection_version",
            "staged_name",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise GachaError("INTERNAL_ERROR", f"不允许更新 SRC 字段: {sorted(unknown)}")
        with self.db.tx() as conn:
            row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
            if row is None:
                raise not_found("原图不存在。", source_id=source_id)
            current = dict(row)
            if expected_revision is not None and int(current["result_revision"]) != int(expected_revision):
                raise conflict(
                    "原图版本已变化，请刷新后重试。",
                    source_id=source_id,
                    current_revision=current["result_revision"],
                )
            assignments = [f"{key} = ?" for key in fields]
            params = list(fields.values())
            params.append(source_id)
            conn.execute(f"UPDATE sources SET {', '.join(assignments)} WHERE id = ?", params)
        return self.get_source(source_id)  # type: ignore[return-value]

    def bump_source_result_revision(self, conn: sqlite3.Connection, source_id: str) -> int:
        conn.execute(
            "UPDATE sources SET result_revision = result_revision + 1, last_verified_at = ? WHERE id = ?",
            (utc_now(), source_id),
        )
        row = conn.execute("SELECT result_revision FROM sources WHERE id = ?", (source_id,)).fetchone()
        return int(row[0]) if row else 0

    def bump_source_selection_version(self, conn: sqlite3.Connection, source_id: str) -> int:
        conn.execute(
            "UPDATE sources SET selection_version = selection_version + 1 WHERE id = ?",
            (source_id,),
        )
        row = conn.execute("SELECT selection_version FROM sources WHERE id = ?", (source_id,)).fetchone()
        return int(row[0]) if row else 0

    # -------------------------------------------------------------- blocks
    def next_block_seq(self, conn: sqlite3.Connection, job_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(enqueue_seq), 0) + 1 FROM blocks WHERE job_id = ?", (job_id,)
        ).fetchone()
        return int(row[0])

    def insert_block(self, record: Mapping[str, Any], conn: Optional[sqlite3.Connection] = None) -> dict[str, Any]:
        now = utc_now()
        if conn is None:
            with self.db.tx() as tx:
                self._insert_block(tx, record, now)
        else:
            self._insert_block(conn, record, now)
        row = self.db.query_one("SELECT * FROM blocks WHERE id = ?", (record["id"],))
        return dict(row) if row else {}

    def _insert_block(self, conn: sqlite3.Connection, record: Mapping[str, Any], now: str) -> None:
        conn.execute(
            """
            INSERT INTO blocks(id, job_id, source_id, kind, priority, enqueue_seq, cursor, status, created_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                record["id"],
                record["job_id"],
                record["source_id"],
                record["kind"],
                int(record["priority"]),
                int(record["enqueue_seq"]),
                int(record.get("cursor", 0)),
                record.get("status", "PENDING"),
                now,
            ),
        )

    def get_block(self, block_id: str) -> Optional[dict[str, Any]]:
        return _dict(self.db.query_one("SELECT * FROM blocks WHERE id = ?", (block_id,)))

    def list_blocks(self, job_id: str) -> list[dict[str, Any]]:
        rows = self.db.query_all("SELECT * FROM blocks WHERE job_id = ? ORDER BY enqueue_seq", (job_id,))
        return [dict(row) for row in rows]

    # --------------------------------------------------------------- draws
    def insert_draws_batch(self, records: Sequence[Mapping[str, Any]], conn: Optional[sqlite3.Connection] = None) -> None:
        now = utc_now()
        params = [
            (
                record["id"],
                record["job_id"],
                record["source_id"],
                record["block_id"],
                int(record["draw_no"]),
                record.get("status", DrawStatus.PENDING),
                int(record.get("row_version", 1)),
                now,
                now,
            )
            for record in records
        ]
        if conn is None:
            with self.db.tx() as tx:
                tx.executemany(
                    """
                    INSERT INTO draws(id, job_id, source_id, block_id, draw_no, status, row_version, created_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    params,
                )
        else:
            conn.executemany(
                """
                INSERT INTO draws(id, job_id, source_id, block_id, draw_no, status, row_version, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                params,
            )

    def max_draw_no(self, source_id: str) -> int:
        row = self.db.query_one("SELECT COALESCE(MAX(draw_no), 0) FROM draws WHERE source_id = ?", (source_id,))
        return int(row[0]) if row else 0

    def get_draw(self, draw_id: str) -> Optional[dict[str, Any]]:
        return _dict(self.db.query_one("SELECT * FROM draws WHERE id = ?", (draw_id,)))

    def count_draws(self, job_id: str) -> int:
        row = self.db.query_one("SELECT COUNT(*) FROM draws WHERE job_id = ?", (job_id,))
        return int(row[0]) if row else 0

    def next_executable_draw(self, job_id: str) -> Optional[dict[str, Any]]:
        """Select the next PENDING DRAW using documented block ordering.

        1. an already-started APPEND block keeps its continuity;
        2. otherwise the oldest pending PRIORITY append block;
        3. otherwise the oldest INITIAL block with work left (source order);
        4. otherwise the oldest pending TAIL append block.
        """
        row = self.db.query_one(
            """
            SELECT d.*, b.kind AS block_kind, b.priority AS block_priority,
                   b.enqueue_seq AS block_seq, b.status AS block_status
            FROM draws d
            JOIN blocks b ON b.id = d.block_id
            WHERE d.job_id = ?
              AND d.status = 'PENDING'
              AND b.status != 'CANCELLED'
            ORDER BY
              CASE
                WHEN b.kind = 'APPEND' AND b.status = 'RUNNING' THEN 0
                WHEN b.kind = 'APPEND' AND b.priority = 0 THEN 1
                WHEN b.kind = 'INITIAL' THEN 2
                WHEN b.kind = 'APPEND' THEN 3
                ELSE 4
              END,
              b.enqueue_seq ASC,
              d.draw_no ASC
            LIMIT 1
            """,
            (job_id,),
        )
        return _dict(row)

    def pending_draw_count(self, job_id: str) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) FROM draws d JOIN blocks b ON b.id=d.block_id "
            "WHERE d.job_id=? AND d.status='PENDING' AND b.status!='CANCELLED'",
            (job_id,),
        )
        return int(row[0]) if row else 0

    def refresh_block_state(self, conn: sqlite3.Connection, block_id: str) -> None:
        row = conn.execute(
            "SELECT COUNT(*) FROM draws WHERE block_id = ? AND status = 'PENDING'", (block_id,)
        ).fetchone()
        pending = int(row[0]) if row else 0
        block = conn.execute("SELECT status FROM blocks WHERE id = ?", (block_id,)).fetchone()
        if block is None:
            return
        if pending == 0 and block["status"] in ("RUNNING", "PENDING"):
            conn.execute(
                "UPDATE blocks SET status='DONE', completed_at=? WHERE id=?", (utc_now(), block_id)
            )

    # ---------------------------------------------------------------- seeds
    def allocate_seed(
        self,
        conn: sqlite3.Connection,
        *,
        job_id: str,
        draw_id: str,
        node_id: str,
        input_name: str,
        seed_max: int,
    ) -> int:
        for _ in range(64):
            candidate = secrets.randbelow(seed_max + 1)
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO seed_allocations(job_id, draw_id, node_id, input_name, seed, created_at)
                VALUES (?,?,?,?,?,?)
                """,
                (job_id, draw_id, node_id, input_name, candidate, utc_now()),
            )
            if cursor.rowcount == 1:
                return candidate
        raise GachaError("SEED_BINDING_INVALID", "无法分配任务内唯一种子，请扩大种子范围或减少计划。")

    def get_seed_map(self, draw_id: str) -> dict[str, int]:
        rows = self.db.query_all(
            "SELECT node_id, input_name, seed FROM seed_allocations WHERE draw_id = ?", (draw_id,)
        )
        return {f"{row['node_id']}::{row['input_name']}": int(row["seed"]) for row in rows}

    def seed_rows(self, draw_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.query_all(
            "SELECT * FROM seed_allocations WHERE draw_id = ? ORDER BY node_id, input_name", (draw_id,)
        )]

    # ------------------------------------------------------------- attempts
    def claim_draw(
        self,
        *,
        draw_id: str,
        attempt_id: str,
        boot_id: str,
        execution_hash: str,
        source_hash: str,
        attempt_dir: str,
        input_stage_name: Optional[str],
        slot_owner: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.db.tx() as conn:
            draw = conn.execute("SELECT * FROM draws WHERE id = ?", (draw_id,)).fetchone()
            if draw is None:
                raise not_found("DRAW 不存在。", draw_id=draw_id)
            # The maintenance boundary is re-checked inside BEGIN IMMEDIATE so
            # a cleanup that started after the scheduler's optimistic check
            # cannot overlap a claim.
            self.assert_maintenance_free(conn=conn)
            if draw["status"] != DrawStatus.PENDING:
                raise conflict(
                    "DRAW 已不在待执行状态。",
                    code="STATE_CONFLICT",
                    draw_id=draw_id,
                    current_status=draw["status"],
                )
            job = conn.execute("SELECT * FROM jobs WHERE id = ?", (draw["job_id"],)).fetchone()
            if job is None:
                raise not_found("任务不存在。", job_id=draw["job_id"])
            if job["status"] != JobStatus.RUNNING or job["run_intent"] != RunIntent.RUN:
                raise conflict(
                    "任务已不再授权新的提交（暂停意图已生效）。",
                    code="STATE_CONFLICT",
                    job_id=job["id"],
                    job_status=job["status"],
                    run_intent=job["run_intent"],
                )
            source = conn.execute("SELECT status FROM sources WHERE id = ?", (draw["source_id"],)).fetchone()
            if source is None or source["status"] != SourceStatus.ACTIVE:
                raise GachaError(
                    "INPUT_CHANGED",
                    "原图已移除或不可用，不能领取新的 DRAW。",
                    details={"source_id": draw["source_id"]},
                )
            slot = conn.execute("SELECT * FROM execution_slot WHERE singleton_id = 1").fetchone()
            if slot is not None and slot["attempt_id"]:
                raise conflict(
                    "已有插件执行占用全局槽。",
                    code="STATE_CONFLICT",
                    occupant=slot["attempt_id"],
                )
            attempt_no_row = conn.execute(
                "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM attempts WHERE draw_id = ?", (draw_id,)
            ).fetchone()
            attempt_no = int(attempt_no_row[0])
            expected_prompt_id = attempt_id
            conn.execute(
                """
                INSERT INTO attempts(
                    id, draw_id, attempt_no, expected_prompt_id, boot_id, status,
                    submission_intent_at, execution_hash, source_hash, input_stage_name,
                    attempt_dir, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    attempt_id,
                    draw_id,
                    attempt_no,
                    expected_prompt_id,
                    boot_id,
                    AttemptStatus.DISPATCHING,
                    now,
                    execution_hash,
                    source_hash,
                    input_stage_name,
                    attempt_dir,
                    now,
                    now,
                ),
            )
            conn.execute(
                "UPDATE draws SET status = ?, row_version = row_version + 1, updated_at = ? WHERE id = ?",
                (DrawStatus.DISPATCHING, now, draw_id),
            )
            conn.execute(
                """
                UPDATE execution_slot
                SET attempt_id = ?, owner = ?, acquired_at = ?, version = version + 1
                WHERE singleton_id = 1
                """,
                (attempt_id, slot_owner, now),
            )
            if draw["block_id"]:
                conn.execute(
                    "UPDATE blocks SET status = 'RUNNING' WHERE id = ? AND status = 'PENDING'",
                    (draw["block_id"],),
                )
            self._event(
                conn,
                job_id=draw["job_id"],
                entity_type="ATTEMPT",
                entity_id=attempt_id,
                action="ATTEMPT_DISPATCHING",
                from_state=None,
                to_state=AttemptStatus.DISPATCHING,
                related_id=draw_id,
                actor="scheduler",
                payload={"attempt_no": attempt_no, "expected_prompt_id": expected_prompt_id},
            )
            # F01: crash before the claim transaction commits.  SQLite must
            # roll the claim/attempt/slot back on restart.
            from .util import fault_point

            fault_point("F01")
        attempt = self.get_attempt(attempt_id)
        assert attempt is not None
        return attempt

    def get_attempt(self, attempt_id: str) -> Optional[dict[str, Any]]:
        return _dict(self.db.query_one("SELECT * FROM attempts WHERE id = ?", (attempt_id,)))

    def get_attempt_for_draw(self, draw_id: str) -> list[dict[str, Any]]:
        rows = self.db.query_all(
            "SELECT * FROM attempts WHERE draw_id = ? ORDER BY attempt_no", (draw_id,)
        )
        return [dict(row) for row in rows]

    def next_attempt_no(self, draw_id: str) -> int:
        row = self.db.query_one(
            "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM attempts WHERE draw_id = ?", (draw_id,)
        )
        return int(row[0]) if row else 1

    def list_active_attempts(self) -> list[dict[str, Any]]:
        rows = self.db.query_all(
            "SELECT * FROM attempts WHERE status IN ('CLAIMED','DISPATCHING','QUEUED','RUNNING','VERIFYING','RECONCILING','UNKNOWN') "
            "ORDER BY created_at"
        )
        return [dict(row) for row in rows]

    def list_unknown_attempts(self, *, job_id: Optional[str] = None) -> list[dict[str, Any]]:
        if job_id:
            rows = self.db.query_all(
                "SELECT a.*, d.job_id AS job_id FROM attempts a JOIN draws d ON d.id=a.draw_id "
                "WHERE a.status='UNKNOWN' AND d.job_id=? ORDER BY a.created_at",
                (job_id,),
            )
        else:
            rows = self.db.query_all(
                "SELECT a.*, d.job_id AS job_id FROM attempts a JOIN draws d ON d.id=a.draw_id "
                "WHERE a.status='UNKNOWN' ORDER BY a.created_at"
            )
        return [dict(row) for row in rows]

    def update_attempt(
        self,
        attempt_id: str,
        *,
        status: Optional[str] = None,
        actual_prompt_id: Optional[str] = None,
        submitted_at: Optional[str] = None,
        started_at: Optional[str] = None,
        finished_at: Optional[str] = None,
        remote_state: Optional[str] = None,
        reconcile_json: Optional[str] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        draw_status: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        row = self.get_attempt(attempt_id)
        if row is None:
            raise not_found("尝试不存在。", attempt_id=attempt_id)
        if status is not None and status != row["status"]:
            check_attempt(row["status"], status)
        now = utc_now()
        assignments: list[str] = ["updated_at = ?"]
        params: list[Any] = [now]
        for key, value in (
            ("status", status),
            ("actual_prompt_id", actual_prompt_id),
            ("submitted_at", submitted_at),
            ("started_at", started_at),
            ("finished_at", finished_at),
            ("remote_state", remote_state),
            ("reconcile_json", reconcile_json),
            ("error_code", error_code),
            ("error_message", error_message),
        ):
            if value is not None:
                assignments.append(f"{key} = ?")
                params.append(value)
        params.append(attempt_id)
        with self.db.tx() as tx:
            tx.execute(f"UPDATE attempts SET {', '.join(assignments)} WHERE id = ?", params)
            if draw_status is not None:
                draw = tx.execute("SELECT * FROM draws WHERE id = ?", (row["draw_id"],)).fetchone()
                if draw is not None and draw["status"] != draw_status:
                    check_draw(draw["status"], draw_status)
                    tx.execute(
                        "UPDATE draws SET status = ?, row_version = row_version + 1, updated_at = ? WHERE id = ?",
                        (draw_status, now, row["draw_id"]),
                    )
                    self._event(
                        tx,
                        job_id=draw["job_id"],
                        entity_type="DRAW",
                        entity_id=row["draw_id"],
                        action="DRAW_STATUS",
                        from_state=draw["status"],
                        to_state=draw_status,
                        related_id=attempt_id,
                        actor="scheduler",
                    )

    def set_draw_status(
        self,
        draw_id: str,
        status: str,
        *,
        failure_code: Optional[str] = None,
        failure_message: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        def _apply(tx: sqlite3.Connection) -> None:
            row = tx.execute("SELECT * FROM draws WHERE id = ?", (draw_id,)).fetchone()
            if row is None:
                raise not_found("DRAW 不存在。", draw_id=draw_id)
            if row["status"] != status:
                check_draw(row["status"], status)
            tx.execute(
                """
                UPDATE draws SET status = ?, failure_code = COALESCE(?, failure_code),
                    failure_message = COALESCE(?, failure_message),
                    row_version = row_version + 1, updated_at = ?
                WHERE id = ?
                """,
                (status, failure_code, failure_message, utc_now(), draw_id),
            )
            self._event(
                tx,
                job_id=row["job_id"],
                entity_type="DRAW",
                entity_id=draw_id,
                action="DRAW_STATUS",
                from_state=row["status"],
                to_state=status,
                reason=failure_code,
                actor="scheduler",
            )

        if conn is not None:
            _apply(conn)
        else:
            with self.db.tx() as tx:
                _apply(tx)

    def finalize_success(
        self,
        *,
        attempt_id: str,
        outputs: Sequence[Mapping[str, Any]],
        manifest_path: str,
        manifest_hash: str,
        actor: str = "scheduler",
    ) -> dict[str, Any]:
        """Accept one attempt, insert verified OUT rows and complete its DRAW.

        Idempotent when the same attempt was already accepted.  A different
        accepted attempt raises a conflict; a DRAW can succeed at most once.
        """
        now = utc_now()
        with self.db.tx() as conn:
            attempt = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
            if attempt is None:
                raise not_found("尝试不存在。", attempt_id=attempt_id)
            draw = conn.execute("SELECT * FROM draws WHERE id = ?", (attempt["draw_id"],)).fetchone()
            if draw is None:
                raise not_found("DRAW 不存在。", draw_id=attempt["draw_id"])
            if draw["status"] == DrawStatus.SUCCEEDED:
                if draw["accepted_attempt_id"] == attempt_id:
                    return {"draw": dict(draw), "attempt": dict(attempt), "already": True}
                raise conflict(
                    "DRAW 已接受另一个成功尝试，不能重复确认。",
                    code="OUTPUT_CONFLICT",
                    draw_id=draw["id"],
                    accepted_attempt_id=draw["accepted_attempt_id"],
                )
            check_attempt(attempt["status"], AttemptStatus.SUCCEEDED)
            conn.execute(
                "UPDATE attempts SET status = ?, finished_at = COALESCE(finished_at, ?), updated_at = ?, "
                "remote_state = 'history_success', error_code = NULL, error_message = NULL WHERE id = ?",
                (AttemptStatus.SUCCEEDED, now, now, attempt_id),
            )
            conn.execute(
                "UPDATE draws SET status = ?, accepted_attempt_id = ?, succeeded_at = ?, updated_at = ?, "
                "failure_code = NULL, failure_message = NULL, row_version = row_version + 1 WHERE id = ?",
                (DrawStatus.SUCCEEDED, attempt_id, now, now, draw["id"]),
            )
            conn.execute(
                "UPDATE outputs SET health='QUARANTINED', note='superseded by verified attempt outputs' "
                "WHERE attempt_id=? AND health IN ('PARTIAL','CORRUPT')",
                (attempt_id,),
            )
            for output in outputs:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO outputs(
                        id, attempt_id, out_no, rel_path, sha256, size, width, height,
                        health, created_at, verified_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        output["id"],
                        attempt_id,
                        int(output["out_no"]),
                        output["rel_path"],
                        output["sha256"],
                        int(output["size"]),
                        int(output["width"]),
                        int(output["height"]),
                        OutputHealth.VERIFIED,
                        now,
                        now,
                    ),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO selections(output_id, selected, updated_at, version) VALUES (?,0,?,0)",
                    (output["id"], now),
                )
            self.bump_source_result_revision(conn, draw["source_id"])
            if draw["block_id"]:
                conn.execute(
                    "UPDATE blocks SET cursor = cursor + 1 WHERE id = ?", (draw["block_id"],)
                )
                self.refresh_block_state(conn, draw["block_id"])
            conn.execute(
                "UPDATE execution_slot SET attempt_id = NULL, owner = NULL, acquired_at = NULL, "
                "version = version + 1 WHERE singleton_id = 1 AND attempt_id = ?",
                (attempt_id,),
            )
            self._event(
                conn,
                job_id=draw["job_id"],
                entity_type="DRAW",
                entity_id=draw["id"],
                action="DRAW_SUCCEEDED",
                from_state=draw["status"],
                to_state=DrawStatus.SUCCEEDED,
                related_id=attempt_id,
                actor=actor,
                payload={
                    "output_count": len(outputs),
                    "manifest_hash": manifest_hash,
                    "manifest_path": manifest_path,
                },
            )
        return {
            "draw": self.get_draw(draw["id"]),
            "attempt": self.get_attempt(attempt_id),
            "already": False,
        }

    def record_partial_outputs(self, attempt_id: str, outputs: Sequence[Mapping[str, Any]]) -> None:
        now = utc_now()
        with self.db.tx() as conn:
            for output in outputs:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO outputs(
                        id, attempt_id, out_no, rel_path, sha256, size, width, height,
                        health, created_at, verified_at, note
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        output["id"],
                        attempt_id,
                        int(output["out_no"]),
                        output["rel_path"],
                        output["sha256"],
                        int(output["size"]),
                        int(output["width"]),
                        int(output["height"]),
                        output.get("health", OutputHealth.PARTIAL),
                        now,
                        None,
                        output.get("note", "partial evidence; not eligible for selection"),
                    ),
                )

    def update_output_health(self, output_id: str, health: str, note: Optional[str] = None) -> None:
        if health not in OutputHealth.ALL:
            raise GachaError("BAD_REQUEST", "输出健康状态无效。")
        with self.db.tx() as conn:
            row = conn.execute("SELECT * FROM outputs WHERE id = ?", (output_id,)).fetchone()
            if row is None:
                raise not_found("输出图片不存在。", output_id=output_id)
            conn.execute(
                "UPDATE outputs SET health = ?, note = COALESCE(?, note) WHERE id = ?",
                (health, note, output_id),
            )
            self._event(
                conn,
                job_id=None,
                entity_type="OUT",
                entity_id=output_id,
                action="OUT_HEALTH",
                from_state=row["health"],
                to_state=health,
                reason=note,
                actor="system",
            )

    # ---------------------------------------------------------------- slot
    def get_slot(self) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM execution_slot WHERE singleton_id = 1")
        return dict(row) if row else {"singleton_id": 1, "attempt_id": None, "owner": None, "version": 0}

    def clear_slot_if_owned(self, attempt_id: str, *, actor: str = "recovery") -> bool:
        with self.db.tx() as conn:
            cursor = conn.execute(
                "UPDATE execution_slot SET attempt_id = NULL, owner = NULL, acquired_at = NULL, "
                "version = version + 1 WHERE singleton_id = 1 AND attempt_id = ?",
                (attempt_id,),
            )
            if cursor.rowcount:
                self._event(
                    conn,
                    job_id=None,
                    entity_type="SLOT",
                    entity_id="1",
                    action="SLOT_RELEASED",
                    reason=attempt_id,
                    actor=actor,
                )
                return True
        return False

    # -------------------------------------------------------------- outputs
    def get_output(self, output_id: str) -> Optional[dict[str, Any]]:
        return _dict(self.db.query_one("SELECT * FROM outputs WHERE id = ?", (output_id,)))

    def list_outputs_for_source(
        self,
        source_id: str,
        *,
        include_unhealthy: bool = False,
        limit: int = 50,
        cursor_draw_no: int = 0,
        cursor_out_no: int = 0,
        direction: str = "forward",
        offset: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Stable ``(draw_no, out_no)`` page.

        Forward pages use a strictly-greater cursor; backward pages use a
        strictly-smaller cursor and are returned in ascending display order.
        An explicit ``offset`` addresses the filtered ordinal range directly
        (used to restore a saved result page); it is never used to infer
        identity, only to reach the same stable ordering.
        """
        limit = max(1, min(int(limit), 200))
        if direction not in {"forward", "backward"}:
            raise GachaError("BAD_REQUEST", "direction 只能是 forward 或 backward。")
        backward = direction == "backward"
        health_filter = "" if include_unhealthy else f"""
                  AND d.status = 'SUCCEEDED'
                  AND d.accepted_attempt_id = o.attempt_id
                  AND o.health = 'VERIFIED'
                  AND {RETENTION_AVAILABLE}"""
        base = f"""
            SELECT o.*, d.draw_no, d.accepted_attempt_id, a.attempt_no,
                   COALESCE(s.selected, 0) AS selected, s.version AS selection_version, s.viewed_at,
                   {RETENTION_STATE_SQL} AS retention_state, ret.purged_at, ret.cleanup_id AS retention_cleanup_id
            FROM outputs o
            JOIN attempts a ON a.id = o.attempt_id
            JOIN draws d ON d.id = a.draw_id
            LEFT JOIN selections s ON s.output_id = o.id
            LEFT JOIN output_retention ret ON ret.output_id = o.id
            WHERE d.source_id = ?{health_filter}
        """
        if offset is not None:
            rows = self.db.query_all(
                base + " ORDER BY d.draw_no ASC, o.out_no ASC LIMIT ? OFFSET ?",
                (source_id, limit, max(0, int(offset))),
            )
            return [dict(row) for row in rows]
        if backward:
            comparison = "(d.draw_no < ? OR (d.draw_no = ? AND o.out_no < ?))"
            order = "d.draw_no DESC, o.out_no DESC"
        else:
            comparison = "(d.draw_no > ? OR (d.draw_no = ? AND o.out_no > ?))"
            order = "d.draw_no ASC, o.out_no ASC"
        rows = self.db.query_all(
            base + f" AND {comparison} ORDER BY {order} LIMIT ?",
            (source_id, int(cursor_draw_no), int(cursor_draw_no), int(cursor_out_no), limit),
        )
        items = [dict(row) for row in rows]
        if backward:
            items.reverse()
        return items

    def count_outputs_for_source(
        self, source_id: str, *, include_unhealthy: bool = False
    ) -> int:
        health_filter = "" if include_unhealthy else f"""
              AND d.status = 'SUCCEEDED'
              AND d.accepted_attempt_id = o.attempt_id
              AND o.health = 'VERIFIED'
              AND {RETENTION_AVAILABLE}"""
        row = self.db.query_one(
            f"""
            SELECT COUNT(*) FROM outputs o
            JOIN attempts a ON a.id = o.attempt_id
            JOIN draws d ON d.id = a.draw_id
            WHERE d.source_id = ?{health_filter}
            """,
            (source_id,),
        )
        return int(row[0] or 0) if row else 0

    def count_outputs_before(
        self,
        source_id: str,
        *,
        draw_no: int,
        out_no: int,
        include_unhealthy: bool = False,
    ) -> int:
        health_filter = "" if include_unhealthy else f"""
              AND d.status = 'SUCCEEDED'
              AND d.accepted_attempt_id = o.attempt_id
              AND o.health = 'VERIFIED'
              AND {RETENTION_AVAILABLE}"""
        row = self.db.query_one(
            f"""
            SELECT COUNT(*) FROM outputs o
            JOIN attempts a ON a.id = o.attempt_id
            JOIN draws d ON d.id = a.draw_id
            WHERE d.source_id = ?{health_filter}
              AND (d.draw_no < ? OR (d.draw_no = ? AND o.out_no < ?))
            """,
            (source_id, int(draw_no), int(draw_no), int(out_no)),
        )
        return int(row[0] or 0) if row else 0

    def output_states_for_source(
        self, source_id: str, output_ids: Sequence[str]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """One consistent read snapshot of lightweight OUT state.

        Only outputs owned by ``source_id`` are returned.  Unknown / foreign
        ids are reported separately and are never treated as "not selected".
        """
        unique: list[str] = []
        seen: set[str] = set()
        for output_id in output_ids:
            if not isinstance(output_id, str) or not output_id:
                raise GachaError("BAD_REQUEST", "output_ids 必须是字符串数组。")
            if output_id in seen:
                continue
            seen.add(output_id)
            unique.append(output_id)
        if len(unique) > 200:
            raise GachaError("BAD_REQUEST", "单次最多查询 200 个 OUT 状态。")
        if not unique:
            return [], []
        placeholders = ",".join("?" for _ in unique)
        with self.db.read() as conn:
            rows = list(
                conn.execute(
                    f"""
                    SELECT o.id AS output_id, o.health,
                           COALESCE(s.selected, 0) AS selected,
                           COALESCE(s.version, 0) AS selection_version,
                           s.viewed_at,
                           {RETENTION_STATE_SQL} AS retention_state
                    FROM outputs o
                    JOIN attempts a ON a.id = o.attempt_id
                    JOIN draws d ON d.id = a.draw_id
                    LEFT JOIN selections s ON s.output_id = o.id
                    LEFT JOIN output_retention ret ON ret.output_id = o.id
                    WHERE d.source_id = ? AND o.id IN ({placeholders})
                    """,
                    (source_id, *unique),
                )
            )
        found = {
            row["output_id"]: {
                "output_id": row["output_id"],
                "selected": bool(row["selected"]),
                "selection_version": int(row["selection_version"]),
                "health": row["health"],
                "viewed_at": row["viewed_at"],
                "retention_state": row["retention_state"],
            }
            for row in rows
        }
        items = [found[output_id] for output_id in unique if output_id in found]
        missing = [output_id for output_id in unique if output_id not in found]
        return items, missing

    def output_cursor_bounds(
        self, source_id: str, *, include_unhealthy: bool = False
    ) -> tuple[Optional[tuple[int, int]], Optional[tuple[int, int]]]:
        health_filter = "" if include_unhealthy else f"""
              AND d.status = 'SUCCEEDED'
              AND d.accepted_attempt_id = o.attempt_id
              AND o.health = 'VERIFIED'
              AND {RETENTION_AVAILABLE}"""
        first = self.db.query_one(
            f"""
            SELECT d.draw_no, o.out_no
            FROM outputs o
            JOIN attempts a ON a.id = o.attempt_id
            JOIN draws d ON d.id = a.draw_id
            WHERE d.source_id = ?{health_filter}
            ORDER BY d.draw_no ASC, o.out_no ASC LIMIT 1
            """,
            (source_id,),
        )
        last = self.db.query_one(
            f"""
            SELECT d.draw_no, o.out_no
            FROM outputs o
            JOIN attempts a ON a.id = o.attempt_id
            JOIN draws d ON d.id = a.draw_id
            WHERE d.source_id = ?{health_filter}
            ORDER BY d.draw_no DESC, o.out_no DESC LIMIT 1
            """,
            (source_id,),
        )
        if first is None or last is None:
            return None, None
        return (
            (int(first["draw_no"]), int(first["out_no"])),
            (int(last["draw_no"]), int(last["out_no"])),
        )

    def count_available_outputs(self, source_id: str) -> int:
        row = self.db.query_one(
            f"""
            SELECT COUNT(*) FROM outputs o
            JOIN attempts a ON a.id=o.attempt_id
            JOIN draws d ON d.id=a.draw_id
            WHERE d.source_id=? AND d.status='SUCCEEDED' AND d.accepted_attempt_id=a.id
              AND a.status='SUCCEEDED'
              AND o.health='VERIFIED'
              AND {RETENTION_AVAILABLE}
            """,
            (source_id,),
        )
        return int(row[0]) if row else 0

    # ---------------------------------------------------------- selections
    def set_selection(
        self,
        output_id: str,
        *,
        selected: bool,
        expected_version: Optional[int] = None,
        viewed: bool = False,
        actor: str = "user",
    ) -> dict[str, Any]:
        if not isinstance(selected, bool):
            raise GachaError("BAD_REQUEST", "selected 必须是布尔值。")
        now = utc_now()
        with self.db.tx() as conn:
            self.assert_maintenance_free(conn=conn)
            output = conn.execute(
                """
                SELECT o.*, d.source_id AS source_id, d.id AS draw_id
                FROM outputs o
                JOIN attempts a ON a.id=o.attempt_id
                JOIN draws d ON d.id=a.draw_id
                WHERE o.id=?
                """,
                (output_id,),
            ).fetchone()
            if output is None:
                raise not_found("输出图片不存在。", output_id=output_id)
            selection = conn.execute("SELECT * FROM selections WHERE output_id=?", (output_id,)).fetchone()
            current_version = int(selection["version"]) if selection else 0
            if expected_version is not None and current_version != int(expected_version):
                raise conflict(
                    "选择版本已变化，请刷新后重试。",
                    output_id=output_id,
                    current_version=current_version,
                    expected_version=expected_version,
                )
            if selection is None:
                conn.execute(
                    "INSERT INTO selections(output_id, selected, viewed_at, updated_at, version) VALUES (?,?,?,?,1)",
                    (output_id, 1 if selected else 0, now if viewed else None, now),
                )
            else:
                conn.execute(
                    "UPDATE selections SET selected=?, viewed_at=CASE WHEN ? THEN ? ELSE viewed_at END, "
                    "updated_at=?, version=version+1 WHERE output_id=?",
                    (1 if selected else 0, 1 if viewed else 0, now, now, output_id),
                )
            self.bump_source_selection_version(conn, output["source_id"])
            new_row = conn.execute("SELECT * FROM selections WHERE output_id=?", (output_id,)).fetchone()
            self._event(
                conn,
                job_id=None,
                entity_type="SELECTION",
                entity_id=output_id,
                action="SELECTION_SET",
                from_state=str(int(selection["selected"])) if selection else None,
                to_state="1" if selected else "0",
                related_id=output["source_id"],
                actor=actor,
            )
            return dict(new_row)

    def mark_viewed(self, output_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.db.tx() as conn:
            row = conn.execute("SELECT * FROM selections WHERE output_id=?", (output_id,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO selections(output_id, selected, viewed_at, updated_at, version) VALUES (?,0,?,?,0)",
                    (output_id, now, now),
                )
            else:
                conn.execute("UPDATE selections SET viewed_at=? WHERE output_id=?", (now, output_id))
            result = conn.execute("SELECT * FROM selections WHERE output_id=?", (output_id,)).fetchone()
            return dict(result)

    def get_selection(self, output_id: str) -> Optional[dict[str, Any]]:
        return _dict(self.db.query_one("SELECT * FROM selections WHERE output_id=?", (output_id,)))

    # --------------------------------------------------------------- events
    def _event(
        self,
        conn: sqlite3.Connection,
        *,
        job_id: Optional[str],
        entity_type: str,
        entity_id: Optional[str],
        action: str,
        from_state: Optional[str] = None,
        to_state: Optional[str] = None,
        reason: Optional[str] = None,
        related_id: Optional[str] = None,
        actor: str = "system",
        payload: Optional[Mapping[str, Any]] = None,
    ) -> int:
        cursor = conn.execute(
            """
            INSERT INTO events(job_id, entity_type, entity_id, action, from_state, to_state,
                               reason, related_id, actor, payload_json, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                job_id,
                entity_type,
                entity_id,
                action,
                from_state,
                to_state,
                reason,
                related_id,
                actor,
                json.dumps(payload, ensure_ascii=False, sort_keys=True) if payload else None,
                utc_now(),
            ),
        )
        return int(cursor.lastrowid)

    def append_event(self, **kwargs: Any) -> int:
        with self.db.tx() as conn:
            return self._event(conn, **kwargs)

    def list_events(
        self,
        *,
        after: int = 0,
        job_id: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        if job_id:
            rows = self.db.query_all(
                "SELECT * FROM events WHERE seq > ? AND job_id = ? ORDER BY seq LIMIT ?",
                (int(after), job_id, limit),
            )
        else:
            rows = self.db.query_all(
                "SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (int(after), limit)
            )
        return [dict(row) for row in rows]

    def last_event_seq(self) -> int:
        row = self.db.query_one("SELECT COALESCE(MAX(seq), 0) FROM events")
        return int(row[0]) if row else 0

    # -------------------------------------------------------------- summary
    def job_summary(self, job_id: str) -> dict[str, Any]:
        draw_row = self.db.query_one(
            """
            SELECT
              COUNT(*) AS total_planned,
              SUM(CASE WHEN b.kind='APPEND' THEN 1 ELSE 0 END) AS appended,
              SUM(CASE WHEN d.status='CANCELLED_BY_USER' THEN 1 ELSE 0 END) AS cancelled,
              SUM(CASE WHEN d.status!='CANCELLED_BY_USER' THEN 1 ELSE 0 END) AS effective,
              SUM(CASE WHEN d.status='SUCCEEDED' THEN 1 ELSE 0 END) AS succeeded,
              SUM(CASE WHEN d.status IN ('CLAIMED','DISPATCHING','QUEUED','RUNNING','VERIFYING') THEN 1 ELSE 0 END) AS inflight,
              SUM(CASE WHEN d.status='PENDING' THEN 1 ELSE 0 END) AS pending,
              SUM(CASE WHEN d.status IN ('FAILED','INTERRUPTED') THEN 1 ELSE 0 END) AS failed,
              SUM(CASE WHEN d.status='UNKNOWN' THEN 1 ELSE 0 END) AS unknown
            FROM draws d JOIN blocks b ON b.id=d.block_id
            WHERE d.job_id=?
            """,
            (job_id,),
        )
        out_row = self.db.query_one(
            f"""
            SELECT
              COUNT(*) AS out_total,
              SUM(CASE WHEN {RETENTION_STATE_SQL}='RETAINED' THEN 1 ELSE 0 END) AS out_retained,
              SUM(CASE WHEN ret.state='PURGED' THEN 1 ELSE 0 END) AS out_purged,
              SUM(CASE WHEN ret.state='CLEANING' THEN 1 ELSE 0 END) AS out_cleaning,
              SUM(CASE WHEN o.health='VERIFIED' AND {RETENTION_STATE_SQL}='RETAINED'
                       THEN 1 ELSE 0 END) AS out_verified,
              SUM(CASE WHEN o.health IN ('PARTIAL','MISSING','CORRUPT','QUARANTINED')
                        AND {RETENTION_STATE_SQL}='RETAINED'
                       THEN 1 ELSE 0 END) AS out_abnormal,
              SUM(CASE WHEN o.health='VERIFIED' AND d.accepted_attempt_id=a.id AND d.status='SUCCEEDED'
                        AND {RETENTION_STATE_SQL}='RETAINED'
                       THEN 1 ELSE 0 END) AS out_available,
              SUM(CASE WHEN COALESCE(s.selected,0)=1 AND {RETENTION_STATE_SQL}='RETAINED'
                       THEN 1 ELSE 0 END) AS selected,
              SUM(CASE WHEN COALESCE(s.selected,0)=1 THEN 1 ELSE 0 END) AS selected_total
            FROM outputs o
            JOIN attempts a ON a.id=o.attempt_id
            JOIN draws d ON d.id=a.draw_id
            LEFT JOIN selections s ON s.output_id=o.id
            LEFT JOIN output_retention ret ON ret.output_id=o.id
            WHERE d.job_id=?
            """,
            (job_id,),
        )
        src_row = self.db.query_one(
            "SELECT COUNT(*) AS sources_total, SUM(CASE WHEN status='REMOVED_BY_USER' THEN 0 ELSE 1 END) AS sources_active "
            "FROM sources WHERE job_id=?",
            (job_id,),
        )
        current = self.db.query_one(
            """
            SELECT d.source_id, s.source_order, s.original_name
            FROM draws d JOIN sources s ON s.id=d.source_id
            WHERE d.job_id=? AND d.status NOT IN ('SUCCEEDED','CANCELLED_BY_USER')
            ORDER BY s.source_order, d.draw_no LIMIT 1
            """,
            (job_id,),
        )
        summary = {key: int(value or 0) for key, value in (dict(draw_row) if draw_row else {}).items()}
        summary.update({key: int(value or 0) for key, value in (dict(out_row) if out_row else {}).items()})
        summary.update({key: int(value or 0) for key, value in (dict(src_row) if src_row else {}).items()})
        purged_bytes = self.db.query_one(
            "SELECT COALESCE(SUM(ret.size),0) FROM output_retention ret "
            "JOIN outputs o ON o.id=ret.output_id "
            "JOIN attempts a ON a.id=o.attempt_id JOIN draws d ON d.id=a.draw_id "
            "WHERE d.job_id=? AND ret.state='PURGED'",
            (job_id,),
        )
        summary["out_purged_bytes"] = int(purged_bytes[0] or 0) if purged_bytes else 0
        summary["storage_revision"] = self.get_storage_revision(job_id)
        summary["current_source"] = dict(current) if current else None
        summary["last_event_seq"] = self.last_event_seq()
        return summary

    def source_summary(self, source_id: str) -> dict[str, Any]:
        draw_row = self.db.query_one(
            """
            SELECT COUNT(*) AS planned,
              SUM(CASE WHEN status='SUCCEEDED' THEN 1 ELSE 0 END) AS succeeded,
              SUM(CASE WHEN status='PENDING' THEN 1 ELSE 0 END) AS pending,
              SUM(CASE WHEN status IN ('FAILED','INTERRUPTED') THEN 1 ELSE 0 END) AS failed,
              SUM(CASE WHEN status='UNKNOWN' THEN 1 ELSE 0 END) AS unknown,
              MAX(draw_no) AS max_draw_no
            FROM draws WHERE source_id=?
            """,
            (source_id,),
        )
        out_row = self.db.query_one(
            f"""
            SELECT COUNT(*) AS out_count,
              SUM(CASE WHEN {RETENTION_STATE_SQL}='RETAINED' THEN 1 ELSE 0 END) AS retained,
              SUM(CASE WHEN ret.state='PURGED' THEN 1 ELSE 0 END) AS purged,
              SUM(CASE WHEN ret.state='CLEANING' THEN 1 ELSE 0 END) AS cleaning,
              SUM(CASE WHEN o.health='VERIFIED' AND d.accepted_attempt_id=a.id
                        AND {RETENTION_STATE_SQL}='RETAINED'
                       THEN 1 ELSE 0 END) AS available,
              SUM(CASE WHEN COALESCE(s.selected,0)=1 AND {RETENTION_STATE_SQL}='RETAINED'
                       THEN 1 ELSE 0 END) AS selected,
              SUM(CASE WHEN COALESCE(s.selected,0)=1 THEN 1 ELSE 0 END) AS selected_total,
              COALESCE(SUM(CASE WHEN ret.state='PURGED' THEN ret.size ELSE 0 END),0) AS purged_bytes
            FROM outputs o JOIN attempts a ON a.id=o.attempt_id JOIN draws d ON d.id=a.draw_id
            LEFT JOIN selections s ON s.output_id=o.id
            LEFT JOIN output_retention ret ON ret.output_id=o.id
            WHERE d.source_id=?
            """,
            (source_id,),
        )
        source = self.get_source(source_id) or {}
        result = {key: int(value or 0) for key, value in (dict(draw_row) if draw_row else {}).items()}
        result.update({key: int(value or 0) for key, value in (dict(out_row) if out_row else {}).items()})
        result.update(
            {
                "source_id": source_id,
                "source_order": source.get("source_order", 0),
                "status": source.get("status"),
                "sha256": source.get("sha256"),
                "width": source.get("width"),
                "height": source.get("height"),
                "result_revision": source.get("result_revision", 0),
                "selection_version": source.get("selection_version", 0),
                "max_draw_no": int(result.get("max_draw_no") or 0),
            }
        )
        return result

    # ------------------------------------------------------ review sessions
    def get_review_session(
        self, job_id: str, conn: Optional[sqlite3.Connection] = None
    ) -> Optional[dict[str, Any]]:
        sql = "SELECT * FROM review_sessions WHERE job_id = ?"
        if conn is not None:
            return _dict(conn.execute(sql, (job_id,)).fetchone())
        return _dict(self.db.query_one(sql, (job_id,)))

    def get_review_session_by_id(
        self, session_id: str, conn: Optional[sqlite3.Connection] = None
    ) -> Optional[dict[str, Any]]:
        sql = "SELECT * FROM review_sessions WHERE id = ?"
        if conn is not None:
            return _dict(conn.execute(sql, (session_id,)).fetchone())
        return _dict(self.db.query_one(sql, (session_id,)))

    def get_review_action(
        self, session_id: str, action_id: str, conn: Optional[sqlite3.Connection] = None
    ) -> Optional[dict[str, Any]]:
        sql = "SELECT * FROM review_session_actions WHERE session_id = ? AND action_id = ?"
        if conn is not None:
            return _dict(conn.execute(sql, (session_id, action_id)).fetchone())
        return _dict(self.db.query_one(sql, (session_id, action_id)))

    # Availability is always the same predicate: a verified OUT produced by the
    # attempt accepted for a SUCCEEDED DRAW.  Unhealthy or superseded evidence
    # never counts as reviewable.
    _REVIEW_AVAILABLE = (
        "d.status='SUCCEEDED' AND d.accepted_attempt_id=a.id AND a.status='SUCCEEDED' "
        "AND o.health='VERIFIED' AND " + RETENTION_AVAILABLE
    )

    def _review_counts_sql(self) -> str:
        available = self._REVIEW_AVAILABLE
        return f"""
            (SELECT COUNT(*)
               FROM draws d JOIN attempts a ON a.id=d.accepted_attempt_id
               JOIN outputs o ON o.attempt_id=a.id
              WHERE d.source_id=s.id AND {available}) AS available_count,
            (SELECT COUNT(*)
               FROM draws d JOIN attempts a ON a.id=d.accepted_attempt_id
               JOIN outputs o ON o.attempt_id=a.id
               JOIN selections sel ON sel.output_id=o.id AND sel.selected=1
              WHERE d.source_id=s.id AND {available}) AS selected_count,
            (SELECT COUNT(*) FROM draws d WHERE d.source_id=s.id AND d.status='PENDING')
                AS pending_draws,
            (SELECT COUNT(*) FROM draws d WHERE d.source_id=s.id) AS planned_draws,
            (SELECT COUNT(*)
               FROM selections sel JOIN outputs o ON o.id=sel.output_id
               JOIN attempts a ON a.id=o.attempt_id JOIN draws d ON d.id=a.draw_id
              WHERE d.source_id=s.id AND sel.viewed_at IS NOT NULL) AS viewed_count
        """

    def review_source_page(
        self,
        job_id: str,
        *,
        plan_version: int,
        scope: str = "pending",
        after_order: Optional[int] = None,
        before_order: Optional[int] = None,
        source_id: Optional[str] = None,
        limit: int = 40,
        conn: Optional[sqlite3.Connection] = None,
    ) -> tuple[list[dict[str, Any]], Optional[int], Optional[int]]:
        """Bounded, stable source_order navigation for result review.

        Returns ``(rows, next_after_order, prev_before_order)``.  The pending
        predicate is evaluated by SQL so the client never has to scan confirmed
        pages one by one.  Logically removed sources keep their numbers and are
        skipped without renumbering.
        """
        if scope not in {"pending", "all"}:
            raise GachaError("BAD_REQUEST", "scope 只能是 pending 或 all。")
        page_size = max(1, min(int(limit), 200))
        if source_id is not None:
            where = ["s.id=?", "s.job_id=?", "s.status!='REMOVED_BY_USER'"]
            params: list[Any] = [int(plan_version), source_id, job_id]
            order_sql = "ORDER BY s.source_order ASC LIMIT 1"
        elif before_order is not None:
            where = ["s.job_id=?", "s.status!='REMOVED_BY_USER'", "s.source_order < ?"]
            params = [int(plan_version), job_id, int(before_order)]
            order_sql = "ORDER BY s.source_order DESC LIMIT ?"
        else:
            where = ["s.job_id=?", "s.status!='REMOVED_BY_USER'", "s.source_order > ?"]
            params = [int(plan_version), job_id, int(after_order or 0)]
            order_sql = "ORDER BY s.source_order ASC LIMIT ?"
        if source_id is None:
            params.append(page_size)
        where.append(
            "EXISTS (SELECT 1 FROM draws d JOIN attempts a ON a.id=d.accepted_attempt_id "
            "JOIN outputs o ON o.attempt_id=a.id "
            f"WHERE d.source_id=s.id AND {self._REVIEW_AVAILABLE})"
        )
        if scope == "pending":
            where.append("r.id IS NULL")
        sql = f"""
            SELECT s.id, s.job_id, s.source_order, s.original_name, s.status, s.size,
                   s.sha256, s.width, s.height, s.image_format, s.result_revision,
                   s.selection_version, s.created_at, s.removed_at,
                   r.decision AS review_decision, r.confirmed_at AS review_confirmed_at,
                   {self._review_counts_sql()}
              FROM sources s
              LEFT JOIN source_reviews r
                ON r.source_id=s.id
               AND r.result_revision=s.result_revision
               AND r.selection_version=s.selection_version
               AND r.plan_version=?
             WHERE {' AND '.join(where)}
             {order_sql}
        """
        if conn is not None:
            rows = [dict(row) for row in conn.execute(sql, params)]
        else:
            rows = [dict(row) for row in self.db.query_all(sql, params)]
        if before_order is not None and source_id is None:
            rows.reverse()
        if source_id is not None:
            return rows, None, None
        next_after = None
        prev_before = None
        if rows:
            first = rows[0]
            last = rows[-1]
            forward = before_order is None
            if forward and len(rows) == page_size:
                probe_params = [int(plan_version), job_id, int(last["source_order"])]
                probe_where = [
                    "s.job_id=?", "s.status!='REMOVED_BY_USER'", "s.source_order > ?",
                ]
                if scope == "pending":
                    probe_where.append("r.id IS NULL")
                probe_where.append(
                    "EXISTS (SELECT 1 FROM draws d "
                    "JOIN attempts a ON a.id=d.accepted_attempt_id "
                    "JOIN outputs o ON o.attempt_id=a.id "
                    f"WHERE d.source_id=s.id AND {self._REVIEW_AVAILABLE})"
                )
                probe_sql = (
                    "SELECT 1 FROM sources s LEFT JOIN source_reviews r "
                    "ON r.source_id=s.id AND r.result_revision=s.result_revision "
                    "AND r.selection_version=s.selection_version AND r.plan_version=? "
                    f"WHERE {' AND '.join(probe_where)} LIMIT 1"
                )
                found = (
                    conn.execute(probe_sql, probe_params).fetchone()
                    if conn is not None
                    else self.db.query_one(probe_sql, probe_params)
                )
                if found:
                    next_after = int(last["source_order"])
            # A backward page always knows the group after it (its last row).
            if not forward or len(rows) == page_size:
                probe_sql = (
                    "SELECT 1 FROM sources s LEFT JOIN source_reviews r "
                    "ON r.source_id=s.id AND r.result_revision=s.result_revision "
                    "AND r.selection_version=s.selection_version AND r.plan_version=? "
                    "WHERE s.job_id=? AND s.status!='REMOVED_BY_USER' "
                    "AND s.source_order < ? "
                    + ("AND r.id IS NULL " if scope == "pending" else "")
                    + "AND EXISTS (SELECT 1 FROM draws d "
                    "JOIN attempts a ON a.id=d.accepted_attempt_id "
                    "JOIN outputs o ON o.attempt_id=a.id "
                    f"WHERE d.source_id=s.id AND {self._REVIEW_AVAILABLE}) LIMIT 1"
                )
                probe_params = [int(plan_version), job_id, int(first["source_order"])]
                # NOTE: join placeholder precedes WHERE placeholders in probe_sql.
                found = (
                    conn.execute(probe_sql, probe_params).fetchone()
                    if conn is not None
                    else self.db.query_one(probe_sql, probe_params)
                )
                if found:
                    prev_before = int(first["source_order"])
        return rows, next_after, prev_before

    def review_next_pending(
        self,
        job_id: str,
        *,
        plan_version: int,
        after_order: int,
        conn: Optional[sqlite3.Connection] = None,
    ) -> Optional[dict[str, Any]]:
        rows, _, _ = self.review_source_page(
            job_id,
            plan_version=plan_version,
            scope="pending",
            after_order=int(after_order),
            limit=1,
            conn=conn,
        )
        return rows[0] if rows else None

    def review_summary(
        self,
        job_id: str,
        *,
        plan_version: int,
        conn: Optional[sqlite3.Connection] = None,
    ) -> dict[str, int]:
        """One consistent aggregate of review progress for the whole job."""
        available = self._REVIEW_AVAILABLE
        sql = f"""
            SELECT
              COUNT(*) AS total_sources,
              COALESCE(SUM(CASE WHEN s.status!='REMOVED_BY_USER' THEN 1 ELSE 0 END), 0)
                  AS active_sources
            FROM sources s WHERE s.job_id=?
        """
        base = (
            conn.execute(sql, (job_id,)).fetchone()
            if conn is not None
            else self.db.query_one(sql, (job_id,))
        )
        sql2 = f"""
            SELECT
              COALESCE(SUM(CASE WHEN q.available_count>0 THEN 1 ELSE 0 END), 0)
                  AS reviewable_sources,
              COALESCE(SUM(CASE WHEN q.available_count>0 AND r.id IS NULL THEN 1 ELSE 0 END), 0)
                  AS pending_sources,
              COALESCE(SUM(CASE WHEN r.id IS NOT NULL THEN 1 ELSE 0 END), 0)
                  AS confirmed_sources,
              COALESCE(SUM(CASE WHEN r.decision='SELECTED' THEN 1 ELSE 0 END), 0)
                  AS selected_groups,
              COALESCE(SUM(CASE WHEN r.decision='SKIPPED' THEN 1 ELSE 0 END), 0)
                  AS skipped_groups,
              COALESCE(SUM(CASE WHEN q.available_count=0 THEN 1 ELSE 0 END), 0)
                  AS no_result_sources,
              COALESCE(SUM(q.selected_count), 0) AS selected_outputs,
              COALESCE(SUM(q.available_count), 0) AS available_outputs,
              COALESCE(SUM(q.pending_draws), 0) AS pending_draws
            FROM (
              SELECT s.id, s.result_revision, s.selection_version,
                     (SELECT COUNT(*)
                        FROM draws d JOIN attempts a ON a.id=d.accepted_attempt_id
                        JOIN outputs o ON o.attempt_id=a.id
                       WHERE d.source_id=s.id AND {available}) AS available_count,
                     (SELECT COUNT(*)
                        FROM draws d JOIN attempts a ON a.id=d.accepted_attempt_id
                        JOIN outputs o ON o.attempt_id=a.id
                        JOIN selections sel ON sel.output_id=o.id AND sel.selected=1
                       WHERE d.source_id=s.id AND {available}) AS selected_count,
                     (SELECT COUNT(*) FROM draws d
                       WHERE d.source_id=s.id AND d.status='PENDING') AS pending_draws
                FROM sources s
               WHERE s.job_id=? AND s.status!='REMOVED_BY_USER'
            ) q
            LEFT JOIN source_reviews r
              ON r.source_id=q.id
             AND r.result_revision=q.result_revision
             AND r.selection_version=q.selection_version
             AND r.plan_version=?
        """
        row = (
            conn.execute(sql2, (job_id, int(plan_version))).fetchone()
            if conn is not None
            else self.db.query_one(sql2, (job_id, int(plan_version)))
        )
        result = {key: int(value or 0) for key, value in (dict(base) if base else {}).items()}
        result.update({key: int(value or 0) for key, value in (dict(row) if row else {}).items()})
        result["plan_version"] = int(plan_version)
        return result

    def review_selected_outputs(
        self,
        source_id: str,
        *,
        limit: int = 200,
        conn: Optional[sqlite3.Connection] = None,
    ) -> tuple[int, list[str]]:
        available = self._REVIEW_AVAILABLE
        sql = f"""
            SELECT o.id
              FROM draws d JOIN attempts a ON a.id=d.accepted_attempt_id
              JOIN outputs o ON o.attempt_id=a.id
              JOIN selections sel ON sel.output_id=o.id AND sel.selected=1
             WHERE d.source_id=? AND {available}
             ORDER BY d.draw_no ASC, o.out_no ASC
        """
        if conn is not None:
            rows = list(conn.execute(sql, (source_id,)))
        else:
            rows = list(self.db.query_all(sql, (source_id,)))
        ids = [str(row[0]) for row in rows]
        return len(ids), ids[: max(1, min(int(limit), 1000))]

    # ------------------------------------------------- retention / cleanup
    def get_storage_revision(
        self, job_id: str, conn: Optional[sqlite3.Connection] = None
    ) -> int:
        sql = "SELECT storage_revision FROM job_storage_state WHERE job_id=?"
        if conn is not None:
            row = conn.execute(sql, (job_id,)).fetchone()
        else:
            row = self.db.query_one(sql, (job_id,))
        return int(row[0]) if row else 0

    def bump_storage_revision(
        self, conn: sqlite3.Connection, job_id: str
    ) -> int:
        now = utc_now()
        conn.execute(
            "INSERT INTO job_storage_state(job_id, storage_revision, updated_at) VALUES (?,1,?) "
            "ON CONFLICT(job_id) DO UPDATE SET storage_revision=storage_revision+1, updated_at=excluded.updated_at",
            (job_id, now),
        )
        row = conn.execute(
            "SELECT storage_revision FROM job_storage_state WHERE job_id=?", (job_id,)
        ).fetchone()
        return int(row[0]) if row else 1

    def get_retention(
        self, output_id: str, conn: Optional[sqlite3.Connection] = None
    ) -> Optional[dict[str, Any]]:
        sql = "SELECT * FROM output_retention WHERE output_id=?"
        if conn is not None:
            return _dict(conn.execute(sql, (output_id,)).fetchone())
        return _dict(self.db.query_one(sql, (output_id,)))

    def set_retention(
        self,
        conn: sqlite3.Connection,
        output_id: str,
        *,
        state: str,
        cleanup_id: Optional[str] = None,
        rel_path: Optional[str] = None,
        sha256: Optional[str] = None,
        size: Optional[int] = None,
        reason: Optional[str] = None,
    ) -> None:
        if state not in {"RETAINED", "CLEANING", "PURGED"}:
            raise GachaError("BAD_REQUEST", "留存状态无效。")
        now = utc_now()
        conn.execute(
            """
            INSERT INTO output_retention(output_id, state, cleanup_id, original_rel_path, sha256, size,
                                         reason, updated_at, purged_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(output_id) DO UPDATE SET
                state=excluded.state,
                cleanup_id=excluded.cleanup_id,
                original_rel_path=COALESCE(output_retention.original_rel_path, excluded.original_rel_path),
                sha256=COALESCE(output_retention.sha256, excluded.sha256),
                size=COALESCE(output_retention.size, excluded.size),
                reason=excluded.reason,
                updated_at=excluded.updated_at,
                purged_at=excluded.purged_at
            """,
            (
                output_id,
                state,
                cleanup_id,
                rel_path,
                sha256,
                size,
                reason,
                now,
                now if state == "PURGED" else None,
            ),
        )

    def get_cleanup(self, cleanup_id: str) -> Optional[dict[str, Any]]:
        row = _dict(
            self.db.query_one("SELECT * FROM cleanup_runs WHERE id=?", (cleanup_id,))
        )
        return row

    def list_cleanup_runs(
        self, job_id: str, *, limit: int = 20, before_created_at: Optional[str] = None
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        if before_created_at:
            rows = self.db.query_all(
                "SELECT * FROM cleanup_runs WHERE job_id=? AND created_at<? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (job_id, before_created_at, limit + 1),
            )
        else:
            rows = self.db.query_all(
                "SELECT * FROM cleanup_runs WHERE job_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
                (job_id, limit + 1),
            )
        return [dict(row) for row in rows]

    def cleanup_item_page(
        self,
        cleanup_id: str,
        *,
        limit: int = 50,
        after: Optional[tuple[int, int, int, str]] = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        if after is None:
            rows = self.db.query_all(
                "SELECT * FROM cleanup_items WHERE cleanup_id=? "
                "ORDER BY source_order, draw_no, out_no, output_id LIMIT ?",
                (cleanup_id, limit),
            )
        else:
            rows = self.db.query_all(
                """
                SELECT * FROM cleanup_items WHERE cleanup_id=? AND (
                    source_order > ? OR
                    (source_order = ? AND draw_no > ?) OR
                    (source_order = ? AND draw_no = ? AND out_no > ?) OR
                    (source_order = ? AND draw_no = ? AND out_no = ? AND output_id > ?)
                ) ORDER BY source_order, draw_no, out_no, output_id LIMIT ?
                """,
                (
                    cleanup_id,
                    after[0], after[0], after[1],
                    after[0], after[1], after[2],
                    after[0], after[1], after[2], after[3],
                    limit,
                ),
            )
        return [dict(row) for row in rows]

    def maintenance(self, conn: Optional[sqlite3.Connection] = None) -> dict[str, Any]:
        sql = "SELECT * FROM maintenance_state WHERE singleton_id=1"
        if conn is not None:
            row = _dict(conn.execute(sql).fetchone())
        else:
            row = _dict(self.db.query_one(sql))
        return row or {"holder": None, "kind": None, "cleanup_id": None, "job_id": None}

    def acquire_maintenance(
        self,
        conn: sqlite3.Connection,
        *,
        holder: str,
        kind: str,
        boot_id: str,
        cleanup_id: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> bool:
        """Atomically take the single maintenance slot.

        Must be called inside ``BEGIN IMMEDIATE``.  The conditional UPDATE is
        the cross-process authority; an in-process lock alone would not be
        enough.
        """
        current = conn.execute(
            "SELECT holder FROM maintenance_state WHERE singleton_id=1"
        ).fetchone()
        if current is None:
            conn.execute(
                "INSERT OR IGNORE INTO maintenance_state(singleton_id, version) VALUES (1,0)"
            )
            current = conn.execute(
                "SELECT holder FROM maintenance_state WHERE singleton_id=1"
            ).fetchone()
        if current is not None and current["holder"]:
            return False
        cursor = conn.execute(
            """
            UPDATE maintenance_state
               SET holder=?, kind=?, cleanup_id=?, job_id=?, boot_id=?, acquired_at=?,
                   version=version+1
             WHERE singleton_id=1 AND (holder IS NULL OR holder='')
            """,
            (holder, kind, cleanup_id, job_id, boot_id, utc_now()),
        )
        return cursor.rowcount == 1

    def release_maintenance(
        self,
        conn: sqlite3.Connection,
        *,
        holder: Optional[str] = None,
        cleanup_id: Optional[str] = None,
    ) -> bool:
        if holder is not None:
            cursor = conn.execute(
                "UPDATE maintenance_state SET holder=NULL, kind=NULL, cleanup_id=NULL, job_id=NULL, "
                "boot_id=NULL, acquired_at=NULL, version=version+1 "
                "WHERE singleton_id=1 AND holder=?",
                (holder,),
            )
        elif cleanup_id is not None:
            cursor = conn.execute(
                "UPDATE maintenance_state SET holder=NULL, kind=NULL, cleanup_id=NULL, job_id=NULL, "
                "boot_id=NULL, acquired_at=NULL, version=version+1 "
                "WHERE singleton_id=1 AND cleanup_id=?",
                (cleanup_id,),
            )
        else:
            cursor = conn.execute(
                "UPDATE maintenance_state SET holder=NULL, kind=NULL, cleanup_id=NULL, job_id=NULL, "
                "boot_id=NULL, acquired_at=NULL, version=version+1 "
                "WHERE singleton_id=1 AND holder IS NOT NULL"
            )
        return cursor.rowcount == 1

    def assert_maintenance_free(
        self, *, job_id: Optional[str] = None, conn: Optional[sqlite3.Connection] = None
    ) -> None:
        state = self.maintenance(conn)
        if not state.get("holder"):
            return
        if state.get("kind") == "RESULT_CLEANUP" and (
            job_id is None or state.get("job_id") == job_id
        ):
            raise GachaError(
                "STORAGE_BUSY",
                "该任务正在执行结果清理；完成或核清前不能同时写入、导出或再次清理。",
                retryable=True,
                details={
                    "holder": state.get("holder"),
                    "cleanup_id": state.get("cleanup_id"),
                    "job_id": state.get("job_id"),
                },
            )

    # -------------------------------------------------------- idempotency
    @staticmethod
    def idempotency_scope(scope: str, key: str) -> tuple[str, str]:
        if not key or len(key) > 200:
            raise GachaError("BAD_REQUEST", "Idempotency-Key 无效。")
        return scope, key

    def get_idempotency(self, scope: str, key: str) -> Optional[dict[str, Any]]:
        return _dict(
            self.db.query_one(
                "SELECT * FROM idempotency_requests WHERE scope=? AND key=?", (scope, key)
            )
        )

    def begin_idempotency(self, scope: str, key: str, request_hash: str) -> None:
        now = utc_now()
        with self.db.tx() as conn:
            conn.execute(
                """
                INSERT INTO idempotency_requests(scope, key, request_hash, status, created_at, updated_at)
                VALUES (?,?,?,'IN_PROGRESS',?,?)
                """,
                (scope, key, request_hash, now, now),
            )

    def complete_idempotency(
        self,
        scope: str,
        key: str,
        *,
        response: Mapping[str, Any],
        http_status: int,
        entity_id: Optional[str],
    ) -> None:
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE idempotency_requests SET status='COMPLETED', response_json=?, http_status=?, "
                "entity_id=?, updated_at=? WHERE scope=? AND key=?",
                (
                    json.dumps(response, ensure_ascii=False, sort_keys=True),
                    int(http_status),
                    entity_id,
                    utc_now(),
                    scope,
                    key,
                ),
            )

    def fail_idempotency(self, scope: str, key: str) -> None:
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE idempotency_requests SET status='FAILED', updated_at=? WHERE scope=? AND key=?",
                (utc_now(), scope, key),
            )

    def delete_idempotency(self, scope: str, key: str) -> None:
        with self.db.tx() as conn:
            conn.execute("DELETE FROM idempotency_requests WHERE scope=? AND key=?", (scope, key))

    # -------------------------------------------------------------- misc
    def set_runtime_meta(self, key: str, value: str) -> None:
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO runtime_meta(key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, utc_now()),
            )

    def get_runtime_meta(self, key: str) -> Optional[str]:
        row = self.db.query_one("SELECT value FROM runtime_meta WHERE key=?", (key,))
        return str(row[0]) if row else None

    @staticmethod
    def _decode_job(job: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if job is None:
            return None
        try:
            job["bindings"] = json.loads(job.get("bindings_json") or "{}")
        except json.JSONDecodeError:
            job["bindings"] = {}
        try:
            job["warnings"] = json.loads(job.get("warnings_json") or "[]")
        except json.JSONDecodeError:
            job["warnings"] = []
        return job
