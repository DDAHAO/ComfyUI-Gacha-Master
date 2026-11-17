"""Restart verification and explicit reconciliation.

Recovery never submits prompts.  It gathers local receipts/files and remote
queue/history evidence, backfills only unambiguous completions, and otherwise
keeps attempts UNKNOWN for an explicit user decision.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Mapping, Optional

from . import storage
from .adapters.base import Engine, LookupResult
from .errors import GachaError, conflict, not_found
from .states import AttemptStatus, DrawStatus, JobStatus, OutputHealth, RunIntent
from .util import json_text, sha256_file, utc_now


class RecoveryManager:
    def __init__(self, *, db: Any, repo: Any, settings: Any, inputs: Any, storage_module=storage) -> None:
        self.db = db
        self.repo = repo
        self.settings = settings
        self.inputs = inputs
        self.storage = storage_module
        self._lock = threading.RLock()
        self.last_report: Optional[dict[str, Any]] = None

    async def startup(self, engine: Engine, *, boot_id: str) -> dict[str, Any]:
        """Clear run authorization and produce a recovery report.

        Must run before the scheduler is allowed to submit anything.
        """
        with self._lock:
            self.repo.set_runtime_meta("boot_id", boot_id)
            self.repo.set_runtime_meta("last_boot_at", utc_now())
            # Restart unconditionally clears automatic run authorization.
            for job in self.repo.all_jobs():
                if job["run_intent"] != RunIntent.STOPPED:
                    try:
                        self.repo.update_job(
                            job["id"],
                            run_intent=RunIntent.STOPPED,
                            actor="recovery",
                            reason="process restart clears run authorization",
                        )
                    except GachaError:
                        pass
            jobs = [
                job
                for job in self.repo.all_jobs()
                if job["status"] in (JobStatus.RUNNING, JobStatus.PAUSING, JobStatus.CREATING, JobStatus.RECOVERY_REQUIRED)
                or self.repo.list_unknown_attempts(job_id=job["id"])
            ]
            reports = []
            for job in jobs:
                report = await self.recover_job(job, engine, boot_id=boot_id)
                try:
                    report["output_check"] = self.verify_completed_outputs(job)
                except Exception as exc:
                    report["output_check"] = {"error": str(exc)}
                reports.append(report)
            # Any active attempt on a job not in the list above is still checked.
            slot = self.repo.get_slot()
            if slot.get("attempt_id"):
                attempt = self.repo.get_attempt(slot["attempt_id"])
                if attempt:
                    draw = self.repo.get_draw(attempt["draw_id"])
                    job = self.repo.get_job(draw["job_id"]) if draw else None
                    if job and not any(r["job_id"] == job["id"] for r in reports):
                        reports.append(await self.recover_job(job, engine, boot_id=boot_id))
            report = {
                "schema_version": 1,
                "generated_at": utc_now(),
                "boot_id": boot_id,
                "jobs": reports,
                "requires_user_confirmation": any(report["needs_attention"] for report in reports),
            }
            self.last_report = report
            self.repo.set_runtime_meta("recovery_report", json_text(report))
            return report

    async def recover_job(self, job: Mapping[str, Any], engine: Engine, *, boot_id: str) -> dict[str, Any]:
        attempts = self.repo.list_active_attempts()
        job_attempts = []
        for attempt in attempts:
            draw = self.repo.get_draw(attempt["draw_id"])
            if draw is None or draw["job_id"] != job["id"]:
                continue
            job_attempts.append(await self.classify_attempt(job, draw, attempt, engine, boot_id=boot_id))
        unknown = self.repo.list_unknown_attempts(job_id=job["id"])
        for attempt in unknown:
            if any(item["attempt_id"] == attempt["id"] for item in job_attempts):
                continue
            draw = self.repo.get_draw(attempt["draw_id"])
            if draw:
                job_attempts.append(await self.classify_attempt(job, draw, attempt, engine, boot_id=boot_id))
        # Collapse completed jobs if all plan work really succeeded.
        latest = self.repo.get_job(job["id"]) or job
        needs_attention = False
        for item in job_attempts:
            if item["classification"] in {
                "unknown",
                "files_found_terminal_unknown",
                "remote_active",
                "output_problem",
                "db_inconsistent",
            }:
                needs_attention = True
        if latest["status"] in (JobStatus.RUNNING, JobStatus.PAUSING):
            needs_attention = True
            if latest["status"] != JobStatus.RECOVERY_REQUIRED:
                try:
                    self.repo.update_job(
                        job["id"],
                        status=JobStatus.RECOVERY_REQUIRED,
                        run_intent=RunIntent.STOPPED,
                        recovery_version=int(latest["recovery_version"]) + 1,
                        current_error_message="进程重启；执行状态已完成初步核验，请确认后继续。",
                        actor="recovery",
                        reason="restart",
                    )
                except GachaError:
                    pass
        elif needs_attention and latest["status"] not in (JobStatus.BLOCKED, JobStatus.RECOVERY_REQUIRED):
            try:
                self.repo.update_job(
                    job["id"],
                    status=JobStatus.RECOVERY_REQUIRED,
                    run_intent=RunIntent.STOPPED,
                    recovery_version=int(latest["recovery_version"]) + 1,
                    current_error_code="SUBMISSION_UNKNOWN",
                    current_error_message="存在需要人工核验的执行或输出。",
                    actor="recovery",
                    reason="needs attention",
                )
            except GachaError:
                pass
        return {
            "job_id": job["id"],
            "display_id": job["display_id"],
            "status": (self.repo.get_job(job["id"]) or latest)["status"],
            "needs_attention": needs_attention,
            "attempts": job_attempts,
        }

    async def classify_attempt(
        self,
        job: Mapping[str, Any],
        draw: Mapping[str, Any],
        attempt: Mapping[str, Any],
        engine: Engine,
        *,
        boot_id: str,
    ) -> dict[str, Any]:
        attempt_id = attempt["id"]
        prompt_id = attempt.get("actual_prompt_id") or attempt.get("expected_prompt_id")
        manifest = None
        manifest_error = None
        try:
            manifest = self.storage.read_attempt_manifest(attempt["attempt_dir"])
        except GachaError as exc:
            manifest_error = exc.message
        receipt = None
        try:
            receipt = self.storage.read_terminal_receipt(job["job_dir"], attempt_id)
        except GachaError:
            receipt = None
        lookup: Optional[LookupResult] = None
        try:
            lookup = await engine.lookup(str(prompt_id))
        except Exception as exc:
            lookup = LookupResult("error", detail=f"{type(exc).__name__}: {exc}")
        evidence = {
            "attempt_id": attempt_id,
            "prompt_id": prompt_id,
            "db_status": attempt["status"],
            "manifest": os.path.join(attempt["attempt_dir"], storage.MANIFEST_NAME) if manifest else None,
            "manifest_error": manifest_error,
            "terminal_receipt": bool(receipt),
            "remote_state": lookup.state if lookup else "error",
            "remote_detail": lookup.detail if lookup else None,
            "checked_at": utc_now(),
        }
        self.repo.update_attempt(
            attempt_id,
            reconcile_json=json.dumps(evidence, ensure_ascii=False),
            remote_state=lookup.state if lookup else "error",
        )
        # Active remote work: keep the attempt and slot; never resubmit.
        if lookup is not None and lookup.state in {"queued", "running"}:
            return {
                "attempt_id": attempt_id,
                "draw_id": draw["id"],
                "classification": "remote_active",
                "evidence": evidence,
                "detail": "远端仍在排队/执行；请继续等待或使用立即暂停的定向能力。",
            }
        terminal_success = None
        if receipt is not None:
            status = (receipt.get("evidence") or {}).get("status") or {}
            if isinstance(status, dict) and status.get("status_str") == "success":
                terminal_success = "receipt"
        if lookup is not None and lookup.state == "history_success":
            terminal_success = "history"
        if terminal_success and manifest is not None:
            try:
                outputs = self.storage.verify_attempt_outputs(
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
                self._mark_output_failed(job, attempt, draw, exc)
                return {
                    "attempt_id": attempt_id,
                    "draw_id": draw["id"],
                    "classification": "output_problem",
                    "evidence": {**evidence, "verify_error": exc.message},
                    "detail": "执行终态成功但输出核验失败；DRAW 记为失败，已保留文件证据。",
                }
            try:
                self.repo.finalize_success(
                    attempt_id=attempt_id,
                    outputs=outputs,
                    manifest_path=os.path.join(attempt["attempt_dir"], storage.MANIFEST_NAME),
                    manifest_hash=sha256_file(os.path.join(attempt["attempt_dir"], storage.MANIFEST_NAME))[0],
                    actor="recovery",
                )
                if receipt is None:
                    self.storage.write_terminal_receipt(
                        job["job_dir"],
                        attempt_id=attempt_id,
                        evidence={"source": "recovery_history", "status": (lookup.history or {}).get(str(prompt_id), {}).get("status"), "observed_at": utc_now()},
                    )
                self.repo.clear_slot_if_owned(attempt_id, actor="recovery")
                return {
                    "attempt_id": attempt_id,
                    "draw_id": draw["id"],
                    "classification": "recovered_success",
                    "evidence": evidence,
                    "detail": "已按可靠终态与完整输出幂等补记成功。",
                }
            except GachaError as exc:
                return {
                    "attempt_id": attempt_id,
                    "draw_id": draw["id"],
                    "classification": "db_inconsistent",
                    "evidence": {**evidence, "finalize_error": exc.message},
                    "detail": "补记成功时遇到冲突，保留原状态。",
                }
        if terminal_success and manifest is None:
            self._mark_output_failed(
                job, attempt, draw, GachaError("OUTPUT_INCOMPLETE", "执行成功但缺少完整输出清单。")
            )
            return {
                "attempt_id": attempt_id,
                "draw_id": draw["id"],
                "classification": "output_problem",
                "evidence": evidence,
                "detail": "执行终态成功，但缺少完整输出清单。",
            }
        if lookup is not None and lookup.state in {"history_error", "history_interrupted"}:
            status = AttemptStatus.INTERRUPTED if lookup.state == "history_interrupted" else AttemptStatus.FAILED
            self.repo.update_attempt(
                attempt_id,
                status=status,
                draw_status=DrawStatus.INTERRUPTED if status == AttemptStatus.INTERRUPTED else DrawStatus.FAILED,
                finished_at=utc_now(),
                error_code="EXECUTION_FAILED",
                error_message="重启核验确认执行失败/中断。",
            )
            self.repo.clear_slot_if_owned(attempt_id, actor="recovery")
            return {
                "attempt_id": attempt_id,
                "draw_id": draw["id"],
                "classification": "failed",
                "evidence": evidence,
                "detail": "远端历史确认失败/中断；等待用户重试。",
            }
        if manifest is not None:
            # Files may be complete, but no reliable success terminal exists.
            self.repo.update_attempt(
                attempt_id,
                status=AttemptStatus.UNKNOWN,
                draw_status=DrawStatus.UNKNOWN,
                remote_state="files_found_terminal_unknown",
                error_code="SUBMISSION_UNKNOWN",
                error_message="文件已找到，执行终态待核实。",
            )
            return {
                "attempt_id": attempt_id,
                "draw_id": draw["id"],
                "classification": "files_found_terminal_unknown",
                "evidence": evidence,
                "detail": "完整文件可找回，但没有可靠执行终态；不能自动认定成功。",
            }
        try:
            partial = self.storage.scan_attempt_files(
                attempt["attempt_dir"], job["results_root"], max_pixels=int(self.settings.decode_max_pixels)
            )
            if partial:
                self.repo.record_partial_outputs(attempt_id, partial)
                evidence["partial_files"] = len(partial)
        except Exception:
            pass
        self.repo.update_attempt(
            attempt_id,
            status=AttemptStatus.UNKNOWN,
            draw_status=DrawStatus.UNKNOWN,
            remote_state="absent",
            error_code="SUBMISSION_UNKNOWN",
            error_message="重启后无法确定执行状态；不自动重提。",
        )
        return {
            "attempt_id": attempt_id,
            "draw_id": draw["id"],
            "classification": "unknown",
            "evidence": evidence,
            "detail": "queue/history 均无匹配证据，保持 UNKNOWN。",
        }

    def _mark_output_failed(
        self, job: Mapping[str, Any], attempt: Mapping[str, Any], draw: Mapping[str, Any], exc: GachaError
    ) -> None:
        partial = self.storage.scan_attempt_files(
            attempt["attempt_dir"],
            job["results_root"],
            max_pixels=int(self.settings.decode_max_pixels),
        )
        if partial:
            try:
                self.repo.record_partial_outputs(attempt["id"], partial)
            except Exception:
                pass
        self.repo.update_attempt(
            attempt["id"],
            status=AttemptStatus.FAILED,
            draw_status=DrawStatus.FAILED,
            finished_at=utc_now(),
            remote_state="output_problem",
            error_code=exc.code,
            error_message=exc.message,
        )
        self.repo.clear_slot_if_owned(attempt["id"], actor="recovery")
        self.repo.append_event(
            job_id=job["id"],
            entity_type="ATTEMPT",
            entity_id=attempt["id"],
            action="OUTPUT_VERIFICATION_FAILED",
            reason=exc.code,
            actor="recovery",
            payload={"message": exc.message, "draw_id": draw["id"]},
        )

    async def reconcile(self, attempt_id: str, engine: Engine, *, boot_id: str = "reconcile") -> dict[str, Any]:
        with self._lock:
            attempt = self.repo.get_attempt(attempt_id)
            if attempt is None:
                raise not_found("尝试不存在。", attempt_id=attempt_id)
            draw = self.repo.get_draw(attempt["draw_id"])
            if draw is None:
                raise not_found("DRAW 不存在。", draw_id=attempt["draw_id"])
            job = self.repo.get_job(draw["job_id"])
            if job is None:
                raise not_found("任务不存在。", job_id=draw["job_id"])
            result = await self.classify_attempt(job, draw, attempt, engine, boot_id=boot_id)
            return result

    async def resolve(
        self,
        attempt_id: str,
        engine: Engine,
        *,
        decision: str,
        reason: Optional[str] = None,
        confirm_inactive: bool = False,
    ) -> dict[str, Any]:
        decision = decision.lower()
        if decision not in {"retry", "mark_failed", "abandon", "keep_unknown", "accept_success"}:
            raise GachaError("BAD_REQUEST", "处理方式无效。")
        with self._lock:
            attempt = self.repo.get_attempt(attempt_id)
            if attempt is None:
                raise not_found("尝试不存在。", attempt_id=attempt_id)
            draw = self.repo.get_draw(attempt["draw_id"])
            if draw is None:
                raise not_found("DRAW 不存在。", draw_id=attempt["draw_id"])
            job = self.repo.get_job(draw["job_id"])
            if job is None:
                raise not_found("任务不存在。", job_id=draw["job_id"])
            if decision == "keep_unknown":
                self._event(job, attempt, "UNKNOWN_KEPT", reason)
                return {"attempt_id": attempt_id, "decision": decision}
            if decision == "accept_success":
                receipts = None
                try:
                    receipts = self.storage.read_terminal_receipt(job["job_dir"], attempt_id)
                except GachaError:
                    receipts = None
                if receipts is None:
                    raise GachaError(
                        "SUBMISSION_UNKNOWN",
                        "没有可靠成功终态证据，不能人工标记成功。",
                    )
                return await self.classify_attempt(job, draw, attempt, engine, boot_id="resolve")
            if decision in {"retry", "mark_failed", "abandon"}:
                if attempt["status"] == AttemptStatus.SUCCEEDED:
                    raise GachaError("STATE_CONFLICT", "已成功的尝试不能重试。")
                if attempt["status"] not in (AttemptStatus.UNKNOWN, AttemptStatus.FAILED, AttemptStatus.INTERRUPTED):
                    raise GachaError(
                        "STATE_CONFLICT",
                        "只有失败、中断或 UNKNOWN 的尝试可以人工处理。",
                        current_status=attempt["status"],
                    )
                lookup = await engine.lookup(str(attempt.get("actual_prompt_id") or attempt.get("expected_prompt_id")))
                if lookup.state in {"queued", "running"} and not confirm_inactive:
                    raise GachaError(
                        "STATE_CONFLICT",
                        "旧执行仍在远端活动；必须先确认不再活动。",
                        details={"remote_state": lookup.state},
                    )
                if lookup.state in {"queued", "running"} and confirm_inactive:
                    # Even with confirmation, do not claim it stopped.  Refuse
                    # to silently overlap a possibly running execution.
                    raise GachaError(
                        "STATE_CONFLICT",
                        "远端仍在活动，不能创建重叠执行；请等待结束或使用定向取消。",
                        details={"remote_state": lookup.state},
                    )
                new_status = AttemptStatus.ABANDONED if decision in {"retry", "abandon"} else AttemptStatus.FAILED
                try:
                    self.repo.update_attempt(
                        attempt_id,
                        status=new_status,
                        finished_at=utc_now(),
                        error_code="EXECUTION_FAILED",
                        error_message=reason or f"用户处理：{decision}",
                        draw_status=DrawStatus.PENDING if decision == "retry" else DrawStatus.FAILED,
                    )
                except GachaError:
                    # UNKNOWN -> PENDING on draw is legal; if attempt is already
                    # terminal, just ensure the draw is retryable.
                    if decision == "retry":
                        self.repo.set_draw_status(draw["id"], DrawStatus.PENDING)
                self.repo.clear_slot_if_owned(attempt_id, actor="user")
                try:
                    self.storage.unregister_attempt_context(attempt_id)
                    self.storage.remove_staged_input(
                        getattr(self.settings, "comfy_input_root", None), attempt.get("input_stage_name")
                    )
                except Exception:
                    pass
                self._event(job, attempt, "UNKNOWN_RESOLVED", reason, decision=decision)
                self._bump_recovery(job)
                return {
                    "attempt_id": attempt_id,
                    "decision": decision,
                    "draw_status": (self.repo.get_draw(draw["id"]) or {}).get("status"),
                }
            raise GachaError("BAD_REQUEST", "处理方式无效。")

    def verify_completed_outputs(self, job: Mapping[str, Any]) -> dict[str, Any]:
        """Re-check files behind already-succeeded DRAWs without rewriting history.

        A PURGED OUT is only exempted from the accidental-missing check when a
        completed cleanup item proves the user deliberately deleted it.  A file
        that reappears at a purged path is reported as an identity conflict and
        never deleted or silently reclassified.
        """
        rows = self.db.query_all(
            """
            SELECT o.*, COALESCE(ret.state,'RETAINED') AS retention_state,
                   EXISTS(SELECT 1 FROM cleanup_items ci
                           WHERE ci.output_id=o.id AND ci.status='DELETED') AS purged_evidence
              FROM outputs o
              JOIN attempts a ON a.id=o.attempt_id
              JOIN draws d ON d.id=a.draw_id
              LEFT JOIN output_retention ret ON ret.output_id=o.id
             WHERE d.job_id=? AND d.accepted_attempt_id=a.id AND d.status='SUCCEEDED'
               AND o.health IN ('VERIFIED','MISSING','CORRUPT')
            """,
            (job["id"],),
        )
        missing = corrupt = checked = purged = conflicts = cleaning = 0
        for row in rows:
            path = os.path.join(job["results_root"], *str(row["rel_path"]).split("/"))
            if row["retention_state"] == "PURGED" and int(row["purged_evidence"] or 0) == 1:
                purged += 1
                if os.path.isfile(path):
                    conflicts += 1
                continue
            if row["retention_state"] == "CLEANING":
                # An unresolved delete intent must be reconciled before this
                # check runs; do not turn it into an accidental MISSING.
                cleaning += 1
                continue
            checked += 1
            if not os.path.isfile(path):
                if row["health"] != OutputHealth.MISSING:
                    self.repo.update_output_health(row["id"], OutputHealth.MISSING, "重启核验：文件缺失")
                    missing += 1
                continue
            try:
                actual, size = sha256_file(path)
            except OSError:
                if row["health"] != OutputHealth.CORRUPT:
                    self.repo.update_output_health(row["id"], OutputHealth.CORRUPT, "重启核验：文件无法读取")
                    corrupt += 1
                continue
            if actual != row["sha256"] or int(size) != int(row["size"]):
                if row["health"] != OutputHealth.CORRUPT:
                    self.repo.update_output_health(row["id"], OutputHealth.CORRUPT, "重启核验：哈希或大小不匹配")
                    corrupt += 1
            elif row["health"] != OutputHealth.VERIFIED:
                self.repo.update_output_health(row["id"], OutputHealth.VERIFIED, "重启核验：文件恢复一致")
        return {
            "checked": checked,
            "missing": missing,
            "corrupt": corrupt,
            "purged": purged,
            "cleaning_unresolved": cleaning,
            "reappeared_conflicts": conflicts,
        }

    def _bump_recovery(self, job: Mapping[str, Any]) -> None:
        latest = self.repo.get_job(job["id"]) or job
        remaining = self.repo.list_unknown_attempts(job_id=job["id"])
        active = [
            attempt
            for attempt in self.repo.list_active_attempts()
            if (self.repo.get_draw(attempt["draw_id"]) or {}).get("job_id") == job["id"]
            and attempt["status"] != AttemptStatus.UNKNOWN
        ]
        new_status = latest["status"]
        if not remaining and not active and latest["status"] == JobStatus.RECOVERY_REQUIRED:
            new_status = JobStatus.PAUSED
        try:
            self.repo.update_job(
                job["id"],
                status=new_status,
                run_intent=RunIntent.STOPPED,
                recovery_version=int(latest["recovery_version"]) + 1,
                actor="user",
                reason="recovery decision",
            )
        except GachaError:
            pass

    def _event(
        self,
        job: Mapping[str, Any],
        attempt: Mapping[str, Any],
        action: str,
        reason: Optional[str],
        **payload: Any,
    ) -> None:
        self.repo.append_event(
            job_id=job["id"],
            entity_type="ATTEMPT",
            entity_id=attempt["id"],
            action=action,
            reason=reason,
            actor="user",
            payload=payload or None,
        )

    async def current_report(
        self,
        job: Mapping[str, Any],
        engine: Engine,
    ) -> dict[str, Any]:
        """Recompute a live report for the recovery panel (read-only)."""
        attempts = []
        seen: set[str] = set()
        candidates = []
        for attempt in self.repo.list_active_attempts():
            draw_for_attempt = self.repo.get_draw(attempt["draw_id"])
            if draw_for_attempt and draw_for_attempt["job_id"] == job["id"]:
                candidates.append(attempt)
        candidates.extend(self.repo.list_unknown_attempts(job_id=job["id"]))
        for attempt in candidates:
            if attempt["id"] in seen:
                continue
            seen.add(attempt["id"])
            draw = self.repo.get_draw(attempt["draw_id"])
            lookup = await engine.lookup(str(attempt.get("actual_prompt_id") or attempt.get("expected_prompt_id")))
            attempts.append(
                {
                    "attempt_id": attempt["id"],
                    "draw_id": attempt["draw_id"],
                    "draw_no": draw["draw_no"] if draw else None,
                    "status": attempt["status"],
                    "expected_prompt_id": attempt["expected_prompt_id"],
                    "actual_prompt_id": attempt.get("actual_prompt_id"),
                    "remote_state": lookup.state,
                    "source_id": draw["source_id"] if draw else None,
                    "attempt_dir": attempt["attempt_dir"],
                    "seeds": self.repo.seed_rows(attempt["draw_id"]),
                    "files": self._attempt_files(attempt),
                    "detail": lookup.detail,
                }
            )
        return {
            "job_id": job["id"],
            "recovery_version": job["recovery_version"],
            "unknown_attempts": attempts,
            "slot": self.repo.get_slot(),
        }

    def _attempt_files(self, attempt: Mapping[str, Any]) -> list[dict[str, Any]]:
        try:
            return self.storage.scan_attempt_files(
                attempt["attempt_dir"], os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(attempt["attempt_dir"])))),
                max_pixels=int(self.settings.decode_max_pixels),
            )
        except Exception:
            return []
