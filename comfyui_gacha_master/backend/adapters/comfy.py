"""Adapter for a running ComfyUI server.

Only documented HTTP routes are used.  No POST retries are enabled, no global
queue clearing is exposed and cancellation is limited to a verified targeted
route.  When targeting cannot be proven safe the adapter reports it and the
scheduler degrades to "stop after the current attempt".
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Mapping, Optional

from ..errors import GachaError
from .base import CancelResult, Engine, LookupResult, SubmitResult


def _queue_contains(queue_entries: Any, prompt_id: str) -> bool:
    if not isinstance(queue_entries, list):
        return False
    for entry in queue_entries:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2 and str(entry[1]) == prompt_id:
            return True
        if isinstance(entry, dict):
            if str(entry.get("prompt_id") or entry.get("id") or "") == prompt_id:
                return True
    return False


class ComfyAdapter(Engine):
    def __init__(
        self,
        *,
        base_url: str,
        submit_timeout: float = 30.0,
        lookup_timeout: float = 10.0,
        targeted_cancel_verified: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.submit_timeout = submit_timeout
        self.lookup_timeout = lookup_timeout
        self.targeted_cancel_verified = targeted_cancel_verified
        self._session = None
        self._session_lock = asyncio.Lock()
        self.client_supplied_prompt_id_state = "unverified"
        self.targeted_cancel_state = "verified_atomic" if targeted_cancel_verified else "unverified"
        self.queue_lookup = False
        self.history_lookup = False
        self.version_info: dict[str, Any] = {}
        self.warnings: list[str] = []
        self.detected = False
        self._object_info_cache: dict[str, Any] = {}

    async def _get_session(self):
        if self._session is None:
            import aiohttp

            async with self._session_lock:
                if self._session is None:
                    self._session = aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=self.lookup_timeout),
                        headers={"User-Agent": "ComfyUI-Gacha-Master/1.0"},
                    )
        return self._session

    async def detect(self) -> dict[str, Any]:
        session = await self._get_session()
        import aiohttp

        warnings = list(self.warnings)
        try:
            async with session.get(f"{self.base_url}/system_stats", timeout=aiohttp.ClientTimeout(total=5)) as response:
                if response.status == 200:
                    data = await response.json()
                    self.version_info = (data or {}).get("system", {}) or {}
                else:
                    warnings.append(f"/system_stats returned {response.status}")
        except Exception as exc:
            warnings.append(f"/system_stats unavailable: {type(exc).__name__}")
        try:
            async with session.get(f"{self.base_url}/queue", timeout=aiohttp.ClientTimeout(total=5)) as response:
                if response.status == 200:
                    data = await response.json()
                    if isinstance(data, dict) and "queue_running" in data and "queue_pending" in data:
                        self.queue_lookup = True
                    else:
                        warnings.append("/queue shape not understood")
                else:
                    warnings.append(f"/queue returned {response.status}")
        except Exception as exc:
            warnings.append(f"/queue unavailable: {type(exc).__name__}")
        try:
            async with session.get(
                f"{self.base_url}/history?max_items=1", timeout=aiohttp.ClientTimeout(total=5)
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if isinstance(data, dict):
                        self.history_lookup = True
                    else:
                        warnings.append("/history shape not understood")
                else:
                    warnings.append(f"/history returned {response.status}")
        except Exception as exc:
            warnings.append(f"/history unavailable: {type(exc).__name__}")
        self.detected = True
        return {
            "comfy_version": self.version_info.get("comfyui_version"),
            "frontend_version": self.version_info.get("required_frontend_version"),
            "python_version": self.version_info.get("python_version"),
            "pytorch_version": self.version_info.get("pytorch_version"),
            "client_supplied_prompt_id": self.client_supplied_prompt_id_state == "verified",
            "client_supplied_prompt_id_state": self.client_supplied_prompt_id_state,
            "targeted_cancel": self.targeted_cancel_state,
            "queue_lookup": self.queue_lookup,
            "history_lookup": self.history_lookup,
            "execution_events": False,
            "load_image_binding": "unverified",
            "save_image_binding": "unverified",
            "warnings": warnings,
        }

    async def capabilities(self) -> dict[str, Any]:
        if not self.detected:
            return await self.detect()
        return {
            "comfy_version": self.version_info.get("comfyui_version"),
            "frontend_version": self.version_info.get("required_frontend_version"),
            "python_version": self.version_info.get("python_version"),
            "pytorch_version": self.version_info.get("pytorch_version"),
            "client_supplied_prompt_id": self.client_supplied_prompt_id_state == "verified",
            "client_supplied_prompt_id_state": self.client_supplied_prompt_id_state,
            "targeted_cancel": self.targeted_cancel_state,
            "queue_lookup": self.queue_lookup,
            "history_lookup": self.history_lookup,
            "execution_events": False,
            "load_image_binding": "unverified",
            "save_image_binding": "unverified",
            "warnings": list(self.warnings),
        }

    async def submit(
        self,
        prompt: Mapping[str, Any],
        *,
        expected_prompt_id: str,
        metadata: Mapping[str, Any],
    ) -> SubmitResult:
        import aiohttp

        session = await self._get_session()
        payload = {
            "prompt": prompt,
            "prompt_id": expected_prompt_id,
            "client_id": metadata.get("client_id"),
            "extra_data": {
                "gacha_master": {
                    "job_id": metadata.get("job_id"),
                    "source_id": metadata.get("source_id"),
                    "draw_id": metadata.get("draw_id"),
                    "attempt_id": metadata.get("attempt_id"),
                }
            },
        }
        try:
            async with session.post(
                f"{self.base_url}/prompt",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self.submit_timeout),
            ) as response:
                text = await response.text()
                try:
                    data = await response.json(content_type=None)
                except Exception:
                    data = None
                if response.status == 200 and isinstance(data, dict) and data.get("prompt_id"):
                    actual = str(data["prompt_id"])
                    if actual == expected_prompt_id:
                        self.client_supplied_prompt_id_state = "verified"
                    else:
                        self.client_supplied_prompt_id_state = "unsupported"
                    return SubmitResult("accepted", prompt_id=actual, http_status=200, detail=text[:500])
                if response.status in (400, 422) and isinstance(data, dict):
                    return SubmitResult(
                        "rejected",
                        http_status=response.status,
                        error={
                            "message": "ComfyUI 拒绝了工作流验证。",
                            "node_errors": data.get("node_errors"),
                            "error": data.get("error"),
                            "detail": text[:1000],
                        },
                    )
                return SubmitResult(
                    "unknown",
                    http_status=response.status,
                    detail=f"unexpected response {response.status}: {text[:500]}",
                )
        except aiohttp.ClientConnectorError as exc:
            return SubmitResult("not_sent", detail=f"connection not established: {exc}")
        except asyncio.TimeoutError:
            return SubmitResult("unknown", detail="submit timeout; execution may have been accepted")
        except aiohttp.ClientError as exc:
            return SubmitResult("unknown", detail=f"client error after send attempt: {type(exc).__name__}: {exc}")
        except Exception as exc:
            return SubmitResult("unknown", detail=f"{type(exc).__name__}: {exc}")

    async def lookup(self, prompt_id: str) -> LookupResult:
        import aiohttp

        session = await self._get_session()
        history: Optional[dict[str, Any]] = None
        try:
            async with session.get(
                f"{self.base_url}/history/{prompt_id}",
                timeout=aiohttp.ClientTimeout(total=self.lookup_timeout),
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if isinstance(data, dict) and prompt_id in data:
                        history = data
                else:
                    return LookupResult("error", http_status=response.status, detail=f"/history returned {response.status}")
        except Exception as exc:
            return LookupResult("error", detail=f"history lookup failed: {type(exc).__name__}: {exc}")
        if history is not None:
            entry = history.get(prompt_id) or {}
            status = entry.get("status")
            if isinstance(status, dict):
                status_str = str(status.get("status_str") or "").lower()
                messages = status.get("messages") or []
                interrupted = any(
                    isinstance(message, dict) and message.get("type") == "execution_interrupted"
                    for message in messages
                    if isinstance(message, dict)
                ) or any(
                    isinstance(message, (list, tuple)) and message and message[0] == "execution_interrupted"
                    for message in messages
                )
                if status_str == "success" and bool(status.get("completed")):
                    return LookupResult("history_success", history=history)
                if status_str == "error":
                    if interrupted:
                        return LookupResult("history_interrupted", history=history)
                    return LookupResult("history_error", history=history)
                return LookupResult("history_unknown", history=history, detail="history status not decisive")
            # No status: file evidence may exist, but execution terminal is unknown.
            return LookupResult("history_unknown", history=history, detail="history entry lacks status")
        # Not in history: inspect queue.
        try:
            async with session.get(
                f"{self.base_url}/queue", timeout=aiohttp.ClientTimeout(total=self.lookup_timeout)
            ) as response:
                if response.status != 200:
                    return LookupResult("error", http_status=response.status, detail=f"/queue returned {response.status}")
                data = await response.json()
        except Exception as exc:
            return LookupResult("error", detail=f"queue lookup failed: {type(exc).__name__}: {exc}")
        running = data.get("queue_running") or []
        pending = data.get("queue_pending") or []
        if _queue_contains(running, prompt_id):
            return LookupResult("running", queue_running=running, queue_pending=pending)
        if _queue_contains(pending, prompt_id):
            return LookupResult("queued", queue_running=running, queue_pending=pending)
        return LookupResult("absent", queue_running=running, queue_pending=pending, detail="not present in queue or history")

    async def cancel(self, prompt_id: str) -> CancelResult:
        if not self.targeted_cancel_verified:
            return CancelResult(
                False,
                False,
                "targeted cancel is not runtime-verified; refusing to send /interrupt or a global cancel",
                "unverified",
            )
        import aiohttp

        session = await self._get_session()
        try:
            async with session.post(
                f"{self.base_url}/api/jobs/{prompt_id}/cancel",
                timeout=aiohttp.ClientTimeout(total=self.lookup_timeout),
            ) as response:
                if response.status != 200:
                    return CancelResult(False, False, f"cancel returned {response.status}", self.targeted_cancel_state)
                data = await response.json(content_type=None)
                dispatched = bool(isinstance(data, dict) and data.get("cancelled"))
                # The endpoint is source-verified atomic, but the caller must
                # still observe the attempt leave the queue before claiming it
                # was interrupted.
                return CancelResult(dispatched, False, "cancel request accepted; awaiting evidence", self.targeted_cancel_state)
        except Exception as exc:
            return CancelResult(False, False, f"{type(exc).__name__}: {exc}", self.targeted_cancel_state)

    async def object_info(self, class_type: str) -> Optional[dict[str, Any]]:
        if class_type in self._object_info_cache:
            return self._object_info_cache[class_type]
        import aiohttp

        session = await self._get_session()
        try:
            async with session.get(
                f"{self.base_url}/object_info/{class_type}",
                timeout=aiohttp.ClientTimeout(total=self.lookup_timeout),
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    value = data.get(class_type) if isinstance(data, dict) else None
                    self._object_info_cache[class_type] = value
                    return value
                if response.status == 404:
                    self._object_info_cache[class_type] = None
                    return None
        except Exception:
            return None
        return None

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
