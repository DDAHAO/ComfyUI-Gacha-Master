"""State definitions and legal transitions.

The database stores these values, but this module is the single authority for
which transitions are legal.  UI text is never used as a state source.
"""

from __future__ import annotations

from .errors import GachaError


class CreationStage:
    """Persistent creation-plan stages.

    These are deliberately separate from inference statuses: a JOB can be
    CREATING while its plan worker is somewhere else in this list.
    """

    WAITING = "WAITING"
    COPYING = "COPYING"
    PLANNING = "PLANNING"
    FINALIZING = "FINALIZING"
    PAUSING = "PAUSING"
    PAUSED = "PAUSED"
    DONE = "DONE"
    FAILED = "FAILED"
    ABANDONED = "ABANDONED"
    ACTIVE = {WAITING, COPYING, PLANNING, FINALIZING}
    ALL = {WAITING, COPYING, PLANNING, FINALIZING, PAUSING, PAUSED, DONE, FAILED, ABANDONED}


class CreationControl:
    CONTINUE = "CONTINUE"
    STOP = "STOP"
    ABANDON = "ABANDON"
    ALL = {CONTINUE, STOP, ABANDON}


class JobStatus:
    CREATING = "CREATING"
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSING = "PAUSING"
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    COMPLETED = "COMPLETED"
    ALL = {
        CREATING,
        READY,
        RUNNING,
        PAUSING,
        PAUSED,
        BLOCKED,
        RECOVERY_REQUIRED,
        COMPLETED,
    }


class RunIntent:
    STOPPED = "STOPPED"
    RUN = "RUN"
    PAUSE_SAFE = "PAUSE_SAFE"
    PAUSE_NOW = "PAUSE_NOW"
    ALL = {STOPPED, RUN, PAUSE_SAFE, PAUSE_NOW}


class SourceStatus:
    ACTIVE = "ACTIVE"
    REMOVED_BY_USER = "REMOVED_BY_USER"
    MISSING = "MISSING"
    CHANGED = "CHANGED"
    ALL = {ACTIVE, REMOVED_BY_USER, MISSING, CHANGED}


class DrawStatus:
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    DISPATCHING = "DISPATCHING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"
    UNKNOWN = "UNKNOWN"
    CANCELLED_BY_USER = "CANCELLED_BY_USER"
    ALL = {
        PENDING,
        CLAIMED,
        DISPATCHING,
        QUEUED,
        RUNNING,
        VERIFYING,
        SUCCEEDED,
        FAILED,
        INTERRUPTED,
        UNKNOWN,
        CANCELLED_BY_USER,
    }
    ACTIVE = {CLAIMED, DISPATCHING, QUEUED, RUNNING, VERIFYING}
    TERMINAL = {SUCCEEDED, FAILED, INTERRUPTED, CANCELLED_BY_USER}


class AttemptStatus:
    CLAIMED = "CLAIMED"
    DISPATCHING = "DISPATCHING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"
    UNKNOWN = "UNKNOWN"
    RECONCILING = "RECONCILING"
    ABANDONED = "ABANDONED"
    ALL = {
        CLAIMED,
        DISPATCHING,
        QUEUED,
        RUNNING,
        VERIFYING,
        SUCCEEDED,
        FAILED,
        INTERRUPTED,
        UNKNOWN,
        RECONCILING,
        ABANDONED,
    }
    ACTIVE = {CLAIMED, DISPATCHING, QUEUED, RUNNING, VERIFYING}
    TERMINAL = {SUCCEEDED, FAILED, INTERRUPTED, ABANDONED}


class OutputHealth:
    STAGED = "STAGED"
    VERIFIED = "VERIFIED"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"
    CORRUPT = "CORRUPT"
    QUARANTINED = "QUARANTINED"
    ALL = {STAGED, VERIFIED, PARTIAL, MISSING, CORRUPT, QUARANTINED}


class RetentionState:
    """Deliberate deletion is not a generation failure.

    ``outputs.health`` keeps recording what happened when the image was
    produced; retention records whether the user intentionally removed the
    file later.  No row is equivalent to RETAINED.
    """

    RETAINED = "RETAINED"
    CLEANING = "CLEANING"
    PURGED = "PURGED"
    ALL = {RETAINED, CLEANING, PURGED}
    UNAVAILABLE = {CLEANING, PURGED}


