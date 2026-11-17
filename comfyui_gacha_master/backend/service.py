"""Application service: job creation, plan freezing, user operations.

This layer owns transaction boundaries and user-visible semantics.  It never
submits prompts directly; only the scheduler does.  Filesystem work happens
before short transactions and every resume path is idempotent by construction
(deterministic source/block/draw IDs).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from . import storage
from .backups import BackupManager
from .config import Settings, save_settings
from .db import Database
from .errors import GachaError, conflict, not_found
from .images import validate_image_bytes
from .inputs import ImageInfo, InputManager
from .repository import Repository
from .states import (
    AttemptStatus,
    CreationControl,
    CreationStage,
    DrawStatus,
    ExportKind,
    ExportLayout,
    JobStatus,
    OutputHealth,
    ReviewDecision,
    RunIntent,
    SourceStatus,
)
from .util import (
    canonical_json,
    copy_bytes_verified,
    ensure_dir,
    fault_point,
    json_text,
    path_is_within,
    read_json,
    require_disk_space,
    sha256_bytes,
    sha256_file,
    sha256_text,
    utc_now,
    uuid4,
)
from .workflow import PreflightResult, make_bindings_record, preflight, seed_keys


def _namespace(value: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"gacha-master/{value}")


def _stable_id(key: str) -> str:
    return str(uuid.uuid5(_namespace("ids"), key))


def job_dto(
    job: dict[str, Any],
    summary: Optional[dict[str, Any]] = None,
    creation: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    dto = {
        "id": job["id"],
        "display_id": job["display_id"],
        "display_name": job["display_name"],
        "status": job["status"],
        "run_intent": job["run_intent"],
        "protection_mode": job["protection_mode"],
        "bindings": job.get("bindings") or {},
        "warnings": job.get("warnings") or [],
        "plan_version": job["plan_version"],
        "row_version": job["row_version"],
        "recovery_version": job["recovery_version"],
        "error_code": job.get("current_error_code"),
        "error_message": job.get("current_error_message"),
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "started_at": job.get("started_at"),
        "completed_at": job.get("completed_at"),
        "summary": summary or {},
    }
    if creation is not None:
        dto["creation"] = creation
    return dto


def creation_dto(progress: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Persisted creation progress.  Counters come from committed facts."""
    if not progress:
        return None
    return {
        "stage": progress["stage"],
        "control_intent": progress["control_intent"],
        "sources_total": int(progress["sources_total"]),
        "sources_committed": int(progress["sources_committed"]),
        "draws_total": int(progress["draws_total"]),
        "draws_committed": int(progress["draws_committed"]),
        "progress_version": int(progress["progress_version"]),
        "generation": int(progress["generation"]),
        "error_code": progress.get("error_code"),
        "error_message": progress.get("error_message"),
        "updated_at": progress.get("updated_at"),
    }


