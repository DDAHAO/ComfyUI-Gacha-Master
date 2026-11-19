"""Permanent cleanup of unselected results.

The database is the index, but the durable authority for a deletion is the
per-job journal that lives next to the job data::

    <data_root>/jobs/<job_uuid>/cleanup/<cleanup_uuid>/operation.json
    <data_root>/jobs/<job_uuid>/cleanup/<cleanup_uuid>/items/<output_uuid>.intent.json
    <data_root>/jobs/<job_uuid>/cleanup/<cleanup_uuid>/items/<output_uuid>.result.json

A file is only unlinked after its intent has been fsync'd, and the item is
only marked PURGED after the result receipt has been fsync'd.  Restoring an
older database therefore loses only the index, never the fact that the user
deliberately deleted an image; ``reconcile_startup`` imports those facts back
before ordinary recovery may classify a missing file as an accident.

Everything here is deliberately conservative: a candidate must be a healthy,
accepted, unselected OUT of a confirmed source group, inside both the job
result directory and the results root, not reached through a link/junction,
and not referenced by any export or draft.  Unknown or unreadable evidence
blocks the whole job instead of being treated as "not referenced".
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from . import storage
from .errors import GachaError, not_found
from .repository import RETENTION_AVAILABLE, RETENTION_STATE_SQL
from .states import (
    AttemptStatus,
    CleanupItemStatus,
    CleanupKind,
    CleanupStatus,
    DrawStatus,
    ExportStatus,
    JobStatus,
    OutputHealth,
    RetentionState,
    RunIntent,
)
from .util import (
    atomic_write_json,
    canonical_json,
    ensure_dir,
    is_link_like,
    json_text,
    parse_utc,
    path_is_within,
    read_json,
    sha256_text,
    utc_now,
)

JOURNAL_SCHEMA = 1
PREVIEW_TTL_SECONDS = 15 * 60
IDENTITY_NAME = "storage-identity.json"
THUMBNAIL_DIRS = ("sources", "outputs")

# Activity that means the job is not safely quiescent for a destructive
# cleanup.  COMPLETED/STOPPED is required in addition to these facts.
_BUSY_DRAW_STATUSES = (
    DrawStatus.PENDING,
    DrawStatus.CLAIMED,
    DrawStatus.DISPATCHING,
    DrawStatus.QUEUED,
    DrawStatus.RUNNING,
    DrawStatus.VERIFYING,
    DrawStatus.UNKNOWN,
)
_BUSY_ATTEMPT_STATUSES = (
    AttemptStatus.CLAIMED,
    AttemptStatus.DISPATCHING,
    AttemptStatus.QUEUED,
    AttemptStatus.RUNNING,
    AttemptStatus.VERIFYING,
    AttemptStatus.UNKNOWN,
    AttemptStatus.RECONCILING,
)
_BUSY_EXPORT_STATUSES = (
    ExportStatus.FROZEN,
    ExportStatus.COPYING,
    ExportStatus.RECOVERY_REQUIRED,
)


def _safe_component(value: str, what: str) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise GachaError("BAD_REQUEST", f"{what}无效。")
    if "\x00" in value or "/" in value or "\\" in value:
        raise GachaError("PATH_NOT_ALLOWED", f"{what}包含路径分隔符。", details={"value": value})
    return value


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _expired(value: Optional[str]) -> bool:
    parsed = parse_utc(value)
    return parsed is not None and parsed <= _now_dt()


def _identity_from_stat(st: os.stat_result) -> dict[str, int]:
    return {
        "dev": int(getattr(st, "st_dev", 0) or 0),
        "ino": int(getattr(st, "st_ino", 0) or 0),
        "size": int(st.st_size),
        "mtime_ns": int(getattr(st, "st_mtime_ns", 0) or 0),
    }


def _same_file_identity(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    """Identity check that degrades safely on platforms without inode numbers."""
    if int(expected.get("size", -1)) != int(actual.get("size", -2)):
        return False
    expected_ino = int(expected.get("ino", 0) or 0)
    actual_ino = int(actual.get("ino", 0) or 0)
    if expected_ino and actual_ino and expected_ino != actual_ino:
        return False
    expected_dev = int(expected.get("dev", 0) or 0)
    actual_dev = int(actual.get("dev", 0) or 0)
    if expected_dev and actual_dev and expected_dev != actual_dev:
        return False
    return True


def _hash_file_handle(path: str) -> tuple[str, int, os.stat_result]:
    """Hash through one open handle so a path swap cannot mix two files."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(fd)
        if (before.st_size, getattr(before, "st_ino", 0)) != (
            after.st_size,
            getattr(after, "st_ino", 0),
        ):
            raise GachaError("OUTPUT_CONFLICT", "文件在读取期间被替换。", details={"path": path})
        return digest.hexdigest(), size, before
    finally:
        os.close(fd)


