"""Durable result-review sessions.

The review workspace lets a user keep several results of one source, decide the
whole group ("keep" / "skip") and move on.  Progress must survive a browser
close and a full ComfyUI restart, so the authoritative position lives in
SQLite:

* ``review_sessions`` stores one bounded navigation state per job,
* ``review_session_actions`` stores decision receipts for lost responses,
* a group decision and the new continuation boundary are written in one
  ``BEGIN IMMEDIATE`` transaction.

Only the session/position is durable state.  Nothing here starts generation,
changes the job run intent, or treats "opened/viewed" as a decision.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Mapping, Optional

from .errors import GachaError, conflict, not_found
from .states import ReviewDecision, SourceStatus
from .util import canonical_json, fault_point, sha256_text, utc_now

MAX_HISTORY = 200
MAX_ACTION_ID = 200


def _stable_id(key: str) -> str:
    """Same deterministic id namespace the service uses for review rows."""
    namespace = uuid.uuid5(uuid.NAMESPACE_URL, "gacha-master/ids")
    return str(uuid.uuid5(namespace, key))


def _int_field(body: Mapping[str, Any], name: str, *, minimum: int = 0, default: Any = None) -> int:
    """Accept 0 as a legitimate version; reject missing/booleans/negatives."""
    value = body.get(name, default)
    if value is None:
        raise GachaError("BAD_REQUEST", f"缺少 {name}。", details={name: value})
    if isinstance(value, bool) or not isinstance(value, int):
        raise GachaError("BAD_REQUEST", f"{name} 必须是整数。", details={name: value})
    if value < minimum:
        raise GachaError("BAD_REQUEST", f"{name} 不能小于 {minimum}。", details={name: value})
    return int(value)


class ReviewManager:
    def __init__(self, service: Any) -> None:
        self.service = service
        self.db = service.db
        self.repo = service.repo

    # ------------------------------------------------------------- DTOs
    def _source_payload(self, row: Mapping[str, Any], job: Mapping[str, Any]) -> dict[str, Any]:
        from .service import source_dto

        available = int(row.get("available_count") or 0)
        selected = int(row.get("selected_count") or 0)
        planned = int(row.get("planned_draws") or 0)
        pending = int(row.get("pending_draws") or 0)
        viewed = int(row.get("viewed_count") or 0)
        decision = row.get("review_decision")
        if decision:
            status = "reviewed"
        elif selected > 0:
            status = "selected_pending_review"
        elif viewed > 0:
            status = "viewed_no_selection"
        elif available > 0:
            status = "available_unreviewed"
        else:
            status = "unreviewed"
        summary = {
            "available": available,
            "selected": selected,
            "planned": planned,
            "succeeded": None,
            "pending": pending,
            "viewed": viewed,
        }
        dto = source_dto(dict(row), summary)
        dto["review"] = {
            "status": status,
            "decision": decision,
            "confirmed_at": row.get("review_confirmed_at"),
        }
        dto["image_url"] = f"/sources/{row['id']}/image"
        dto["thumbnail_url"] = f"/sources/{row['id']}/thumbnail?v={row['sha256'][:16]}"
        dto["reviewable"] = available > 0
        return dto

    @staticmethod
    def _session_dto(row: Optional[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
        if not row:
            return None
        try:
            history = json.loads(row.get("history_json") or "[]")
        except (TypeError, ValueError):
            history = []
        if not isinstance(history, list):
            history = []
        current = None
        if row.get("current_source_id"):
            current = {
                "source_id": row["current_source_id"],
                "source_order": row.get("current_source_order"),
                "output_id": row.get("current_output_id"),
                "result_offset": int(row.get("result_offset") or 0),
            }
        confirmed = None
        if row.get("confirmed_source_id"):
            confirmed = {
                "source_id": row["confirmed_source_id"],
                "source_order": row.get("confirmed_source_order"),
                "decision": row.get("confirmed_decision"),
                "result_revision": row.get("confirmed_result_revision"),
                "selection_version": row.get("confirmed_selection_version"),
                "plan_version": row.get("confirmed_plan_version"),
                "confirmed_at": row.get("confirmed_at"),
            }
        return {
            "id": row["id"],
            "job_id": row["job_id"],
            "scope": row["scope"],
            "version": int(row["version"]),
            "break_type": row["break_type"],
            "current": current,
            "confirmed": confirmed,
            "history": [
                {
                    "source_id": item.get("source_id"),
                    "source_order": item.get("source_order"),
                }
                for item in history[-MAX_HISTORY:]
                if isinstance(item, dict) and item.get("source_id")
            ],
            "finished": bool(row.get("finished_at")),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # ------------------------------------------------------- read helpers
    def _plan_version(self, job: Mapping[str, Any]) -> int:
        return int(job["plan_version"])

    def _next_pending(
        self,
        job: Mapping[str, Any],
        after_order: int,
        conn: Optional[sqlite3.Connection] = None,
    ) -> Optional[dict[str, Any]]:
        row = self.repo.review_next_pending(
            job["id"],
            plan_version=self._plan_version(job),
            after_order=int(after_order),
            conn=conn,
        )
        return self._source_payload(row, job) if row else None

    def _first_pending(
        self,
        job: Mapping[str, Any],
        conn: Optional[sqlite3.Connection] = None,
    ) -> Optional[dict[str, Any]]:
        return self._next_pending(job, -1, conn=conn)

    def _resume(
        self,
        job: Mapping[str, Any],
        session: Optional[Mapping[str, Any]],
        summary: Mapping[str, Any],
        conn: Optional[sqlite3.Connection] = None,
    ) -> dict[str, Any]:
        if not session:
            first = self._first_pending(job, conn=conn)
            return {
                "mode": "NONE",
                "reason": "no_session",
                "source": first,
                "next_after_order": (first or {}).get("source_order"),
                "requeue_available": False,
            }
        plan_version = self._plan_version(job)
        pending_before = int(summary.get("pending_sources") or 0)
        after = None
        reason = ""
        mode = "NEXT_PENDING"
        rows: list[dict[str, Any]] = []
        if session.get("break_type") == "AFTER_CONFIRMED":
            after = int(session.get("confirmed_source_order") or 0)
            reason = "after_confirmed"
        else:
            current_id = session.get("current_source_id")
            if current_id:
                rows, _, _ = self.repo.review_source_page(
                    job["id"],
                    plan_version=plan_version,
                    scope="all",
                    source_id=current_id,
                    conn=conn,
                )
            if rows:
                current = rows[0]
                # An explicit current group wins even when it was already
                # confirmed: "back" must reopen it for inspection/redo.  Only
                # AFTER_CONFIRMED (written by the decision transaction) means
                # "this group is done, continue at the stored boundary".
                if int(current.get("available_count") or 0) > 0:
                    return {
                        "mode": "IN_GROUP",
                        "reason": "current_group",
                        "source": self._source_payload(current, job),
                        "next_after_order": current["source_order"],
                        "requeue_available": False,
                    }
                else:
                    after = int(current["source_order"])
                    reason = "current_group_unavailable"
            else:
                # Removed/unknown source: never reuse the array position; use
                # the stored stable order to locate the next pending group.
                after = int(session.get("current_source_order") or 0)
                reason = "current_group_removed"
        if after is None:
            after = 0
        nxt = self._next_pending(job, after, conn=conn)
        if nxt:
            return {
                "mode": "NEXT_PENDING",
                "reason": reason or "next_pending",
                "source": nxt,
                "next_after_order": nxt["source_order"],
                "requeue_available": False,
            }
        # The sweep reached its end.  Groups before the boundary can still be
        # pending when new results invalidated an earlier review; surface that
        # instead of silently looping back to the beginning.
        requeue = pending_before > 0
        return {
            "mode": "FINISHED",
            "reason": reason or "no_pending_after",
            "source": None,
            "requeue_source": self._first_pending(job, conn=conn) if requeue else None,
            "next_after_order": None,
            "requeue_available": requeue,
        }

    def get_session(self, job_id: str) -> dict[str, Any]:
        job = self.service._job_or_404(job_id)
        with self.db.read() as conn:
            session = self.repo.get_review_session(job_id, conn=conn)
            summary = self.repo.review_summary(
                job_id, plan_version=self._plan_version(job), conn=conn
            )
            resume = self._resume(job, session, summary, conn=conn)
        return {
            "session": self._session_dto(session),
            "resume": resume,
            "summary": summary,
            "plan_version": self._plan_version(job),
            "job_id": job_id,
        }

    def list_sources(
        self,
        job_id: str,
        *,
        scope: str = "pending",
        after_order: Optional[int] = None,
        before_order: Optional[int] = None,
        limit: int = 40,
    ) -> dict[str, Any]:
        if scope not in {"pending", "all"}:
            raise GachaError("BAD_REQUEST", "scope 只能是 pending 或 all。")
        job = self.service._job_or_404(job_id)
        plan_version = self._plan_version(job)
        page_size = max(1, min(int(limit), 200))
        with self.db.read() as conn:
            rows, next_after, prev_before = self.repo.review_source_page(
                job_id,
                plan_version=plan_version,
                scope=scope,
                after_order=after_order,
                before_order=before_order,
                limit=page_size,
                conn=conn,
            )
            summary = self.repo.review_summary(
                job_id, plan_version=plan_version, conn=conn
            )
        items = [self._source_payload(row, job) for row in rows]
        return {
            "items": items,
            "next_after_order": next_after,
            "prev_before_order": prev_before,
            "scope": scope,
            "page_size": page_size,
            "summary": summary,
            "plan_version": plan_version,
            "event_seq": self.repo.last_event_seq(),
        }

    def source_state(self, source_id: str) -> dict[str, Any]:
        source = self.repo.get_source(source_id)
        if source is None:
            raise not_found("原图不存在。", source_id=source_id)
        job = self.service._job_or_404(source["job_id"])
        plan_version = self._plan_version(job)
        with self.db.read() as conn:
            rows, _, _ = self.repo.review_source_page(
                job["id"],
                plan_version=plan_version,
                scope="all",
                source_id=source_id,
                conn=conn,
            )
            selected_count, selected_ids = self.repo.review_selected_outputs(
                source_id, limit=200, conn=conn
            )
        if not rows:
            # Still a real source, just not currently reviewable (for example
            # every output is unhealthy).  Report authoritative versions.
            row = self.repo.get_source(source_id)
            assert row is not None
            row = dict(row)
            row.setdefault("available_count", 0)
            row.setdefault("selected_count", selected_count)
            row.setdefault("pending_draws", 0)
            row.setdefault("planned_draws", 0)
            row.setdefault("viewed_count", 0)
            row.setdefault("review_decision", None)
            row.setdefault("review_confirmed_at", None)
            payload = self._source_payload(row, job)
        else:
            payload = self._source_payload(rows[0], job)
        return {
            "source": payload,
            "versions": {
                "result_revision": int(source["result_revision"]),
                "selection_version": int(source["selection_version"]),
                "plan_version": plan_version,
            },
            "available": int(payload["summary"]["available"]),
            "selected": selected_count,
            "selected_output_ids": selected_ids,
            "selected_truncated": selected_count > len(selected_ids),
            "pending_draws": int(payload["summary"]["pending"]),
            "reviewable": int(payload["summary"]["available"]) > 0,
            "job_id": job["id"],
        }

    # ------------------------------------------------------------ open
    def open_session(
        self,
        job_id: str,
        *,
        scope: str = "pending",
        start_source_id: Optional[str] = None,
    ) -> dict[str, Any]:
        if scope not in {"pending", "all"}:
            raise GachaError("BAD_REQUEST", "scope 只能是 pending 或 all。")
        job = self.service._job_or_404(job_id)
        plan_version = self._plan_version(job)
        with self.service.write_lock:
            with self.db.tx() as conn:
                existing = self.repo.get_review_session(job_id, conn=conn)
                if existing is None:
                    start_row = None
                    if start_source_id:
                        rows, _, _ = self.repo.review_source_page(
                            job_id,
                            plan_version=plan_version,
                            scope="all",
                            source_id=start_source_id,
                            conn=conn,
                        )
                        start_row = rows[0] if rows else None
                    if start_row is None:
                        start_row = self.repo.review_next_pending(
                            job_id,
                            plan_version=plan_version,
                            after_order=-1,
                            conn=conn,
                        )
                    now = utc_now()
                    session_id = _stable_id(f"review-session/{job_id}")
                    if start_row is None:
                        conn.execute(
                            """
                            INSERT INTO review_sessions(
                                id, job_id, scope, version, break_type,
                                current_source_id, current_source_order, current_output_id,
                                result_offset, history_json, confirmed_source_order, finished_at,
                                created_at, updated_at)
                            VALUES (?,?,?,1,'AFTER_CONFIRMED',NULL,NULL,NULL,0,'[]',0,?,?,?)
                            ON CONFLICT(job_id) DO NOTHING
                            """,
                            (session_id, job_id, scope, now, now, now),
                        )
                    else:
                        conn.execute(
                            """
                            INSERT INTO review_sessions(
                                id, job_id, scope, version, break_type,
                                current_source_id, current_source_order, current_output_id,
                                result_offset, history_json, created_at, updated_at)
                            VALUES (?,?,?,1,'IN_GROUP',?,?,NULL,0,'[]',?,?)
                            ON CONFLICT(job_id) DO NOTHING
                            """,
                            (
                                session_id,
                                job_id,
                                scope,
                                start_row["id"],
                                int(start_row["source_order"]),
                                now,
                                now,
                            ),
                        )
                    self.repo._event(
                        conn,
                        job_id=job_id,
                        entity_type="REVIEW",
                        entity_id=session_id,
                        action="REVIEW_SESSION_STARTED",
                        actor="user",
                        payload={"scope": scope, "source_order": (
                            int(start_row["source_order"]) if start_row else None
                        )},
                    )
        return self.get_session(job_id)

    # -------------------------------------------------------- position
    def _output_offset(
        self, conn: sqlite3.Connection, source_id: str, output_id: str
    ) -> Optional[int]:
        row = conn.execute(
            """
            SELECT o.id, o.health, o.out_no, d.draw_no, d.status AS draw_status,
                   d.accepted_attempt_id, a.status AS attempt_status, a.id AS attempt_id,
                   COALESCE(ret.state,'RETAINED') AS retention_state
              FROM outputs o JOIN attempts a ON a.id=o.attempt_id
              JOIN draws d ON d.id=a.draw_id
              LEFT JOIN output_retention ret ON ret.output_id=o.id
             WHERE o.id=?
            """,
            (output_id,),
        ).fetchone()
        if row is None or str(row["draw_status"]) != "SUCCEEDED":
            return None
        if str(row["accepted_attempt_id"]) != str(row["attempt_id"]):
            return None
        count = conn.execute(
            """
            SELECT COUNT(*)
              FROM outputs o JOIN attempts a ON a.id=o.attempt_id
              JOIN draws d ON d.id=a.draw_id
             WHERE d.source_id=? AND d.status='SUCCEEDED'
               AND d.accepted_attempt_id=a.id AND a.status='SUCCEEDED'
               AND o.health='VERIFIED'
               AND NOT EXISTS (SELECT 1 FROM output_retention ret
                               WHERE ret.output_id=o.id AND ret.state IN ('CLEANING','PURGED'))
               AND (d.draw_no < ? OR (d.draw_no = ? AND o.out_no < ?))
            """,
            (source_id, int(row["draw_no"]), int(row["draw_no"]), int(row["out_no"])),
        ).fetchone()
        if str(row["retention_state"]) in {"CLEANING", "PURGED"}:
            # A saved bookmark can point at a cleaned OUT.  Locate the same
            # ordinal among remaining results instead of failing or pretending
            # the group became unreviewed.
            return int(count[0] or 0)
        if str(row["attempt_status"]) != "SUCCEEDED" or str(row["health"]) != "VERIFIED":
            return None
        return int(count[0] or 0)

    def save_position(self, session_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        expected_version = _int_field(body, "expected_version", minimum=1)
        scope = body.get("scope")
        if scope is not None and scope not in {"pending", "all"}:
            raise GachaError("BAD_REQUEST", "scope 只能是 pending 或 all。")
        current_source_id = body.get("current_source_id")
        current_output_id = body.get("current_output_id")
        result_offset = body.get("result_offset")
        if result_offset is not None:
            if isinstance(result_offset, bool) or not isinstance(result_offset, int) or result_offset < 0:
                raise GachaError("BAD_REQUEST", "result_offset 必须是非负整数。")
        with self.service.write_lock:
            with self.db.tx() as conn:
                session = self.repo.get_review_session_by_id(session_id, conn=conn)
                if session is None:
                    raise not_found("筛选会话不存在。", session_id=session_id)
                job = self.repo.get_job(session["job_id"])
                if job is None:
                    raise not_found("任务不存在。", job_id=session["job_id"])
                if int(session["version"]) != expected_version:
                    raise conflict(
                        "筛选进度已在其他页面更新，请回读最新位置。",
                        current_session=self._session_dto(session),
                        current_version=int(session["version"]),
                        expected_version=expected_version,
                    )
                new_source_id = session.get("current_source_id")
                new_source_order = session.get("current_source_order")
                history = json.loads(session.get("history_json") or "[]")
                if not isinstance(history, list):
                    history = []
                if current_source_id:
                    # Validate the target on every position write, including a
                    # rewrite of the current source: a removed/unavailable
                    # group must never be persisted as a resumable breakpoint.
                    rows, _, _ = self.repo.review_source_page(
                        job["id"],
                        plan_version=self._plan_version(job),
                        scope="all",
                        source_id=current_source_id,
                        conn=conn,
                    )
                    if not rows:
                        raise conflict(
                            "目标原图没有可用结果或已被移除，请回读待筛列表。",
                            source_id=current_source_id,
                        )
                    row = rows[0]
                    if current_source_id != session.get("current_source_id"):
                        if session.get("current_source_id"):
                            history.append(
                                {
                                    "source_id": session["current_source_id"],
                                    "source_order": session.get("current_source_order"),
                                }
                            )
                            history = history[-MAX_HISTORY:]
                        if current_output_id is None:
                            result_offset = 0 if result_offset is None else result_offset
                        new_source_id = row["id"]
                        new_source_order = int(row["source_order"])
                offset = int(result_offset or 0)
                if current_output_id:
                    if not new_source_id:
                        raise GachaError(
                            "BAD_REQUEST", "保存结果位置前必须先指定原图。"
                        )
                    computed = self._output_offset(conn, new_source_id, current_output_id)
                    if computed is None:
                        raise conflict(
                            "当前结果已不可用，请回读本组结果后继续。",
                            output_id=current_output_id,
                        )
                    if result_offset is None:
                        offset = computed
                now = utc_now()
                conn.execute(
                    """
                    UPDATE review_sessions
                       SET version=version+1, scope=COALESCE(?, scope),
                           break_type='IN_GROUP', current_source_id=?, current_source_order=?,
                           current_output_id=?, result_offset=?, history_json=?,
                           finished_at=NULL, updated_at=?
                     WHERE id=? AND version=?
                    """,
                    (
                        scope,
                        new_source_id,
                        new_source_order,
                        current_output_id or None,
                        offset,
                        json.dumps(history, ensure_ascii=False, sort_keys=True),
                        now,
                        session_id,
                        expected_version,
                    ),
                )
                self.repo._event(
                    conn,
                    job_id=job["id"],
                    entity_type="REVIEW",
                    entity_id=session_id,
                    action="REVIEW_POSITION_SAVED",
                    actor="user",
                    payload={
                        "source_order": new_source_order,
                        "output_id": current_output_id or None,
                        "result_offset": offset,
                    },
                )
        result = self.get_session(session["job_id"])
        result["idempotent"] = False
        return result

    # -------------------------------------------------------- decision
    def _decision_response(
        self,
        conn: sqlite3.Connection,
        *,
        job: Mapping[str, Any],
        session: Mapping[str, Any],
        source_id: str,
        decision: str,
        selected_count: int,
        selected_ids: list[str],
        idempotent: bool,
    ) -> dict[str, Any]:
        review = conn.execute(
            "SELECT * FROM source_reviews WHERE source_id=? AND result_revision=? "
            "AND selection_version=? AND plan_version=?",
            (
                source_id,
                int(session["confirmed_result_revision"]),
                int(session["confirmed_selection_version"]),
                int(session["confirmed_plan_version"]),
            ),
        ).fetchone()
        summary = self.repo.review_summary(
            job["id"], plan_version=self._plan_version(job), conn=conn
        )
        nxt = self._next_pending(job, int(session["confirmed_source_order"] or 0), conn=conn)
        return {
            "session": self._session_dto(session),
            "review": {
                "source_id": source_id,
                "decision": decision,
                "result_revision": int(session["confirmed_result_revision"]),
                "selection_version": int(session["confirmed_selection_version"]),
                "plan_version": int(session["confirmed_plan_version"]),
                "confirmed_at": review["confirmed_at"] if review else session.get("confirmed_at"),
                "selected_count": selected_count,
                "selected_output_ids": selected_ids,
            },
            "next": {
                "mode": "NEXT_PENDING" if nxt else "FINISHED",
                "source": nxt,
            },
            "summary": summary,
            "idempotent": idempotent,
        }

    def decide_from_body(
        self, session_id: str, body: Mapping[str, Any], action_id: str
    ) -> dict[str, Any]:
        """Validate the complete version envelope before touching state.

        Every expected version is required.  ``0`` is a legitimate business
        version (a source with no results/selections yet); a missing field is
        rejected instead of being silently treated as 0.
        """
        source_id = body.get("source_id")
        if not isinstance(source_id, str) or not source_id:
            raise GachaError("BAD_REQUEST", "缺少 source_id。")
        decision = str(body.get("decision") or "")
        expected_session_version = _int_field(body, "expected_session_version", minimum=1)
        expected_result_revision = _int_field(body, "expected_result_revision")
        expected_selection_version = _int_field(body, "expected_selection_version")
        expected_plan_version = _int_field(body, "expected_plan_version")
        reason = body.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise GachaError("BAD_REQUEST", "reason 必须是字符串。")
        request_hash = sha256_text(
            canonical_json(
                {
                    "source_id": source_id,
                    "decision": decision,
                    "expected_session_version": expected_session_version,
                    "expected_result_revision": expected_result_revision,
                    "expected_selection_version": expected_selection_version,
                    "expected_plan_version": expected_plan_version,
                    "reason": reason,
                }
            )
        )
        return self.decide(
            session_id,
            source_id=source_id,
            decision=decision,
            expected_session_version=expected_session_version,
            expected_result_revision=expected_result_revision,
            expected_selection_version=expected_selection_version,
            expected_plan_version=expected_plan_version,
            action_id=action_id,
            request_hash=request_hash,
            reason=reason,
        )

    def decide(
        self,
        session_id: str,
        *,
        source_id: str,
        decision: str,
        expected_session_version: int,
        expected_result_revision: int,
        expected_selection_version: int,
        expected_plan_version: int,
        action_id: str,
        request_hash: str,
        reason: Optional[str] = None,
    ) -> dict[str, Any]:
        if decision not in {ReviewDecision.SELECTED, ReviewDecision.SKIPPED}:
            raise GachaError("BAD_REQUEST", "审核决定无效。")
        if not isinstance(action_id, str) or not action_id.strip():
            raise GachaError("BAD_REQUEST", "缺少动作 ID（Idempotency-Key）。")
        if len(action_id) > MAX_ACTION_ID:
            raise GachaError("BAD_REQUEST", "动作 ID 过长。")
        action_id = action_id.strip()
        if not source_id:
            raise GachaError("BAD_REQUEST", "缺少 source_id。")
        existing = self.repo.get_review_action(session_id, action_id)
        if existing is not None:
            payload = json.loads(existing["response_json"])
            if existing["request_hash"] != request_hash:
                raise GachaError(
                    "IDEMPOTENCY_CONFLICT",
                    "相同动作 ID 用于了不同的筛选决定。",
                    details={"session_id": session_id, "action_id": action_id},
                )
            payload["idempotent"] = True
            return payload
        with self.service.write_lock:
            with self.db.tx() as conn:
                session = self.repo.get_review_session_by_id(session_id, conn=conn)
                if session is None:
                    raise not_found("筛选会话不存在。", session_id=session_id)
                job = self.repo.get_job(session["job_id"])
                if job is None:
                    raise not_found("任务不存在。", job_id=session["job_id"])
                source = self.repo.get_source_for_update(conn, source_id)
                if source is None or str(source["job_id"]) != str(job["id"]):
                    raise not_found("原图不存在于该任务。", source_id=source_id)
                if str(source["status"]) != SourceStatus.ACTIVE:
                    raise conflict("原图已被移除，不能确认本组。", source_id=source_id)
                # A duplicate can arrive after the first request committed but
                # before its response was stored.  Receipts are checked again
                # under the write lock so exactly one transaction advances.
                receipt = self.repo.get_review_action(session_id, action_id, conn=conn)
                if receipt is not None:
                    payload = json.loads(receipt["response_json"])
                    if receipt["request_hash"] != request_hash:
                        raise GachaError(
                            "IDEMPOTENCY_CONFLICT",
                            "相同动作 ID 用于了不同的筛选决定。",
                            details={"session_id": session_id, "action_id": action_id},
                        )
                    payload["idempotent"] = True
                    return payload
                if int(session["version"]) != int(expected_session_version):
                    # Same group/versions/decision already committed: a retry
                    # without the same action id must not advance again.
                    already = conn.execute(
                        """
                        SELECT 1 FROM source_reviews
                         WHERE source_id=? AND result_revision=? AND selection_version=?
                           AND plan_version=? AND decision=?
                        """,
                        (
                            source_id,
                            int(expected_result_revision),
                            int(expected_selection_version),
                            int(expected_plan_version),
                            decision,
                        ),
                    ).fetchone()
                    if (
                        already
                        and session.get("confirmed_source_id") == source_id
                        and session.get("confirmed_decision") == decision
                        and session.get("break_type") == "AFTER_CONFIRMED"
                    ):
                        count, ids = self.repo.review_selected_outputs(
                            source_id, conn=conn
                        )
                        payload = self._decision_response(
                            conn,
                            job=job,
                            session=session,
                            source_id=source_id,
                            decision=decision,
                            selected_count=count,
                            selected_ids=ids,
                            idempotent=True,
                        )
                        self._store_receipt(
                            conn,
                            session_id=session_id,
                            job_id=job["id"],
                            source_id=source_id,
                            decision=decision,
                            action_id=action_id,
                            request_hash=request_hash,
                            payload=payload,
                        )
                        return payload
                    raise conflict(
                        "筛选会话版本已变化，请回读最新进度后再决定。",
                        current_session=self._session_dto(session),
                        current_version=int(session["version"]),
                        expected_version=int(expected_session_version),
                        current_summary=self.repo.review_summary(
                            job["id"], plan_version=self._plan_version(job), conn=conn
                        ),
                    )
                if int(source["result_revision"]) != int(expected_result_revision):
                    raise conflict(
                        "本组结果已更新，请回读后重新决定。",
                        current_result_revision=int(source["result_revision"]),
                        expected_result_revision=int(expected_result_revision),
                    )
                if int(source["selection_version"]) != int(expected_selection_version):
                    raise conflict(
                        "本组入选状态已变化，请回读后重新决定。",
                        current_selection_version=int(source["selection_version"]),
                        expected_selection_version=int(expected_selection_version),
                    )
                if int(job["plan_version"]) != int(expected_plan_version):
                    raise conflict(
                        "任务计划版本已变化，请回读后重新决定。",
                        current_plan_version=int(job["plan_version"]),
                        expected_plan_version=int(expected_plan_version),
                    )
                available_count = conn.execute(
                    """
                    SELECT COUNT(*)
                      FROM draws d JOIN attempts a ON a.id=d.accepted_attempt_id
                      JOIN outputs o ON o.attempt_id=a.id
                     WHERE d.source_id=? AND d.status='SUCCEEDED'
                       AND a.status='SUCCEEDED' AND o.health='VERIFIED'
                       AND NOT EXISTS (SELECT 1 FROM output_retention ret
                                       WHERE ret.output_id=o.id AND ret.state IN ('CLEANING','PURGED'))
                    """,
                    (source_id,),
                ).fetchone()[0]
                if int(available_count or 0) == 0:
                    raise GachaError(
                        "STATE_CONFLICT",
                        "本组当前没有可用结果，不能作为组决定；它不进入普通筛选队列。",
                        details={"source_id": source_id},
                    )
                selected_count, selected_ids = self.repo.review_selected_outputs(
                    source_id, conn=conn
                )
                # Let an in-flight selection commit before reading the count.
                if decision == ReviewDecision.SELECTED and selected_count == 0:
                    raise GachaError(
                        "STATE_CONFLICT",
                        "本组没有已保存的入选项，不能“保留并下一张”；请先入选或跳过。",
                        details={"source_id": source_id, "selected": selected_count},
                    )
                if decision == ReviewDecision.SKIPPED and selected_count > 0:
                    raise GachaError(
                        "STATE_CONFLICT",
                        f"本组已选 {selected_count} 张；取消入选后可跳过，或选择保留并继续。",
                        details={"source_id": source_id, "selected": selected_count},
                    )
                now = utc_now()
                review_id = _stable_id(
                    f"review/{source_id}/{int(source['result_revision'])}/"
                    f"{int(source['selection_version'])}/{int(job['plan_version'])}"
                )
                conn.execute(
                    """
                    INSERT INTO source_reviews(
                        id, source_id, result_revision, selection_version, plan_version,
                        decision, reason, confirmed_at)
                    VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(source_id, result_revision, selection_version, plan_version)
                    DO UPDATE SET decision=excluded.decision, reason=excluded.reason,
                                  confirmed_at=excluded.confirmed_at
                    """,
                    (
                        review_id,
                        source_id,
                        int(source["result_revision"]),
                        int(source["selection_version"]),
                        int(job["plan_version"]),
                        decision,
                        reason,
                        now,
                    ),
                )
                # Confirming a group the session was not pointing at is still a
                # navigation; keep the previous position so "上一张原图" works
                # even if the client skipped its position write.
                history = json.loads(session.get("history_json") or "[]")
                if not isinstance(history, list):
                    history = []
                if session.get("current_source_id") and session["current_source_id"] != source_id:
                    history.append(
                        {
                            "source_id": session["current_source_id"],
                            "source_order": session.get("current_source_order"),
                        }
                    )
                    history = history[-MAX_HISTORY:]
                fault_point("R01")
                # "Finished" means no further pending group exists after this
                # boundary, not merely "this group was decided".  Otherwise a
                # restart after the last visible group would look completed.
                has_next = self.repo.review_next_pending(
                    job["id"],
                    plan_version=int(job["plan_version"]),
                    after_order=int(source["source_order"]),
                    conn=conn,
                ) is not None
                conn.execute(
                    """
                    UPDATE review_sessions
                       SET version=version+1, break_type='AFTER_CONFIRMED',
                           current_source_id=?, current_source_order=?,
                           current_output_id=NULL, result_offset=0, history_json=?,
                           confirmed_source_id=?, confirmed_source_order=?,
                           confirmed_decision=?, confirmed_result_revision=?,
                           confirmed_selection_version=?, confirmed_plan_version=?,
                           confirmed_at=?, finished_at=?, updated_at=?
                     WHERE id=? AND version=?
                    """,
                    (
                        source_id,
                        int(source["source_order"]),
                        json.dumps(history, ensure_ascii=False, sort_keys=True),
                        source_id,
                        int(source["source_order"]),
                        decision,
                        int(source["result_revision"]),
                        int(source["selection_version"]),
                        int(job["plan_version"]),
                        now,
                        None if has_next else now,
                        now,
                        session_id,
                        int(expected_session_version),
                    ),
                )
                updated = self.repo.get_review_session_by_id(session_id, conn=conn)
                assert updated is not None
                payload = self._decision_response(
                    conn,
                    job=job,
                    session=updated,
                    source_id=source_id,
                    decision=decision,
                    selected_count=selected_count,
                    selected_ids=selected_ids,
                    idempotent=False,
                )
                self._store_receipt(
                    conn,
                    session_id=session_id,
                    job_id=job["id"],
                    source_id=source_id,
                    decision=decision,
                    action_id=action_id,
                    request_hash=request_hash,
                    payload=payload,
                )
                self.repo._event(
                    conn,
                    job_id=job["id"],
                    entity_type="REVIEW",
                    entity_id=session_id,
                    action="REVIEW_GROUP_CONFIRMED",
                    from_state="IN_GROUP",
                    to_state="AFTER_CONFIRMED",
                    related_id=source_id,
                    actor="user",
                    payload={
                        "source_order": int(source["source_order"]),
                        "decision": decision,
                        "result_revision": int(source["result_revision"]),
                        "selection_version": int(source["selection_version"]),
                        "plan_version": int(job["plan_version"]),
                        "selected_count": selected_count,
                        "selected_output_ids": selected_ids,
                    },
                )
        fault_point("R02")
        return payload

    def _store_receipt(
        self,
        conn: sqlite3.Connection,
        *,
        session_id: str,
        job_id: str,
        source_id: str,
        decision: str,
        action_id: str,
        request_hash: str,
        payload: Mapping[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO review_session_actions(
                id, session_id, job_id, action_id, kind, source_id, decision,
                request_hash, response_json, created_at)
            VALUES (?,?,?,?,'DECISION',?,?,?,?,?)
            ON CONFLICT(session_id, action_id) DO NOTHING
            """,
            (
                _stable_id(f"review-action/{session_id}/{action_id}"),
                session_id,
                job_id,
                action_id,
                source_id,
                decision,
                request_hash,
                json.dumps(dict(payload), ensure_ascii=False, sort_keys=True),
                utc_now(),
            ),
        )

    # ------------------------------------------------------------- back
    def back(self, session_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        expected_version = _int_field(body, "expected_version", minimum=1)
        with self.service.write_lock:
            with self.db.tx() as conn:
                session = self.repo.get_review_session_by_id(session_id, conn=conn)
                if session is None:
                    raise not_found("筛选会话不存在。", session_id=session_id)
                job = self.repo.get_job(session["job_id"])
                if job is None:
                    raise not_found("任务不存在。", job_id=session["job_id"])
                if int(session["version"]) != expected_version:
                    raise conflict(
                        "筛选进度已在其他页面更新，请回读最新位置。",
                        current_session=self._session_dto(session),
                        current_version=int(session["version"]),
                        expected_version=expected_version,
                    )
                history = json.loads(session.get("history_json") or "[]")
                if not isinstance(history, list):
                    history = []
                target = None
                while history:
                    candidate = history.pop()
                    if not isinstance(candidate, dict) or not candidate.get("source_id"):
                        continue
                    rows, _, _ = self.repo.review_source_page(
                        job["id"],
                        plan_version=self._plan_version(job),
                        scope="all",
                        source_id=candidate["source_id"],
                        conn=conn,
                    )
                    if rows and int(rows[0].get("available_count") or 0) > 0:
                        target = rows[0]
                        break
                if target is None:
                    raise GachaError(
                        "STATE_CONFLICT",
                        "没有可返回的上一组（历史为空或原图已不可用）。",
                        details={"session_id": session_id},
                    )
                now = utc_now()
                conn.execute(
                    """
                    UPDATE review_sessions
                       SET version=version+1, break_type='IN_GROUP',
                           current_source_id=?, current_source_order=?,
                           current_output_id=NULL, result_offset=0,
                           history_json=?, finished_at=NULL, updated_at=?
                     WHERE id=? AND version=?
                    """,
                    (
                        target["id"],
                        int(target["source_order"]),
                        json.dumps(history[-MAX_HISTORY:], ensure_ascii=False, sort_keys=True),
                        now,
                        session_id,
                        expected_version,
                    ),
                )
                self.repo._event(
                    conn,
                    job_id=job["id"],
                    entity_type="REVIEW",
                    entity_id=session_id,
                    action="REVIEW_BACK",
                    actor="user",
                    payload={"source_order": int(target["source_order"])},
                )
        result = self.get_session(job["id"])
        result["idempotent"] = False
        return result

    # --------------------------------------------------------- helpers
    @staticmethod
    def request_hash(payload: Mapping[str, Any]) -> str:
        return sha256_text(canonical_json(dict(payload)))
