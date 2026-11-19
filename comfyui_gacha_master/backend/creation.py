"""Bounded, persistent creation-plan worker.

A JOB is registered (identity, frozen workflow and exact request checkpoint)
and answers ``POST /jobs`` before any source copy or DRAW insert.  This module
then advances the persisted ``creation_progress`` row in short transactions:

* source snapshots are copied outside the database transaction;
* each batch commits DRAW rows, every sampler seed for those DRAWs and the
  progress counter in one transaction;
* pause/abandon intents are persisted and checked before every commit;
* a process restart never resumes automatically: startup reconciles active
  rows to PAUSED and the user must explicitly continue.

Only one worker thread exists per runtime.  There is no thread/future per JOB.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Mapping, Optional

from . import storage
from .errors import GachaError
from .schema import SCHEMA_VERSION
from .states import CreationControl, CreationStage, JobStatus, RunIntent
from .util import fault_point, read_json, sha256_file, sha256_text, utc_now
from .workflow import PreflightResult

LOGGER = logging.getLogger("gacha.creation")

# Initial engineering budget from the production-readiness specification.
TRANSACTION_BUDGET_SECONDS = 0.200


class _Cancelled(Exception):
    """The persisted generation/control intent no longer permits this work."""


class CreationManager:
    def __init__(self, service: Any) -> None:
        self.service = service
        self.settings = service.settings
        self.db = service.db
        self.repo = service.repo
        self.paths = service.paths
        self._thread: Optional[threading.Thread] = None
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._lock = threading.RLock()
        self.batch_size = max(1, int(getattr(self.settings, "plan_insert_batch", 500)))
        self._batch_limit = self.batch_size
        self.processing_job_id: Optional[str] = None

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                self._wake.set()
                return
            self._stopping.clear()
            self._thread = threading.Thread(
                target=self._worker_main, name="gacha-creation", daemon=True
            )
            self._thread.start()

    def notify(self) -> None:
        self._wake.set()

    def stop(self, timeout: float = 30.0) -> None:
        """Stop the worker.  Must succeed before the DB is closed."""
        with self._lock:
            self._stopping.set()
            self._wake.set()
            thread = self._thread
            self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout)
            if thread.is_alive():
                # A batch is bounded, so this indicates a stuck filesystem or
                # database call.  Keep the database open rather than letting a
                # live thread write into a closed instance.
                raise RuntimeError("creation worker did not stop in time")

    # --------------------------------------------------------------- worker
    def _worker_main(self) -> None:
        while not self._stopping.is_set():
            self._wake.wait(0.5)
            self._wake.clear()
            while not self._stopping.is_set():
                claimed = self._claim_next()
                if claimed is None:
                    break
                job_id, generation = claimed
                try:
                    self._process(job_id, generation)
                except _Cancelled:
                    self._settle_cancelled(job_id, generation)
                except GachaError as exc:
                    LOGGER.warning("creation failed for %s: %s", job_id, exc.code)
                    self._fail(job_id, generation, exc.code, exc.message)
                except Exception as exc:  # pragma: no cover - defensive
                    LOGGER.exception("creation worker crashed for %s", job_id)
                    self._fail(
                        job_id,
                        generation,
                        "INTERNAL_ERROR",
                        f"创建计划工作器异常：{type(exc).__name__}: {exc}",
                    )
                finally:
                    self.processing_job_id = None

    def _claim_next(self) -> Optional[tuple[str, int]]:
        with self.db.tx() as conn:
            row = conn.execute(
                """
                SELECT p.* FROM creation_progress p
                JOIN jobs j ON j.id=p.job_id
                WHERE j.status='CREATING' AND p.control_intent='CONTINUE'
                  AND p.stage IN ('WAITING','COPYING','PLANNING','FINALIZING')
                ORDER BY p.created_at LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            generation = int(row["generation"]) + 1
            conn.execute(
                "UPDATE creation_progress SET generation=?, progress_version=progress_version+1, "
                "updated_at=? WHERE job_id=?",
                (generation, utc_now(), row["job_id"]),
            )
            job_id = str(row["job_id"])
            self.processing_job_id = job_id
            return job_id, generation

    # ---------------------------------------------------------- progress I/O
    def _guard(self, conn, job_id: str, generation: int) -> Mapping[str, Any]:
        row = conn.execute(
            "SELECT p.*, j.status AS job_status FROM creation_progress p "
            "JOIN jobs j ON j.id=p.job_id WHERE p.job_id=?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise _Cancelled()
        if int(row["generation"]) != int(generation):
            raise _Cancelled()
        if row["control_intent"] != CreationControl.CONTINUE:
            raise _Cancelled()
        if row["job_status"] != JobStatus.CREATING:
            raise _Cancelled()
        return row

    def _set_stage(self, job_id: str, generation: int, stage: str) -> None:
        with self.db.tx() as conn:
            self._guard(conn, job_id, generation)
            conn.execute(
                "UPDATE creation_progress SET stage=?, progress_version=progress_version+1, updated_at=? "
                "WHERE job_id=? AND generation=?",
                (stage, utc_now(), job_id, generation),
            )

    # ---------------------------------------------------------------- process
    def _load_context(
        self, job_id: str
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], PreflightResult]:
        job = self.repo.get_job(job_id)
        if job is None:
            raise GachaError("NOT_FOUND", "任务不存在。", details={"job_id": job_id})
        request_path = os.path.join(job["job_dir"], "create_request.json")
        if not os.path.isfile(request_path):
            raise GachaError(
                "RECOVERY_REQUIRED",
                "创建请求检查点缺失，无法继续生成计划。",
                details={"job_id": job_id},
            )
        payload = read_json(request_path)
        if sha256_file(job["workflow_api_path"])[0] != job["workflow_api_sha256"]:
            raise GachaError("WORKFLOW_UNSUPPORTED", "冻结 API 工作流哈希发生变化。")
        if sha256_file(job["workflow_ui_path"])[0] != job["workflow_ui_sha256"]:
            raise GachaError("WORKFLOW_UNSUPPORTED", "冻结 UI 工作流哈希发生变化。")
        request = payload.get("request") or {}
        pre = self.service.preflight(
            workflow_ui=payload.get("workflow_ui"),
            workflow_api=payload.get("workflow_api"),
            bindings=request.get("bindings") or {},
        )
        pre.require_ok()
        return job, request, payload, pre

    def _process(self, job_id: str, generation: int) -> None:
        progress = self.repo.get_creation(job_id)
        if progress is None:
            raise GachaError("RECOVERY_REQUIRED", "缺少创建进度记录。")
        job, request, payload, pre = self._load_context(job_id)
        sources_request = list(request.get("sources") or [])
        count = int(request.get("count_per_source") or 0)
        if not sources_request or count < 1:
            raise GachaError("CREATION_CHECKPOINT_INVALID", "创建检查点内容无效。")
        expected_sources = len(sources_request)
        expected_draws = expected_sources * count
        if int(progress["sources_total"]) != expected_sources or int(progress["draws_total"]) != expected_draws:
            raise GachaError(
                "CREATION_CHECKPOINT_INVALID",
                "创建进度总量与请求检查点不一致。",
                details={
                    "progress_sources": progress["sources_total"],
                    "request_sources": expected_sources,
                    "progress_draws": progress["draws_total"],
                    "request_draws": expected_draws,
                },
            )
        recorded_hash = str(progress["request_hash"] or "")
        checkpoint_hash = str(payload.get("request_hash") or "")
        if recorded_hash and checkpoint_hash and recorded_hash != checkpoint_hash:
            # The checkpoint is the authority; a mismatched hash means the
            # plan must not be silently resumed with different content.
            raise GachaError("CREATION_CHECKPOINT_INVALID", "创建请求哈希与持久进度不一致。")

        # ---- stage 1: source snapshots / reference records
        if progress["stage"] != CreationStage.PLANNING:
            self._set_stage(job_id, generation, CreationStage.COPYING)
        existing = {
            int(source["source_order"]): source
            for source in self.repo.all_sources(job_id, include_removed=True)
        }
        for order, requested in enumerate(sources_request, start=1):
            if order in existing:
                continue
            self._check_generation(job_id, generation)
            record = self.service._build_source_record(job, order, requested, pre)
            self._commit_source(job_id, generation, record, expected_sources)
            existing[order] = self.repo.get_source(record["id"]) or record
            fault_point(f"F17_SOURCE_{order}")

        # ---- stage 2: DRAW + seed batches
        self._set_stage(job_id, generation, CreationStage.PLANNING)
        sources = self.repo.all_sources(job_id)
        if len(sources) != expected_sources:
            raise GachaError(
                "CREATION_CHECKPOINT_INVALID",
                "已提交原图数量与请求不一致。",
                details={"committed": len(sources), "expected": expected_sources},
            )
        bindings = [
            binding
            for binding in job["bindings"].get("seed_bindings", [])
            if binding.get("active", True)
        ]
        self._plan_sources(job_id, generation, sources, count, bindings)

        # ---- stage 3: validate and publish READY
        self._finalize(job_id, generation, pre, count, expected_sources, expected_draws, bindings)

    def _check_generation(self, job_id: str, generation: int) -> None:
        if self._stopping.is_set():
            raise _Cancelled()
        row = self.repo.get_creation(job_id)
        if row is None or int(row["generation"]) != int(generation):
            raise _Cancelled()
        if row["control_intent"] != CreationControl.CONTINUE:
            raise _Cancelled()
        job = self.repo.get_job(job_id)
        if job is None or job["status"] != JobStatus.CREATING:
            raise _Cancelled()

    def _commit_source(
        self, job_id: str, generation: int, record: Mapping[str, Any], sources_total: int
    ) -> None:
        now = utc_now()
        with self.db.tx() as conn:
            self._guard(conn, job_id, generation)
            self.repo.insert_source(record, conn=conn)
            committed = int(
                conn.execute("SELECT COUNT(*) FROM sources WHERE job_id=?", (job_id,)).fetchone()[0]
            )
            if committed > sources_total:
                raise GachaError("CREATION_CHECKPOINT_INVALID", "原图数量超过请求总量。")
            conn.execute(
                "UPDATE creation_progress SET sources_committed=?, stage='COPYING', "
                "progress_version=progress_version+1, updated_at=? WHERE job_id=? AND generation=?",
                (committed, now, job_id, generation),
            )

    def _plan_sources(
        self,
        job_id: str,
        generation: int,
        sources: list[Mapping[str, Any]],
        count: int,
        bindings: list[Mapping[str, Any]],
    ) -> None:
        """Fill every source's DRAW range in job-wide batches.

        A batch may span several sources (for 5000 sources x 20 draws this is
        the difference between 200 transactions and 5000).  Each DRAW, all of
        its sampler seeds and the progress counter still commit together.
        """
        rows = {
            row["source_id"]: row
            for row in self.db.query_all(
                "SELECT source_id, COUNT(*) AS n, COALESCE(MIN(draw_no),0) AS mn, "
                "COALESCE(MAX(draw_no),0) AS mx FROM draws WHERE job_id=? GROUP BY source_id",
                (job_id,),
            )
        }
        segments: list[dict[str, Any]] = []
        for source in sources:
            row = rows.get(source["id"])
            done = int(row["n"]) if row else 0
            mn = int(row["mn"]) if row else 0
            mx = int(row["mx"]) if row else 0
            if done > count or (done and (mn != 1 or mx != done)):
                raise GachaError(
                    "CREATION_CHECKPOINT_INVALID",
                    "已提交 DRAW 编号不连续或超过计划数量。",
                    details={
                        "source_id": source["id"], "draws": done,
                        "min_draw_no": mn, "max_draw_no": mx, "count": count,
                    },
                )
            if done < count:
                segments.append({"source": source, "next": done + 1, "remaining": count - done})
        while segments:
            self._check_generation(job_id, generation)
            batch_items: list[dict[str, Any]] = []
            budget = self.batch_size
            while budget > 0 and segments:
                segment = segments[0]
                take = min(segment["remaining"], budget)
                batch_items.append(
                    {"source": segment["source"], "start_no": segment["next"], "count": take}
                )
                segment["next"] += take
                segment["remaining"] -= take
                budget -= take
                if segment["remaining"] == 0:
                    segments.pop(0)
            batch = sum(item["count"] for item in batch_items)
            before_total = self.repo.count_draws(job_id)
            fault_point(f"F17_DRAW_{before_total}_{batch}")
            started = time.perf_counter()
            committed = self._commit_batch(job_id, generation, batch_items, bindings)
            elapsed = time.perf_counter() - started
            total = before_total + batch
            fault_point(f"F18_DRAW_{total}")
            if total >= int(self.repo.get_creation(job_id)["draws_total"]):
                fault_point("F18_LAST")
            if elapsed > TRANSACTION_BUDGET_SECONDS:
                self.batch_size = max(1, batch // 2)
            elif elapsed < TRANSACTION_BUDGET_SECONDS / 4 and self.batch_size < self._batch_limit:
                self.batch_size = min(self._batch_limit, self.batch_size * 2)
            LOGGER.debug(
                "creation batch committed job=%s draws=%s batch=%s sources=%s elapsed=%.3fs",
                job_id,
                committed,
                batch,
                len(batch_items),
                elapsed,
            )

    def _commit_batch(
        self,
        job_id: str,
        generation: int,
        batch_items: list[Mapping[str, Any]],
        bindings: list[Mapping[str, Any]],
    ) -> int:
        from .service import _stable_id

        now = utc_now()
        with self.db.tx() as conn:
            self._guard(conn, job_id, generation)
            for item in batch_items:
                source = item["source"]
                start_no = int(item["start_no"])
                batch = int(item["count"])
                block_id = _stable_id(f"block/{job_id}/{source['id']}/INITIAL")
                block = conn.execute("SELECT id FROM blocks WHERE id=?", (block_id,)).fetchone()
                if block is None:
                    seq = int(
                        conn.execute(
                            "SELECT COALESCE(MAX(enqueue_seq),0)+1 FROM blocks WHERE job_id=?",
                            (job_id,),
                        ).fetchone()[0]
                    )
                    conn.execute(
                        "INSERT INTO blocks(id, job_id, source_id, kind, priority, enqueue_seq, cursor, status, created_at) "
                        "VALUES (?,?,?,'INITIAL',1,?,0,'PENDING',?)",
                        (block_id, job_id, source["id"], seq, now),
                    )
                for offset in range(batch):
                    draw_no = start_no + offset
                    draw_id = _stable_id(f"draw/{job_id}/{source['id']}/{draw_no}")
                    conn.execute(
                        "INSERT INTO draws(id, job_id, source_id, block_id, draw_no, status, row_version, created_at, updated_at) "
                        "VALUES (?,?,?,?,?,'PENDING',1,?,?)",
                        (draw_id, job_id, source["id"], block_id, draw_no, now, now),
                    )
                    for binding in bindings:
                        maximum = min(
                            int(binding.get("max", 9007199254740991)),
                            int(self.settings.seed_domain_max),
                        )
                        self.repo.allocate_seed(
                            conn,
                            job_id=job_id,
                            draw_id=draw_id,
                            node_id=binding["node_id"],
                            input_name=binding["input_name"],
                            seed_max=maximum,
                        )
            total = int(
                conn.execute("SELECT COUNT(*) FROM draws WHERE job_id=?", (job_id,)).fetchone()[0]
            )
            conn.execute(
                "UPDATE creation_progress SET draws_committed=?, stage='PLANNING', "
                "progress_version=progress_version+1, updated_at=? WHERE job_id=? AND generation=?",
                (total, now, job_id, generation),
            )
            return total

    def _finalize(
        self,
        job_id: str,
        generation: int,
        pre: PreflightResult,
        count: int,
        expected_sources: int,
        expected_draws: int,
        bindings: list[Mapping[str, Any]],
    ) -> None:
        # Full independent check before the READY transaction.
        draw_total = self.repo.count_draws(job_id)
        if draw_total != expected_draws:
            raise GachaError(
                "CREATION_CHECKPOINT_INVALID",
                "计划 DRAW 总数与请求不一致。",
                details={"actual": draw_total, "expected": expected_draws},
            )
        sources = self.repo.all_sources(job_id)
        if len(sources) != expected_sources:
            raise GachaError(
                "CREATION_CHECKPOINT_INVALID",
                "计划原图总数与请求不一致。",
                details={"actual": len(sources), "expected": expected_sources},
            )
        # Aggregate checks: one grouped scan per fact table instead of one
        # query per source.  A 100k plan must not spend minutes in validation.
        draw_groups = {
            row["source_id"]: row
            for row in self.db.query_all(
                "SELECT source_id, COUNT(*) AS n, MIN(draw_no) AS mn, MAX(draw_no) AS mx "
                "FROM draws WHERE job_id=? GROUP BY source_id",
                (job_id,),
            )
        }
        block_groups = {
            row["source_id"]: int(row["n"])
            for row in self.db.query_all(
                "SELECT source_id, COUNT(*) AS n FROM blocks "
                "WHERE job_id=? AND kind='INITIAL' GROUP BY source_id",
                (job_id,),
            )
        }
        for source in sources:
            row = draw_groups.get(source["id"])
            if (
                row is None
                or int(row["n"]) != count
                or int(row["mn"]) != 1
                or int(row["mx"]) != count
            ):
                raise GachaError(
                    "CREATION_CHECKPOINT_INVALID",
                    "原图 DRAW 数量或编号不完整。",
                    details={
                        "source_id": source["id"],
                        "actual": None if row is None else int(row["n"]),
                        "min_draw_no": None if row is None else int(row["mn"]),
                        "max_draw_no": None if row is None else int(row["mx"]),
                        "expected": count,
                    },
                )
            if block_groups.get(source["id"], 0) != 1:
                raise GachaError("CREATION_CHECKPOINT_INVALID", "原图缺少唯一的初始块。")
        if bindings:
            seed_total = self.db.query_one(
                "SELECT COUNT(*) AS n, COUNT(DISTINCT seed) AS d FROM seed_allocations WHERE job_id=?",
                (job_id,),
            )
            expected = expected_draws * len(bindings)
            if int(seed_total["n"]) != expected or int(seed_total["d"]) != expected:
                raise GachaError(
                    "CREATION_CHECKPOINT_INVALID",
                    "种子分配不完整或重复。",
                    details={
                        "actual": int(seed_total["n"]),
                        "distinct": int(seed_total["d"]),
                        "expected": expected,
                    },
                )
            per_source = {
                row["source_id"]: int(row["n"])
                for row in self.db.query_all(
                    "SELECT d.source_id, COUNT(s.seed) AS n FROM draws d "
                    "LEFT JOIN seed_allocations s ON s.draw_id=d.id "
                    "WHERE d.job_id=? GROUP BY d.source_id",
                    (job_id,),
                )
            }
            for source in sources:
                expected_source = count * len(bindings)
                if per_source.get(source["id"], 0) != expected_source:
                    raise GachaError(
                        "CREATION_CHECKPOINT_INVALID",
                        "原图种子数量不完整。",
                        details={
                            "source_id": source["id"],
                            "actual": per_source.get(source["id"], 0),
                            "expected": expected_source,
                        },
                    )
            missing = self.db.query_one(
                """
                SELECT COUNT(*) FROM (
                  SELECT d.id, COUNT(s.seed) AS n FROM draws d
                  LEFT JOIN seed_allocations s ON s.draw_id=d.id
                  WHERE d.job_id=? GROUP BY d.id HAVING n != ?
                )
                """,
                (job_id, len(bindings)),
            )
            if int(missing[0]) != 0:
                raise GachaError(
                    "CREATION_CHECKPOINT_INVALID",
                    "存在缺少采样器种子的 DRAW。",
                    details={"draws": int(missing[0]), "expected_per_draw": len(bindings)},
                )

        plan = {
            "schema_version": 1,
            "job_id": job_id,
            "status": "READY",
            "count_per_source": count,
            "sources": len(sources),
            "draws": draw_total,
            "seeds": draw_total * len(bindings),
            "created_at": utc_now(),
        }
        self.service._atomic_json(os.path.join(self.service.repo.get_job(job_id)["job_dir"], "plan.json"), plan)

        self._set_stage(job_id, generation, CreationStage.FINALIZING)
        if self._stopping.is_set():
            raise _Cancelled()
        fault_point(f"F19_{draw_total}")
        with self.db.tx() as conn:
            self._guard(conn, job_id, generation)
            self.repo.update_job(
                job_id,
                status=JobStatus.READY,
                plan_version=1,
                run_intent=RunIntent.STOPPED,
                current_error_code=None,
                current_error_message=None,
                actor="system",
                reason="plan complete",
            )
            conn.execute(
                "UPDATE creation_progress SET stage='DONE', sources_committed=sources_total, "
                "draws_committed=draws_total, error_code=NULL, error_message=NULL, "
                "progress_version=progress_version+1, updated_at=? WHERE job_id=? AND generation=?",
                (utc_now(), job_id, generation),
            )
        fault_point("F20")

        if not self._stopping.is_set():
            try:
                checkpoint_backup = self.service.backups.create_backup(reason="job_created")
                self.repo.append_event(
                    job_id=job_id,
                    entity_type="BACKUP",
                    entity_id=checkpoint_backup["id"],
                    action="BACKUP_CREATED",
                    actor="system",
                    payload={"reason": "job_created"},
                )
            except GachaError as exc:
                try:
                    self.repo.append_event(
                        job_id=job_id,
                        entity_type="BACKUP",
                        entity_id=None,
                        action="BACKUP_FAILED",
                        reason=exc.code,
                        actor="system",
                        payload={"message": exc.message},
                    )
                except Exception:
                    pass
        # Claimed upload inbox entries can now be removed; snapshots remain
        # authoritative.  A failed snapshot never reaches this point.
        job = self.repo.get_job(job_id)
        if job is not None:
            try:
                request = read_json(os.path.join(job["job_dir"], "create_request.json")).get("request") or {}
                for item in request.get("sources") or []:
                    upload_id = item.get("upload_id")
                    if upload_id:
                        self.service.inputs.remove_upload(str(upload_id))
            except Exception:
                pass

    # ------------------------------------------------------------- terminal
    def _settle_cancelled(self, job_id: str, generation: int) -> None:
        """Persist PAUSED after a stop intent, but never override abandon."""
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT p.*, j.status AS job_status FROM creation_progress p "
                "JOIN jobs j ON j.id=p.job_id WHERE p.job_id=?",
                (job_id,),
            ).fetchone()
            if row is None:
                return
            if int(row["generation"]) != int(generation):
                return
            if row["control_intent"] == CreationControl.ABANDON:
                return
            if row["job_status"] != JobStatus.CREATING:
                return
            if row["control_intent"] == CreationControl.STOP and row["stage"] in (
                CreationStage.WAITING,
                CreationStage.COPYING,
                CreationStage.PLANNING,
                CreationStage.FINALIZING,
                CreationStage.PAUSING,
            ):
                conn.execute(
                    "UPDATE creation_progress SET stage='PAUSED', progress_version=progress_version+1, "
                    "updated_at=? WHERE job_id=?",
                    (utc_now(), job_id),
                )

    def _fail(self, job_id: str, generation: int, code: str, message: str) -> None:
        try:
            with self.db.tx() as conn:
                row = conn.execute(
                    "SELECT p.*, j.status AS job_status FROM creation_progress p "
                    "JOIN jobs j ON j.id=p.job_id WHERE p.job_id=?",
                    (job_id,),
                ).fetchone()
                if row is None:
                    return
                if int(row["generation"]) != int(generation):
                    return
                if row["control_intent"] == CreationControl.ABANDON:
                    return
                if row["job_status"] != JobStatus.CREATING:
                    return
                conn.execute(
                    "UPDATE creation_progress SET stage='FAILED', error_code=?, error_message=?, "
                    "progress_version=progress_version+1, updated_at=? WHERE job_id=?",
                    (code, message, utc_now(), job_id),
                )
                self.repo.update_job(
                    job_id,
                    status=JobStatus.BLOCKED,
                    run_intent=RunIntent.STOPPED,
                    current_error_code=code,
                    current_error_message=message,
                    actor="system",
                    reason="creation failed",
                )
        except Exception:
            LOGGER.exception("could not persist creation failure for %s", job_id)

    # ------------------------------------------------------------- startup
    def reconcile_startup(self) -> dict[str, Any]:
        """Never resume on boot.  Rebuild progress for legacy CREATING jobs."""
        report = {"paused": 0, "legacy": 0, "invalid": 0, "completed": 0}
        progress_ids = {row["job_id"] for row in self.repo.list_creation_for_creating_jobs()}
        for job in self.repo.all_jobs():
            if job["status"] != JobStatus.CREATING:
                continue
            row = self.repo.get_creation(job["id"])
            if row is not None:
                if row["stage"] in CreationStage.ACTIVE or row["stage"] == CreationStage.PAUSING:
                    with self.db.tx() as conn:
                        conn.execute(
                            "UPDATE creation_progress SET stage='PAUSED', "
                            "progress_version=progress_version+1, updated_at=? WHERE job_id=?",
                            (utc_now(), job["id"]),
                        )
                    report["paused"] += 1
                elif row["stage"] == CreationStage.DONE:
                    # Crash window between the DONE row and the JOB row is
                    # impossible with the single transaction, but a legacy
                    # hand-edited database may contain it.  Trust the exact
                    # prepared facts only after validation.
                    self._promote_done(job)
                    report["completed"] += 1
                elif row["stage"] == CreationStage.ABANDONED:
                    self._block_abandoned(job["id"])
                continue
            if job["id"] in progress_ids:  # pragma: no cover - defensive
                continue
            outcome = self._adopt_legacy_creation(job)
            report[outcome] += 1
        return report

    def _adopt_legacy_creation(self, job: Mapping[str, Any]) -> str:
        request_path = os.path.join(job["job_dir"], "create_request.json")
        if not os.path.isfile(request_path):
            self._block_with(job["id"], "CREATION_CHECKPOINT_MISSING", "旧创建任务缺少请求检查点，必须人工处理。")
            return "invalid"
        try:
            payload = read_json(request_path)
            request = payload.get("request") or {}
            sources_request = list(request.get("sources") or [])
            count = int(request.get("count_per_source") or 0)
            if not sources_request or count < 1:
                raise ValueError("empty checkpoint")
            request_hash = str(payload.get("request_hash") or sha256_text(json.dumps(payload, sort_keys=True)))
            from .service import _stable_id

            sources = self.repo.all_sources(job["id"], include_removed=True)
            if len(sources) > len(sources_request):
                raise ValueError("more committed sources than requested")
            for source in sources:
                expected_id = _stable_id(f"source/{job['id']}/{int(source['source_order'])}")
                if source["id"] != expected_id:
                    raise ValueError("source identity mismatch")
                if int(source["source_order"]) > len(sources_request):
                    raise ValueError("source order out of range")
            draws = self.repo.count_draws(job["id"])
            if draws > count * len(sources_request):
                raise ValueError("more draws than requested")
            with self.db.tx() as conn:
                self.repo.insert_creation(
                    job_id=job["id"],
                    request_hash=request_hash,
                    sources_total=len(sources_request),
                    draws_total=count * len(sources_request),
                    conn=conn,
                    generation=1,
                )
                committed = conn.execute(
                    "SELECT COUNT(*) FROM sources WHERE job_id=?", (job["id"],)
                ).fetchone()[0]
                conn.execute(
                    "UPDATE creation_progress SET sources_committed=?, draws_committed=?, stage='PAUSED', "
                    "progress_version=2, updated_at=? WHERE job_id=?",
                    (int(committed), draws, utc_now(), job["id"]),
                )
            self.repo.append_event(
                job_id=job["id"],
                entity_type="JOB",
                entity_id=job["id"],
                action="CREATION_ADOPTED_LEGACY",
                actor="system",
                payload={"sources": len(sources), "draws": draws},
            )
            return "legacy"
        except Exception as exc:
            self._block_with(
                job["id"],
                "CREATION_CHECKPOINT_INVALID",
                f"旧创建任务无法根据已提交事实恢复：{type(exc).__name__}: {exc}",
            )
            return "invalid"

    def _block_abandoned(self, job_id: str) -> None:
        job = self.repo.get_job(job_id)
        if job and job["status"] == JobStatus.CREATING:
            self._block_with(job_id, "SNAPSHOT_FAILED", "创建已放弃；未完成计划不会进入 READY。")

    def _block_with(self, job_id: str, code: str, message: str) -> None:
        try:
            self.repo.update_job(
                job_id,
                status=JobStatus.BLOCKED,
                run_intent=RunIntent.STOPPED,
                current_error_code=code,
                current_error_message=message,
                actor="system",
                reason="creation checkpoint invalid",
            )
        except Exception:
            LOGGER.exception("could not block creation job %s", job_id)

    def _promote_done(self, job: Mapping[str, Any]) -> None:
        """Only promote a DONE progress row when the plan facts fully check out."""
        try:
            row = self.repo.get_creation(job["id"])
            if row is None:
                return
            total = self.repo.count_draws(job["id"])
            sources = len(self.repo.all_sources(job["id"], include_removed=True))
            if total != int(row["draws_total"]) or sources != int(row["sources_total"]):
                raise ValueError("progress counters do not match facts")
            self.repo.update_job(
                job["id"],
                status=JobStatus.READY,
                run_intent=RunIntent.STOPPED,
                actor="system",
                reason="finished creation adopted on startup",
            )
        except Exception as exc:
            self._block_with(
                job["id"],
                "CREATION_CHECKPOINT_INVALID",
                f"已完成的创建进度无法与计划事实核对：{type(exc).__name__}: {exc}",
            )
