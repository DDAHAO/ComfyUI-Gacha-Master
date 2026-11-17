"""Structured errors shared by the service, API and recovery layers.

Every user-facing failure carries a stable code, a Chinese message, whether it
is directly retryable and any entity identifiers needed to act on it.  The
HTTP layer maps these to status codes; the frontend never has to infer a
failure from a 200 response.
"""

from __future__ import annotations

import uuid
from typing import Any, Mapping, Optional


ERROR_STATUS = {
    "BAD_REQUEST": 400,
    "PATH_NOT_ALLOWED": 403,
    "NOT_FOUND": 404,
    "VERSION_CONFLICT": 409,
    "STATE_CONFLICT": 409,
    "IDEMPOTENCY_CONFLICT": 409,
    "EXPORT_FILE_CONFLICT": 409,
    "OUTPUT_CONFLICT": 409,
    "OUTPUT_PURGED": 410,
    "OUTPUT_CLEANING": 409,
    "CLEANUP_NOT_ELIGIBLE": 409,
    "CLEANUP_PREVIEW_STALE": 409,
    "CLEANUP_RECOVERY_REQUIRED": 409,
    "STORAGE_BUSY": 409,
    "INSTANCE_LOCKED": 409,
    "PAYLOAD_TOO_LARGE": 413,
    "UNSUPPORTED_MEDIA": 415,
    "WORKFLOW_UNSUPPORTED": 422,
    "NODE_MISSING": 422,
    "SEED_BINDING_INVALID": 422,
    "EXPORT_REVIEW_REQUIRED": 422,
    "DISK_LOW": 507,
    "READ_ONLY": 507,
    "DB_CORRUPT": 503,
    "DB_BUSY": 503,
    "DB_READ_ONLY": 503,
    "SERVICE_NOT_READY": 503,
    "INPUT_CHANGED": 409,
    "INPUT_MISSING": 409,
    "SNAPSHOT_FAILED": 500,
    "SUBMISSION_UNKNOWN": 409,
    "EXECUTION_FAILED": 500,
    "CANCEL_UNSUPPORTED": 409,
    "OUTPUT_INCOMPLETE": 409,
    "OUTPUT_MISSING": 409,
    "OUTPUT_CORRUPT": 409,
    "INTERNAL_ERROR": 500,
}


class GachaError(Exception):
    """An error that is safe to expose through the plugin API."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: Optional[int] = None,
        retryable: bool = False,
        details: Optional[Mapping[str, Any]] = None,
        trace_id: Optional[str] = None,
        http_status: Optional[int] = None,
        **extra_details: Any,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = dict(details or {})
        # Call sites commonly pass entity ids as keyword arguments
        # (job_id=..., output_id=...); keep them in details so they are visible
        # in the structured error instead of raising TypeError.
        self.details.update(extra_details)
        self.trace_id = trace_id or str(uuid.uuid4())
        self.http_status = http_status or status or ERROR_STATUS.get(code, 500)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "error": {
                "code": self.code,
                "message": self.message,
                "trace_id": self.trace_id,
                "retryable": self.retryable,
                "details": self.details,
            },
        }

    def with_trace(self, trace_id: str) -> "GachaError":
        if not self.trace_id:
            self.trace_id = trace_id
        return self


def bad_request(message: str, **details: Any) -> GachaError:
    return GachaError("BAD_REQUEST", message, details=details)


def not_found(message: str, **details: Any) -> GachaError:
    return GachaError("NOT_FOUND", message, details=details)


def conflict(message: str, code: str = "VERSION_CONFLICT", **details: Any) -> GachaError:
    return GachaError(code, message, details=details)


def internal(message: str = "服务器内部错误", **details: Any) -> GachaError:
    return GachaError("INTERNAL_ERROR", message, details=details)
