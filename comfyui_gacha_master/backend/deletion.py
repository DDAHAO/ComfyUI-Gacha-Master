"""Delete a project and its managed files while preserving external copies."""

from __future__ import annotations

import logging
import os
import re
import shutil
from typing import Any

from . import storage
from .errors import GachaError, not_found
from .states import CreationStage, JobStatus, RunIntent
from .util import atomic_write_json, fsync_dir, is_link_like, path_is_within, read_json, utc_now

LOGGER = logging.getLogger("gacha.deletion")


class JobDeletion:
    def __init__(self, db, repo, paths) -> None:
        self.db = db
        self.repo = repo
        self.paths = paths
        self.receipts_root = os.path.join(paths.data_root, "deleted-jobs")

    def _receipt_path(self, job_id: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", job_id):
            raise not_found("任务不存在。", job_id=job_id)
        path = os.path.join(self.receipts_root, job_id + ".json")
        if not path_is_within(self.paths.data_root, path) or is_link_like(self.receipts_root) or is_link_like(path):
            raise GachaError("PATH_NOT_ALLOWED", "项目删除记录路径无效。")
        return path

    def _read_receipt(self, job_id: str) -> dict[str, Any] | None:
        path = self._receipt_path(job_id)
        try:
            receipt = read_json(path)
        except FileNotFoundError:
            return None
        if not isinstance(receipt, dict) or receipt.get("id") != job_id:
            raise GachaError("STATE_CONFLICT", "项目删除记录与任务编号不一致。")
        return receipt

    def has_receipt(self, job_id: str) -> bool:
        return os.path.isfile(self._receipt_path(job_id))

    def _owned_dirs(self, job: dict[str, Any]) -> tuple[str, str]:
        display_id = job["display_id"]
        if not re.fullmatch(r"JOB-[0-9]{8}-[0-9A-F]{6}", display_id):
            raise GachaError("PATH_NOT_ALLOWED", "任务显示编号无效。")
        expected = (
            storage.job_result_dir(self.paths.results_root, display_id),
            os.path.join(self.paths.jobs_root, job["id"]),
        )
        for path, saved in zip(expected, (job["result_dir"], job["job_dir"])):
            # Parent directories may be on another volume. The job directory
            # itself must be the exact child and must not redirect elsewhere.
            if (
                os.path.normcase(os.path.abspath(saved)) != os.path.normcase(path)
                or is_link_like(path)
                or os.path.ismount(path)
                or (os.path.lexists(path) and not os.path.isdir(path))
                or os.path.normcase(os.path.realpath(path))
                != os.path.normcase(os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path)))
            ):
                raise GachaError("PATH_NOT_ALLOWED", "任务目录与当前受控路径不一致。", path=path)
        if not path_is_within(self.paths.data_root, self.paths.jobs_root):
            raise GachaError("PATH_NOT_ALLOWED", "任务数据目录超出插件数据根。")
        if path_is_within(expected[0], self.paths.jobs_root) or path_is_within(expected[1], self.paths.results_root):
            raise GachaError("PATH_NOT_ALLOWED", "任务目录包含共享任务数据或结果根目录。")
        return expected

    def _assert_idle(self, conn, job, scheduler, creation) -> None:
        job_id = job["id"]
        progress = self.repo.get_creation(job_id) if job["status"] == JobStatus.CREATING else None
        creating = job["status"] == JobStatus.CREATING and (progress or {}).get("stage") != CreationStage.PAUSED
        if (
            creating
            or job["status"] in (JobStatus.RUNNING, JobStatus.PAUSING)
            or job["run_intent"] != RunIntent.STOPPED
            or getattr(creation, "processing_job_id", None) == job_id
            or getattr(scheduler, "processing_job_id", None) == job_id
        ):
            raise GachaError("STATE_CONFLICT", "请先暂停该任务，等待当前操作结束后再删除。", retryable=True)
        if conn.execute(
            """
            SELECT 1 FROM draws d JOIN attempts a ON a.draw_id=d.id
            LEFT JOIN execution_slot s ON s.attempt_id=a.id
            WHERE d.job_id=? AND (
                s.attempt_id IS NOT NULL OR
                a.status IN ('CLAIMED','DISPATCHING','QUEUED','RUNNING','VERIFYING','RECONCILING','UNKNOWN')
            ) LIMIT 1
            """,
            (job_id,),
        ).fetchone():
            raise GachaError("STATE_CONFLICT", "该任务仍有执行待结束或待核实，请处理后再删除。")
        if conn.execute(
            "SELECT 1 FROM exports WHERE job_id=? AND status IN ('FROZEN','COPYING') LIMIT 1", (job_id,)
        ).fetchone():
            raise GachaError("STATE_CONFLICT", "导出正在进行，请等待完成后再删除。", retryable=True)

    def delete(self, job_id: str, confirmation_name: Any, *, scheduler, creation) -> dict[str, Any]:
        receipt_path = self._receipt_path(job_id)
        with self.db.tx() as conn:
            job = self.repo.get_job(job_id)
            receipt = self._read_receipt(job_id)
            target = job or receipt
            if target is None:
                raise not_found("任务不存在。", job_id=job_id)
            project_name = target["display_name"] or target["display_id"]
            if not isinstance(confirmation_name, str) or confirmation_name != project_name:
                raise GachaError("BAD_REQUEST", "请输入与项目名称完全一致的内容。")
            if job is not None:
                self.repo.assert_maintenance_free(conn=conn)
                self._assert_idle(conn, job, scheduler, creation)
                dirs = self._owned_dirs(job)
                for export in conn.execute("SELECT batch_dir FROM exports WHERE batch_dir IS NOT NULL"):
                    if any(path_is_within(directory, export["batch_dir"]) for directory in dirs):
                        raise GachaError("PATH_NOT_ALLOWED", "任务目录内存在导出批次，请先移出该导出目录。")
                self.repo.delete_job_records(conn, job_id)
                if receipt is None:
                    receipt = {field: job[field] for field in ("id", "display_id", "display_name", "result_dir", "job_dir")}
                    receipt["requested_at"] = utc_now()
                    # Publish the intent before committing SQLite. It survives
                    # both an interrupted purge and restoring an older backup.
                    atomic_write_json(receipt_path, receipt)
        self._remove_files(receipt)
        return {"id": job_id, "deleted": True}

    def _remove_files(self, receipt: dict[str, Any]) -> None:
        if receipt.get("completed_at"):
            return
        try:
            # A later project can only reuse the display ID after the old
            # results directory is gone. Leave its new files alone on retry.
            other = self.repo.get_job_by_display_id(receipt["display_id"])
            if other is not None:
                raise GachaError("STATE_CONFLICT", "结果目录编号已由其他任务使用。")
            for directory in self._owned_dirs(receipt):
                if os.path.isdir(directory):
                    shutil.rmtree(directory)
                    fsync_dir(os.path.dirname(directory))
            atomic_write_json(self._receipt_path(receipt["id"]), {**receipt, "completed_at": utc_now()})
        except (GachaError, OSError) as exc:
            raise GachaError(
                "JOB_DELETE_INCOMPLETE",
                "项目已从任务列表移除，仍有文件待删除。请检查文件占用或权限后重试，重启插件也会重试。",
                retryable=True,
                details={"job_id": receipt["id"], "error": str(exc)},
            ) from exc

    def reconcile(self) -> None:
        if not os.path.isdir(self.receipts_root) or is_link_like(self.receipts_root):
            return
        for entry in os.scandir(self.receipts_root):
            if not entry.name.endswith(".json") or not entry.is_file(follow_symlinks=False):
                continue
            job_id = entry.name[:-5]
            try:
                receipt = self._read_receipt(job_id)
                if receipt is None:
                    continue
                with self.db.tx() as conn:
                    job = self.repo.get_job(job_id)
                    if job is not None:
                        if any(job[key] != receipt[key] for key in ("display_id", "display_name")):
                            raise GachaError("STATE_CONFLICT", "任务与删除记录不匹配。")
                        if not receipt.get("completed_at"):
                            self._owned_dirs(job)
                        self.repo.delete_job_records(conn, job_id)
                self._remove_files(receipt)
            except (GachaError, OSError, ValueError, KeyError):
                LOGGER.exception("Could not finish deleting job %s", job_id)
