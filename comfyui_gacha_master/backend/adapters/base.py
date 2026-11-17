"""Engine adapter contract shared by the real ComfyUI adapter and test fakes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


@dataclass
class SubmitResult:
    # accepted | rejected | unknown | not_sent
    status: str
    prompt_id: Optional[str] = None
    http_status: Optional[int] = None
    error: Optional[dict[str, Any]] = None
    detail: Optional[str] = None

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"

    @property
    def decisive(self) -> bool:
        return self.status in {"accepted", "rejected", "not_sent"}


@dataclass
class LookupResult:
    # queued | running | history_success | history_error | history_interrupted | absent | error
    state: str
    history: Optional[dict[str, Any]] = None
    queue_running: list[Any] = field(default_factory=list)
    queue_pending: list[Any] = field(default_factory=list)
    detail: Optional[str] = None
    http_status: Optional[int] = None

    @property
    def active(self) -> bool:
        return self.state in {"queued", "running"}

    @property
    def terminal(self) -> bool:
        return self.state in {"history_success", "history_error", "history_interrupted"}


@dataclass
class CancelResult:
    dispatched: bool
    verified: bool = False
    detail: Optional[str] = None
    capability: str = "unverified"


class Engine:
    """Minimal async interface used by the scheduler."""

    async def capabilities(self) -> dict[str, Any]:
        raise NotImplementedError

    async def submit(
        self,
        prompt: Mapping[str, Any],
        *,
        expected_prompt_id: str,
        metadata: Mapping[str, Any],
    ) -> SubmitResult:
        raise NotImplementedError

    async def lookup(self, prompt_id: str) -> LookupResult:
        raise NotImplementedError

    async def cancel(self, prompt_id: str) -> CancelResult:
        raise NotImplementedError

    async def object_info(self, class_type: str) -> Optional[dict[str, Any]]:
        return None

    async def close(self) -> None:
        return None