def source_dto(source: dict[str, Any], summary: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    return {
        "id": source["id"],
        "job_id": source["job_id"],
        "source_order": source["source_order"],
        "original_name": source["original_name"],
        "status": source["status"],
        "size": source["size"],
        "sha256": source["sha256"],
        "width": source["width"],
        "height": source["height"],
        "image_format": source["image_format"],
        "result_revision": source["result_revision"],
        "selection_version": source["selection_version"],
        "created_at": source["created_at"],
        "removed_at": source.get("removed_at"),
        "summary": summary or {},
    }


def output_dto(output: dict[str, Any]) -> dict[str, Any]:
    retention_state = output.get("retention_state") or "RETAINED"
    return {
        "id": output["id"],
        "attempt_id": output["attempt_id"],
        "out_no": output["out_no"],
        "draw_no": output.get("draw_no"),
        "attempt_no": output.get("attempt_no"),
        "health": output["health"],
        "retention_state": retention_state,
        "available": retention_state == "RETAINED",
        "width": output["width"],
        "height": output["height"],
        "size": output["size"],
        "sha256": output["sha256"],
        "selected": bool(output.get("selected", 0)),
        "selection_version": output.get("selection_version", 0),
        "viewed_at": output.get("viewed_at"),
        "purged_at": output.get("purged_at"),
        "image_url": f"/outputs/{output['id']}/image",
        "thumbnail_url": f"/outputs/{output['id']}/thumbnail?v={output['sha256'][:16]}",
    }


class Service:
    def __init__(
        self,
        *,
        db: Database,
        repo: Repository,
        settings: Settings,
        paths: Any,
        inputs: InputManager,
        backup_manager: BackupManager,
        scheduler: Any = None,
        exports: Any = None,
        cleanup: Any = None,
        capabilities_provider: Optional[Callable[[], Any]] = None,
        include_metadata: bool = True,
    ) -> None:
        self.db = db
        self.repo = repo
        self.settings = settings
        self.paths = paths
        self.inputs = inputs
        self.backups = backup_manager
        self.scheduler = scheduler
        self.exports = exports
        self.cleanup = cleanup
        self.capabilities_provider = capabilities_provider
        self.include_metadata = include_metadata
        self.creation: Any = None
        self.write_lock = __import__("threading").RLock()
        from .review import ReviewManager

        self.review = ReviewManager(self)

    # ------------------------------------------------------------- helpers
    def _job_or_404(self, job_id: str) -> dict[str, Any]:
        job = self.repo.get_job(job_id)
        if job is None:
            raise not_found("任务不存在。", job_id=job_id)
        return job

    def _job_by_display_or_id(self, value: str) -> dict[str, Any]:
        return self.repo.get_job(value) or self.repo.get_job_by_display_id(value) or self._raise_job(value)

    def _output_job_id(self, output_id: str) -> Optional[str]:
        row = self.db.query_one(
            "SELECT d.job_id FROM outputs o JOIN attempts a ON a.id=o.attempt_id "
            "JOIN draws d ON d.id=a.draw_id WHERE o.id=?",
            (output_id,),
        )
        return str(row[0]) if row else None

    def _raise_job(self, job_id: str):
        raise not_found("任务不存在。", job_id=job_id)

    def _unique_display_id(self, stamp: str) -> str:
        for _ in range(50):
            candidate = f"JOB-{stamp}-{uuid.uuid4().hex[:6].upper()}"
            if self.repo.get_job_by_display_id(candidate) is None:
                return candidate
        raise GachaError("INTERNAL_ERROR", "无法生成唯一任务编号。")

    def _settings_roots(self) -> None:
        # Ensure every configured root exists and is absolute.
        for root in list(self.settings.allowed_input_roots):
            if not os.path.isdir(root):
                raise GachaError("PATH_NOT_ALLOWED", "输入根目录不存在。", details={"root": root})
        for root in list(self.settings.allowed_export_roots):
            if not os.path.isdir(root):
                raise GachaError("PATH_NOT_ALLOWED", "导出根目录不存在。", details={"root": root})

    # ---------------------------------------------------------- preflight
    def preflight(
        self,
        *,
        workflow_ui: Any,
        workflow_api: Any,
        bindings: Mapping[str, Any],
        node_info_provider: Optional[Callable[[str], Any]] = None,
    ) -> PreflightResult:
        result = preflight(
            workflow_ui,
            workflow_api,
            bindings,
            node_info_provider=node_info_provider,
        )
        return result

    # ----------------------------------------------------------- create
    def create_job(
        self,
        request: Mapping[str, Any],
        *,
        idempotency_key: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> dict[str, Any]:
        with self.write_lock:
            mode = str(request.get("protection_mode") or self.settings.protection_mode)
            if mode not in {"snapshot", "reference"}:
                raise GachaError("BAD_REQUEST", "protection_mode 只能是 snapshot 或 reference。")
            count = int(request.get("count_per_source") or 0)
            if count < 1:
                raise GachaError("BAD_REQUEST", "每个原图的抽卡次数必须是正整数。")
            sources_request = request.get("sources") or []
            if not isinstance(sources_request, list) or not sources_request:
                raise GachaError("BAD_REQUEST", "至少需要一张原图。")
            if len(sources_request) > 5000:
                raise GachaError("BAD_REQUEST", "单次任务原图数量过多。")
            total_draws = count * len(sources_request)
            if total_draws > int(self.settings.plan_limit):
                raise GachaError(
                    "BAD_REQUEST",
                    "计划 DRAW 数超过单任务上限。",
                    details={"total": total_draws, "limit": self.settings.plan_limit},
                )
            if mode == "reference":
                for item in sources_request:
                    if item.get("upload_id"):
                        raise GachaError(
                            "WORKFLOW_UNSUPPORTED",
                            "浏览器上传没有可持久访问的原路径，不能使用引用模式。",
                        )
            bindings = request.get("bindings") or {}
            if not bindings.get("load_image_node_id") or not bindings.get("save_image_node_id"):
                raise GachaError("WORKFLOW_UNSUPPORTED", "必须绑定一个 LoadImage 和一个最终 SaveImage。")
            workflow_ui = request.get("workflow_ui")
            workflow_api = request.get("workflow_api")
            pre = self.preflight(workflow_ui=workflow_ui, workflow_api=workflow_api, bindings=bindings)
            pre.require_ok()

            request_hash = sha256_text(
                canonical_json(
                    {
                        "name": request.get("name"),
                        "protection_mode": mode,
                        "count_per_source": count,
                        "sources": sources_request,
                        "bindings": bindings,
                        "workflow_ui": workflow_ui,
                        "workflow_api": workflow_api,
                    }
                )
            )
            if idempotency_key:
                job_id = _stable_id(f"job/{idempotency_key}")
            else:
                job_id = uuid4()
            existing = self.repo.get_job(job_id)
            if existing is not None:
                checkpoint = os.path.join(existing["job_dir"], "create_request.json")
                if os.path.isfile(checkpoint):
                    old = read_json(checkpoint)
                    if old.get("request_hash") not in (None, request_hash):
                        raise GachaError(
                            "IDEMPOTENCY_CONFLICT",
                            "相同任务标识对应了不同的创建内容。",
                            details={"job_id": existing["id"]},
                        )
                # A retried create with the same identity never starts a second
                # plan worker or allocates new seeds.  The caller reads current
                # progress from GET /jobs/{id}.
                return self.job_detail(existing["id"])
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
            display_id = self._unique_display_id(stamp)
            job_dir = os.path.join(self.paths.jobs_root, job_id)
            ensure_dir(job_dir)
            result_dir = storage.job_result_dir(self.paths.results_root, display_id)
            try:
                os.makedirs(result_dir, exist_ok=False)
            except FileExistsError:
                raise GachaError("OUTPUT_CONFLICT", "任务结果目录已存在，拒绝复用。", details={"path": result_dir})
            ui_path = os.path.join(job_dir, "workflow_ui.json")
            api_path = os.path.join(job_dir, "workflow_api.json")
            self._write_frozen_json(ui_path, workflow_ui)
            self._write_frozen_json(api_path, workflow_api)
            bind_record = make_bindings_record(pre)
            bind_record["seed_bindings"] = [
                binding.to_dict() for binding in pre.seed_bindings if binding.active or binding.active
            ]
            now = utc_now()
            job = self.repo.create_job(
                {
                    "id": job_id,
                    "display_id": display_id,
                    "display_name": str(request.get("name") or f"抽卡任务 {stamp}"),
                    "status": JobStatus.CREATING,
                    "run_intent": RunIntent.STOPPED,
                    "protection_mode": mode,
                    "workflow_ui_path": ui_path,
                    "workflow_api_path": api_path,
                    "workflow_ui_sha256": sha256_file(ui_path)[0],
                    "workflow_api_sha256": sha256_file(api_path)[0],
                    "bindings": bind_record,
                    "warnings": pre.warnings,
                    "data_root": self.paths.data_root,
                    "results_root": self.paths.results_root,
                    "job_dir": job_dir,
                    "result_dir": result_dir,
                }
            )
            # Persist the exact creation request so CREATING can be resumed
            # after a crash without inventing a new plan.
            atomic_request = {
                "schema_version": 1,
                "request": {
                    "name": job["display_name"],
                    "protection_mode": mode,
                    "count_per_source": count,
                    "sources": sources_request,
                    "bindings": bindings,
                },
                "workflow_ui": workflow_ui,
                "workflow_api": workflow_api,
                "request_hash": request_hash,
            }
            try:
                self._atomic_json(os.path.join(job_dir, "create_request.json"), atomic_request)
            except Exception as exc:
                self.repo.update_job(
                    job_id,
                    status=JobStatus.BLOCKED,
                    run_intent=RunIntent.STOPPED,
                    current_error_code="SNAPSHOT_FAILED",
                    current_error_message="创建检查点无法写入，任务保持未完成。",
                    actor="system",
                    reason="create checkpoint write failed",
                )
                raise GachaError("SNAPSHOT_FAILED", "创建检查点无法写入。", details={"error": str(exc)})
            try:
                self.repo.insert_creation(
                    job_id=job_id,
                    request_hash=request_hash,
                    sources_total=len(sources_request),
                    draws_total=total_draws,
                    stage=CreationStage.WAITING,
                )
            except Exception as exc:
                self.repo.update_job(
                    job_id,
                    status=JobStatus.BLOCKED,
                    run_intent=RunIntent.STOPPED,
                    current_error_code="SNAPSHOT_FAILED",
                    current_error_message="创建进度无法持久化，任务保持未完成。",
                    actor="system",
                    reason="creation progress insert failed",
                )
                raise GachaError("SNAPSHOT_FAILED", "创建进度无法持久化。", details={"error": str(exc)})
            # The response proves the request is safely recorded, not that the
            # plan is READY.  Only a separate explicit start authorizes work.
            if self.creation is not None:
                self.creation.notify()
            return self.job_detail(job_id)

    def _write_frozen_json(self, path: str, value: Any) -> None:
        ensure_dir(os.path.dirname(path))
        data = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        temp = storage.write_temp_file(os.path.dirname(path), os.path.basename(path), data)
        try:
            storage.publish_noreplace(temp, path)
        finally:
            if os.path.exists(temp):
                try:
                    os.unlink(temp)
                except OSError:
                    pass

    def _atomic_json(self, path: str, value: Any) -> None:
        from .util import atomic_write_json

        atomic_write_json(path, value)

    def _build_source_record(
        self,
        job: dict[str, Any],
        source_order: int,
        requested: Mapping[str, Any],
        pre: PreflightResult,
    ) -> dict[str, Any]:
        """Prepare (and when needed copy) one source without touching the DB.

        File reads, decoding, hashing and snapshot publication happen outside
        every database transaction.  The caller commits the returned record in
        the same transaction as the progress counter.
        """
        source_id = _stable_id(f"source/{job['id']}/{source_order}")
        mode = job["protection_mode"]
        upload_id = requested.get("upload_id")
        if upload_id:
            upload = self.inputs.get_upload(str(upload_id))
            if upload["sha256"] != requested.get("sha256") and requested.get("sha256"):
                raise GachaError(
                    "INPUT_CHANGED",
                    "上传内容在扫描后发生变化。",
                    details={"upload_id": upload_id},
                )
            with open(upload["path"], "rb") as handle:
                data = handle.read(self.settings.upload_max_file + 1)
            info = validate_image_bytes(data, max_pixels=self.settings.decode_max_pixels)
            snapshot = self.inputs.snapshot_source(
                job=job,
                source_id=source_id,
                source_order=source_order,
                original_name=upload["original_name"],
                source_bytes=data,
                image_info=info,
                original_path=None,
            )
            return {
                "id": source_id,
                "job_id": job["id"],
                "source_order": source_order,
                "original_name": upload["original_name"],
                "snapshot_path": snapshot["snapshot_path"],
                "size": info.size,
                "sha256": info.sha256,
                "width": info.width,
                "height": info.height,
                "image_format": info.format,
            }
        root = requested.get("root")
        rel = requested.get("rel_path") or requested.get("path")
        if not root or not rel:
            raise GachaError("BAD_REQUEST", "原图条目缺少 root/rel_path。", details={"source_order": source_order})
        absolute = os.path.abspath(os.path.join(str(root), str(rel)))
        self.inputs.resolve_allowed_root(os.path.dirname(absolute))
        if not os.path.isfile(absolute):
            raise GachaError("INPUT_MISSING", "原图不存在。", details={"path": absolute})
        read = self.inputs._read_stable(absolute)
        if read.info.sha256 != requested.get("sha256") and requested.get("sha256"):
            raise GachaError(
                "INPUT_CHANGED",
                "原图在扫描后发生变化，已阻断创建。",
                details={"path": absolute, "expected": requested.get("sha256"), "actual": read.info.sha256},
            )
        if mode == "snapshot":
            snapshot = self.inputs.snapshot_source(
                job=job,
                source_id=source_id,
                source_order=source_order,
                original_name=os.path.basename(absolute),
                source_bytes=read.data,
                image_info=read.info,
                original_path=absolute,
            )
            return {
                "id": source_id,
                "job_id": job["id"],
                "source_order": source_order,
                "original_name": os.path.basename(absolute),
                "original_path": absolute,
                "snapshot_path": snapshot["snapshot_path"],
                "size": read.info.size,
                "sha256": read.info.sha256,
                "width": read.info.width,
                "height": read.info.height,
                "image_format": read.info.format,
            }
        return {
            "id": source_id,
            "job_id": job["id"],
            "source_order": source_order,
            "original_name": os.path.basename(absolute),
            "original_path": absolute,
            "size": read.info.size,
            "sha256": read.info.sha256,
            "width": read.info.width,
            "height": read.info.height,
            "image_format": read.info.format,
        }

    # ------------------------------------------------------------- start
    def resume_creation(self, job_id: str) -> dict[str, Any]:
        """Explicitly continue a paused creation plan.

        This only authorizes plan work.  It never turns a future READY into a
        running inference job: the user must start again after the plan is
        complete.
        """
        with self.write_lock:
            job = self._job_or_404(job_id)
            if job["status"] != JobStatus.CREATING:
                raise GachaError("STATE_CONFLICT", "该任务已不在创建阶段。")
            progress = self.repo.get_creation(job_id)
            if progress is None:
                raise GachaError(
                    "RECOVERY_REQUIRED",
                    "缺少创建进度记录；请重启插件完成旧库迁移后再继续。",
                    details={"job_id": job_id},
                )
            stage = progress["stage"]
            if stage == CreationStage.DONE:
                return self.job_detail(job_id)
            if stage in (CreationStage.FAILED, CreationStage.ABANDONED):
                raise GachaError(
                    "STATE_CONFLICT",
                    "创建计划已失败或已放弃，不能继续。",
                    details={"stage": stage, "error": progress.get("error_message")},
                )
            request_path = os.path.join(job["job_dir"], "create_request.json")
            if not os.path.isfile(request_path):
                raise GachaError("RECOVERY_REQUIRED", "创建检查点缺失，不能继续。")
            payload = read_json(request_path)
            checkpoint_hash = str(payload.get("request_hash") or "")
            if progress["request_hash"] and checkpoint_hash and progress["request_hash"] != checkpoint_hash:
                raise GachaError("STATE_CONFLICT", "创建检查点与持久进度不一致，不能继续。")
            request = payload.get("request") or {}
            sources_request = list(request.get("sources") or [])
            count = int(request.get("count_per_source") or 0)
            if len(sources_request) != int(progress["sources_total"]) or count * len(sources_request) != int(
                progress["draws_total"]
            ):
                raise GachaError("STATE_CONFLICT", "创建检查点总量与进度不一致，不能继续。")
            with self.db.tx() as conn:
                row = conn.execute(
                    "SELECT stage, control_intent FROM creation_progress WHERE job_id=?", (job_id,)
                ).fetchone()
                if row is None or row["stage"] in (CreationStage.DONE, CreationStage.ABANDONED):
                    raise GachaError("STATE_CONFLICT", "创建进度已进入终态，不能继续。")
                conn.execute(
                    "UPDATE creation_progress SET control_intent='CONTINUE', stage='WAITING', "
                    "generation=generation+1, error_code=NULL, error_message=NULL, "
                    "progress_version=progress_version+1, updated_at=? WHERE job_id=?",
                    (utc_now(), job_id),
                )
            self.repo.append_event(
                job_id=job_id,
                entity_type="JOB",
                entity_id=job_id,
                action="CREATION_CONTINUE_REQUESTED",
                actor="user",
                payload={"stage": stage},
            )
            if self.creation is not None:
                self.creation.notify()
            return self.job_detail(job_id)

    def start_job(
        self,
        job_id: str,
        *,
        expected_version: Optional[int] = None,
        recovery_version: Optional[int] = None,
        acknowledge_recovery: bool = False,
    ) -> dict[str, Any]:
        with self.write_lock:
            job = self._job_or_404(job_id)
            self.repo.assert_maintenance_free(job_id=job_id)
            progress = self.repo.get_creation(job_id)
            if progress is not None and progress["stage"] in (
                CreationStage.ABANDONED,
                CreationStage.FAILED,
            ):
                raise GachaError(
                    "STATE_CONFLICT",
                    "创建计划已放弃或失败，该任务不可执行。",
                    details={"stage": progress["stage"], "error": progress.get("error_message")},
                )
            if job["status"] == JobStatus.CREATING:
                return self.resume_creation(job_id)
            if job["status"] == JobStatus.RECOVERY_REQUIRED:
                if not acknowledge_recovery:
                    raise GachaError(
                        "RECOVERY_REQUIRED",
                        "该任务需要先核验并确认恢复方式。",
                        details={"job_id": job_id, "recovery_version": job["recovery_version"]},
                    )
                if recovery_version is not None and int(recovery_version) != int(job["recovery_version"]):
                    raise conflict(
                        "恢复版本已变化，请刷新恢复报告。",
                        current_recovery_version=job["recovery_version"],
                    )
            if job["status"] in (JobStatus.CREATING,):
                raise GachaError("STATE_CONFLICT", "任务计划尚未完成。")
            if job["status"] == JobStatus.COMPLETED:
                raise GachaError("STATE_CONFLICT", "任务已完成；追加后使用“开始追加”。")
            if job["status"] == JobStatus.RUNNING:
                return self.job_detail(job_id)
            if expected_version is not None and int(expected_version) != int(job["row_version"]):
                raise conflict("任务版本已变化，请刷新后重试。", current_version=job["row_version"])
            other = self.repo.active_run_job()
            if other is not None and other["id"] != job_id:
                raise conflict(
                    "另一个任务已持有运行意图，不能同时运行两个任务。",
                    code="STATE_CONFLICT",
                    occupant=other["id"],
                )
            slot = self.repo.get_slot()
            if slot.get("attempt_id"):
                raise conflict(
                    "仍有未核实的插件执行占用全局槽。",
                    code="STATE_CONFLICT",
                    occupant=slot["attempt_id"],
                )
            if self.repo.pending_draw_count(job_id) == 0:
                summary = self.repo.job_summary(job_id)
                if summary.get("effective", 0) != summary.get("succeeded", 0):
                    raise GachaError(
                        "STATE_CONFLICT",
                        "没有待执行的 DRAW；请先处理失败或中断项，或使用重试。",
                        details={"failed": summary.get("failed"), "unknown": summary.get("unknown")},
                    )
            unknown = self.repo.list_unknown_attempts(job_id=job_id)
            if unknown:
                raise GachaError(
                    "SUBMISSION_UNKNOWN",
                    "任务仍有 UNKNOWN 执行，必须先核验。",
                    details={"attempts": [a["id"] for a in unknown]},
                )
            # Re-verify the frozen workflow hash and bindings.
            try:
                ui_data = read_json(job["workflow_ui_path"])
                api_data = read_json(job["workflow_api_path"])
            except Exception as exc:
                raise GachaError("WORKFLOW_UNSUPPORTED", "冻结工作流无法读取，不能启动。", details={"error": str(exc)})
            if sha256_file(job["workflow_api_path"])[0] != job["workflow_api_sha256"]:
                raise GachaError("WORKFLOW_UNSUPPORTED", "冻结 API 工作流哈希发生变化，不能启动。")
            if sha256_file(job["workflow_ui_path"])[0] != job["workflow_ui_sha256"]:
                raise GachaError("WORKFLOW_UNSUPPORTED", "冻结 UI 工作流哈希发生变化，不能启动。")
            pre = self.preflight(
                workflow_ui=ui_data,
                workflow_api=api_data,
                bindings={"load_image_node_id": job["bindings"]["load_image_node_id"], "save_image_node_id": job["bindings"]["save_image_node_id"]},
            )
            pre.require_ok()
            # Re-verify every active input before scheduling a single new DRAW.
            for source in self.repo.all_sources(job_id):
                try:
                    self.inputs.verify_source(source, protection_mode=job["protection_mode"])
                except GachaError as exc:
                    raise GachaError(
                        exc.code,
                        f"原图 SRC-{int(source['source_order']):04d} 校验失败：{exc.message}",
                        details={"source_id": source["id"], **exc.details},
                    )
            updated = self.repo.update_job(
                job_id,
                status=JobStatus.RUNNING,
                run_intent=RunIntent.RUN,
                current_error_code=None,
                current_error_message=None,
                started_at=job.get("started_at") or utc_now(),
                actor="user",
                reason="start",
            )
            self.repo.append_event(
                job_id=job_id,
                entity_type="JOB",
                entity_id=job_id,
                action="JOB_STARTED",
                to_state=JobStatus.RUNNING,
                actor="user",
            )
            if self.scheduler is not None:
                self.scheduler.start()
            return self.job_detail(job_id)

    def abandon_creation(self, job_id: str, *, reason: Optional[str] = None) -> dict[str, Any]:
        """Stop a half-created JOB without ever making it READY."""
        with self.write_lock:
            job = self._job_or_404(job_id)
            if job["status"] != JobStatus.CREATING:
                raise GachaError("STATE_CONFLICT", "只有创建中的任务可以放弃创建。")
            message = reason or "用户放弃创建；未完成的任务不会进入 READY，也不会自动执行。"
            with self.db.tx() as conn:
                row = conn.execute(
                    "SELECT * FROM creation_progress WHERE job_id=?", (job_id,)
                ).fetchone()
                if row is not None:
                    conn.execute(
                        "UPDATE creation_progress SET stage='ABANDONED', control_intent='ABANDON', "
                        "generation=generation+1, error_code='CREATION_ABANDONED', error_message=?, "
                        "progress_version=progress_version+1, updated_at=? WHERE job_id=?",
                        (message, utc_now(), job_id),
                    )
                self.repo.update_job(
                    job_id,
                    status=JobStatus.BLOCKED,
                    run_intent=RunIntent.STOPPED,
                    current_error_code="SNAPSHOT_FAILED",
                    current_error_message=message,
                    actor="user",
                    reason="abandon creation",
                )
                self.repo._event(
                    conn,
                    job_id=job_id,
                    entity_type="JOB",
                    entity_id=job_id,
                    action="CREATION_ABANDONED",
                    actor="user",
                    reason=reason,
                )
            if self.creation is not None:
                self.creation.notify()
            return self.job_detail(job_id)

    def _pause_creation(self, job: dict[str, Any]) -> dict[str, Any]:
        job_id = job["id"]
        progress = self.repo.get_creation(job_id)
        if progress is None:
            raise GachaError("RECOVERY_REQUIRED", "缺少创建进度记录，不能暂停创建。")
        stage = progress["stage"]
        if stage == CreationStage.DONE:
            return {
                "job": self.job_detail(job_id),
                "stage": "creation_done",
                "message": "创建计划已完成；任务不会自动开始推理。",
            }
        if stage in (CreationStage.FAILED, CreationStage.ABANDONED):
            raise GachaError("STATE_CONFLICT", "创建计划已进入终态，不能暂停。")
        if stage == CreationStage.PAUSED:
            return {
                "job": self.job_detail(job_id),
                "stage": "paused_creation",
                "message": "创建计划已停止；已提交的进度和源图副本保留。",
            }
        worker_busy = bool(
            self.creation is not None
            and getattr(self.creation, "processing_job_id", None) == job_id
        )
        target_stage = CreationStage.PAUSING if worker_busy else CreationStage.PAUSED
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE creation_progress SET control_intent='STOP', stage=?, "
                "progress_version=progress_version+1, updated_at=? WHERE job_id=?",
                (target_stage, utc_now(), job_id),
            )
            self.repo._event(
                conn,
                job_id=job_id,
                entity_type="JOB",
                entity_id=job_id,
                action="CREATION_PAUSE_REQUESTED",
                from_state=stage,
                to_state=CreationStage.PAUSING,
                actor="user",
            )
        if self.creation is not None:
            self.creation.notify()
        if worker_busy:
            return {
                "job": self.job_detail(job_id),
                "stage": "pausing_creation",
                "message": "已记录停止创建意图；当前原子批次完成后停止，不会自动开始推理。",
            }
        return {
            "job": self.job_detail(job_id),
            "stage": "paused_creation",
            "message": "创建计划已停止；已提交的进度和源图副本保留，继续时从已提交事实续作。",
        }

    def pause_job(self, job_id: str, *, mode: str = "safe") -> dict[str, Any]:
        if mode not in {"safe", "now"}:
            raise GachaError("BAD_REQUEST", "mode 只能是 safe 或 now。")
        with self.write_lock:
            job = self._job_or_404(job_id)
            if job["status"] == JobStatus.CREATING:
                return self._pause_creation(job)
            if job["status"] == JobStatus.RECOVERY_REQUIRED:
                return {
                    "job": self.job_detail(job_id),
                    "stage": "recovery_required",
                    "message": "当前仍有待核实的执行，不能显示为已安全暂停。",
                }
            slot = self.repo.get_slot()
            attempt = self.repo.get_attempt(slot["attempt_id"]) if slot.get("attempt_id") else None
            intent = RunIntent.PAUSE_SAFE if mode == "safe" else RunIntent.PAUSE_NOW
            if attempt is None:
                self.repo.update_job(
                    job_id,
                    status=JobStatus.PAUSED,
                    run_intent=RunIntent.STOPPED,
                    actor="user",
                    reason=f"pause {mode}",
                )
                self.repo.append_event(
                    job_id=job_id,
                    entity_type="JOB",
                    entity_id=job_id,
                    action="JOB_PAUSED",
                    to_state=JobStatus.PAUSED,
                    actor="user",
                )
                return {"job": self.job_detail(job_id), "stage": "paused", "message": "已暂停。"}
            if attempt["status"] == AttemptStatus.UNKNOWN:
                return {
                    "job": self.job_detail(job_id),
                    "stage": "recovery_required",
                    "message": "当前尝试状态未知，不能通过暂停猜测其状态。",
                }
            if job["status"] not in (JobStatus.RUNNING, JobStatus.PAUSING):
                raise GachaError("STATE_CONFLICT", "当前任务状态不能暂停。", current_status=job["status"])
            self.repo.update_job(
                job_id,
                status=JobStatus.PAUSING,
                run_intent=intent,
                actor="user",
                reason=f"pause {mode}",
            )
            self.repo.append_event(
                job_id=job_id,
                entity_type="JOB",
                entity_id=job_id,
                action="PAUSE_INTENT",
                to_state=JobStatus.PAUSING,
                actor="user",
                payload={"mode": mode, "attempt_id": attempt["id"]},
            )
            if self.scheduler is not None:
                self.scheduler.start()
            capability = self.repo.get_runtime_meta("targeted_cancel") or "unverified"
            if mode == "now":
                if capability == "verified_atomic":
                    stage = "cancel_requested"
                    message = "已记录立即暂停意图；只针对本插件执行，等待核验结果。"
                else:
                    stage = "degraded_waiting_current"
                    message = "当前版本无法安全定向中断，等待本次执行结束后暂停。"
            else:
                stage = "waiting_current"
                message = "正在等待当前一次执行完成、核验并记账后暂停。"
            return {
                "job": self.job_detail(job_id),
                "stage": stage,
                "message": message,
                "targeted_cancel": capability,
            }

    # ------------------------------------------------------------- append
    def append_block(
        self,
        source_id: str,
        *,
        count: int,
        priority: str = "tail",
        expected_version: Optional[int] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        count = int(count)
        if count < 1:
            raise GachaError("BAD_REQUEST", "追加次数必须是正整数。")
        if priority not in {"priority", "tail"}:
            raise GachaError("BAD_REQUEST", "priority 只能是 priority 或 tail。")
        with self.write_lock:
            source = self.repo.get_source(source_id)
            if source is None:
                raise not_found("原图不存在。", source_id=source_id)
            job = self._job_or_404(source["job_id"])
            self.repo.assert_maintenance_free(job_id=job["id"])
            if job["status"] == JobStatus.CREATING:
                raise GachaError("STATE_CONFLICT", "任务计划尚未完成，不能追加抽卡。")
            if source["status"] != SourceStatus.ACTIVE:
                raise GachaError("STATE_CONFLICT", "原图已移除，不能追加。")
            key = idempotency_key or uuid4()
            block_id = _stable_id(f"append/{job['id']}/{source_id}/{key}")
            existing = self.repo.get_block(block_id)
            if existing is not None:
                existing_count = self.db.query_one(
                    "SELECT COUNT(*) FROM draws WHERE block_id=?", (block_id,)
                )[0]
                if int(existing_count) != count:
                    raise GachaError(
                        "IDEMPOTENCY_CONFLICT",
                        "相同追加标识对应了不同次数。",
                        details={"block_id": block_id, "existing_count": int(existing_count), "requested": count},
                    )
                return self._append_response(job, existing, source)
            if expected_version is not None and int(expected_version) != int(job["row_version"]):
                raise conflict(
                    "任务版本已变化，请刷新后重试。",
                    current_version=job["row_version"],
                    expected_version=expected_version,
                )
            if self.repo.count_draws(job["id"]) + count > int(self.settings.plan_limit):
                raise GachaError("BAD_REQUEST", "追加后计划超过单任务上限。")
            bindings = [
                binding
                for binding in job["bindings"].get("seed_bindings", [])
                if binding.get("active", True)
            ]
            with self.db.tx() as conn:
                self.repo.assert_maintenance_free(job_id=job["id"], conn=conn)
                seq = self.repo.next_block_seq(conn, job["id"])
                conn.execute(
                    """
                    INSERT INTO blocks(id, job_id, source_id, kind, priority, enqueue_seq, cursor, status, created_at)
                    VALUES (?,?,?,'APPEND',?,?,0,'PENDING',?)
                    """,
                    (block_id, job["id"], source_id, 0 if priority == "priority" else 2, seq, utc_now()),
                )
                max_no_row = conn.execute(
                    "SELECT COALESCE(MAX(draw_no),0) FROM draws WHERE source_id=?", (source_id,)
                ).fetchone()
                start_no = int(max_no_row[0]) + 1
                for offset in range(count):
                    draw_no = start_no + offset
                    draw_id = _stable_id(f"draw/{job['id']}/{source_id}/{draw_no}")
                    conn.execute(
                        """
                        INSERT INTO draws(id, job_id, source_id, block_id, draw_no, status, row_version, created_at, updated_at)
                        VALUES (?,?,?,?,?,'PENDING',1,?,?)
                        """,
                        (draw_id, job["id"], source_id, block_id, draw_no, utc_now(), utc_now()),
                    )
                    for binding in bindings:
                        maximum = min(int(binding.get("max", 9007199254740991)), int(self.settings.seed_domain_max))
                        self.repo.allocate_seed(
                            conn,
                            job_id=job["id"],
                            draw_id=draw_id,
                            node_id=binding["node_id"],
                            input_name=binding["input_name"],
                            seed_max=maximum,
                        )
                new_status = JobStatus.PAUSED if job["status"] == JobStatus.COMPLETED else job["status"]
                conn.execute(
                    "UPDATE jobs SET plan_version=plan_version+1, row_version=row_version+1, status=?, updated_at=? WHERE id=?",
                    (new_status, utc_now(), job["id"]),
                )
                self.repo._event(
                    conn,
                    job_id=job["id"],
                    entity_type="BLOCK",
                    entity_id=block_id,
                    action="BLOCK_APPENDED",
                    actor="user",
                    payload={
                        "source_id": source_id,
                        "count": count,
                        "priority": priority,
                        "draw_start": start_no,
                        "draw_end": start_no + count - 1,
                    },
                )
                if new_status == JobStatus.PAUSED and job["status"] == JobStatus.COMPLETED:
                    self.repo._event(
                        conn,
                        job_id=job["id"],
                        entity_type="JOB",
                        entity_id=job["id"],
                        action="JOB_STATUS",
                        from_state=JobStatus.COMPLETED,
                        to_state=JobStatus.PAUSED,
                        reason="append",
                        actor="system",
                    )
                fault_point("F09")
            block = self.repo.get_block(block_id)
            assert block is not None
            return self._append_response(self.repo.get_job(job["id"]) or job, block, source)

    def _append_response(self, job: dict[str, Any], block: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
        draws = self.db.query_all(
            "SELECT id, draw_no, status FROM draws WHERE block_id=? ORDER BY draw_no", (block["id"],)
        )
        return {
            "block_id": block["id"],
            "source_id": source["id"],
            "draw_start": int(draws[0]["draw_no"]) if draws else None,
            "draw_end": int(draws[-1]["draw_no"]) if draws else None,
            "count": len(draws),
            "plan_version": job["plan_version"],
            "job_status": job["status"],
            "run_intent": job["run_intent"],
            "still_paused": job["status"] != JobStatus.RUNNING,
            "draws": [dict(row) for row in draws],
        }

    # ------------------------------------------------------------ sources
    def remove_source(self, source_id: str, *, expected_version: Optional[int] = None) -> dict[str, Any]:
        with self.write_lock:
            source = self.repo.get_source(source_id)
            if source is None:
                raise not_found("原图不存在。", source_id=source_id)
            job = self._job_or_404(source["job_id"])
            self.repo.assert_maintenance_free(job_id=job["id"])
            if job["status"] == JobStatus.CREATING:
                raise GachaError("STATE_CONFLICT", "任务计划尚未完成，不能移除原图。")
            if expected_version is not None and int(expected_version) != int(job["row_version"]):
                raise conflict("任务版本已变化，请刷新后重试。", current_version=job["row_version"])
            rows = self.db.query_all(
                "SELECT d.*, (SELECT COUNT(*) FROM attempts a WHERE a.draw_id=d.id) AS attempt_count "
                "FROM draws d WHERE d.source_id=?",
                (source_id,),
            )
            for row in rows:
                if int(row["attempt_count"]) > 0 or row["status"] not in (DrawStatus.PENDING, DrawStatus.CANCELLED_BY_USER):
                    raise GachaError(
                        "STATE_CONFLICT",
                        "该原图已有执行历史，不能普通移除；请保留并继续使用。",
                        details={"source_id": source_id, "draw_no": row["draw_no"]},
                    )
            with self.db.tx() as conn:
                self.repo.assert_maintenance_free(job_id=job["id"], conn=conn)
                conn.execute(
                    "UPDATE sources SET status=?, removed_at=? WHERE id=?",
                    (SourceStatus.REMOVED_BY_USER, utc_now(), source_id),
                )
                conn.execute(
                    "UPDATE draws SET status='CANCELLED_BY_USER', updated_at=? WHERE source_id=? AND status='PENDING'",
                    (utc_now(), source_id),
                )
                conn.execute(
                    "UPDATE blocks SET status='CANCELLED', completed_at=? WHERE source_id=?",
                    (utc_now(), source_id),
                )
                conn.execute(
                    "UPDATE jobs SET plan_version=plan_version+1, row_version=row_version+1, updated_at=? WHERE id=?",
                    (utc_now(), job["id"]),
                )
                self.repo._event(
                    conn,
                    job_id=job["id"],
                    entity_type="SRC",
                    entity_id=source_id,
                    action="SOURCE_REMOVED",
                    from_state=SourceStatus.ACTIVE,
                    to_state=SourceStatus.REMOVED_BY_USER,
                    actor="user",
                )
            return self.source_detail(source_id)

    def relocate_source(self, source_id: str, new_path: str) -> dict[str, Any]:
        with self.write_lock:
            source = self.repo.get_source(source_id)
            if source is None:
                raise not_found("原图不存在。", source_id=source_id)
            job = self._job_or_404(source["job_id"])
            if job["status"] == JobStatus.CREATING:
                raise GachaError("STATE_CONFLICT", "任务计划尚未完成，不能重新定位原图。")
            if not source.get("original_path"):
                raise GachaError("WORKFLOW_UNSUPPORTED", "快照模式不支持重新定位原图。")
            self.inputs.relocate_reference(source, new_path)
            absolute = os.path.abspath(new_path)
            self.repo.update_source(source_id, original_path=absolute, status=SourceStatus.ACTIVE, last_verified_at=utc_now())
            self.repo.append_event(
                job_id=source["job_id"],
                entity_type="SRC",
                entity_id=source_id,
                action="SOURCE_RELOCATED",
                actor="user",
                payload={"path": absolute},
            )
            return self.source_detail(source_id)

    # ---------------------------------------------------------- selection
    def set_selection(self, output_id: str, selected: bool, *, expected_version: Optional[int] = None) -> dict[str, Any]:
        with self.write_lock:
            output = self.repo.get_output(output_id)
            if output is None:
                raise not_found("输出不存在。", output_id=output_id)
            retention = self.repo.get_retention(output_id)
            retention_state = (retention or {}).get("state") or "RETAINED"
            if selected and retention_state != "RETAINED":
                code = "OUTPUT_CLEANING" if retention_state == "CLEANING" else "OUTPUT_PURGED"
                message = (
                    "该结果正在清理，不能入选。"
                    if retention_state == "CLEANING"
                    else "该结果的图片已被永久清理，不能入选。"
                )
                raise GachaError(code, message, details={"output_id": output_id})
            self.repo.assert_maintenance_free(job_id=self._output_job_id(output_id))
            if selected:
                state = self.db.query_one(
                    "SELECT o.health, d.status AS draw_status, d.accepted_attempt_id "
                    "FROM outputs o JOIN attempts a ON a.id=o.attempt_id JOIN draws d ON d.id=a.draw_id "
                    "WHERE o.id=?",
                    (output_id,),
                )
                if (
                    state is None
                    or state["health"] != "VERIFIED"
                    or state["draw_status"] != "SUCCEEDED"
                    or state["accepted_attempt_id"] != output["attempt_id"]
                ):
                    raise GachaError(
                        "OUTPUT_INCOMPLETE",
                        "异常或未被接受的输出不能入选；请修复/重试该 DRAW。",
                        details={"output_id": output_id},
                    )
            existing = self.repo.get_selection(output_id)
            if existing is not None and bool(existing["selected"]) == bool(selected) and not (
                expected_version is not None and int(expected_version) != int(existing["version"])
            ):
                return {
                    "output_id": output_id,
                    "selected": bool(existing["selected"]),
                    "version": existing["version"],
                    "idempotent": True,
                }
            row = self.repo.set_selection(output_id, selected=selected, expected_version=expected_version)
            return {
                "output_id": output_id,
                "selected": bool(row["selected"]),
                "version": row["version"],
                "idempotent": False,
            }

    def mark_viewed(self, output_id: str) -> dict[str, Any]:
        if self.repo.get_output(output_id) is None:
            raise not_found("输出不存在。", output_id=output_id)
        row = self.repo.mark_viewed(output_id)
        return {"output_id": output_id, "viewed_at": row.get("viewed_at"), "version": row.get("version")}

    def confirm_source_review(
        self,
        source_id: str,
        *,
        decision: str,
        reason: Optional[str] = None,
    ) -> dict[str, Any]:
        if decision not in {ReviewDecision.SELECTED, ReviewDecision.SKIPPED}:
            raise GachaError("BAD_REQUEST", "审核决定无效。")
        source = self.repo.get_source(source_id)
        if source is None:
            raise not_found("原图不存在。", source_id=source_id)
        job = self._job_or_404(source["job_id"])
        self.repo.assert_maintenance_free(job_id=job["id"])
        review_id = _stable_id(
            f"review/{source_id}/{source['result_revision']}/{source['selection_version']}/{job['plan_version']}"
        )
        with self.db.tx() as conn:
            self.repo.assert_maintenance_free(job_id=job["id"], conn=conn)
            conn.execute(
                """
                INSERT INTO source_reviews(id, source_id, result_revision, selection_version, plan_version, decision, reason, confirmed_at)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(source_id, result_revision, selection_version, plan_version)
                DO UPDATE SET decision=excluded.decision, reason=excluded.reason, confirmed_at=excluded.confirmed_at
                """,
                (
                    review_id,
                    source_id,
                    int(source["result_revision"]),
                    int(source["selection_version"]),
                    int(job["plan_version"]),
                    decision,
                    reason,
                    utc_now(),
                ),
            )
        return {
            "source_id": source_id,
            "decision": decision,
            "result_revision": source["result_revision"],
            "selection_version": source["selection_version"],
            "plan_version": job["plan_version"],
        }

    def source_review_status(self, source: Mapping[str, Any], job: Mapping[str, Any]) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT * FROM source_reviews WHERE source_id=? AND result_revision=? AND selection_version=? AND plan_version=?",
            (
                source["id"],
                int(source["result_revision"]),
                int(source["selection_version"]),
                int(job["plan_version"]),
            ),
        )
        summary = self.repo.source_summary(source["id"])
        if row is not None:
            status = "reviewed"
            decision = row["decision"]
        elif summary.get("selected", 0) > 0:
            status = "selected_pending_review"
            decision = None
        else:
            viewed = self.db.query_one(
                "SELECT COUNT(*) FROM selections s JOIN outputs o ON o.id=s.output_id "
                "JOIN attempts a ON a.id=o.attempt_id JOIN draws d ON d.id=a.draw_id "
                "WHERE d.source_id=? AND s.viewed_at IS NOT NULL",
                (source["id"],),
            )
            if int(viewed[0] or 0) > 0:
                status = "viewed_no_selection"
            elif summary.get("available", 0) > 0:
                status = "available_unreviewed"
            else:
                status = "unreviewed"
            decision = None
        return {"status": status, "decision": decision, "confirmed_at": row["confirmed_at"] if row else None}

    # --------------------------------------------------------------- read
    def job_detail(self, job_id: str) -> dict[str, Any]:
        job = self._job_or_404(job_id)
        summary = self.repo.job_summary(job_id)
        dto = job_dto(job, summary, creation_dto(self.repo.get_creation(job_id)))
        slot = self.repo.get_slot()
        dto["slot_attempt_id"] = slot.get("attempt_id")
        if slot.get("attempt_id"):
            attempt = self.repo.get_attempt(slot["attempt_id"])
            dto["active_attempt"] = {
                "id": attempt["id"],
                "draw_id": attempt["draw_id"],
                "status": attempt["status"],
                "expected_prompt_id": attempt["expected_prompt_id"],
                "actual_prompt_id": attempt.get("actual_prompt_id"),
                "started_at": attempt.get("started_at"),
                "submitted_at": attempt.get("submitted_at"),
                "attempt_no": attempt["attempt_no"],
            } if attempt else None
        return dto

    def list_jobs(self, *, limit: int = 50, cursor: Optional[str] = None) -> dict[str, Any]:
        cursor_created = cursor_id = None
        if cursor:
            try:
                payload = json.loads(cursor)
                cursor_created = payload.get("created_at")
                cursor_id = payload.get("id")
            except Exception:
                raise GachaError("BAD_REQUEST", "分页游标无效。")
        jobs = self.repo.list_jobs(limit=limit, cursor_created_at=cursor_created, cursor_id=cursor_id)
        items = []
        for job in jobs:
            items.append(job_dto(job, self.repo.job_summary(job["id"])))
        next_cursor = None
        if len(jobs) == max(1, min(int(limit), 200)):
            last = jobs[-1]
            next_cursor = json.dumps({"created_at": last["created_at"], "id": last["id"]}, separators=(",", ":"))
        return {"items": items, "next_cursor": next_cursor, "event_seq": self.repo.last_event_seq()}

    def source_detail(self, source_id: str) -> dict[str, Any]:
        source = self.repo.get_source(source_id)
        if source is None:
            raise not_found("原图不存在。", source_id=source_id)
        job = self._job_or_404(source["job_id"])
        dto = source_dto(source, self.repo.source_summary(source_id))
        dto["review"] = self.source_review_status(source, job)
        dto["image_url"] = f"/sources/{source_id}/image"
        dto["thumbnail_url"] = f"/sources/{source_id}/thumbnail?v={source['sha256'][:16]}"
        return dto

    def list_sources(self, job_id: str, *, limit: int = 200, after_order: int = 0) -> dict[str, Any]:
        job = self._job_or_404(job_id)
        page_size = max(1, min(int(limit), 200))
        sources = self.repo.list_sources(job_id, limit=page_size, after_order=after_order)
        dto = []
        for source in sources:
            item = source_dto(source, self.repo.source_summary(source["id"]))
            item["review"] = self.source_review_status(source, job)
            item["image_url"] = f"/sources/{source['id']}/image"
            item["thumbnail_url"] = f"/sources/{source['id']}/thumbnail?v={source['sha256'][:16]}"
            dto.append(item)
        totals = self.db.query_one(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN status!='REMOVED_BY_USER' THEN 1 ELSE 0 END) AS active "
            "FROM sources WHERE job_id=?",
            (job_id,),
        )
        next_after_order = None
        if len(sources) == page_size:
            last_order = int(sources[-1]["source_order"])
            more = self.db.query_one(
                "SELECT 1 FROM sources WHERE job_id=? AND source_order>? "
                "AND status!='REMOVED_BY_USER' LIMIT 1",
                (job_id, last_order),
            )
            if more is not None:
                next_after_order = last_order
        return {
            "items": dto,
            "next_after_order": next_after_order,
            "prev_after_order": None,
            "page_size": page_size,
            "total_sources": int(totals["total"] or 0) if totals else 0,
            "active_sources": int(totals["active"] or 0) if totals else 0,
            "removed_sources": int((totals["total"] or 0) - (totals["active"] or 0)) if totals else 0,
            "event_seq": self.repo.last_event_seq(),
        }

    def list_outputs(
        self,
        source_id: str,
        *,
        include_unhealthy: bool = False,
        limit: int = 50,
        cursor_draw_no: int = 0,
        cursor_out_no: int = 0,
        direction: str = "forward",
        offset: Optional[int] = None,
    ) -> dict[str, Any]:
        source = self.repo.get_source(source_id)
        if source is None:
            raise not_found("原图不存在。", source_id=source_id)
        if direction not in {"forward", "backward"}:
            raise GachaError("BAD_REQUEST", "direction 只能是 forward 或 backward。")
        page_size = max(1, min(int(limit), 200))
        if offset is not None and (isinstance(offset, bool) or int(offset) < 0):
            raise GachaError("BAD_REQUEST", "offset 必须是非负整数。")
        rows = self.repo.list_outputs_for_source(
            source_id,
            include_unhealthy=include_unhealthy,
            limit=page_size,
            cursor_draw_no=cursor_draw_no,
            cursor_out_no=cursor_out_no,
            direction=direction,
            offset=None if offset is None else int(offset),
        )
        items = [output_dto(row) for row in rows]
        min_cursor, max_cursor = self.repo.output_cursor_bounds(
            source_id, include_unhealthy=include_unhealthy
        )
        next_cursor = prev_cursor = None
        if rows:
            first = (int(rows[0]["draw_no"]), int(rows[0]["out_no"]))
            last = (int(rows[-1]["draw_no"]), int(rows[-1]["out_no"]))
            if min_cursor is not None and min_cursor < first:
                prev_cursor = {"draw_no": first[0], "out_no": first[1]}
            if max_cursor is not None and last < max_cursor:
                next_cursor = {"draw_no": last[0], "out_no": last[1]}
        total = self.repo.count_outputs_for_source(
            source_id, include_unhealthy=include_unhealthy
        )
        if offset is not None:
            actual_offset = int(offset)
        elif rows:
            actual_offset = self.repo.count_outputs_before(
                source_id,
                draw_no=int(rows[0]["draw_no"]),
                out_no=int(rows[0]["out_no"]),
                include_unhealthy=include_unhealthy,
            )
        else:
            actual_offset = 0
        return {
            "items": items,
            "next_cursor": next_cursor,
            "prev_cursor": prev_cursor,
            "direction": direction,
            "page_size": page_size,
            "offset": actual_offset,
            "total": total,
            "event_seq": self.repo.last_event_seq(),
        }

    def output_states(self, source_id: str, output_ids: Sequence[str]) -> dict[str, Any]:
        """Lightweight consistent state for a bounded set of loaded OUTs."""
        source = self.repo.get_source(source_id)
        if source is None:
            raise not_found("原图不存在。", source_id=source_id)
        if not isinstance(output_ids, (list, tuple)):
            raise GachaError("BAD_REQUEST", "output_ids 必须是数组。")
        items, missing = self.repo.output_states_for_source(source_id, output_ids)
        return {
            "source_id": source_id,
            "result_revision": int(source["result_revision"]),
            "selection_version": int(source["selection_version"]),
            "items": items,
            "missing_ids": missing,
        }

    def get_output(self, output_id: str) -> dict[str, Any]:
        output = self.repo.get_output(output_id)
        if output is None:
            raise not_found("输出不存在。", output_id=output_id)
        row = self.db.query_one(
            "SELECT o.*, d.draw_no, a.attempt_no, COALESCE(s.selected,0) AS selected, "
            "s.version AS selection_version, s.viewed_at, "
            "COALESCE(ret.state,'RETAINED') AS retention_state, ret.purged_at "
            "FROM outputs o JOIN attempts a ON a.id=o.attempt_id JOIN draws d ON d.id=a.draw_id "
            "LEFT JOIN selections s ON s.output_id=o.id "
            "LEFT JOIN output_retention ret ON ret.output_id=o.id WHERE o.id=?",
            (output_id,),
        )
        return output_dto(dict(row))

    # -------------------------------------------------------------- events
    def events(self, *, after: int = 0, job_id: Optional[str] = None, limit: int = 200) -> dict[str, Any]:
        events = self.repo.list_events(after=after, job_id=job_id, limit=limit)
        return {
            "items": events,
            "last_seq": self.repo.last_event_seq(),
            "has_more": len(events) == max(1, min(int(limit), 1000)),
        }

    # ------------------------------------------------------------ backups
    def create_backup(self, reason: str = "manual") -> dict[str, Any]:
        return self.backups.create_backup(reason=reason)

    def list_backups(self) -> list[dict[str, Any]]:
        return self.backups.list_backups()

    def restore_backup(self, backup_id: str) -> dict[str, Any]:
        with self.write_lock:
            self.repo.assert_maintenance_free()
            slot = self.repo.get_slot()
            if slot.get("attempt_id"):
                raise GachaError("STATE_CONFLICT", "仍有执行占用全局槽，不能恢复备份。")
            active = [a for a in self.repo.list_active_attempts() if a["status"] != AttemptStatus.UNKNOWN]
            if active:
                raise GachaError(
                    "STATE_CONFLICT",
                    "仍有未结束执行，不能恢复备份。",
                    details={"attempts": [a["id"] for a in active[:10]]},
                )
            if self.scheduler is not None:
                self.scheduler.stop()
            # The creation worker must be fully stopped before the database is
            # replaced; a live worker could otherwise write into the old file.
            creation_was_running = self.creation is not None
            if self.creation is not None:
                self.creation.stop()
            result = self.backups.restore(backup_id)
            if self.cleanup is not None:
                # A restored database may predate deliberate deletions.  The
                # external journal is the authority; re-import retention before
                # anything else can treat those files as accidentally missing.
                result["cleanup_reconcile"] = self.cleanup.reconcile_startup()
            if creation_was_running and self.creation is not None:
                self.creation.start()
            # A restored database may still say RUNNING; it must not resume by
            # itself. Clear run authorization and require explicit verification.
            for restored in self.repo.all_jobs():
                new_status = restored["status"]
                if new_status in (JobStatus.RUNNING, JobStatus.PAUSING):
                    new_status = JobStatus.RECOVERY_REQUIRED
                try:
                    self.db.execute(
                        "UPDATE jobs SET run_intent=?, status=?, row_version=row_version+1, "
                        "recovery_version=recovery_version+?, current_error_message=?, updated_at=? WHERE id=?",
                        (
                            RunIntent.STOPPED,
                            new_status,
                            1 if new_status == JobStatus.RECOVERY_REQUIRED else 0,
                            "已恢复备份；请重新核验执行与文件后确认继续。"
                            if new_status == JobStatus.RECOVERY_REQUIRED
                            else restored.get("current_error_message"),
                            utc_now(),
                            restored["id"],
                        ),
                    )
                except GachaError:
                    pass
            if self.scheduler is not None:
                self.scheduler.start()
            return result

    # ------------------------------------------------------------ settings
    def get_settings(self) -> dict[str, Any]:
        data = self.settings.to_dict()
        data["data_root"] = self.paths.data_root
        data["results_root"] = self.paths.results_root
        data["db_path"] = self.paths.db_path
        data["backups_root"] = self.paths.backups_root
        data["logs_root"] = self.paths.logs_root
        data["inbox_root"] = self.paths.inbox_root
        data["source_roots"] = self.inputs.allowed_input_roots()
        return data

    def update_settings(self, changes: Mapping[str, Any]) -> dict[str, Any]:
        with self.write_lock:
            self.repo.assert_maintenance_free()
            if "results_root" in changes and changes["results_root"] not in (None, self.paths.results_root):
                raise GachaError(
                    "STATE_CONFLICT",
                    "运行中不能直接修改结果根目录；请停机后按迁移流程处理。",
                )
            data = self.settings.to_dict()
            for key, value in changes.items():
                if key in {"data_root", "db_path", "backups_root", "logs_root", "inbox_root", "source_roots"}:
                    continue
                if key not in data:
                    raise GachaError("BAD_REQUEST", f"未知设置项：{key}")
                data[key] = value
            new_settings = Settings.from_dict(data)
            for root in new_settings.allowed_export_roots:
                absolute = os.path.abspath(root)
                if not os.path.isdir(absolute):
                    raise GachaError("PATH_NOT_ALLOWED", "导出根目录不存在。", details={"root": absolute})
                if path_is_within(self.paths.data_root, absolute) or path_is_within(self.paths.results_root, absolute):
                    raise GachaError(
                        "PATH_NOT_ALLOWED",
                        "导出根不能位于插件数据目录或已有结果目录内。",
                        details={"root": absolute},
                    )
            for root in new_settings.allowed_input_roots:
                absolute = os.path.abspath(root)
                if not os.path.isdir(absolute):
                    raise GachaError("PATH_NOT_ALLOWED", "输入根目录不存在。", details={"root": absolute})
            save_settings(self.paths.config_path, new_settings)
            self.settings.__dict__.update(new_settings.__dict__)
            self.inputs.settings = self.settings
            self.backups.settings = self.settings
            if self.exports is not None:
                self.exports.settings = self.settings
            return self.get_settings()

    # -------------------------------------------------------------- health
    def health(self) -> dict[str, Any]:
        slot = self.repo.get_slot()
        summary = {
            "plugin_version": "1.0.0",
            "schema_version": self.db.schema_version,
            "boot_id": self.repo.get_runtime_meta("boot_id"),
            "slot": slot,
            "db": {
                "ok": not self.db.fatal,
                "fatal_code": self.db.fatal_code,
                "pragmas": self.db.pragmas,
            },
            "backups": {
                "last_error": getattr(self.backups, "last_error", None),
                "count": len(self.backups.list_backups(limit=200)) if not self.db.fatal else 0,
            },
            "unknown_attempts": len(self.repo.list_unknown_attempts()) if not self.db.fatal else 0,
            "last_event_seq": self.repo.last_event_seq() if not self.db.fatal else 0,
            "disk": {
                "data_root": self.paths.data_root,
                "data_free": _disk_free(self.paths.data_root),
                "results_root": self.paths.results_root,
                "results_free": _disk_free(self.paths.results_root),
                "reserve": self.settings.disk_reserve,
            },
            "scheduler": {
                "running": bool(self.scheduler and self.scheduler.running),
                "last_error": self.scheduler.last_loop_error if self.scheduler else None,
            },
            "creation": {
                "running": bool(
                    self.creation
                    and getattr(self.creation, "_thread", None)
                    and self.creation._thread.is_alive()
                ),
                "batch_limit": int(self.settings.plan_insert_batch),
                "batch_current": int(getattr(self.creation, "batch_size", self.settings.plan_insert_batch)),
            },
        }
        return summary

    def storage_summary(self) -> dict[str, Any]:
        """Known on-disk usage plus a disjoint result/thumbnail breakdown.

        ``results_bytes`` includes the thumbnail cache subtree; the frontend
        must not add ``thumbnails_bytes`` to it a second time.  The mutually
        exclusive fields are ``result_image_bytes`` + ``thumbnails_bytes`` +
        ``results_other_bytes``.
        """
        thumbnails_root = os.path.abspath(self.paths.thumbnails_root)

        def tree_size(
            root: str,
            *,
            skip: Optional[str] = None,
            extensions: Optional[set[str]] = None,
        ) -> tuple[int, int]:
            total = 0
            files = 0
            skip_norm = os.path.normcase(skip) if skip else None
            if not os.path.isdir(root):
                return 0, 0
            for dirpath, dirnames, filenames in os.walk(root):
                if skip_norm and os.path.normcase(dirpath) == skip_norm:
                    dirnames[:] = []
                    continue
                if skip_norm:
                    dirnames[:] = [
                        name
                        for name in dirnames
                        if os.path.normcase(os.path.join(dirpath, name)) != skip_norm
                    ]
                for name in filenames:
                    if extensions is not None and os.path.splitext(name)[1].lower() not in extensions:
                        continue
                    try:
                        total += os.path.getsize(os.path.join(dirpath, name))
                        files += 1
                    except OSError:
                        pass
            return total, files

        image_extensions = {".png", ".jpg", ".jpeg", ".webp"}
        results_bytes, results_files = tree_size(self.paths.results_root)
        result_image_bytes, result_image_files = tree_size(
            self.paths.results_root, skip=thumbnails_root, extensions=image_extensions
        )
        thumbnails_bytes, thumbnail_files = tree_size(thumbnails_root)
        data_bytes, data_files = tree_size(self.paths.data_root)
        backups_bytes, _ = tree_size(self.paths.backups_root)
        export_roots = []
        exports_bytes = 0
        exports_unknown = False
        for root in self.settings.allowed_export_roots:
            if not root:
                continue
            absolute = os.path.abspath(root)
            size, files = tree_size(absolute)
            if not os.path.isdir(absolute):
                exports_unknown = True
                export_roots.append({"root": absolute, "bytes": None, "files": None, "stat": "unavailable"})
            else:
                exports_bytes += size
                export_roots.append({"root": absolute, "bytes": size, "files": files, "stat": "known"})
        retention_counts = self.db.query_one(
            "SELECT "
            "SUM(CASE WHEN state='PURGED' THEN 1 ELSE 0 END) AS purged,"
            "SUM(CASE WHEN state='CLEANING' THEN 1 ELSE 0 END) AS cleaning,"
            "COALESCE(SUM(CASE WHEN state='PURGED' THEN size ELSE 0 END),0) AS purged_bytes "
            "FROM output_retention"
        )
        return {
            "data_root": self.paths.data_root,
            "results_root": self.paths.results_root,
            "thumbnails_root": self.paths.thumbnails_root,
            "data_bytes": data_bytes,
            "data_files": data_files,
            "results_bytes": results_bytes,
            "results_files": results_files,
            "results_includes_thumbnails": path_is_within(
                self.paths.results_root, thumbnails_root
            ),
            "result_image_bytes": result_image_bytes,
            "result_image_files": result_image_files,
            "thumbnails_bytes": thumbnails_bytes,
            "thumbnail_files": thumbnail_files,
            "results_other_bytes": max(0, results_bytes - result_image_bytes - thumbnails_bytes),
            "backups_bytes": backups_bytes,
            "exports_bytes": exports_bytes,
            "export_roots": export_roots,
            "exports_stats": "known" if not exports_unknown else "partial",
            "retention": {
                "purged_outputs": int(retention_counts["purged"] or 0) if retention_counts else 0,
                "cleaning_outputs": int(retention_counts["cleaning"] or 0) if retention_counts else 0,
                "purged_bytes": int(retention_counts["purged_bytes"] or 0) if retention_counts else 0,
            },
            "breakdown_note": (
                "结果图片与缩略图缓存为互斥分项；results_bytes 已包含缩略图，不能再与 thumbnails_bytes 相加。"
                "导出成品单独统计；无法读取的项目标为 unavailable/partial。"
            ),
            "disk": {
                "data_free": _disk_free(self.paths.data_root),
                "results_free": _disk_free(self.paths.results_root),
                "reserve": self.settings.disk_reserve,
            },
        }

    def clear_thumbnail_cache(self) -> dict[str, Any]:
        if self.cleanup is None:
            return {"removed": 0, "root": self.paths.thumbnails_root, "failed": 0, "skipped": 0}
        return self.cleanup.clear_thumbnail_cache()


def _disk_free(path: str) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0
