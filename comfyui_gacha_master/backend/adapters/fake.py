"""In-memory engine used by L0 tests and fault injection.

This is deliberately a test double: it is never selected by the ComfyUI
integration.  It can accept or reject submissions, drop the HTTP response
after accepting, fail execution, and produce real PNG files through the same
``storage.save_batch`` path used by GachaSaveImage.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from .. import storage
from ..errors import GachaError
from ..util import utc_now
from .base import CancelResult, Engine, LookupResult, SubmitResult


@dataclass
class FakeJob:
    prompt_id: str
    expected_prompt_id: str
    prompt: Mapping[str, Any]
    attempt_id: str
    state: str = "queued"  # queued/running/success/error/interrupted
    external: bool = False
    submissions: int = 0
    fail: bool = False
    interrupted: bool = False
    output_count: int = 1
    image_size: tuple[int, int] = (8, 8)
    seed_color: int = 0
    completed_once: bool = False
    started: bool = False
    fail_save_at: Optional[int] = None


class _FailingImage:
    """PIL-like image whose save fails at the injected output index."""

    def __init__(self, image):
        self._image = image

    def save(self, *args, **kwargs):
        if kwargs.get("format") == "PNG":
            raise OSError("injected PNG save failure")
        return self._image.save(*args, **kwargs)


class FakeEngine(Engine):
    def __init__(
        self,
        *,
        honor_client_id: bool = True,
        targeted_cancel: bool = False,
        auto_complete: bool = True,
        outputs_per_attempt: int = 1,
        cancel_removes_pending: bool = False,
    ) -> None:
        self.honor_client_id = honor_client_id
        self._targeted_cancel = targeted_cancel
        self.auto_complete = auto_complete
        self.outputs_per_attempt = outputs_per_attempt
        self.cancel_removes_pending = cancel_removes_pending
        self.jobs: dict[str, FakeJob] = {}
        self.external_jobs: dict[str, FakeJob] = {}
        self.submissions: list[dict[str, Any]] = []
        self.lookups: list[str] = []
        self.cancel_calls: list[str] = []
        self._lock = threading.RLock()
        self.drop_response_next = False
        self.reject_next: Optional[dict[str, Any]] = None
        self.not_sent_next = False
        self.fail_next = False
        self.interrupt_next = False
        self.return_wrong_id = False
        self.auto_run = True
        self.lookup_delay = 0.0
        self.fail_save_at: Optional[int] = None

    # -------------------------------------------------------------- setup
    def add_external(self, prompt_id: str, state: str = "queued") -> None:
        with self._lock:
            self.external_jobs[prompt_id] = FakeJob(
                prompt_id=prompt_id,
                expected_prompt_id=prompt_id,
                prompt={},
                attempt_id="",
                state=state,
                external=True,
            )

    def set_outputs(self, prompt_id: str, count: int, size: tuple[int, int] = (8, 8), color: int = 0) -> None:
        with self._lock:
            job = self.jobs.get(prompt_id)
            if job:
                job.output_count = count
                job.image_size = size
                job.seed_color = color

    # ------------------------------------------------------------ engine
    async def capabilities(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "comfy_version": "fake-engine",
            "frontend_version": "fake",
            "client_supplied_prompt_id": self.honor_client_id,
            "targeted_cancel": "verified_atomic" if self._targeted_cancel else "unverified",
            "queue_lookup": True,
            "history_lookup": True,
            "execution_events": False,
            "load_image_binding": "verified",
            "save_image_binding": "verified",
            "filesystem_no_replace_publish": True,
            "warnings": ["test double: not a real ComfyUI engine"],
        }

    async def submit(
        self,
        prompt: Mapping[str, Any],
        *,
        expected_prompt_id: str,
        metadata: Mapping[str, Any],
    ) -> SubmitResult:
        with self._lock:
            self.submissions.append(
                {
                    "expected_prompt_id": expected_prompt_id,
                    "at": utc_now(),
                    "attempt_id": metadata.get("attempt_id"),
                }
            )
            if self.not_sent_next:
                self.not_sent_next = False
                return SubmitResult("not_sent", detail="connection refused before request bytes")
            if self.reject_next is not None:
                error = self.reject_next
                self.reject_next = None
                return SubmitResult("rejected", http_status=400, error=error)
            actual = expected_prompt_id if self.honor_client_id else str(uuid.uuid4())
            if self.return_wrong_id:
                actual = str(uuid.uuid4())
            job = FakeJob(
                prompt_id=actual,
                expected_prompt_id=expected_prompt_id,
                prompt=prompt,
                attempt_id=str(metadata.get("attempt_id") or ""),
                output_count=self.outputs_per_attempt,
                fail=self.fail_next,
                interrupted=self.interrupt_next,
                fail_save_at=self.fail_save_at,
            )
            self.fail_next = False
            self.interrupt_next = False
            self.jobs[actual] = job
            if self.drop_response_next:
                self.drop_response_next = False
                return SubmitResult("unknown", detail="response dropped after server accepted")
            return SubmitResult("accepted", prompt_id=actual, http_status=200)

    async def lookup(self, prompt_id: str) -> LookupResult:
        if self.lookup_delay:
            await asyncio.sleep(self.lookup_delay)
        with self._lock:
            self.lookups.append(prompt_id)
            job = self.jobs.get(prompt_id)
            if job is None:
                if prompt_id in self.external_jobs:
                    ext = self.external_jobs[prompt_id]
                    return LookupResult(ext.state, queue_running=[ext] if ext.state == "running" else [], queue_pending=[ext] if ext.state == "queued" else [])
                return LookupResult("absent", detail="not in queue or history")
            if job.state in {"success", "error", "interrupted"}:
                return self._terminal_result(job)
            if job.state == "queued" and self.auto_run:
                job.state = "running"
                job.started = True
            if job.state == "running" and self.auto_complete:
                try:
                    self._produce_outputs(job)
                except GachaError as exc:
                    job.state = "error"
                    return LookupResult(
                        "history_error",
                        history=self._history(job, error=exc.message),
                        detail=exc.message,
                    )
                if job.interrupted:
                    job.state = "interrupted"
                elif job.fail:
                    job.state = "error"
                else:
                    job.state = "success"
                return self._terminal_result(job)
            return LookupResult(
                job.state,
                queue_running=[job] if job.state == "running" else [],
                queue_pending=[job] if job.state == "queued" else [],
            )

    def _terminal_result(self, job: FakeJob) -> LookupResult:
        if job.state == "success":
            return LookupResult("history_success", history=self._history(job))
        if job.state == "interrupted":
            return LookupResult("history_interrupted", history=self._history(job, interrupted=True))
        return LookupResult("history_error", history=self._history(job, error="execution failed"))

    def _produce_outputs(self, job: FakeJob) -> None:
        if job.completed_once:
            return
        job.completed_once = True
        from PIL import Image

        images = []
        for index in range(job.output_count):
            color = (
                (job.seed_color + index * 37) % 256,
                (job.seed_color * 3 + index * 11) % 256,
                (job.seed_color * 7 + index * 53) % 256,
            )
            image = Image.new("RGB", job.image_size, color)
            if job.fail_save_at is not None and index == job.fail_save_at:
                images.append(_FailingImage(image))
            else:
                images.append(image)
        storage.save_batch(job.attempt_id, images, max_pixels=100_000_000)

    def _history(self, job: FakeJob, *, error: str = None, interrupted: bool = False) -> dict[str, Any]:
        messages = []
        if interrupted:
            messages.append({"type": "execution_interrupted", "data": {}})
        elif error:
            messages.append({"type": "execution_error", "data": {"exception_message": error}})
        else:
            messages.append({"type": "execution_success", "data": {"prompt_id": job.prompt_id}})
        return {
            job.prompt_id: {
                "prompt": [0, job.prompt_id, job.prompt, {}, []],
                "outputs": {},
                "status": {
                    "status_str": "success" if not error and not interrupted else "error",
                    "completed": True,
                    "messages": messages,
                },
            }
        }

    async def cancel(self, prompt_id: str) -> CancelResult:
        with self._lock:
            self.cancel_calls.append(prompt_id)
            if not self._targeted_cancel:
                return CancelResult(False, False, "targeted cancel unsupported", "unverified")
            if prompt_id in self.external_jobs:
                return CancelResult(False, False, "refusing to cancel external job", "verified_atomic")
            job = self.jobs.get(prompt_id)
            if job is None:
                return CancelResult(False, False, "unknown prompt id", "verified_atomic")
            if job.state in {"queued", "running"}:
                if job.state == "queued" and self.cancel_removes_pending:
                    # Real ComfyUI dequeues a pending prompt without writing
                    # history; expose the same absent-after-cancel shape.
                    self.jobs.pop(prompt_id, None)
                else:
                    job.state = "interrupted"
                    job.interrupted = True
                return CancelResult(True, True, "cancelled", "verified_atomic")
            return CancelResult(False, False, "already terminal", "verified_atomic")

    async def object_info(self, class_type: str) -> Optional[dict[str, Any]]:
        for name in (
            "LoadImage",
            "SaveImage",
            "KSampler",
            "KSamplerAdvanced",
            "CheckpointLoaderSimple",
            "CLIPTextEncode",
            "VAEDecode",
            "VAEEncode",
            "EmptyLatentImage",
            "PreviewImage",
            "GachaSaveImage",
        ):
            if class_type == name:
                return {"name": name, "input": {"required": {}}}
        return None

    async def close(self) -> None:
        return None