class CleanupKind:
    UNSELECTED_RESULTS = "UNSELECTED_RESULTS"
    ALL = {UNSELECTED_RESULTS}


class CleanupStatus:
    PREVIEW = "PREVIEW"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL_FAILED = "PARTIAL_FAILED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    ALL = {
        PREVIEW,
        RUNNING,
        COMPLETED,
        PARTIAL_FAILED,
        RECOVERY_REQUIRED,
        EXPIRED,
        CANCELLED,
    }
    TERMINAL = {COMPLETED, PARTIAL_FAILED, EXPIRED, CANCELLED}


class CleanupItemStatus:
    PENDING = "PENDING"
    INTENT = "INTENT"
    DELETED = "DELETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    ALL = {PENDING, INTENT, DELETED, FAILED, SKIPPED}


class ExportStatus:
    DRAFT = "DRAFT"
    FROZEN = "FROZEN"
    COPYING = "COPYING"
    COMPLETED = "COMPLETED"
    PARTIAL_FAILED = "PARTIAL_FAILED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    ALL = {
        DRAFT,
        FROZEN,
        COPYING,
        COMPLETED,
        PARTIAL_FAILED,
        RECOVERY_REQUIRED,
    }


class ExportItemStatus:
    PENDING = "PENDING"
    COPYING = "COPYING"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    ALL = {PENDING, COPYING, VERIFIED, FAILED}


class ExportKind:
    QUICK = "QUICK"
    FINAL = "FINAL"
    ALL = {QUICK, FINAL}


class ExportLayout:
    FLAT = "FLAT"
    GROUPED = "GROUPED"
    ALL = {FLAT, GROUPED}


class ReviewDecision:
    SELECTED = "SELECTED"
    SKIPPED = "SKIPPED"


JOB_TRANSITIONS: dict[str, set[str]] = {
    JobStatus.CREATING: {JobStatus.READY, JobStatus.BLOCKED, JobStatus.RECOVERY_REQUIRED},
    JobStatus.READY: {JobStatus.RUNNING, JobStatus.PAUSED, JobStatus.BLOCKED, JobStatus.CREATING},
    JobStatus.RUNNING: {
        JobStatus.PAUSING,
        JobStatus.PAUSED,
        JobStatus.BLOCKED,
        JobStatus.RECOVERY_REQUIRED,
        JobStatus.COMPLETED,
    },
    JobStatus.PAUSING: {
        JobStatus.PAUSED,
        JobStatus.BLOCKED,
        JobStatus.RECOVERY_REQUIRED,
        JobStatus.RUNNING,
    },
    JobStatus.PAUSED: {
        JobStatus.RUNNING,
        JobStatus.READY,
        JobStatus.BLOCKED,
        JobStatus.RECOVERY_REQUIRED,
        JobStatus.COMPLETED,
    },
    JobStatus.BLOCKED: {
        JobStatus.PAUSED,
        JobStatus.RUNNING,
        JobStatus.RECOVERY_REQUIRED,
        JobStatus.COMPLETED,
    },
    JobStatus.RECOVERY_REQUIRED: {
        JobStatus.PAUSED,
        JobStatus.RUNNING,
        JobStatus.BLOCKED,
        JobStatus.COMPLETED,
    },
    JobStatus.COMPLETED: {JobStatus.PAUSED, JobStatus.RUNNING, JobStatus.RECOVERY_REQUIRED},
}

DRAW_TRANSITIONS: dict[str, set[str]] = {
    DrawStatus.PENDING: {DrawStatus.CLAIMED, DrawStatus.CANCELLED_BY_USER},
    DrawStatus.CLAIMED: {DrawStatus.DISPATCHING, DrawStatus.PENDING, DrawStatus.FAILED},
    DrawStatus.DISPATCHING: {
        DrawStatus.QUEUED,
        DrawStatus.RUNNING,
        DrawStatus.UNKNOWN,
        DrawStatus.FAILED,
        DrawStatus.INTERRUPTED,
        DrawStatus.VERIFYING,
        DrawStatus.SUCCEEDED,
    },
    DrawStatus.QUEUED: {
        DrawStatus.RUNNING,
        DrawStatus.INTERRUPTED,
        DrawStatus.FAILED,
        DrawStatus.UNKNOWN,
        DrawStatus.VERIFYING,
        DrawStatus.SUCCEEDED,
    },
    DrawStatus.RUNNING: {
        DrawStatus.VERIFYING,
        DrawStatus.FAILED,
        DrawStatus.INTERRUPTED,
        DrawStatus.UNKNOWN,
        DrawStatus.SUCCEEDED,
    },
    DrawStatus.VERIFYING: {
        DrawStatus.SUCCEEDED,
        DrawStatus.FAILED,
        DrawStatus.UNKNOWN,
        DrawStatus.INTERRUPTED,
    },
    DrawStatus.SUCCEEDED: set(),
    DrawStatus.FAILED: {DrawStatus.PENDING, DrawStatus.UNKNOWN},
    DrawStatus.INTERRUPTED: {DrawStatus.PENDING, DrawStatus.UNKNOWN},
    DrawStatus.UNKNOWN: {
        DrawStatus.PENDING,
        DrawStatus.FAILED,
        DrawStatus.SUCCEEDED,
        DrawStatus.INTERRUPTED,
        DrawStatus.VERIFYING,
    },
    DrawStatus.CANCELLED_BY_USER: set(),
}

