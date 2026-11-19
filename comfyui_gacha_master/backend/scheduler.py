"""Single-inflight scheduler, submission protocol and terminal verification.

The scheduler is the only component that submits prompts.  Each loop makes a
serialized decision against SQLite: pause intent and a claim cannot both win.
An accepted submission is never repeated; unclear responses become UNKNOWN and
hold the global slot until evidence or an explicit user decision resolves them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import traceback
from typing import Any, Callable, Mapping, Optional

from . import storage
from .adapters.base import CancelResult, Engine, LookupResult, SubmitResult
from .errors import GachaError
from .repository import Repository
from .states import AttemptStatus, DrawStatus, JobStatus, RunIntent
from .util import (
    atomic_write_json,
    fault_point,
    json_text,
    read_json,
    require_disk_space,
    require_writable_dir,
    sha256_file,
    sha256_text,
    utc_now,
    uuid4,
)
from .workflow import (
    apply_output_token,
    build_execution_prompt,
    execution_workflow_hash,
    seed_keys,
)

LOGGER = logging.getLogger("gacha.scheduler")


class Scheduler:
    def __init__(
        self,
        *,
        db: Any,
        repository: Repository,
        settings: Any,
        paths: Any,
        inputs: Any,
        engine_factory: Callable[[], Engine],
        backup_manager: Any = None,
        include_metadata: bool = True,
    ) -> None:
        self.db = db
        self.repo = repository
        self.settings = settings
        self.paths = paths
        self.inputs = inputs
        self.engine_factory = engine_factory
        self.backup_manager = backup_manager
        self.include_metadata = include_metadata
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._engine: Optional[Engine] = None
        self._stop = threading.Event()
        self._started = threading.Event()
        self._error_count = 0
        self._consecutive_lookup_errors = 0
        self._consecutive_absent = 0
        self._cancel_requested: set[str] = set()
        self._cancel_dispatched: set[str] = set()
        self._workflow_cache: dict[str, tuple[str, dict[str, Any]]] = {}
        self._last_long_warning: dict[str, float] = {}
        self.processing_job_id: Optional[str] = None
        self.last_loop_error: Optional[str] = None

    # -------------------------------------------------------------- thread
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._thread_main, name="gacha-scheduler", daemon=True)
        self._thread.start()
        self._started.wait(5.0)

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            self._engine = self.engine_factory()
            self._started.set()
            loop.run_until_complete(self._run())
        except Exception:
            LOGGER.exception("scheduler thread crashed")
            self.last_loop_error = traceback.format_exc()
        finally:
            try:
                if self._engine is not None:
                    loop.run_until_complete(self._engine.close())
            except Exception:
                pass
            try:
                loop.close()
            finally:
                self._loop = None

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    async def _run(self) -> None:
        interval = max(0.01, min(float(self.settings.status_poll), 1.0))
        while not self._stop.is_set():
            try:
                await self.step()
                self._error_count = 0
            except GachaError as exc:
                self.last_loop_error = f"{exc.code}: {exc.message}"
                LOGGER.error("scheduler step error %s: %s", exc.code, exc.message)
                self._error_count += 1
                if exc.code in {"DB_CORRUPT", "DB_READ_ONLY", "DISK_LOW"}:
                    try:
                        job = self.repo.active_run_job()
                        if job is not None:
                            self._block_job(job, exc.code, exc.message, details=exc.details)
                    except Exception:
                        pass
            except Exception:
                self.last_loop_error = traceback.format_exc()
                LOGGER.exception("unexpected scheduler error")
                self._error_count += 1
            delay = interval if self._error_count == 0 else min(2.0, interval * 4)
            await asyncio.sleep(delay)

    # ---------------------------------------------------------------- step
    async def step(self) -> None:
        try:
            await self._step()
        finally:
            self.processing_job_id = None

    async def _step(self) -> None:
        if self.db.fatal:
            return
        # Publish the job before releasing the DB lock so a simultaneous
        # deletion can see preparation and finalization work outside the slot.
        with self.db.read():
            slot = self.repo.get_slot()
            attempt_id = slot.get("attempt_id")
            attempt = self.repo.get_attempt(attempt_id) if attempt_id else None
            draw = self.repo.get_draw(attempt["draw_id"]) if attempt else None
            job = self.repo.get_job(draw["job_id"]) if draw else None
            if not attempt_id:
                job = self.repo.active_run_job()
            self.processing_job_id = job["id"] if job else None
        if attempt_id:
            if attempt is None:
                # Orphan slot: record and clear only if no matching attempt can
                # exist.  This is a DB inconsistency, not a reason to re-submit.
                self.repo.clear_slot_if_owned(attempt_id, actor="scheduler-orphan")
                return
            if draw is None or job is None:
                self.repo.clear_slot_if_owned(attempt_id, actor="scheduler-orphan")
                return
            await self._monitor_attempt(job, draw, attempt)
            return

        if job is None:
            return
        if job["status"] == JobStatus.PAUSING or job["run_intent"] in (RunIntent.PAUSE_NOW, RunIntent.PAUSE_SAFE):
            self._settle_pause(job, reason="no in-flight attempt")
            return
        if job["status"] != JobStatus.RUNNING or job["run_intent"] != RunIntent.RUN:
            return
        if self.repo.pending_draw_count(job["id"]) == 0:
            self._check_completion(job)
            return
        await self._claim_and_submit(job)

    # ------------------------------------------------------------- submit
    async def _claim_and_submit(self, job: Mapping[str, Any]) -> None:
        try:
            self.repo.assert_maintenance_free()
        except GachaError:
            # A result cleanup holds the shared maintenance boundary; the next
            # scheduler tick will retry after it releases.  Never cancel or
            # interrupt anything to make room.
            return
        draw = self.repo.next_executable_draw(job["id"])
        if draw is None:
            self._check_completion(job)
            return
        source = self.repo.get_source(draw["source_id"])
        if source is None:
            self._block_job(job, "INPUT_MISSING", "原图记录不存在，已停止后续调度。")
            return
        try:
            read = self.inputs.verify_source(source, protection_mode=job["protection_mode"])
        except GachaError as exc:
            self._block_job(job, exc.code, exc.message, details=exc.details)
            return
        comfy_input_root = getattr(self.settings, "comfy_input_root", None)
        if not comfy_input_root:
            self._block_job(
                job,
                "SERVICE_NOT_READY",
                "未配置 ComfyUI 输入目录，无法把受控原图交给 LoadImage。",
            )
            return
        try:
            require_writable_dir(self.paths.results_root, what="结果目录")
            require_writable_dir(self.paths.data_root, what="数据目录")
            require_disk_space(self.paths.results_root, 0, int(self.settings.disk_reserve))
            require_disk_space(self.paths.data_root, 0, int(self.settings.disk_reserve))
        except GachaError as exc:
            self._block_job(job, exc.code, exc.message, details=exc.details)
            return
        workflow = self._workflow(job)
        if workflow is None:
            return
        attempt_id = uuid4()
        try:
            staged_name, staged_path = storage.stage_input_file(
                source_bytes=read.data,
                comfy_input_root=comfy_input_root,
                attempt_id=attempt_id,
                extension=self._extension_for(source),
                max_pixels=int(self.settings.decode_max_pixels),
            )
        except GachaError as exc:
            self._block_job(job, exc.code, exc.message, details=exc.details)
            return
        attempt_no = self.repo.next_attempt_no(draw["id"])
        job_result = job["result_dir"]
        source_dir = storage.source_result_dir(job_result, int(source["source_order"]))
        draw_dir = storage.draw_result_dir(source_dir, int(draw["draw_no"]))
        attempt_dir = storage.attempt_result_dir(draw_dir, attempt_no, attempt_id)
        try:
            storage.allocate_attempt_dir(attempt_dir)
        except GachaError as exc:
            storage.remove_staged_input(comfy_input_root, staged_name)
            self._block_job(job, exc.code, exc.message, details=exc.details)
            return
        try:
            prompt, _ = build_execution_prompt(
                workflow,
                load_image_node_id=str(job["bindings"]["load_image_node_id"]),
                save_image_node_id=str(job["bindings"]["save_image_node_id"]),
                staged_name=staged_name,
                seeds=self.repo.get_seed_map(draw["id"]),
            )
            prompt = apply_output_token(prompt, str(job["bindings"]["save_image_node_id"]), attempt_id)
            execution_hash = execution_workflow_hash(prompt)
            expected_seed_keys = seed_keys(
                [
                    type("B", (), {"node_id": binding["node_id"], "input_name": binding["input_name"]})
                    for binding in job["bindings"].get("seed_bindings", [])
                ]
            )
            got_keys = set(self.repo.get_seed_map(draw["id"]).keys())
            if got_keys != set(expected_seed_keys):
                raise GachaError(
                    "SEED_BINDING_INVALID",
                    "DRAW 的种子映射与工作流绑定不一致，拒绝提交。",
                    details={"expected": sorted(expected_seed_keys), "actual": sorted(got_keys)},
                )
            atomic_write_json(
                os.path.join(job["job_dir"], "receipts", attempt_id, "execution_prompt.json"),
                {
                    "schema_version": 1,
                    "attempt_id": attempt_id,
                    "execution_hash": execution_hash,
                    "staged_name": staged_name,
                    "prompt": prompt,
                },
            )
            context = storage.AttemptContext(
                job_id=job["id"],
                job_display_id=job["display_id"],
                source_id=source["id"],
                source_order=int(source["source_order"]),
                draw_id=draw["id"],
                draw_no=int(draw["draw_no"]),
                attempt_id=attempt_id,
                attempt_no=attempt_no,
                attempt_dir=attempt_dir,
                results_root=job["results_root"],
                source_sha256=source["sha256"],
                execution_hash=execution_hash,
                include_metadata=self.include_metadata,
                max_pixels=int(self.settings.decode_max_pixels),
                comfy_output_root=getattr(self.settings, "comfy_output_root", None),
            )
            storage.register_attempt_context(context)
            attempt = self.repo.claim_draw(
                draw_id=draw["id"],
                attempt_id=attempt_id,
                boot_id=str(self.repo.get_runtime_meta("boot_id") or "unknown"),
                execution_hash=execution_hash,
                source_hash=source["sha256"],
                attempt_dir=attempt_dir,
                input_stage_name=staged_name,
                slot_owner=str(self.repo.get_runtime_meta("boot_id") or "unknown"),
            )
        except GachaError as exc:
            storage.remove_staged_input(comfy_input_root, staged_name)
            try:
                storage.unregister_attempt_context(attempt_id)
            except Exception:
                pass
            if exc.code in {"STATE_CONFLICT", "STORAGE_BUSY"}:
                # Pause or a result cleanup won the race; retry on a later tick
                # without changing job state.
                try:
                    if os.path.isdir(attempt_dir) and not os.listdir(attempt_dir):
                        os.rmdir(attempt_dir)
                except OSError:
                    pass
                return
            self._block_job(job, exc.code, exc.message, details=exc.details)
            return
        except Exception as exc:
            storage.remove_staged_input(comfy_input_root, staged_name)
            try:
                storage.unregister_attempt_context(attempt_id)
            except Exception:
                pass
            self._block_job(job, "INTERNAL_ERROR", "准备提交时发生内部错误。", details={"error": str(exc)})
            return

        fault_point("F02")
        metadata = {
            "job_id": job["id"],
            "source_id": source["id"],
            "draw_id": draw["id"],
            "attempt_id": attempt_id,
            "client_id": "gacha-master-" + str(self.repo.get_runtime_meta("boot_id") or "boot"),
        }
        try:
            result = await self._engine.submit(prompt, expected_prompt_id=attempt_id, metadata=metadata)
        except Exception as exc:
            result = SubmitResult("unknown", detail=f"adapter exception: {type(exc).__name__}: {exc}")
        self._handle_submit_result(job, draw, attempt, result, staged_name, comfy_input_root)

    def _handle_submit_result(
        self,
        job: Mapping[str, Any],
        draw: Mapping[str, Any],
        attempt: Mapping[str, Any],
        result: SubmitResult,
        staged_name: str,
        comfy_input_root: str,
    ) -> None:
        attempt_id = attempt["id"]
        if result.status == "accepted":
            actual = result.prompt_id or attempt["expected_prompt_id"]
            self.repo.update_attempt(
                attempt_id,
                status=AttemptStatus.QUEUED,
                actual_prompt_id=actual,
                submitted_at=utc_now(),
                remote_state="queued",
                draw_status=DrawStatus.QUEUED,
            )
            if actual != attempt["expected_prompt_id"]:
                self.repo.append_event(
                    job_id=job["id"],
                    entity_type="ATTEMPT",
                    entity_id=attempt_id,
                    action="CLIENT_PROMPT_ID_REPLACED",
                    reason="server did not honour client-supplied prompt id",
                    actor="scheduler",
                    payload={"expected": attempt["expected_prompt_id"], "actual": actual},
                )
            self._consecutive_absent = 0
            self._consecutive_lookup_errors = 0
            return
        if result.status == "rejected":
            detail = (result.error or {}).get("detail") or json.dumps(result.error or {}, ensure_ascii=False)[:2000]
            self._fail_attempt(
                job,
                draw,
                attempt,
                code="WORKFLOW_UNSUPPORTED",
                message="ComfyUI 拒绝了工作流验证，未入队。",
                detail=detail,
                release_slot=True,
                block=True,
            )
            storage.remove_staged_input(comfy_input_root, staged_name)
            storage.unregister_attempt_context(attempt_id)
            return
        if result.status == "not_sent":
            self._fail_attempt(
                job,
                draw,
                attempt,
                code="EXECUTION_FAILED",
                message="连接未建立，请求未发送；已停止后续调度。",
                detail=result.detail,
                release_slot=True,
                block=True,
            )
            storage.remove_staged_input(comfy_input_root, staged_name)
            storage.unregister_attempt_context(attempt_id)
            return
        # unknown: the request may have been accepted.  Never POST again.
        self._mark_unknown(
            job,
            draw,
            attempt,
            message="提交结果未知；已停止自动提交，等待核验。",
            detail=result.detail,
            remote_state="unknown",
        )
        # Keep the staged input and context: the remote execution may still run
        # and produce recoverable files.

    # ------------------------------------------------------------ monitor
    async def _monitor_attempt(
        self, job: Mapping[str, Any], draw: Mapping[str, Any], attempt: Mapping[str, Any]
    ) -> None:
        prompt_id = attempt.get("actual_prompt_id") or attempt.get("expected_prompt_id")
        lookup = await self._engine.lookup(str(prompt_id))
        if lookup.state == "error":
            self._consecutive_lookup_errors += 1
            LOGGER.warning("lookup error %s: %s", prompt_id, lookup.detail)
            if self._consecutive_lookup_errors >= 5 and job["status"] not in (
                JobStatus.RECOVERY_REQUIRED,
                JobStatus.BLOCKED,
                JobStatus.PAUSED,
            ):
                self.repo.update_job(
                    job["id"],
                    status=JobStatus.RECOVERY_REQUIRED,
                    run_intent=RunIntent.STOPPED,
                    current_error_code="DB_BUSY" if "busy" in str(lookup.detail).lower() else "SUBMISSION_UNKNOWN",
                    current_error_message="无法读取 ComfyUI 队列/历史，执行状态待核实。",
                    actor="scheduler",
                    reason="status lookup failed",
                )
            return
        self._consecutive_lookup_errors = 0
        if attempt["status"] == AttemptStatus.UNKNOWN and lookup.state in {"queued", "running", "absent"}:
            # Remote activity may still exist.  Do not transition back to a
            # normal running state; record the observation and keep the slot.
            if lookup.state in {"queued", "running"}:
                self.repo.update_attempt(
                    attempt["id"], remote_state=lookup.state, reconcile_json=json.dumps(
                        {"state": lookup.state, "observed_at": utc_now()}, ensure_ascii=False
                    )
                )
            return
        if lookup.state in {"queued", "running"}:
            self._consecutive_absent = 0
            desired = AttemptStatus.QUEUED if lookup.state == "queued" else AttemptStatus.RUNNING
            if attempt["status"] != desired:
                fields: dict[str, Any] = {
                    "status": desired,
                    "draw_status": DrawStatus.QUEUED if desired == AttemptStatus.QUEUED else DrawStatus.RUNNING,
                    "remote_state": lookup.state,
                }
                if desired == AttemptStatus.RUNNING:
                    fields["started_at"] = utc_now()
                self.repo.update_attempt(attempt["id"], **fields)
            # Immediate pause: only use a verified targeted cancel.  The intent
            # is already durable; unsupported versions keep waiting.
            if job["run_intent"] == RunIntent.PAUSE_NOW and attempt["id"] not in self._cancel_requested:
                self._cancel_requested.add(attempt["id"])
                cancel: CancelResult = await self._engine.cancel(str(prompt_id))
                self.repo.append_event(
                    job_id=job["id"],
                    entity_type="ATTEMPT",
                    entity_id=attempt["id"],
                    action="CANCEL_REQUESTED",
                    reason=cancel.detail,
                    actor="user",
                    payload={
                        "dispatched": cancel.dispatched,
                        "verified": cancel.verified,
                        "capability": cancel.capability,
                    },
                )
                if cancel.dispatched:
                    # The endpoint acknowledged a targeted cancel for this
                    # exact prompt.  A queued prompt has no history entry, so
                    # absence after acknowledgement is the evidence of a
                    # successful dequeue; a running target still produces
                    # history_interrupted and is handled there.
                    self._cancel_dispatched.add(attempt["id"])
                if not cancel.dispatched:
                    self.repo.append_event(
                        job_id=job["id"],
                        entity_type="JOB",
                        entity_id=job["id"],
                        action="CANCEL_DEGRADED",
                        reason="当前版本无法安全定向中断，等待本次结束",
                        actor="scheduler",
                    )
            self._check_long_execution(job, attempt)
            return
        if lookup.state == "absent":
            self._consecutive_absent += 1
            if self._consecutive_absent >= 3:
                if attempt["id"] in self._cancel_dispatched:
                    # Cancel endpoint acknowledged this exact prompt and it
                    # left a pending queue without a history entry.  This is
                    # terminal evidence for a queued targeted cancel.
                    self._handle_interrupted(
                        job,
                        draw,
                        attempt,
                        lookup,
                        remote_state="cancel_acknowledged",
                        detail="targeted cancel acknowledged; prompt absent from queue/history",
                    )
                    return
                self._mark_unknown(
                    job,
                    draw,
                    attempt,
                    message="队列与历史均查不到该执行；不能据此重提，已保留为 UNKNOWN。",
                    detail=lookup.detail,
                    remote_state="absent",
                )
            return
        if lookup.state == "history_success":
            self._cancel_dispatched.discard(attempt["id"])
            await self._complete_success(job, draw, attempt, lookup)
            return
        if lookup.state == "history_error":
            self._cancel_dispatched.discard(attempt["id"])
            self._fail_attempt(
                job,
                draw,
                attempt,
                code="EXECUTION_FAILED",
                message="ComfyUI 执行失败；已停止后续调度，不自动重试。",
                detail=self._history_detail(lookup),
                release_slot=True,
                block=True,
                lookup=lookup,
            )
            return
        if lookup.state == "history_interrupted":
            self._handle_interrupted(job, draw, attempt, lookup)
            return
        if lookup.state == "history_unknown":
            self._mark_unknown(
                job,
                draw,
                attempt,
                message="执行历史存在但缺少可靠终态；文件不能单独证明成功。",
                detail=lookup.detail,
                remote_state="history_unknown",
                keep_active=True,
            )
            return

    def _check_long_execution(self, job: Mapping[str, Any], attempt: Mapping[str, Any]) -> None:
        started = attempt.get("started_at") or attempt.get("submitted_at")
        if not started:
            return
        from .util import parse_utc

        then = parse_utc(started)
        if then is None:
            return
        elapsed = time.time() - then.timestamp()
        if elapsed >= int(self.settings.long_execution_warning):
            last = self._last_long_warning.get(attempt["id"], 0)
            if time.time() - last > 600:
                self._last_long_warning[attempt["id"]] = time.time()
                self.repo.append_event(
                    job_id=job["id"],
                    entity_type="ATTEMPT",
                    entity_id=attempt["id"],
                    action="LONG_EXECUTION_WARNING",
                    reason=f"执行已持续约 {int(elapsed // 60)} 分钟",
                    actor="system",
                )

    # ------------------------------------------------------------ success
    async def _complete_success(
        self,
        job: Mapping[str, Any],
        draw: Mapping[str, Any],
        attempt: Mapping[str, Any],
        lookup: LookupResult,
    ) -> None:
        attempt_id = attempt["id"]
        if attempt["status"] in (AttemptStatus.QUEUED, AttemptStatus.RUNNING, AttemptStatus.DISPATCHING):
            self.repo.update_attempt(
                attempt_id,
                status=AttemptStatus.VERIFYING,
                draw_status=DrawStatus.VERIFYING,
                remote_state="history_success",
            )
        history = lookup.history or {}
        entry = history.get(attempt.get("actual_prompt_id") or attempt.get("expected_prompt_id")) or {}
        evidence = {
            "source": "comfy_history",
            "prompt_id": attempt.get("actual_prompt_id") or attempt.get("expected_prompt_id"),
            "status": entry.get("status"),
            "node_outputs": entry.get("outputs"),
            "observed_at": utc_now(),
        }
        try:
            outputs = storage.verify_attempt_outputs(
                attempt_dir=attempt["attempt_dir"],
                results_root=job["results_root"],
                expected={
                    "job_id": job["id"],
                    "source_id": draw["source_id"],
                    "draw_id": draw["id"],
                    "attempt_id": attempt_id,
                    "source_sha256": attempt["source_hash"],
                    "execution_hash": attempt["execution_hash"],
                },
                max_pixels=int(self.settings.decode_max_pixels),
            )
        except GachaError as exc:
            partial = storage.scan_attempt_files(
                attempt["attempt_dir"], job["results_root"], max_pixels=int(self.settings.decode_max_pixels)
            )
            if partial:
                self.repo.record_partial_outputs(attempt_id, partial)
            self._fail_attempt(
                job,
                draw,
                attempt,
                code=exc.code,
                message="输出核验未通过，不能认定 DRAW 成功。",
                detail=json.dumps(exc.details, ensure_ascii=False),
                release_slot=True,
                block=True,
                lookup=lookup,
            )
            return
        # F06: complete file manifest exists but terminal evidence is not yet
        # persisted.  A crash here must not be interpreted as success.
        fault_point("F06")
        try:
            storage.write_terminal_receipt(job["job_dir"], attempt_id=attempt_id, evidence=evidence)
        except GachaError as exc:
            self._fail_attempt(
                job,
                draw,
                attempt,
                code=exc.code,
                message="终态收据无法持久化，拒绝确认成功。",
                detail=json.dumps(exc.details, ensure_ascii=False),
                release_slot=True,
                block=True,
                lookup=lookup,
            )
            return
        manifest_path = os.path.join(attempt["attempt_dir"], storage.MANIFEST_NAME)
        manifest_hash, _ = sha256_file(manifest_path)
        try:
            self.repo.finalize_success(
                attempt_id=attempt_id,
                outputs=outputs,
                manifest_path=manifest_path,
                manifest_hash=manifest_hash,
            )
        except GachaError as exc:
            self._block_job(job, exc.code, exc.message, details=exc.details)
            return
        fault_point("F08")
        storage.unregister_attempt_context(attempt_id)
        storage.remove_staged_input(getattr(self.settings, "comfy_input_root", None) or "", attempt.get("input_stage_name"))
        self.repo.append_event(
            job_id=job["id"],
            entity_type="ATTEMPT",
            entity_id=attempt_id,
            action="ATTEMPT_SUCCEEDED",
            to_state=AttemptStatus.SUCCEEDED,
            actor="scheduler",
            payload={"output_count": len(outputs), "manifest_hash": manifest_hash},
        )
        updated_job = self.repo.get_job(job["id"]) or job
        self._maybe_backup(updated_job)
        self._check_completion(updated_job)
        latest = self.repo.get_job(job["id"]) or updated_job
        if latest["status"] == JobStatus.COMPLETED:
            return
        if latest["run_intent"] in (RunIntent.PAUSE_SAFE, RunIntent.PAUSE_NOW):
            self._settle_pause(latest, reason="current attempt finished")
            return

    # ------------------------------------------------------------ failure
    def _fail_attempt(
        self,
        job: Mapping[str, Any],
        draw: Mapping[str, Any],
        attempt: Mapping[str, Any],
        *,
        code: str,
        message: str,
        detail: Optional[str],
        release_slot: bool,
        block: bool,
        lookup: Optional[LookupResult] = None,
    ) -> None:
        attempt_id = attempt["id"]
        try:
            partial = storage.scan_attempt_files(
                attempt["attempt_dir"], job["results_root"], max_pixels=int(self.settings.decode_max_pixels)
            )
            if partial:
                self.repo.record_partial_outputs(attempt_id, partial)
        except Exception:
            LOGGER.exception("failed to record partial outputs for %s", attempt_id)
        try:
            self.repo.update_attempt(
                attempt_id,
                status=AttemptStatus.FAILED,
                draw_status=DrawStatus.FAILED,
                finished_at=utc_now(),
                remote_state=(lookup.state if lookup else "failure"),
                error_code=code,
                error_message=message,
            )
        except GachaError:
            # A parallel decision may already have moved it; only release the
            # slot if it still belongs to this attempt.
            LOGGER.exception("failed to mark attempt failed")
        if release_slot:
            self.repo.clear_slot_if_owned(attempt_id)
        storage.remove_staged_input(
            getattr(self.settings, "comfy_input_root", None) or "", attempt.get("input_stage_name")
        )
        storage.unregister_attempt_context(attempt_id)
        self.repo.append_event(
            job_id=job["id"],
            entity_type="ATTEMPT",
            entity_id=attempt_id,
            action="ATTEMPT_FAILED",
            to_state=AttemptStatus.FAILED,
            reason=code,
            actor="scheduler",
            payload={"message": message, "detail": (detail or "")[:2000], "draw_id": draw["id"]},
        )
        if block:
            self._block_job(job, code, message, details={"attempt_id": attempt_id, "detail": detail})

    def _handle_interrupted(
        self,
        job: Mapping[str, Any],
        draw: Mapping[str, Any],
        attempt: Mapping[str, Any],
        lookup: LookupResult,
        *,
        remote_state: str = "history_interrupted",
        detail: Optional[str] = None,
    ) -> None:
        attempt_id = attempt["id"]
        self._cancel_dispatched.discard(attempt_id)
        try:
            self.repo.update_attempt(
                attempt_id,
                status=AttemptStatus.INTERRUPTED,
                draw_status=DrawStatus.INTERRUPTED,
                finished_at=utc_now(),
                remote_state=remote_state,
                error_code="EXECUTION_FAILED",
                error_message="执行被中断。",
            )
        except GachaError:
            LOGGER.exception("failed to mark attempt interrupted")
        self.repo.clear_slot_if_owned(attempt_id)
        storage.remove_staged_input(
            getattr(self.settings, "comfy_input_root", None) or "", attempt.get("input_stage_name")
        )
        storage.unregister_attempt_context(attempt_id)
        self.repo.append_event(
            job_id=job["id"],
            entity_type="ATTEMPT",
            entity_id=attempt_id,
            action="ATTEMPT_INTERRUPTED",
            to_state=AttemptStatus.INTERRUPTED,
            actor="scheduler",
            payload={"detail": detail if detail is not None else self._history_detail(lookup)},
        )
        current = self.repo.get_job(job["id"]) or job
        if current["run_intent"] in (RunIntent.PAUSE_NOW, RunIntent.PAUSE_SAFE):
            self.repo.update_job(
                job["id"],
                status=JobStatus.PAUSED,
                run_intent=RunIntent.STOPPED,
                current_error_code="EXECUTION_FAILED",
                current_error_message="当前 DRAW 已中断；如需继续请重试该 DRAW 后恢复。",
                actor="scheduler",
                reason="immediate pause interrupted current attempt",
            )
        else:
            self._block_job(job, "EXECUTION_FAILED", "执行被中断，等待用户处理。")

    def _mark_unknown(
        self,
        job: Mapping[str, Any],
        draw: Mapping[str, Any],
        attempt: Mapping[str, Any],
        *,
        message: str,
        detail: Optional[str],
        remote_state: str,
        keep_active: bool = False,
    ) -> None:
        attempt_id = attempt["id"]
        try:
            self.repo.update_attempt(
                attempt_id,
                status=AttemptStatus.UNKNOWN,
                draw_status=DrawStatus.UNKNOWN,
                remote_state=remote_state,
                error_code="SUBMISSION_UNKNOWN",
                error_message=message,
            )
        except GachaError:
            # If it was already UNKNOWN, still keep the slot.
            LOGGER.warning("attempt %s already left active state while marking UNKNOWN", attempt_id)
        # Slot deliberately stays occupied: an unverified execution may still
        # be running, and this invariant prevents a second one.
        self.repo.update_job(
            job["id"],
            status=JobStatus.RECOVERY_REQUIRED,
            run_intent=RunIntent.STOPPED,
            current_error_code="SUBMISSION_UNKNOWN",
            current_error_message=message,
            actor="scheduler",
            reason=remote_state,
        )
        self.repo.append_event(
            job_id=job["id"],
            entity_type="ATTEMPT",
            entity_id=attempt_id,
            action="ATTEMPT_UNKNOWN",
            to_state=AttemptStatus.UNKNOWN,
            reason=remote_state,
            actor="scheduler",
            payload={"message": message, "detail": (detail or "")[:2000]},
        )

    def _history_detail(self, lookup: LookupResult) -> str:
        history = lookup.history or {}
        for entry in history.values():
            status = entry.get("status") if isinstance(entry, dict) else None
            if isinstance(status, dict):
                return json.dumps(status, ensure_ascii=False)[:2000]
        return (lookup.detail or "")[:2000]

    # ------------------------------------------------------------- helpers
    def _block_job(
        self,
        job: Mapping[str, Any],
        code: str,
        message: str,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        try:
            self.repo.update_job(
                job["id"],
                status=JobStatus.BLOCKED,
                run_intent=RunIntent.STOPPED,
                current_error_code=code,
                current_error_message=message,
                actor="scheduler",
                reason=code,
            )
            self.repo.append_event(
                job_id=job["id"],
                entity_type="JOB",
                entity_id=job["id"],
                action="JOB_BLOCKED",
                to_state=JobStatus.BLOCKED,
                reason=code,
                actor="scheduler",
                payload=dict(details or {}),
            )
        except GachaError:
            LOGGER.exception("failed to block job %s", job["id"])

    def _settle_pause(self, job: Mapping[str, Any], *, reason: str) -> None:
        current = self.repo.get_job(job["id"])
        if current is None:
            return
        # A user resume may have committed after the scheduler observed the
        # pause intent.  Never overwrite a fresh RUN authorization.
        if (
            current["run_intent"] not in (RunIntent.PAUSE_SAFE, RunIntent.PAUSE_NOW)
            and current["status"] != JobStatus.PAUSING
        ):
            return
        if current["status"] == JobStatus.PAUSED and current["run_intent"] == RunIntent.STOPPED:
            return
        try:
            self.repo.update_job(
                current["id"],
                status=JobStatus.PAUSED,
                run_intent=RunIntent.STOPPED,
                actor="scheduler",
                reason=reason,
            )
            self.repo.append_event(
                job_id=current["id"],
                entity_type="JOB",
                entity_id=current["id"],
                action="JOB_PAUSED",
                to_state=JobStatus.PAUSED,
                reason=reason,
                actor="scheduler",
            )
        except GachaError:
            LOGGER.exception("failed to settle pause")

    def _check_completion(self, job: Mapping[str, Any]) -> None:
        summary = self.repo.job_summary(job["id"])
        if summary.get("pending", 0) or summary.get("inflight", 0) or summary.get("unknown", 0):
            return
        if summary.get("effective", 0) == summary.get("succeeded", 0) and summary.get("effective", 0) > 0:
            if job["status"] == JobStatus.COMPLETED:
                return
            if job["status"] == JobStatus.RECOVERY_REQUIRED:
                # A recovered success is reported, but completion waits for the
                # user's explicit confirmation after a restart/unknown.
                return
            self.repo.update_job(
                job["id"],
                status=JobStatus.COMPLETED,
                run_intent=RunIntent.STOPPED,
                completed_at=utc_now(),
                actor="scheduler",
                reason="all effective draws succeeded",
            )
            self.repo.append_event(
                job_id=job["id"],
                entity_type="JOB",
                entity_id=job["id"],
                action="JOB_COMPLETED",
                to_state=JobStatus.COMPLETED,
                actor="scheduler",
            )
            try:
                storage.write_job_manifest(
                    job["result_dir"],
                    job=self.repo.get_job(job["id"]) or job,
                    summary=self.repo.job_summary(job["id"]),
                )
            except Exception:
                LOGGER.exception("failed to write job manifest")

    def _maybe_backup(self, job: Mapping[str, Any]) -> None:
        if self.backup_manager is None:
            return
        summary = self.repo.job_summary(job["id"])
        succeeded = int(summary.get("succeeded", 0))
        last_count = int(job.get("last_backup_draw_count") or 0)
        try:
            backup = self.backup_manager.maybe_backup(
                draws_since=succeeded - last_count,
                last_backup_at=job.get("last_backup_at"),
            )
            if backup is not None:
                self.repo.update_job(
                    job["id"],
                    last_backup_draw_count=succeeded,
                    last_backup_at=utc_now(),
                    actor="system",
                    reason="scheduled backup",
                )
                self.repo.append_event(
                    job_id=job["id"],
                    entity_type="BACKUP",
                    entity_id=backup["id"],
                    action="BACKUP_CREATED",
                    actor="system",
                )
        except GachaError as exc:
            LOGGER.error("backup failed: %s", exc.message)
            self.repo.append_event(
                job_id=job["id"],
                entity_type="BACKUP",
                entity_id=None,
                action="BACKUP_FAILED",
                reason=exc.code,
                actor="system",
                payload={"message": exc.message},
            )

    def _workflow(self, job: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        job_id = job["id"]
        sha = job["workflow_api_sha256"]
        cached = self._workflow_cache.get(job_id)
        if cached and cached[0] == sha:
            return cached[1]
        path = job["workflow_api_path"]
        actual, _ = sha256_file(path)
        if actual != sha:
            self._workflow_cache.pop(job_id, None)
            self._block_job(job, "WORKFLOW_UNSUPPORTED", "冻结工作流哈希发生变化，拒绝继续执行。")
            return None
        try:
            data = read_json(path)
        except Exception as exc:
            self._block_job(job, "WORKFLOW_UNSUPPORTED", "冻结的 API 工作流无法读取。", details={"error": str(exc)})
            return None
        if not isinstance(data, dict):
            self._block_job(job, "WORKFLOW_UNSUPPORTED", "冻结的 API 工作流格式错误。")
            return None
        self._workflow_cache[job_id] = (sha, data)
        return data

    @staticmethod
    def _extension_for(source: Mapping[str, Any]) -> str:
        fmt = str(source.get("image_format") or "").upper()
        return {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}.get(fmt, ".png")