class StorageCleanupManager:
    """Preview, confirm, execute and reconcile a job's unselected-result cleanup."""

    def __init__(self, *, db: Any, repo: Any, settings: Any, paths: Any, storage_module=storage) -> None:
        self.db = db
        self.repo = repo
        self.settings = settings
        self.paths = paths
        self.storage = storage_module
        self._lock = threading.RLock()
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.last_reconcile: Optional[dict[str, Any]] = None

    # ------------------------------------------------------------ identity
    def identity_path(self) -> str:
        return os.path.join(self.paths.data_root, IDENTITY_NAME)

    def ensure_storage_identity(self) -> dict[str, Any]:
        """Load or create the storage instance identity.

        This file is outside SQLite on purpose.  Restoring a database taken
        before a cleanup must not change the identity that authorizes the
        journal; a new identity would silently orphan valid receipts.
        """
        path = self.identity_path()
        if os.path.isfile(path):
            try:
                data = read_json(path)
            except Exception as exc:
                raise GachaError(
                    "CLEANUP_RECOVERY_REQUIRED",
                    "存储实例身份文件损坏，恢复清理日志前必须人工核验。",
                    details={"path": path, "error": str(exc)},
                )
            if not isinstance(data, dict) or not isinstance(data.get("instance_id"), str):
                raise GachaError(
                    "CLEANUP_RECOVERY_REQUIRED",
                    "存储实例身份文件缺少 instance_id。",
                    details={"path": path},
                )
            return data
        adopted = self._adopt_journal_identity()
        data = {
            "schema_version": 1,
            "instance_id": adopted or str(uuid.uuid4()),
            "created_at": utc_now(),
            "data_root_hint": self.paths.data_root,
            "note": "结果清理日志绑定的存储实例身份；迁移数据时必须连同此文件保留。",
        }
        try:
            atomic_write_json(path, data)
        except OSError as exc:
            raise GachaError(
                "CLEANUP_RECOVERY_REQUIRED",
                "无法持久化存储实例身份，已禁止结果清理。",
                details={"path": path, "error": str(exc)},
            )
        return data

    def _journal_instance_ids(self) -> set[str]:
        found: set[str] = set()
        jobs_root = self.paths.jobs_root
        if not os.path.isdir(jobs_root):
            return found
        try:
            job_dirs = list(os.scandir(jobs_root))
        except OSError:
            return found
        for job_entry in job_dirs:
            if not job_entry.is_dir(follow_symlinks=False):
                continue
            cleanup_root = os.path.join(job_entry.path, "cleanup")
            if not os.path.isdir(cleanup_root):
                continue
            try:
                cleanup_dirs = list(os.scandir(cleanup_root))
            except OSError:
                continue
            for cleanup_entry in cleanup_dirs:
                operation = os.path.join(cleanup_entry.path, "operation.json")
                if not os.path.isfile(operation):
                    continue
                try:
                    data = read_json(operation)
                except Exception:
                    continue
                if isinstance(data, dict) and isinstance(data.get("storage_instance_id"), str):
                    found.add(data["storage_instance_id"])
        return found

    def _adopt_journal_identity(self) -> Optional[str]:
        found = self._journal_instance_ids()
        if len(found) == 1:
            return next(iter(found))
        return None

    def storage_instance_id(self) -> str:
        return str(self.ensure_storage_identity()["instance_id"])

    # --------------------------------------------------------- eligibility
    def _job(self, job_id: str) -> dict[str, Any]:
        job = self.repo.get_job(job_id)
        if job is None:
            raise not_found("任务不存在。", job_id=job_id)
        return job

    def _assert_no_unresolved_run(self, job_id: str) -> None:
        row = self.db.query_one(
            "SELECT id FROM cleanup_runs WHERE job_id=? AND status='RECOVERY_REQUIRED' LIMIT 1",
            (job_id,),
        )
        if row is not None:
            raise GachaError(
                "CLEANUP_RECOVERY_REQUIRED",
                "该任务存在尚未核清的清理运行，核清前不能再次预览或删图。",
                details={"cleanup_id": row["id"]},
            )

    def eligibility(self, job: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        """Return a block reason, or None when the job may be previewed."""
        if job["status"] != JobStatus.COMPLETED:
            return {
                "code": "JOB_NOT_COMPLETED",
                "message": "只有状态为 COMPLETED 的任务可以清理未入选结果。",
                "status": job["status"],
            }
        if job["run_intent"] != RunIntent.STOPPED:
            return {
                "code": "RUN_INTENT_ACTIVE",
                "message": "任务仍有运行意图；请先停止任务再清理。",
                "run_intent": job["run_intent"],
            }
        job_id = job["id"]
        # Generation delivery is serialized instance-wide (max_inflight=1):
        # any pending/in-flight/unknown DRAW or attempt means a cleanup must
        # wait, not just the target job's own work.
        busy_draws = self.db.query_one(
            "SELECT COUNT(*) FROM draws WHERE status IN ("
            + ",".join("?" for _ in _BUSY_DRAW_STATUSES)
            + ")",
            _BUSY_DRAW_STATUSES,
        )
        if busy_draws and int(busy_draws[0] or 0) > 0:
            return {
                "code": "BUSY_DRAWS",
                "message": "实例内仍有待执行、在途或待核实的 DRAW；本功能不会暂停或取消执行。",
                "count": int(busy_draws[0]),
            }
        busy_attempts = self.db.query_one(
            "SELECT COUNT(*) FROM attempts WHERE status IN ("
            + ",".join("?" for _ in _BUSY_ATTEMPT_STATUSES)
            + ")",
            _BUSY_ATTEMPT_STATUSES,
        )
        if busy_attempts and int(busy_attempts[0] or 0) > 0:
            return {
                "code": "BUSY_ATTEMPTS",
                "message": "实例内仍有活动、UNKNOWN 或 RECONCILING 的执行；核清前不能清理结果。",
                "count": int(busy_attempts[0]),
            }
        slot = self.repo.get_slot()
        if slot.get("attempt_id"):
            return {
                "code": "SLOT_HELD",
                "message": "实例仍有执行占用全局槽；不能清理结果。",
                "attempt_id": slot.get("attempt_id"),
            }
        busy_exports = self.db.query_one(
            "SELECT id, status FROM exports WHERE status IN ("
            + ",".join("?" for _ in _BUSY_EXPORT_STATUSES)
            + ") LIMIT 1",
            _BUSY_EXPORT_STATUSES,
        )
        if busy_exports is not None:
            return {
                "code": "EXPORT_IN_PROGRESS",
                "message": "存在正在冻结、复制或待恢复的导出；完成或核清后再清理结果。",
                "export_id": busy_exports["id"],
                "status": busy_exports["status"],
            }
        other_running = self.db.query_one(
            "SELECT id FROM jobs WHERE id!=? AND status IN ('RUNNING','PAUSING') LIMIT 1",
            (job_id,),
        )
        if other_running is not None:
            return {
                "code": "OTHER_JOB_RUNNING",
                "message": "另一个任务仍有运行意图；结果清理与生成投递需要串行。",
                "job_id": other_running["id"],
            }
        state = self.repo.maintenance()
        if state.get("holder") and state.get("kind") == "RESULT_CLEANUP":
            return {
                "code": "STORAGE_BUSY",
                "message": "已有结果清理正在执行；同一实例一次只能运行一个清理。",
                "cleanup_id": state.get("cleanup_id"),
                "job_id": state.get("job_id"),
            }
        return None

    def pending_review_sources(self, job: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return (unconfirmed groups with results, confirmed group reviews)."""
        available = self.repo._REVIEW_AVAILABLE
        rows = self.db.query_all(
            f"""
            SELECT s.id AS source_id, s.source_order, s.original_name, s.status,
                   s.result_revision, s.selection_version,
                   r.decision AS review_decision, r.confirmed_at AS review_confirmed_at,
                   r.result_revision AS review_result_revision,
                   r.selection_version AS review_selection_version,
                   r.plan_version AS review_plan_version,
                   (SELECT COUNT(*) FROM draws d
                      JOIN attempts a ON a.id=d.accepted_attempt_id
                      JOIN outputs o ON o.attempt_id=a.id
                     WHERE d.source_id=s.id AND {available}) AS available_count
              FROM sources s
              LEFT JOIN source_reviews r
                ON r.source_id=s.id
               AND r.result_revision=s.result_revision
               AND r.selection_version=s.selection_version
               AND r.plan_version=?
             WHERE s.job_id=? AND s.status!='REMOVED_BY_USER'
             ORDER BY s.source_order
            """,
            (int(job["plan_version"]), job["id"]),
        )
        pending = []
        confirmed = []
        for row in rows:
            if int(row["available_count"] or 0) <= 0:
                continue
            if row["review_decision"] is None:
                pending.append(dict(row))
            else:
                confirmed.append(dict(row))
        return pending, confirmed

    # ------------------------------------------------------ export protect
    def export_protected_ids(self, job_id: str) -> set[str]:
        """Every OUT referenced by an export member, draft or review.

        Raises ``CLEANUP_NOT_ELIGIBLE`` when stored reference data cannot be
        parsed.  Treating an unreadable reference list as "empty" would let a
        later deletion destroy imagery the export still points at.
        """
        protected: set[str] = set()
        exports = self.db.query_all("SELECT id, status, preview_json FROM exports WHERE job_id=?", (job_id,))
        for export in exports:
            raw = export["preview_json"] or "{}"
            try:
                preview = json.loads(raw)
            except Exception as exc:
                raise GachaError(
                    "CLEANUP_NOT_ELIGIBLE",
                    "导出预览数据无法解析，不能确认引用保护范围；已阻止清理。",
                    details={"export_id": export["id"], "error": str(exc)},
                )
            if not isinstance(preview, dict):
                raise GachaError(
                    "CLEANUP_NOT_ELIGIBLE",
                    "导出预览数据格式异常，不能确认引用保护范围；已阻止清理。",
                    details={"export_id": export["id"]},
                )
            sources = preview.get("sources") or []
            if not isinstance(sources, list):
                raise GachaError(
                    "CLEANUP_NOT_ELIGIBLE",
                    "导出预览成员格式异常，不能确认引用保护范围；已阻止清理。",
                    details={"export_id": export["id"]},
                )
            for item in sources:
                if not isinstance(item, dict):
                    raise GachaError(
                        "CLEANUP_NOT_ELIGIBLE",
                        "导出预览成员格式异常，不能确认引用保护范围；已阻止清理。",
                        details={"export_id": export["id"]},
                    )
                ids = item.get("selected_output_ids") or []
                if not isinstance(ids, list) or any(not isinstance(value, str) for value in ids):
                    raise GachaError(
                        "CLEANUP_NOT_ELIGIBLE",
                        "导出预览成员列表无法解析，不能确认引用保护范围；已阻止清理。",
                        details={"export_id": export["id"]},
                    )
                protected.update(ids)
        for row in self.db.query_all("SELECT output_id FROM export_items WHERE export_id IN "
                                     "(SELECT id FROM exports WHERE job_id=?)", (job_id,)):
            protected.add(str(row["output_id"]))
        for row in self.db.query_all(
            "SELECT export_id, source_id, selected_output_ids_json FROM export_reviews "
            "WHERE export_id IN (SELECT id FROM exports WHERE job_id=?)",
            (job_id,),
        ):
            try:
                ids = json.loads(row["selected_output_ids_json"] or "[]")
            except Exception as exc:
                raise GachaError(
                    "CLEANUP_NOT_ELIGIBLE",
                    "导出审核成员列表无法解析，不能确认引用保护范围；已阻止清理。",
                    details={"export_id": row["export_id"], "source_id": row["source_id"], "error": str(exc)},
                )
            if not isinstance(ids, list) or any(not isinstance(value, str) for value in ids):
                raise GachaError(
                    "CLEANUP_NOT_ELIGIBLE",
                    "导出审核成员列表格式异常，不能确认引用保护范围；已阻止清理。",
                    details={"export_id": row["export_id"], "source_id": row["source_id"]},
                )
            protected.update(ids)
        return protected

    # ------------------------------------------------------------ preview
    def _result_root(self, job: Mapping[str, Any]) -> str:
        return str(job["results_root"])

    def _candidate_path(self, job: Mapping[str, Any], rel_path: str) -> str:
        raw = str(rel_path or "")
        if "\x00" in raw or not raw:
            raise GachaError("PATH_NOT_ALLOWED", "结果相对路径无效。", details={"rel_path": raw})
        normalized = raw.replace("\\", "/")
        if normalized.startswith("/") or normalized.startswith("//") or (
            len(normalized) > 1 and normalized[1] == ":"
        ):
            raise GachaError("PATH_NOT_ALLOWED", "结果路径是绝对路径，拒绝处理。", details={"rel_path": raw})
        parts = [part for part in normalized.split("/") if part]
        if not parts or any(part in {".", ".."} for part in parts):
            raise GachaError("PATH_NOT_ALLOWED", "结果路径包含非法段，拒绝处理。", details={"rel_path": raw})
        results_root = self._result_root(job)
        path = os.path.join(results_root, *parts)
        job_root = str(job["result_dir"])
        if not path_is_within(results_root, path):
            raise GachaError(
                "PATH_NOT_ALLOWED",
                "结果路径越出结果根目录，拒绝处理。",
                details={"rel_path": raw, "results_root": results_root},
            )
        if not path_is_within(job_root, path):
            raise GachaError(
                "PATH_NOT_ALLOWED",
                "结果路径不属于该任务的结果目录，拒绝处理。",
                details={"rel_path": raw, "result_dir": job_root},
            )
        self._assert_chain_safe(results_root, path)
        return path

    @staticmethod
    def _assert_chain_safe(root: str, path: str) -> None:
        root_abs = os.path.abspath(root)
        rel = os.path.relpath(os.path.abspath(path), root_abs)
        if rel == ".":
            return
        current = root_abs
        for part in rel.split(os.sep):
            current = os.path.join(current, part)
            try:
                info = os.lstat(current)
            except OSError:
                return
            if stat.S_ISLNK(info.st_mode):
                raise GachaError(
                    "PATH_NOT_ALLOWED",
                    "路径经过符号链接/junction，拒绝删除。",
                    details={"path": current},
                )
            try:
                if os.path.isdir(current) and hasattr(os.path, "isjunction") and os.path.isjunction(current):
                    raise GachaError(
                        "PATH_NOT_ALLOWED",
                        "路径经过 junction，拒绝删除。",
                        details={"path": current},
                    )
            except OSError:
                return

    def _thumbnail_plan(self, candidates: Sequence[Mapping[str, Any]]) -> None:
        """Annotate candidates with an orphaned thumbnail cache entry.

        A content-addressed thumbnail is only removed when no other retained
        OUT still uses the same key; the original-image thumbnails are never
        touched.
        """
        keys = {str(row["sha256"])[:32] for row in candidates}
        if not keys:
            return
        placeholders = ",".join("?" for _ in keys)
        rows = self.db.query_all(
            f"""
            SELECT substr(o.sha256,1,32) AS key, COUNT(*) AS n
              FROM outputs o
              JOIN attempts a ON a.id=o.attempt_id
             WHERE o.health='VERIFIED' AND {RETENTION_AVAILABLE}
               AND substr(o.sha256,1,32) IN ({placeholders})
             GROUP BY substr(o.sha256,1,32)
            """,
            tuple(keys),
        )
        counts = {str(row["key"]): int(row["n"]) for row in rows}
        for row in candidates:
            key = str(row["sha256"])[:32]
            # The candidate itself is one reference; more than one means a
            # shared cache key that must survive this job's cleanup.
            if counts.get(key, 1) > 1:
                row["thumbnail_rel_path"] = None
                row["thumbnail_size"] = 0
                continue
            cache_rel = f"outputs/{key}.jpg"
            cache_abs = os.path.join(self.paths.thumbnails_root, "outputs", f"{key}.jpg")
            try:
                if os.path.isfile(cache_abs) and not is_link_like(cache_abs):
                    row["thumbnail_rel_path"] = cache_rel
                    row["thumbnail_size"] = int(os.path.getsize(cache_abs))
                else:
                    row["thumbnail_rel_path"] = None
                    row["thumbnail_size"] = 0
            except OSError:
                row["thumbnail_rel_path"] = None
                row["thumbnail_size"] = 0

    def _plan_payload(
        self,
        job: Mapping[str, Any],
        items: Sequence[Mapping[str, Any]],
        confirmed: Sequence[Mapping[str, Any]],
        export_protected: set[str],
        storage_revision: int,
    ) -> dict[str, Any]:
        normalized_items = []
        for row in items:
            identity = row.get("file_identity")
            if identity is None:
                identity = json.loads(row.get("file_identity_json") or "{}")
            normalized_items.append(
                {
                    "output_id": row["output_id"],
                    "rel_path": row["rel_path"],
                    "sha256": row["sha256"],
                    "size": int(row["size"]),
                    "file_identity": identity,
                }
            )
        return {
            "job_id": job["id"],
            "job_display_id": job["display_id"],
            "kind": CleanupKind.UNSELECTED_RESULTS,
            "plan_version": int(job["plan_version"]),
            "storage_revision": int(storage_revision),
            "items": normalized_items,
            "reviews": [
                {
                    "source_id": row["source_id"],
                    "result_revision": int(row["result_revision"]),
                    "selection_version": int(row["selection_version"]),
                    "plan_version": int(job["plan_version"]),
                    "decision": row["review_decision"],
                }
                for row in confirmed
            ],
            "protected": {
                "export_ids": sorted(export_protected),
            },
        }

    def preview(self, job_id: str) -> dict[str, Any]:
        job = self._job(job_id)
        with self._lock:
            self._assert_no_unresolved_run(job_id)
            blocked = self.eligibility(job)
            if blocked is not None:
                raise GachaError(
                    "CLEANUP_NOT_ELIGIBLE",
                    blocked["message"],
                    details={**blocked, "job_id": job_id, "next_action": "job_detail"},
                )
            pending, confirmed = self.pending_review_sources(job)
            if pending:
                raise GachaError(
                    "CLEANUP_NOT_ELIGIBLE",
                    "仍有原图组没有按当前结果/选择/计划版本确认，不能清理。请先继续筛选。",
                    details={
                        "job_id": job_id,
                        "pending_sources": [
                            {
                                "source_id": row["source_id"],
                                "source_order": row["source_order"],
                                "original_name": row["original_name"],
                                "available_count": row["available_count"],
                            }
                            for row in pending[:50]
                        ],
                        "pending_count": len(pending),
                        "next_action": "continue_review",
                    },
                )
            all_skipped = bool(confirmed) and all(
                row["review_decision"] == "SKIPPED" for row in confirmed
            )
            export_protected = self.export_protected_ids(job_id)
            return self._build_preview(
                job,
                confirmed,
                export_protected,
                all_skipped=all_skipped,
            )

    def _build_preview(
        self,
        job: Mapping[str, Any],
        confirmed: Sequence[Mapping[str, Any]],
        export_protected: set[str],
        *,
        all_skipped: bool,
    ) -> dict[str, Any]:
        available = self.repo._REVIEW_AVAILABLE
        rows = self.db.query_all(
            f"""
            SELECT o.id AS output_id, o.rel_path, o.sha256, o.size, o.health, o.out_no,
                   s.id AS source_id, s.source_order, s.status AS source_status,
                   d.id AS draw_id, d.draw_no, d.status AS draw_status,
                   a.id AS attempt_id, a.attempt_no, a.status AS attempt_status,
                   d.accepted_attempt_id,
                   COALESCE(sel.selected,0) AS selected,
                   {RETENTION_STATE_SQL} AS retention_state,
                   r.decision AS review_decision
              FROM outputs o
              JOIN attempts a ON a.id=o.attempt_id
              JOIN draws d ON d.id=a.draw_id
              JOIN sources s ON s.id=d.source_id
              LEFT JOIN selections sel ON sel.output_id=o.id
              LEFT JOIN output_retention ret ON ret.output_id=o.id
              LEFT JOIN source_reviews r
                ON r.source_id=s.id
               AND r.result_revision=s.result_revision
               AND r.selection_version=s.selection_version
               AND r.plan_version=?
             WHERE d.job_id=? AND s.status!='REMOVED_BY_USER'
             ORDER BY s.source_order, d.draw_no, o.out_no
            """,
            (int(job["plan_version"]), job["id"]),
        )
        excluded = {
            "abnormal": 0,
            "unaccepted": 0,
            "removed_source": 0,
            "purged": 0,
            "cleaning": 0,
            "unconfirmed": 0,
            "file_missing": 0,
            "file_changed": 0,
            "not_regular": 0,
        }
        protected_selected = 0
        protected_export = 0
        candidates: list[dict[str, Any]] = []
        for raw in rows:
            row = dict(raw)
            retention = row.get("retention_state") or RetentionState.RETAINED
            if row["source_status"] == "REMOVED_BY_USER":
                excluded["removed_source"] += 1
                continue
            if retention == RetentionState.PURGED:
                excluded["purged"] += 1
                continue
            if retention == RetentionState.CLEANING:
                excluded["cleaning"] += 1
                continue
            healthy = (
                row["health"] == OutputHealth.VERIFIED
                and row["draw_status"] == DrawStatus.SUCCEEDED
                and row["accepted_attempt_id"] == row["attempt_id"]
                and row["attempt_status"] == AttemptStatus.SUCCEEDED
            )
            if not healthy:
                excluded["abnormal"] += 1
                continue
            if row["review_decision"] is None:
                excluded["unconfirmed"] += 1
                continue
            if int(row["selected"] or 0) == 1:
                protected_selected += 1
                continue
            if row["output_id"] in export_protected:
                protected_export += 1
                continue
            path = self._candidate_path(job, row["rel_path"])
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                excluded["file_missing"] += 1
                continue
            except OSError:
                excluded["file_missing"] += 1
                continue
            if stat.S_ISLNK(info.st_mode):
                raise GachaError(
                    "PATH_NOT_ALLOWED",
                    "候选结果文件是符号链接，拒绝清理。",
                    details={"output_id": row["output_id"], "path": path},
                )
            if not stat.S_ISREG(info.st_mode):
                excluded["not_regular"] += 1
                continue
            if int(info.st_size) != int(row["size"]):
                excluded["file_changed"] += 1
                continue
            try:
                digest, size, stat_info = _hash_file_handle(path)
            except GachaError:
                excluded["file_changed"] += 1
                continue
            except OSError:
                excluded["file_missing"] += 1
                continue
            if digest != row["sha256"] or int(size) != int(row["size"]):
                excluded["file_changed"] += 1
                continue
            candidates.append(
                {
                    "output_id": row["output_id"],
                    "source_id": row["source_id"],
                    "source_order": int(row["source_order"]),
                    "original_name": None,
                    "draw_id": row["draw_id"],
                    "draw_no": int(row["draw_no"]),
                    "attempt_id": row["attempt_id"],
                    "attempt_no": int(row["attempt_no"]),
                    "out_no": int(row["out_no"]),
                    "rel_path": str(row["rel_path"]),
                    "sha256": str(row["sha256"]),
                    "size": int(row["size"]),
                    "file_identity": _identity_from_stat(stat_info),
                }
            )
        # Annotate candidates with rebuiladable thumbnail cache entries that
        # would become orphaned after this run.
        self._thumbnail_plan(candidates)
        estimated_bytes = sum(int(row["size"]) for row in candidates)
        estimated_thumbnail_bytes = sum(int(row.get("thumbnail_size") or 0) for row in candidates)
        storage_revision = self.repo.get_storage_revision(job["id"])
        plan = self._plan_payload(job, candidates, confirmed, export_protected, storage_revision)
        plan_hash = sha256_text(canonical_json(plan))
        plan_token = secrets.token_urlsafe(32)
        cleanup_id = str(uuid.uuid4())
        now = utc_now()
        expires_at = (_now_dt() + timedelta(seconds=PREVIEW_TTL_SECONDS)).isoformat()
        storage_instance_id = self.storage_instance_id()
        protection = {
            "selected_count": protected_selected,
            "export_count": protected_export,
            "export_reference_count": len(export_protected),
            "excluded": excluded,
            "all_skipped": all_skipped,
            "overlap_note": (
                "入选保护与导出引用保护按互斥顺序统计：先排除入选图片，再统计其中未入选但被导出引用的图片；"
                "异常、未确认、已清理等排除项可能与导出引用重叠，不相加为总数。"
            ),
        }
        with self.db.tx() as conn:
            self._job(job["id"])
            conn.execute(
                "UPDATE cleanup_runs SET status='EXPIRED', updated_at=? "
                "WHERE job_id=? AND status='PREVIEW'",
                (now, job["id"]),
            )
            conn.execute(
                """
                INSERT INTO cleanup_runs(
                    id, job_id, kind, status, storage_instance_id, plan_version, storage_revision,
                    plan_token_hash, plan_hash, protection_json, candidate_count, estimated_bytes,
                    estimated_thumbnail_bytes, protected_selected_count, protected_export_count,
                    all_skipped, expires_at, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    cleanup_id,
                    job["id"],
                    CleanupKind.UNSELECTED_RESULTS,
                    CleanupStatus.PREVIEW,
                    storage_instance_id,
                    int(job["plan_version"]),
                    int(plan["storage_revision"]),
                    sha256_text(plan_token),
                    plan_hash,
                    json_text(protection),
                    len(candidates),
                    estimated_bytes,
                    estimated_thumbnail_bytes,
                    protected_selected,
                    protected_export,
                    1 if all_skipped else 0,
                    expires_at,
                    now,
                    now,
                ),
            )
            for row in candidates:
                conn.execute(
                    """
                    INSERT INTO cleanup_items(
                        cleanup_id, output_id, source_id, draw_id, attempt_id,
                        source_order, draw_no, out_no, rel_path, sha256, size,
                        file_identity_json, thumbnail_rel_path, thumbnail_size, status)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        cleanup_id,
                        row["output_id"],
                        row["source_id"],
                        row["draw_id"],
                        row["attempt_id"],
                        row["source_order"],
                        row["draw_no"],
                        row["out_no"],
                        row["rel_path"],
                        row["sha256"],
                        row["size"],
                        json_text(row["file_identity"]),
                        row.get("thumbnail_rel_path"),
                        int(row.get("thumbnail_size") or 0),
                        CleanupItemStatus.PENDING,
                    ),
                )
        dto = self.get(cleanup_id)
        dto["plan_token"] = plan_token
        return dto

    # -------------------------------------------------------------- reads
    def _run_dto(self, run: Mapping[str, Any]) -> dict[str, Any]:
        protection = json.loads(run.get("protection_json") or "{}")
        confirmation = json.loads(run.get("confirmation_json") or "{}")
        return {
            "id": run["id"],
            "job_id": run["job_id"],
            "kind": run["kind"],
            "status": run["status"],
            "plan_version": int(run["plan_version"]),
            "storage_revision": int(run["storage_revision"]),
            "candidate_count": int(run["candidate_count"]),
            "estimated_bytes": int(run["estimated_bytes"]),
            "estimated_thumbnail_bytes": int(run["estimated_thumbnail_bytes"]),
            "protected_selected_count": int(run["protected_selected_count"]),
            "protected_export_count": int(run["protected_export_count"]),
            "deleted_files": int(run["deleted_files"]),
            "deleted_bytes": int(run["deleted_bytes"]),
            "deleted_thumbnail_bytes": int(run.get("deleted_thumbnail_bytes") or 0),
            "failed_count": int(run["failed_count"]),
            "skipped_count": int(run["skipped_count"]),
            "all_skipped": bool(run.get("all_skipped")),
            "protection": protection,
            "excluded": protection.get("excluded") or {},
            "overlap_note": protection.get("overlap_note"),
            "confirmation": confirmation,
            "error_code": run.get("error_code"),
            "error_message": run.get("error_message"),
            "created_at": run["created_at"],
            "expires_at": run["expires_at"],
            "confirmed_at": run.get("confirmed_at"),
            "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"),
            "updated_at": run["updated_at"],
            "progress": {
                "total": int(run["candidate_count"]),
                "deleted": int(run["deleted_files"]),
                "failed": int(run["failed_count"]),
                "skipped": int(run["skipped_count"]),
            },
            "confirmation_text": (
                f"将永久删除本任务中已完成筛选且未入选的 {int(run['candidate_count'])} 张结果，"
                f"预计减少 {int(run['estimated_bytes'])} 字节的文件占用。"
                "入选图片、原图、导出成品和任务记录会保留。删除后无法通过数据库备份找回图片。"
            ),
        }

    def get(self, cleanup_id: str) -> dict[str, Any]:
        run = self.repo.get_cleanup(cleanup_id)
        if run is None:
            raise not_found("清理记录不存在。", cleanup_id=cleanup_id)
        dto = self._run_dto(run)
        # Progress is derived from item rows, so it stays accurate while the
        # background worker is running and after an interrupted process.
        counts = self.db.query_one(
            "SELECT SUM(CASE WHEN status='DELETED' THEN 1 ELSE 0 END) AS deleted,"
            "SUM(CASE WHEN status='FAILED' THEN 1 ELSE 0 END) AS failed,"
            "SUM(CASE WHEN status='SKIPPED' THEN 1 ELSE 0 END) AS skipped,"
            "COALESCE(SUM(CASE WHEN status='DELETED' THEN size ELSE 0 END),0) AS deleted_bytes,"
            "COALESCE(SUM(CASE WHEN status='DELETED' THEN thumbnail_size ELSE 0 END),0) AS thumb_bytes "
            "FROM cleanup_items WHERE cleanup_id=?",
            (cleanup_id,),
        )
        if counts is not None:
            dto["deleted_files"] = int(counts["deleted"] or 0)
            dto["deleted_bytes"] = int(counts["deleted_bytes"] or 0)
            dto["deleted_thumbnail_bytes"] = int(counts["thumb_bytes"] or 0)
            dto["failed_count"] = int(counts["failed"] or 0)
            dto["skipped_count"] = int(counts["skipped"] or 0)
            dto["progress"] = {
                "total": int(run["candidate_count"]),
                "deleted": dto["deleted_files"],
                "failed": dto["failed_count"],
                "skipped": dto["skipped_count"],
            }
        failures = self.db.query_all(
            "SELECT output_id, status, error_code, error_message FROM cleanup_items "
            "WHERE cleanup_id=? AND status IN ('FAILED') ORDER BY id LIMIT 10",
            (cleanup_id,),
        )
        dto["errors"] = [dict(row) for row in failures]
        return dto

    def items(self, cleanup_id: str, *, limit: int = 50, cursor: Optional[str] = None) -> dict[str, Any]:
        run = self.repo.get_cleanup(cleanup_id)
        if run is None:
            raise not_found("清理记录不存在。", cleanup_id=cleanup_id)
        after = None
        if cursor:
            try:
                payload = json.loads(cursor)
                after = (
                    int(payload["source_order"]),
                    int(payload["draw_no"]),
                    int(payload["out_no"]),
                    str(payload["output_id"]),
                )
            except Exception:
                raise GachaError("BAD_REQUEST", "清理候选分页游标无效。")
        page = self.repo.cleanup_item_page(cleanup_id, limit=limit, after=after)
        next_cursor = None
        if len(page) == max(1, min(int(limit), 200)):
            last = page[-1]
            next_cursor = json.dumps(
                {
                    "source_order": int(last["source_order"]),
                    "draw_no": int(last["draw_no"]),
                    "out_no": int(last["out_no"]),
                    "output_id": last["output_id"],
                },
                separators=(",", ":"),
            )
        return {
            "cleanup_id": cleanup_id,
            "items": [
                {
                    "output_id": row["output_id"],
                    "source_id": row["source_id"],
                    "draw_id": row["draw_id"],
                    "attempt_id": row["attempt_id"],
                    "source_order": int(row["source_order"]),
                    "draw_no": int(row["draw_no"]),
                    "out_no": int(row["out_no"]),
                    "rel_path": row["rel_path"],
                    "sha256": row["sha256"],
                    "size": int(row["size"]),
                    "status": row["status"],
                    "error_code": row.get("error_code"),
                    "error_message": row.get("error_message"),
                    "image_url": f"/outputs/{row['output_id']}/image",
                    "thumbnail_url": f"/outputs/{row['output_id']}/thumbnail",
                }
                for row in page
            ],
            "next_cursor": next_cursor,
        }

    def list_job_cleanups(self, job_id: str, *, limit: int = 20, cursor: Optional[str] = None) -> dict[str, Any]:
        self._job(job_id)
        before = None
        if cursor:
            try:
                before = str(json.loads(cursor)["created_at"])
            except Exception:
                raise GachaError("BAD_REQUEST", "清理历史分页游标无效。")
        runs = self.repo.list_cleanup_runs(job_id, limit=limit, before_created_at=before)
        has_more = len(runs) > max(1, min(int(limit), 100))
        runs = runs[: max(1, min(int(limit), 100))]
        next_cursor = None
        if has_more and runs:
            next_cursor = json.dumps({"created_at": runs[-1]["created_at"]}, separators=(",", ":"))
        return {
            "job_id": job_id,
            "items": [self._run_dto(run) for run in runs],
            "next_cursor": next_cursor,
        }

    # ------------------------------------------------------------ confirm
    def _journal_root(self, job_id: str, cleanup_id: str) -> str:
        job_id = _safe_component(job_id, "任务 ID")
        cleanup_id = _safe_component(cleanup_id, "清理 ID")
        return os.path.join(self.paths.jobs_root, job_id, "cleanup", cleanup_id)

    def _operation_payload(self, run: Mapping[str, Any], job: Mapping[str, Any], token: str) -> dict[str, Any]:
        return {
            "schema_version": JOURNAL_SCHEMA,
            "kind": CleanupKind.UNSELECTED_RESULTS,
            "storage_instance_id": self.storage_instance_id(),
            "cleanup_id": run["id"],
            "job_id": job["id"],
            "job_display_id": job["display_id"],
            "job_dir": job["job_dir"],
            "result_dir": job["result_dir"],
            "results_root": job["results_root"],
            "plan_version": int(run["plan_version"]),
            "storage_revision": int(run["storage_revision"]),
            "plan_token_hash": run["plan_token_hash"],
            "plan_hash": run["plan_hash"],
            "candidate_count": int(run["candidate_count"]),
            "estimated_bytes": int(run["estimated_bytes"]),
            "created_at": run["created_at"],
            "confirmed_at": utc_now(),
            "confirmation": {
                "confirm_permanent_delete": True,
                "actor": "user",
                "summary": f"永久清理 {int(run['candidate_count'])} 张未入选结果",
            },
        }

    def _write_json(self, path: str, payload: Mapping[str, Any]) -> None:
        ensure_dir(os.path.dirname(os.path.abspath(path)))
        atomic_write_json(path, dict(payload))

    def confirm(
        self,
        cleanup_id: str,
        *,
        plan_token: str,
        expected_candidate_count: int,
        confirm_permanent_delete: bool,
    ) -> dict[str, Any]:
        if confirm_permanent_delete is not True:
            raise GachaError(
                "BAD_REQUEST",
                "必须显式确认 confirm_permanent_delete=true 才能永久删除。",
            )
        with self._lock:
            run = self.repo.get_cleanup(cleanup_id)
            if run is None:
                raise not_found("清理预览不存在。", cleanup_id=cleanup_id)
            if not isinstance(plan_token, str) or not plan_token:
                raise GachaError("BAD_REQUEST", "缺少 plan_token。")
            if int(expected_candidate_count) != int(run["candidate_count"]):
                raise GachaError(
                    "CLEANUP_PREVIEW_STALE",
                    "候选数量与预览不一致，请重新预览。",
                    details={
                        "expected_candidate_count": expected_candidate_count,
                        "current_candidate_count": int(run["candidate_count"]),
                    },
                )
            if sha256_text(plan_token) != run["plan_token_hash"]:
                raise GachaError(
                    "CLEANUP_PREVIEW_STALE",
                    "计划令牌不匹配，请重新预览。",
                    details={"cleanup_id": cleanup_id},
                )
            if run["status"] in {
                CleanupStatus.RUNNING,
                CleanupStatus.COMPLETED,
                CleanupStatus.PARTIAL_FAILED,
            }:
                return self.get(cleanup_id)
            if run["status"] in {CleanupStatus.EXPIRED, CleanupStatus.CANCELLED}:
                raise GachaError(
                    "CLEANUP_PREVIEW_STALE",
                    "该清理计划已过期或被停止；请重新预览后再确认。",
                    details={"cleanup_id": cleanup_id, "status": run["status"]},
                )
            if run["status"] == CleanupStatus.RECOVERY_REQUIRED:
                raise GachaError(
                    "CLEANUP_RECOVERY_REQUIRED",
                    "上一次清理尚未核清，不能继续删除。",
                    details={"cleanup_id": cleanup_id},
                )
            if run["status"] != CleanupStatus.PREVIEW:
                raise GachaError("STATE_CONFLICT", "清理状态不允许确认。", details={"status": run["status"]})
            if _expired(run["expires_at"]):
                with self.db.tx() as conn:
                    conn.execute(
                        "UPDATE cleanup_runs SET status='EXPIRED', updated_at=? WHERE id=?",
                        (utc_now(), cleanup_id),
                    )
                raise GachaError(
                    "CLEANUP_PREVIEW_STALE",
                    "预览已过期，请重新预览清理范围。",
                    details={"cleanup_id": cleanup_id, "expires_at": run["expires_at"]},
                )
            job = self._job(run["job_id"])
            self._assert_no_unresolved_run(job["id"])
            blocked = self.eligibility(job)
            if blocked is not None:
                raise GachaError(
                    "CLEANUP_NOT_ELIGIBLE",
                    blocked["message"],
                    details={**blocked, "job_id": job["id"]},
                )
            pending, confirmed = self.pending_review_sources(job)
            if pending:
                raise GachaError(
                    "CLEANUP_PREVIEW_STALE",
                    "原图组确认状态已变化，请重新预览。",
                    details={"cleanup_id": cleanup_id, "pending_count": len(pending)},
                )
            export_protected = self.export_protected_ids(job["id"])
            if int(run["storage_revision"]) != self.repo.get_storage_revision(job["id"]):
                raise GachaError(
                    "CLEANUP_PREVIEW_STALE",
                    "留存版本已变化，请重新预览。",
                    details={"cleanup_id": cleanup_id},
                )
            if int(run["plan_version"]) != int(job["plan_version"]):
                raise GachaError(
                    "CLEANUP_PREVIEW_STALE",
                    "任务计划版本已变化，请重新预览。",
                    details={"cleanup_id": cleanup_id},
                )
            # Recompute the immutable plan from current facts before deleting
            # anything.  A group confirmation or export reference change must
            # block the old confirmation.
            plan_items = self.repo.cleanup_item_page(cleanup_id, limit=200)
            while plan_items:
                last = plan_items[-1]
                after = (
                    int(last["source_order"]),
                    int(last["draw_no"]),
                    int(last["out_no"]),
                    last["output_id"],
                )
                more = self.repo.cleanup_item_page(cleanup_id, limit=200, after=after)
                if not more:
                    break
                plan_items.extend(more)
            current_count = self._current_candidate_count(job, export_protected)
            excluded_at_preview = json.loads(run.get("protection_json") or "{}").get("excluded") or {}
            # Preview only freezes files that existed and matched their stored
            # hash.  Missing/changed/not-regular rows were counted as excluded;
            # adding them back keeps the DB-level count comparable without
            # turning a pre-existing missing file into an endless stale loop.
            expected_db_count = int(run["candidate_count"]) + sum(
                int(excluded_at_preview.get(key) or 0)
                for key in ("file_missing", "file_changed", "not_regular")
            )
            if current_count != expected_db_count:
                raise GachaError(
                    "CLEANUP_PREVIEW_STALE",
                    "候选范围已变化，请重新预览。",
                    details={
                        "cleanup_id": cleanup_id,
                        "preview_count": int(run["candidate_count"]),
                        "current_count": current_count,
                        "expected_count": expected_db_count,
                    },
                )
            recomputed = self._plan_payload(
                job,
                plan_items,
                confirmed,
                export_protected,
                int(run["storage_revision"]),
            )
            if sha256_text(canonical_json(recomputed)) != run["plan_hash"]:
                raise GachaError(
                    "CLEANUP_PREVIEW_STALE",
                    "入选、组确认或导出引用已变化，请重新预览。",
                    details={"cleanup_id": cleanup_id},
                )
            holder = f"result-cleanup:{cleanup_id}"
            now = utc_now()
            with self.db.tx() as conn:
                acquired = self.repo.acquire_maintenance(
                    conn,
                    holder=holder,
                    kind="RESULT_CLEANUP",
                    cleanup_id=cleanup_id,
                    job_id=job["id"],
                    boot_id=str(self.repo.get_runtime_meta("boot_id") or ""),
                )
                if not acquired:
                    raise GachaError(
                        "STORAGE_BUSY",
                        "另一个结果清理已取得维护占用。",
                        retryable=True,
                        details={"cleanup_id": cleanup_id, "holder": self.repo.maintenance(conn)},
                    )
                updated = conn.execute(
                    "UPDATE cleanup_runs SET status='RUNNING', confirmed_at=?, started_at=?, "
                    "confirmation_json=?, updated_at=? WHERE id=? AND status='PREVIEW'",
                    (
                        now,
                        now,
                        json_text(
                            {
                                "confirm_permanent_delete": True,
                                "actor": "user",
                                "candidate_count": int(run["candidate_count"]),
                                "plan_token_hash": run["plan_token_hash"],
                            }
                        ),
                        now,
                        cleanup_id,
                    ),
                )
                if not updated.rowcount:
                    raise not_found("清理预览不存在。", cleanup_id=cleanup_id)
            # Everything below happens outside a DB transaction.  The
            # maintenance slot prevents business writes from changing facts
            # while hashes are checked.
            try:
                self._revalidate_files(job, plan_items)
                operation = self._operation_payload(self.repo.get_cleanup(cleanup_id), job, plan_token)
                self._write_json(
                    os.path.join(self._journal_root(job["id"], cleanup_id), "operation.json"),
                    operation,
                )
            except Exception as exc:
                with self.db.tx() as conn:
                    conn.execute(
                        "UPDATE cleanup_runs SET status='CANCELLED', error_code=?, error_message=?, "
                        "finished_at=?, updated_at=? WHERE id=?",
                        (
                            getattr(exc, "code", "CLEANUP_ABORTED"),
                            getattr(exc, "message", str(exc)),
                            utc_now(),
                            utc_now(),
                            cleanup_id,
                        ),
                    )
                    self.repo.release_maintenance(conn, holder=holder)
                raise
            worker = threading.Thread(
                target=self._execute,
                args=(cleanup_id, holder),
                name=f"gacha-cleanup-{cleanup_id[:8]}",
                daemon=True,
            )
            self._worker = worker
            worker.start()
            return self.get(cleanup_id)

    def _current_candidate_count(self, job: Mapping[str, Any], export_protected: set[str]) -> int:
        available = self.repo._REVIEW_AVAILABLE
        rows = self.db.query_all(
            f"""
            SELECT o.id AS output_id, o.health, d.status AS draw_status,
                   d.accepted_attempt_id, a.status AS attempt_status,
                   COALESCE(sel.selected,0) AS selected,
                   s.status AS source_status,
                   r.decision AS review_decision
              FROM outputs o
              JOIN attempts a ON a.id=o.attempt_id
              JOIN draws d ON d.id=a.draw_id
              JOIN sources s ON s.id=d.source_id
              LEFT JOIN selections sel ON sel.output_id=o.id
              LEFT JOIN source_reviews r
                ON r.source_id=s.id
               AND r.result_revision=s.result_revision
               AND r.selection_version=s.selection_version
               AND r.plan_version=?
             WHERE d.job_id=? AND s.status!='REMOVED_BY_USER'
               AND {available}
            """,
            (int(job["plan_version"]), job["id"]),
        )
        count = 0
        for row in rows:
            if row["review_decision"] is None:
                continue
            if int(row["selected"] or 0) == 1:
                continue
            if row["output_id"] in export_protected:
                continue
            count += 1
        return count

    def _revalidate_files(
        self, job: Mapping[str, Any], items: Sequence[Mapping[str, Any]]
    ) -> None:
        for item in items:
            path = self._candidate_path(job, item["rel_path"])
            try:
                info = os.lstat(path)
            except OSError as exc:
                raise GachaError(
                    "CLEANUP_PREVIEW_STALE",
                    "候选文件已缺失，请重新预览。",
                    details={"output_id": item["output_id"], "error": str(exc)},
                )
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise GachaError(
                    "CLEANUP_PREVIEW_STALE",
                    "候选文件类型已变化，请重新预览。",
                    details={"output_id": item["output_id"]},
                )
            expected = json.loads(item.get("file_identity_json") or "{}")
            if not _same_file_identity(expected, _identity_from_stat(info)):
                raise GachaError(
                    "CLEANUP_PREVIEW_STALE",
                    "候选文件身份或大小已变化，请重新预览。",
                    details={"output_id": item["output_id"]},
                )
            digest, size, _ = _hash_file_handle(path)
            if digest != item["sha256"] or int(size) != int(item["size"]):
                raise GachaError(
                    "CLEANUP_PREVIEW_STALE",
                    "候选文件内容已变化，请重新预览。",
                    details={"output_id": item["output_id"]},
                )

    # ------------------------------------------------------------ execute
    def _execute(self, cleanup_id: str, holder: str) -> None:
        recovery_required = False
        try:
            run = self.repo.get_cleanup(cleanup_id)
            if run is None:
                return
            job = self.repo.get_job(run["job_id"])
            if job is None:
                recovery_required = True
                return
            export_protected = self.export_protected_ids(job["id"])
            last: Optional[tuple[int, int, int, str]] = None
            while not self._stop.is_set():
                page = self.repo.cleanup_item_page(
                    cleanup_id, limit=100, after=last
                )
                if not page:
                    break
                for item in page:
                    if self._stop.is_set():
                        break
                    outcome = self._process_item(job, run, item, export_protected)
                    if outcome == "RECOVERY_REQUIRED":
                        recovery_required = True
                        break
                if recovery_required:
                    break
                last_item = page[-1]
                last = (
                    int(last_item["source_order"]),
                    int(last_item["draw_no"]),
                    int(last_item["out_no"]),
                    last_item["output_id"],
                )
                if len(page) < 100:
                    break
        except Exception:
            recovery_required = True
        finally:
            try:
                self._finish_run(cleanup_id, recovery_required=recovery_required)
            except Exception:
                recovery_required = True
            try:
                with self.db.tx() as conn:
                    self.repo.release_maintenance(conn, holder=holder)
            except Exception:
                pass
            with self._lock:
                self._worker = None

    def _finish_run(self, cleanup_id: str, *, recovery_required: bool) -> None:
        run = self.repo.get_cleanup(cleanup_id)
        if run is None:
            return
        now = utc_now()
        with self.db.tx() as conn:
            counts = conn.execute(
                "SELECT "
                "SUM(CASE WHEN status='DELETED' THEN 1 ELSE 0 END) AS deleted,"
                "SUM(CASE WHEN status='FAILED' THEN 1 ELSE 0 END) AS failed,"
                "SUM(CASE WHEN status='SKIPPED' THEN 1 ELSE 0 END) AS skipped,"
                "COALESCE(SUM(CASE WHEN status='DELETED' THEN size ELSE 0 END),0) AS deleted_bytes,"
                "COALESCE(SUM(CASE WHEN status='DELETED' THEN thumbnail_size ELSE 0 END),0) AS thumb_bytes,"
                "SUM(CASE WHEN status='INTENT' THEN 1 ELSE 0 END) AS unresolved,"
                "SUM(CASE WHEN status='PENDING' THEN 1 ELSE 0 END) AS pending "
                "FROM cleanup_items WHERE cleanup_id=?",
                (cleanup_id,),
            ).fetchone()
            deleted = int(counts["deleted"] or 0)
            failed = int(counts["failed"] or 0)
            skipped = int(counts["skipped"] or 0)
            unresolved = int(counts["unresolved"] or 0)
            pending = int(counts["pending"] or 0)
            if recovery_required or unresolved:
                status = CleanupStatus.RECOVERY_REQUIRED
                error_code = "CLEANUP_RECOVERY_REQUIRED"
                error_message = "清理过程中出现状态不确定项；请重启对账或人工核验，不能当作全部成功。"
            elif pending:
                status = CleanupStatus.CANCELLED
                error_code = "CLEANUP_STOPPED"
                error_message = "清理在完成前被停止；已成功项不会回滚，剩余项需要重新预览确认。"
            elif failed:
                status = CleanupStatus.PARTIAL_FAILED
                error_code = "PARTIAL_FAILED"
                error_message = f"{failed} 张结果未能删除；成功项已释放，失败项保留并可读。"
            else:
                status = CleanupStatus.COMPLETED
                error_code = None
                error_message = None
            conn.execute(
                "UPDATE cleanup_runs SET status=?, deleted_files=?, deleted_bytes=?, "
                "deleted_thumbnail_bytes=?, failed_count=?, skipped_count=?, error_code=?, "
                "error_message=?, finished_at=?, updated_at=? WHERE id=?",
                (
                    status,
                    deleted,
                    int(counts["deleted_bytes"] or 0),
                    int(counts["thumb_bytes"] or 0),
                    failed,
                    skipped,
                    error_code,
                    error_message,
                    now,
                    now,
                    cleanup_id,
                ),
            )
            if status != CleanupStatus.RECOVERY_REQUIRED:
                self.repo.release_maintenance(conn, cleanup_id=cleanup_id)
        if deleted and not recovery_required:
            with self.db.tx() as conn:
                self.repo.bump_storage_revision(conn, run["job_id"])
        self.repo.append_event(
            job_id=run["job_id"],
            entity_type="CLEANUP",
            entity_id=cleanup_id,
            action="CLEANUP_FINISHED",
            to_state=status,
            actor="system",
            payload={
                "deleted_files": deleted,
                "deleted_bytes": int(counts["deleted_bytes"] or 0),
                "failed_count": failed,
                "skipped_count": skipped,
            },
        )

    def _process_item(
        self,
        job: Mapping[str, Any],
        run: Mapping[str, Any],
        item: Mapping[str, Any],
        export_protected: set[str],
    ) -> Optional[str]:
        output_id = item["output_id"]
        cleanup_id = run["id"]
        row = self.db.query_one(
            """
            SELECT o.id, o.health, o.rel_path, o.sha256, o.size,
                   s.id AS source_id, s.status AS source_status, s.source_order,
                   d.id AS draw_id, d.draw_no, d.status AS draw_status, d.accepted_attempt_id,
                   a.id AS attempt_id, a.status AS attempt_status,
                   COALESCE(sel.selected,0) AS selected,
                   COALESCE(ret.state,'RETAINED') AS retention_state
              FROM outputs o
              JOIN attempts a ON a.id=o.attempt_id
              JOIN draws d ON d.id=a.draw_id
              JOIN sources s ON s.id=d.source_id
              LEFT JOIN selections sel ON sel.output_id=o.id
              LEFT JOIN output_retention ret ON ret.output_id=o.id
             WHERE o.id=?
            """,
            (output_id,),
        )
        if row is None or row["source_status"] == "REMOVED_BY_USER":
            return self._finish_item(cleanup_id, output_id, CleanupItemStatus.SKIPPED, "OUTPUT_UNAVAILABLE",
                                     "结果已不可用或原图已移除，跳过。")
        if row["retention_state"] != RetentionState.RETAINED:
            return self._finish_item(cleanup_id, output_id, CleanupItemStatus.SKIPPED, "ALREADY_PURGED",
                                     "结果已处于清理/已清理状态，跳过。")
        healthy = (
            row["health"] == OutputHealth.VERIFIED
            and row["draw_status"] == DrawStatus.SUCCEEDED
            and row["accepted_attempt_id"] == row["attempt_id"]
            and row["attempt_status"] == AttemptStatus.SUCCEEDED
        )
        if not healthy:
            return self._finish_item(cleanup_id, output_id, CleanupItemStatus.SKIPPED, "NOT_ELIGIBLE",
                                     "结果已不再满足健康/接受条件。")
        if int(row["selected"] or 0) == 1:
            return self._finish_item(cleanup_id, output_id, CleanupItemStatus.SKIPPED, "PROTECTED_SELECTED",
                                     "结果已经被选中，已保护。")
        if output_id in export_protected:
            return self._finish_item(cleanup_id, output_id, CleanupItemStatus.SKIPPED, "PROTECTED_EXPORT",
                                     "结果被导出引用，已保护。")
        path = self._candidate_path(job, item["rel_path"])
        try:
            info = os.lstat(path)
        except OSError:
            return self._finish_item(cleanup_id, output_id, CleanupItemStatus.FAILED, "FILE_MISSING",
                                     "执行时文件已缺失；不计入本次释放量。")
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return self._finish_item(cleanup_id, output_id, CleanupItemStatus.FAILED, "FILE_CHANGED",
                                     "执行时文件类型已变化，拒绝删除。")
        expected = json.loads(item.get("file_identity_json") or "{}")
        if not _same_file_identity(expected, _identity_from_stat(info)):
            return self._finish_item(cleanup_id, output_id, CleanupItemStatus.FAILED, "FILE_CHANGED",
                                     "执行时文件身份或大小已变化，拒绝删除。")
        try:
            digest, size, stat_info = _hash_file_handle(path)
        except OSError:
            return self._finish_item(cleanup_id, output_id, CleanupItemStatus.FAILED, "FILE_UNREADABLE",
                                     "执行时文件无法读取，拒绝删除。")
        if digest != item["sha256"] or int(size) != int(item["size"]):
            return self._finish_item(cleanup_id, output_id, CleanupItemStatus.FAILED, "FILE_CHANGED",
                                     "执行时文件内容与计划不一致，拒绝删除。")
        identity = _identity_from_stat(stat_info)
        intent_path, result_path = self._item_paths(job["id"], cleanup_id, output_id)
        intent = {
            "schema_version": JOURNAL_SCHEMA,
            "kind": CleanupKind.UNSELECTED_RESULTS,
            "storage_instance_id": self.storage_instance_id(),
            "cleanup_id": cleanup_id,
            "job_id": job["id"],
            "output_id": output_id,
            "source_id": item["source_id"],
            "draw_id": item["draw_id"],
            "attempt_id": item["attempt_id"],
            "rel_path": item["rel_path"],
            "sha256": item["sha256"],
            "size": int(item["size"]),
            "file_identity": identity,
            "intended_at": utc_now(),
        }
        try:
            self._write_json(intent_path, intent)
        except OSError as exc:
            return self._finish_item(cleanup_id, output_id, CleanupItemStatus.FAILED, "JOURNAL_WRITE_FAILED",
                                     f"无法持久写入删除意图，未删除：{exc}")
        try:
            with self.db.tx() as conn:
                conn.execute(
                    "UPDATE cleanup_items SET status='INTENT', intended_at=? WHERE cleanup_id=? AND output_id=?",
                    (utc_now(), cleanup_id, output_id),
                )
                self.repo.set_retention(
                    conn,
                    output_id,
                    state=RetentionState.CLEANING,
                    cleanup_id=cleanup_id,
                    rel_path=item["rel_path"],
                    sha256=item["sha256"],
                    size=int(item["size"]),
                    reason="user confirmed unselected-result cleanup",
                )
                self.repo.append_event(
                    job_id=job["id"],
                    entity_type="OUTPUT",
                    entity_id=output_id,
                    action="CLEANUP_INTENT",
                    related_id=cleanup_id,
                    actor="user",
                )
        except Exception:
            # No DB intent means no deletion.  Close the durable intent as a
            # cancellation so a later restart does not treat it as abandoned.
            try:
                self._write_json(
                    result_path,
                    {
                        "schema_version": JOURNAL_SCHEMA,
                        "cleanup_id": cleanup_id,
                        "output_id": output_id,
                        "status": "CANCELLED",
                        "reason": "database intent failed",
                        "completed_at": utc_now(),
                    },
                )
            except OSError:
                return "RECOVERY_REQUIRED"
            return self._finish_item(cleanup_id, output_id, CleanupItemStatus.FAILED, "DB_INTENT_FAILED",
                                     "数据库未能记录删除意图，文件未删除。")
        # Re-check immediately before unlink using the path, then delete.
        try:
            current = os.lstat(path)
            if not stat.S_ISREG(current.st_mode) or not _same_file_identity(identity, _identity_from_stat(current)):
                raise GachaError("OUTPUT_CONFLICT", "文件在删除前被替换。")
        except GachaError as exc:
            return self._close_cancelled(job, cleanup_id, output_id, item, result_path, str(exc))
        except OSError as exc:
            return self._close_cancelled(job, cleanup_id, output_id, item, result_path, str(exc))
        deleted_bytes = int(item["size"])
        thumbnail_rel = item.get("thumbnail_rel_path")
        thumbnail_bytes = int(item.get("thumbnail_size") or 0)
        try:
            os.unlink(path)
        except OSError as exc:
            try:
                still = os.path.exists(path)
            except OSError:
                still = True
            if still:
                try:
                    self._write_json(
                        result_path,
                        {
                            "schema_version": JOURNAL_SCHEMA,
                            "cleanup_id": cleanup_id,
                            "output_id": output_id,
                            "status": "FAILED",
                            "error": f"{type(exc).__name__}: {exc}",
                            "completed_at": utc_now(),
                        },
                    )
                except OSError:
                    return "RECOVERY_REQUIRED"
                with self.db.tx() as conn:
                    conn.execute(
                        "UPDATE cleanup_items SET status='FAILED', error_code='DELETE_FAILED', "
                        "error_message=?, completed_at=? WHERE cleanup_id=? AND output_id=?",
                        (str(exc), utc_now(), cleanup_id, output_id),
                    )
                    self.repo.set_retention(conn, output_id, state=RetentionState.RETAINED,
                                            reason="cleanup delete failed; file still present")
                self.repo.append_event(
                    job_id=job["id"], entity_type="OUTPUT", entity_id=output_id,
                    action="CLEANUP_ITEM_FAILED", related_id=cleanup_id, reason=str(exc), actor="system",
                )
                return None
            # The file disappeared despite the error: the durable intent plus
            # absence is exactly the durable deletion evidence.
        try:
            self._write_json(
                result_path,
                {
                    "schema_version": JOURNAL_SCHEMA,
                    "cleanup_id": cleanup_id,
                    "output_id": output_id,
                    "status": "DELETED",
                    "deleted": True,
                    "bytes": deleted_bytes,
                    "sha256": item["sha256"],
                    "size": int(item["size"]),
                    "completed_at": utc_now(),
                },
            )
        except OSError:
            return "RECOVERY_REQUIRED"
        thumb_deleted = 0
        if thumbnail_rel:
            thumb_deleted = self._delete_orphan_thumbnail(thumbnail_rel)
        try:
            with self.db.tx() as conn:
                conn.execute(
                    "UPDATE cleanup_items SET status='DELETED', completed_at=?, "
                    "thumbnail_size=? WHERE cleanup_id=? AND output_id=?",
                    (utc_now(), thumbnail_bytes if thumb_deleted else 0, cleanup_id, output_id),
                )
                self.repo.set_retention(
                    conn,
                    output_id,
                    state=RetentionState.PURGED,
                    cleanup_id=cleanup_id,
                    rel_path=item["rel_path"],
                    sha256=item["sha256"],
                    size=int(item["size"]),
                    reason="user confirmed unselected-result cleanup",
                )
                self.repo.append_event(
                    job_id=job["id"],
                    entity_type="OUTPUT",
                    entity_id=output_id,
                    action="CLEANUP_PURGED",
                    related_id=cleanup_id,
                    actor="user",
                    payload={"bytes": deleted_bytes},
                )
        except Exception:
            return "RECOVERY_REQUIRED"
        return None

    def _item_paths(self, job_id: str, cleanup_id: str, output_id: str) -> tuple[str, str]:
        output_id = _safe_component(output_id, "结果 ID")
        root = os.path.join(self._journal_root(job_id, cleanup_id), "items")
        return (
            os.path.join(root, f"{output_id}.intent.json"),
            os.path.join(root, f"{output_id}.result.json"),
        )

    def _close_cancelled(
        self,
        job: Mapping[str, Any],
        cleanup_id: str,
        output_id: str,
        item: Mapping[str, Any],
        result_path: str,
        reason: str,
    ) -> str:
        try:
            self._write_json(
                result_path,
                {
                    "schema_version": JOURNAL_SCHEMA,
                    "cleanup_id": cleanup_id,
                    "output_id": output_id,
                    "status": "CANCELLED",
                    "reason": reason,
                    "completed_at": utc_now(),
                },
            )
        except OSError:
            return "RECOVERY_REQUIRED"
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE cleanup_items SET status='FAILED', error_code='FILE_CHANGED', "
                "error_message=?, completed_at=? WHERE cleanup_id=? AND output_id=?",
                (reason, utc_now(), cleanup_id, output_id),
            )
            self.repo.set_retention(conn, output_id, state=RetentionState.RETAINED,
                                    reason="cleanup cancelled before delete")
        return None

    def _finish_item(
        self,
        cleanup_id: str,
        output_id: str,
        status: str,
        code: Optional[str],
        message: Optional[str],
    ) -> Optional[str]:
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE cleanup_items SET status=?, error_code=?, error_message=?, completed_at=? "
                "WHERE cleanup_id=? AND output_id=?",
                (status, code, message, utc_now(), cleanup_id, output_id),
            )
        return None

    def _delete_orphan_thumbnail(self, thumbnail_rel_path: str) -> int:
        """Remove one content-addressed output thumbnail if still unreferenced."""
        normalized = str(thumbnail_rel_path).replace("\\", "/")
        parts = [part for part in normalized.split("/") if part]
        if len(parts) != 2 or parts[0] != "outputs" or ".." in parts:
            return 0
        path = os.path.join(self.paths.thumbnails_root, *parts)
        if not path_is_within(self.paths.thumbnails_root, path):
            return 0
        key = os.path.splitext(parts[1])[0]
        row = self.db.query_one(
            f"""
            SELECT COUNT(*) FROM outputs o
            JOIN attempts a ON a.id=o.attempt_id
            JOIN draws d ON d.id=a.draw_id
            WHERE substr(o.sha256,1,32)=? AND o.health='VERIFIED' AND {RETENTION_AVAILABLE}
            """,
            (key,),
        )
        if row and int(row[0] or 0) > 0:
            return 0
        try:
            if is_link_like(path) or not os.path.isfile(path):
                return 0
            size = int(os.path.getsize(path))
            os.unlink(path)
            return size
        except OSError:
            return 0

    # ------------------------------------------------------------- cache
    def clear_thumbnail_cache(self) -> dict[str, Any]:
        """Delete only controlled, rebuildable thumbnail cache files.

        Returns accurate scanned/removed/failed/skipped counts and the logical
        bytes removed.  A file that disappears concurrently (regenerated and
        removed) is counted as skipped, not as a failure.
        """
        root = self.paths.thumbnails_root
        scanned = removed = skipped = failed = 0
        removed_bytes = 0
        errors: list[dict[str, Any]] = []

        def record_error(path: str, exc: Exception) -> None:
            nonlocal failed
            failed += 1
            if len(errors) < 20:
                errors.append({"path": path, "error": f"{type(exc).__name__}: {exc}"})

        if not os.path.isdir(root):
            return {
                "root": root,
                "scanned": 0,
                "removed": 0,
                "bytes": 0,
                "failed": 0,
                "skipped": 0,
                "errors": [],
            }
        if is_link_like(root):
            raise GachaError("PATH_NOT_ALLOWED", "缩略图根目录是链接，拒绝清理。", details={"root": root})
        for subdir in THUMBNAIL_DIRS:
            directory = os.path.join(root, subdir)
            if not os.path.isdir(directory):
                continue
            if is_link_like(directory):
                skipped += 1
                continue
            try:
                entries = list(os.scandir(directory))
            except OSError as exc:
                record_error(directory, exc)
                continue
            for entry in entries:
                try:
                    if entry.is_symlink():
                        skipped += 1
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        skipped += 1
                        continue
                    name = entry.name.lower()
                    if not name.endswith((".jpg", ".jpeg")):
                        skipped += 1
                        continue
                    scanned += 1
                    size = int(entry.stat(follow_symlinks=False).st_size)
                    os.unlink(entry.path)
                    removed += 1
                    removed_bytes += size
                except FileNotFoundError:
                    # Regenerated/removed during the scan: not part of this run.
                    skipped += 1
                except OSError as exc:
                    record_error(entry.path, exc)
        return {
            "root": root,
            "scanned": scanned,
            "removed": removed,
            "bytes": removed_bytes,
            "failed": failed,
            "skipped": skipped,
            "errors": errors,
            "note": "缩略图可重建；再次浏览会重新生成，当前缓存占用可能增加。",
        }

    # --------------------------------------------------------- reconcile
    def reconcile_startup(self) -> dict[str, Any]:
        """Import journals, close interrupted intents, and clear stale leases.

        Runs before ordinary output verification so a journaled deliberate
        deletion can never be reclassified as an accidental MISSING file.
        """
        report: dict[str, Any] = {
            "storage_instance_id": None,
            "journals": 0,
            "imported_runs": 0,
            "recovered_purged": 0,
            "cancelled_intents": 0,
            "conflicts": [],
            "orphan_journals": [],
            "stale_leases_cleared": 0,
        }
        try:
            identity = self.ensure_storage_identity()
            report["storage_instance_id"] = identity["instance_id"]
        except GachaError as exc:
            report["conflicts"].append({"error": exc.message, "code": exc.code})
            self.last_reconcile = report
            return report
        instance_id = identity["instance_id"]
        jobs_root = self.paths.jobs_root
        seen_cleanups: set[str] = set()
        if os.path.isdir(jobs_root):
            for job_entry in list(os.scandir(jobs_root)):
                if not job_entry.is_dir(follow_symlinks=False):
                    continue
                cleanup_root = os.path.join(job_entry.path, "cleanup")
                if not os.path.isdir(cleanup_root):
                    continue
                for cleanup_entry in list(os.scandir(cleanup_root)):
                    if not cleanup_entry.is_dir(follow_symlinks=False):
                        continue
                    seen_cleanups.add(cleanup_entry.name)
                    report["journals"] += 1
                    try:
                        self._reconcile_journal(
                            job_entry.name, cleanup_entry.path, instance_id, report
                        )
                    except Exception as exc:
                        report["conflicts"].append(
                            {
                                "cleanup_id": cleanup_entry.name,
                                "error": f"{type(exc).__name__}: {exc}",
                            }
                        )
        # A lease cannot survive a process restart without reconciliation.
        try:
            state = self.repo.maintenance()
            if state.get("holder"):
                with self.db.tx() as conn:
                    if self.repo.release_maintenance(conn):
                        report["stale_leases_cleared"] += 1
        except Exception:
            pass
        # Any DB run still claiming RUNNING has no live worker after a boot.
        # If no durable operation journal exists, no deletion was ever
        # authorized, so the run can be closed as cancelled instead of
        # blocking every future cleanup forever.
        try:
            runs = self.db.query_all(
                "SELECT id, deleted_files FROM cleanup_runs WHERE status IN ('RUNNING','RECOVERY_REQUIRED')"
            )
            for row in runs:
                cleanup_id = row["id"]
                if cleanup_id in seen_cleanups:
                    continue
                deleted_files = int(row["deleted_files"] or 0)
                status = CleanupStatus.CANCELLED if deleted_files == 0 else CleanupStatus.RECOVERY_REQUIRED
                with self.db.tx() as conn:
                    conn.execute(
                        "UPDATE cleanup_runs SET status=?, error_code=?, error_message=?, "
                        "finished_at=?, updated_at=? WHERE id=?",
                        (
                            status,
                            "CLEANUP_STOPPED" if status == CleanupStatus.CANCELLED else "CLEANUP_RECOVERY_REQUIRED",
                            "进程重启且没有持久清理计划；未删除任何文件。"
                            if status == CleanupStatus.CANCELLED
                            else "进程重启且清理日志缺失，但数据库记录已有删除；需人工核验。",
                            utc_now(),
                            utc_now(),
                            cleanup_id,
                        ),
                    )
                report["conflicts"].append(
                    {
                        "cleanup_id": cleanup_id,
                        "warning": "run had no durable operation journal at startup; status=" + status,
                    }
                )
        except Exception:
            pass
        # A journal that exists but could not be reconciled leaves the run
        # RUNNING.  Do not advertise it as active; require manual attention
        # instead of silently allowing another cleanup.
        try:
            leftover = self.db.query_all("SELECT id FROM cleanup_runs WHERE status='RUNNING'")
            for row in leftover:
                with self.db.tx() as conn:
                    conn.execute(
                        "UPDATE cleanup_runs SET status='RECOVERY_REQUIRED', error_code=?, "
                        "error_message=?, finished_at=?, updated_at=? WHERE id=?",
                        (
                            "CLEANUP_RECOVERY_REQUIRED",
                            "清理日志存在但无法完成对账；需人工核验后才能再次清理。",
                            utc_now(),
                            utc_now(),
                            row["id"],
                        ),
                    )
                report["conflicts"].append(
                    {"cleanup_id": row["id"], "error": "journal present but not reconciled"}
                )
        except Exception:
            pass
        self.last_reconcile = report
        return report

    def _reconcile_journal(
        self,
        job_dir_name: str,
        cleanup_dir: str,
        instance_id: str,
        report: dict[str, Any],
    ) -> None:
        operation_path = os.path.join(cleanup_dir, "operation.json")
        if not os.path.isfile(operation_path):
            report["conflicts"].append(
                {"cleanup_id": os.path.basename(cleanup_dir), "error": "missing operation.json"}
            )
            return
        try:
            operation = read_json(operation_path)
        except Exception as exc:
            report["conflicts"].append(
                {"cleanup_id": os.path.basename(cleanup_dir), "error": f"unreadable operation.json: {exc}"}
            )
            return
        if not isinstance(operation, dict) or operation.get("schema_version") != JOURNAL_SCHEMA:
            report["conflicts"].append(
                {"cleanup_id": os.path.basename(cleanup_dir), "error": "unknown journal schema"}
            )
            return
        if operation.get("storage_instance_id") != instance_id:
            report["conflicts"].append(
                {
                    "cleanup_id": os.path.basename(cleanup_dir),
                    "error": "storage instance identity does not match; journal not used for deletion evidence",
                }
            )
            return
        if operation.get("job_id") != job_dir_name:
            report["conflicts"].append(
                {"cleanup_id": os.path.basename(cleanup_dir), "error": "job id does not match journal path"}
            )
            return
        cleanup_id = str(operation.get("cleanup_id") or os.path.basename(cleanup_dir))
        if cleanup_id != os.path.basename(cleanup_dir):
            report["conflicts"].append(
                {"cleanup_id": cleanup_id, "error": "cleanup id does not match journal directory"}
            )
            return
        job = self.repo.get_job(job_dir_name)
        if job is None:
            report["orphan_journals"].append({"job_id": job_dir_name, "cleanup_id": cleanup_id})
            return
        run = self.repo.get_cleanup(cleanup_id)
        if run is None:
            self._import_run(operation, job, cleanup_dir, report)
            run = self.repo.get_cleanup(cleanup_id)
            if run is None:
                return
        self._reconcile_intents(operation, job, cleanup_dir, run, report)

    def _import_run(
        self,
        operation: Mapping[str, Any],
        job: Mapping[str, Any],
        cleanup_dir: str,
        report: dict[str, Any],
    ) -> None:
        cleanup_id = str(operation["cleanup_id"])
        now = utc_now()
        with self.db.tx() as conn:
            existing = conn.execute("SELECT job_id FROM cleanup_runs WHERE id=?", (cleanup_id,)).fetchone()
            if existing is not None:
                if existing["job_id"] != job["id"]:
                    report["conflicts"].append(
                        {"cleanup_id": cleanup_id, "error": "cleanup id collides with another job"}
                    )
                return
            conn.execute(
                """
                INSERT INTO cleanup_runs(
                    id, job_id, kind, status, storage_instance_id, plan_version, storage_revision,
                    plan_token_hash, plan_hash, candidate_count, estimated_bytes,
                    confirmed_at, expires_at, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    cleanup_id,
                    job["id"],
                    CleanupKind.UNSELECTED_RESULTS,
                    CleanupStatus.RECOVERY_REQUIRED,
                    operation.get("storage_instance_id"),
                    int(operation.get("plan_version") or job["plan_version"]),
                    int(operation.get("storage_revision") or 0),
                    str(operation.get("plan_token_hash") or ""),
                    str(operation.get("plan_hash") or ""),
                    int(operation.get("candidate_count") or 0),
                    int(operation.get("estimated_bytes") or 0),
                    operation.get("confirmed_at"),
                    now,
                    str(operation.get("created_at") or now),
                    now,
                ),
            )
        report["imported_runs"] += 1

    def _reconcile_intents(
        self,
        operation: Mapping[str, Any],
        job: Mapping[str, Any],
        cleanup_dir: str,
        run: Mapping[str, Any],
        report: dict[str, Any],
    ) -> None:
        cleanup_id = run["id"]
        items_dir = os.path.join(cleanup_dir, "items")
        if not os.path.isdir(items_dir):
            self._refresh_imported_run(cleanup_id, report)
            return
        for entry in sorted(os.scandir(items_dir), key=lambda item: item.name):
            if not entry.is_file(follow_symlinks=False) or not entry.name.endswith(".intent.json"):
                continue
            output_id = entry.name[: -len(".intent.json")]
            result_path = os.path.join(items_dir, f"{output_id}.result.json")
            has_result = os.path.isfile(result_path)
            try:
                intent = read_json(entry.path)
            except Exception as exc:
                report["conflicts"].append({"output_id": output_id, "error": f"unreadable intent: {exc}"})
                continue
            if not isinstance(intent, dict) or intent.get("cleanup_id") != cleanup_id:
                report["conflicts"].append({"output_id": output_id, "error": "intent identity mismatch"})
                continue
            if intent.get("storage_instance_id") != operation.get("storage_instance_id"):
                report["conflicts"].append({"output_id": output_id, "error": "intent storage identity mismatch"})
                continue
            if intent.get("job_id") != job["id"]:
                report["conflicts"].append({"output_id": output_id, "error": "intent job mismatch"})
                continue
            if has_result:
                # A finished receipt is authoritative even when the database
                # was restored from before the cleanup: re-import the fact so
                # a PURGED image is never shown as available or MISSING.
                try:
                    result = read_json(result_path)
                except Exception as exc:
                    report["conflicts"].append({"output_id": output_id, "error": f"unreadable result: {exc}"})
                    continue
                if not isinstance(result, dict):
                    report["conflicts"].append({"output_id": output_id, "error": "result is not an object"})
                    continue
                status = {
                    "DELETED": CleanupItemStatus.DELETED,
                    "FAILED": CleanupItemStatus.FAILED,
                    "CANCELLED": CleanupItemStatus.SKIPPED,
                }.get(str(result.get("status") or ""))
                if status is None:
                    report["conflicts"].append({"output_id": output_id, "error": "unknown result status"})
                    continue
                self._import_item(job, cleanup_id, intent, status=status)
                if status == CleanupItemStatus.DELETED:
                    self._set_purged_if_present(job, cleanup_id, intent, report)
                continue
            rel_path = str(intent.get("rel_path") or "")
            try:
                path = self._candidate_path(job, rel_path)
            except GachaError as exc:
                report["conflicts"].append({"output_id": output_id, "error": exc.message})
                continue
            result_path = os.path.join(items_dir, f"{output_id}.result.json")
            exists = os.path.isfile(path)
            if not exists:
                # Exact durable intent + absence = deliberate deletion.
                try:
                    self._write_json(
                        result_path,
                        {
                            "schema_version": JOURNAL_SCHEMA,
                            "cleanup_id": cleanup_id,
                            "output_id": output_id,
                            "status": "DELETED",
                            "deleted": True,
                            "recovered": True,
                            "bytes": int(intent.get("size") or 0),
                            "sha256": intent.get("sha256"),
                            "size": int(intent.get("size") or 0),
                            "completed_at": utc_now(),
                        },
                    )
                except OSError as exc:
                    report["conflicts"].append({"output_id": output_id, "error": f"cannot close intent: {exc}"})
                    continue
                self._import_item(job, cleanup_id, intent, status=CleanupItemStatus.DELETED)
                self._set_purged_if_present(job, cleanup_id, intent, report)
                report["recovered_purged"] += 1
                continue
            # File still exists: verify then close the old intent as cancelled.
            try:
                digest, size, _ = _hash_file_handle(path)
                known = (
                    digest == intent.get("sha256")
                    and int(size) == int(intent.get("size") or -1)
                )
            except OSError:
                known = False
            status = CleanupItemStatus.SKIPPED if known else CleanupItemStatus.FAILED
            try:
                self._write_json(
                    result_path,
                    {
                        "schema_version": JOURNAL_SCHEMA,
                        "cleanup_id": cleanup_id,
                        "output_id": output_id,
                        "status": "CANCELLED",
                        "reason": "file still present after restart; deletion not continued"
                        if known
                        else "file identity changed while intent was open; manual check required",
                        "completed_at": utc_now(),
                    },
                )
            except OSError as exc:
                report["conflicts"].append({"output_id": output_id, "error": f"cannot close intent: {exc}"})
                continue
            self._import_item(job, cleanup_id, intent, status=status)
            report["cancelled_intents"] += 1
            if not known:
                report["conflicts"].append(
                    {"output_id": output_id, "error": "intent size/hash does not match current file"}
                )
        self._refresh_imported_run(cleanup_id, report)

    def _import_item(
        self,
        job: Mapping[str, Any],
        cleanup_id: str,
        intent: Mapping[str, Any],
        *,
        status: str,
    ) -> None:
        output_id = str(intent.get("output_id"))
        row = self.db.query_one(
            """
            SELECT o.id, s.id AS source_id, s.source_order, d.id AS draw_id, d.draw_no,
                   a.id AS attempt_id, o.out_no
              FROM outputs o
              JOIN attempts a ON a.id=o.attempt_id
              JOIN draws d ON d.id=a.draw_id
              JOIN sources s ON s.id=d.source_id
             WHERE o.id=? AND d.job_id=?
            """,
            (output_id, job["id"]),
        )
        if row is None:
            return
        with self.db.tx() as conn:
            existing = conn.execute(
                "SELECT status FROM cleanup_items WHERE cleanup_id=? AND output_id=?",
                (cleanup_id, output_id),
            ).fetchone()
            if existing is not None:
                conn.execute(
                    "UPDATE cleanup_items SET status=?, intended_at=COALESCE(intended_at,?), "
                    "completed_at=? WHERE cleanup_id=? AND output_id=?",
                    (
                        status,
                        intent.get("intended_at"),
                        utc_now() if status != CleanupItemStatus.INTENT else None,
                        cleanup_id,
                        output_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO cleanup_items(
                        cleanup_id, output_id, source_id, draw_id, attempt_id,
                        source_order, draw_no, out_no, rel_path, sha256, size,
                        file_identity_json, status, intended_at, completed_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        cleanup_id,
                        output_id,
                        row["source_id"],
                        row["draw_id"],
                        row["attempt_id"],
                        int(row["source_order"]),
                        int(row["draw_no"]),
                        int(row["out_no"]),
                        str(intent.get("rel_path") or ""),
                        str(intent.get("sha256") or ""),
                        int(intent.get("size") or 0),
                        json_text(intent.get("file_identity") or {}),
                        status,
                        intent.get("intended_at"),
                        utc_now() if status != CleanupItemStatus.INTENT else None,
                    ),
                )

    def _set_purged_if_present(
        self,
        job: Mapping[str, Any],
        cleanup_id: str,
        intent: Mapping[str, Any],
        report: dict[str, Any],
    ) -> None:
        output_id = str(intent.get("output_id"))
        row = self.db.query_one(
            "SELECT o.id, o.health, o.size, o.rel_path, o.sha256, d.job_id FROM outputs o "
            "JOIN attempts a ON a.id=o.attempt_id JOIN draws d ON d.id=a.draw_id WHERE o.id=?",
            (output_id,),
        )
        if row is None or row["job_id"] != job["id"]:
            return
        previous = self.repo.get_retention(output_id)
        with self.db.tx() as conn:
            self.repo.set_retention(
                conn,
                output_id,
                state=RetentionState.PURGED,
                cleanup_id=cleanup_id,
                rel_path=str(intent.get("rel_path") or row["rel_path"]),
                sha256=str(intent.get("sha256") or row["sha256"]),
                size=int(intent.get("size") or row["size"]),
                reason="recovered from durable cleanup journal after database restore",
            )
            if (previous or {}).get("state") != RetentionState.PURGED:
                self.repo.bump_storage_revision(conn, job["id"])

    def _refresh_imported_run(self, cleanup_id: str, report: dict[str, Any]) -> None:
        run = self.repo.get_cleanup(cleanup_id)
        if run is None:
            return
        counts = self.db.query_one(
            "SELECT SUM(CASE WHEN status='DELETED' THEN 1 ELSE 0 END) AS deleted,"
            "SUM(CASE WHEN status='FAILED' THEN 1 ELSE 0 END) AS failed,"
            "SUM(CASE WHEN status='INTENT' THEN 1 ELSE 0 END) AS unresolved,"
            "SUM(CASE WHEN status='PENDING' THEN 1 ELSE 0 END) AS pending,"
            "COALESCE(SUM(CASE WHEN status='DELETED' THEN size ELSE 0 END),0) AS deleted_bytes "
            "FROM cleanup_items WHERE cleanup_id=?",
            (cleanup_id,),
        )
        deleted = int(counts["deleted"] or 0)
        failed = int(counts["failed"] or 0)
        unresolved = int(counts["unresolved"] or 0)
        pending = int(counts["pending"] or 0)
        if unresolved:
            status = CleanupStatus.RECOVERY_REQUIRED
            code = "CLEANUP_RECOVERY_REQUIRED"
            message = "仍有未闭合的删除意图，需人工核验。"
        elif pending:
            status = CleanupStatus.CANCELLED
            code = "CLEANUP_STOPPED"
            message = "重启对账时仍有未执行的候选；未继续删除，需要重新预览。"
        elif failed:
            status = CleanupStatus.PARTIAL_FAILED
            code = "PARTIAL_FAILED"
            message = "部分候选未能删除。"
        elif deleted:
            status = CleanupStatus.COMPLETED
            code = None
            message = None
        else:
            status = CleanupStatus.CANCELLED
            code = None
            message = "重启对账时文件仍在，未继续删除。"
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE cleanup_runs SET status=?, deleted_files=?, deleted_bytes=?, failed_count=?, "
                "error_code=?, error_message=?, updated_at=? WHERE id=?",
                (status, deleted, int(counts["deleted_bytes"] or 0), failed, code, message, utc_now(), cleanup_id),
            )

    # ------------------------------------------------------------- worker
    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=timeout)