ATTEMPT_TRANSITIONS: dict[str, set[str]] = {
    AttemptStatus.CLAIMED: {AttemptStatus.DISPATCHING, AttemptStatus.UNKNOWN, AttemptStatus.FAILED},
    AttemptStatus.DISPATCHING: {
        AttemptStatus.QUEUED,
        AttemptStatus.RUNNING,
        AttemptStatus.UNKNOWN,
        AttemptStatus.FAILED,
        AttemptStatus.INTERRUPTED,
        AttemptStatus.VERIFYING,
        AttemptStatus.SUCCEEDED,
    },
    AttemptStatus.QUEUED: {
        AttemptStatus.RUNNING,
        AttemptStatus.INTERRUPTED,
        AttemptStatus.FAILED,
        AttemptStatus.UNKNOWN,
        AttemptStatus.VERIFYING,
        AttemptStatus.SUCCEEDED,
    },
    AttemptStatus.RUNNING: {
        AttemptStatus.VERIFYING,
        AttemptStatus.FAILED,
        AttemptStatus.INTERRUPTED,
        AttemptStatus.UNKNOWN,
        AttemptStatus.SUCCEEDED,
    },
    AttemptStatus.VERIFYING: {
        AttemptStatus.SUCCEEDED,
        AttemptStatus.FAILED,
        AttemptStatus.UNKNOWN,
        AttemptStatus.INTERRUPTED,
    },
    AttemptStatus.SUCCEEDED: set(),
    AttemptStatus.FAILED: set(),
    AttemptStatus.INTERRUPTED: set(),
    AttemptStatus.UNKNOWN: {
        AttemptStatus.SUCCEEDED,
        AttemptStatus.FAILED,
        AttemptStatus.INTERRUPTED,
        AttemptStatus.ABANDONED,
        AttemptStatus.RECONCILING,
        AttemptStatus.VERIFYING,
    },
    AttemptStatus.RECONCILING: {
        AttemptStatus.SUCCEEDED,
        AttemptStatus.FAILED,
        AttemptStatus.INTERRUPTED,
        AttemptStatus.UNKNOWN,
        AttemptStatus.ABANDONED,
    },
    AttemptStatus.ABANDONED: set(),
}


def _check(table: dict[str, set[str]], entity: str, before: str, after: str) -> None:
    allowed = table.get(before)
    if allowed is None or after not in allowed:
        raise GachaError(
            "STATE_CONFLICT",
            f"{entity} 状态不能从 {before} 变为 {after}",
            details={"entity": entity, "from": before, "to": after},
        )


def check_job(before: str, after: str) -> None:
    _check(JOB_TRANSITIONS, "JOB", before, after)


def check_draw(before: str, after: str) -> None:
    _check(DRAW_TRANSITIONS, "DRAW", before, after)


def check_attempt(before: str, after: str) -> None:
    _check(ATTEMPT_TRANSITIONS, "ATTEMPT", before, after)


def is_active_attempt(status: str) -> bool:
    return status in AttemptStatus.ACTIVE


def is_unverified_attempt(status: str) -> bool:
    return status in {
        AttemptStatus.CLAIMED,
        AttemptStatus.DISPATCHING,
        AttemptStatus.QUEUED,
        AttemptStatus.RUNNING,
        AttemptStatus.VERIFYING,
        AttemptStatus.UNKNOWN,
    }
