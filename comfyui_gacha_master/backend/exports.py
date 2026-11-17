"""Temporary/final exports: drafts, reviews, freezing, copying and resume.

Exports copy frozen OUT files; they never move, re-encode or hard-link source
results.  A frozen export never gains or loses members when new results arrive.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import uuid
from typing import Any, Iterable, Mapping, Optional

from . import storage
from .errors import GachaError, conflict, not_found
from .states import (
    ExportItemStatus,
    ExportKind,
    ExportLayout,
    ExportStatus,
    JobStatus,
    OutputHealth,
    ReviewDecision,
)
from .util import (
    atomic_write_json,
    copy_file_verified,
    ensure_dir,
    fault_point,
    json_text,
    path_is_within,
    publish_json_once,
    publish_noreplace,
    sha256_file,
    utc_now,
    write_temp_file,
)


def _export_item_name(source_order: int, draw_no: int, attempt_no: int, out_no: int, layout: str) -> str:
    base = (
        f"SRC-{int(source_order):04d}_DRAW-{int(draw_no):06d}_"
        f"ATTEMPT-{int(attempt_no):03d}_OUT-{int(out_no):04d}.png"
    )
    if layout == ExportLayout.GROUPED:
        return f"SRC-{int(source_order):04d}/{base.split('_', 1)[1]}"
    return base


class ExportManager:
    def __init__(self, *, db: Any, repo: Any, settings: Any, paths: Any, backup_manager: Any = None) -> None:
        self.db = db
        self.repo = repo
        self.settings = settings
        self.paths = paths
        self.backup_manager = backup_manager
        self._copy_lock = threading.Lock()
        self._copying: set[str] = set()

    # ----------------------------------------------------------- preview
    def preview(
        self,
        job_id: str,
        *,
        kind: str,
        layout: Optional[str] = None,
        target_root: Optional[str] = None,
        draft_id: Optional[str] = None,
    ) -> dict[str, Any]:
        kind = kind.upper()
        if kind not in ExportKind.ALL:
            raise GachaError("BAD_REQUEST", "导出类型只能是 QUICK 或 FINAL。")
        layout = (layout or self.settings.export_layout).upper()
        if layout not in ExportLayout.ALL:
            raise GachaError("BAD_REQUEST", "布局只能是 FLAT 或 GROUPED。")
        job = self.repo.get_job(job_id)
        if job is None:
            raise not_found("任务不存在。", job_id=job_id)
        self._assert_no_purged_selected(job_id)
        target = self._validate_target(target_root)
        snapshot = self._snapshot(job)
        sources = self.repo.all_sources(job_id)
        preview_sources = []
        total_selected = 0
        for source in sources:
            selected = self._selected_outputs(job_id, source["id"])
            total_selected += len(selected)
            summary = self.repo.source_summary(source["id"])
            preview_sources.append(
                {
                    "source_id": source["id"],
                    "source_order": source["source_order"],
                    "original_name": source["original_name"],
                    "selected_output_ids": [item["output_id"] for item in selected],
                    "selected_count": len(selected),
                    "available": summary.get("available", 0),
                    "pending": summary.get("pending", 0),
                    "failed": summary.get("failed", 0),
                    "unknown": summary.get("unknown", 0),
                    "result_revision": source["result_revision"],
                    "selection_version": source["selection_version"],
                }
            )
        if kind == ExportKind.QUICK and total_selected == 0:
            raise GachaError("EXPORT_REVIEW_REQUIRED", "尚未入选任何健康结果，不能创建临时导出。")
        export_id = draft_id or str(uuid.uuid4())
        existing = self.repo_export(export_id)
        if existing is not None:
            return self.get_export(export_id)
        draft = {
            "schema_version": 1,
            "kind": kind,
            "layout": layout,
            "target_root": target,
            "job_id": job_id,
            "job_display_id": job["display_id"],
            "snapshot": snapshot,
            "sources": preview_sources,
            "total_selected": total_selected,
            "created_at": utc_now(),
        }
        now = utc_now()
        with self.db.tx() as conn:
            conn.execute(
                """
                INSERT INTO exports(id, job_id, kind, layout, status, target_root, draft_version, row_version,
                                    preview_json, cutoff_summary_json, created_at, updated_at)
                VALUES (?,?,?,?,'DRAFT',?,1,1,?,?,?,?)
                """,
                (
                    export_id,
                    job_id,
                    kind,
                    layout,
                    target,
                    json_text(draft),
                    json_text(
                        {
                            "plan_version": snapshot["plan_version"],
                            "last_event_seq": snapshot["last_event_seq"],
                            "pending_draws": snapshot["pending_draws"],
                            "unknown_draws": snapshot["unknown_draws"],
                        }
                    ),
                    now,
                    now,
                ),
            )
        return self.get_export(export_id)

    def _validate_target(self, target_root: Optional[str]) -> str:
        if not target_root:
            raise GachaError("BAD_REQUEST", "必须选择导出目标目录。")
        absolute = os.path.abspath(target_root)
        if not os.path.isdir(absolute):
            raise GachaError("PATH_NOT_ALLOWED", "导出目标目录不存在。", details={"root": absolute})
        allowed = [os.path.abspath(root) for root in self.settings.allowed_export_roots if root]
        if not any(path_is_within(root, absolute) for root in allowed):
            raise GachaError(
                "PATH_NOT_ALLOWED",
                "目标目录不在已登记的导出根内。",
                details={"root": absolute, "allowed": allowed},
            )
        if path_is_within(self.paths.data_root, absolute) or path_is_within(self.paths.results_root, absolute):
            raise GachaError("PATH_NOT_ALLOWED", "导出目标不能位于插件数据/结果目录内。")
        return absolute

    def _snapshot(self, job: Mapping[str, Any]) -> dict[str, Any]:
        summary = self.repo.job_summary(job["id"])
        return {
            "plan_version": job["plan_version"],
            "last_event_seq": self.repo.last_event_seq(),
            "pending_draws": summary.get("pending", 0),
            "unknown_draws": summary.get("unknown", 0),
            "inflight_draws": summary.get("inflight", 0),
            "succeeded": summary.get("succeeded", 0),
        }

    def _selected_outputs(self, job_id: str, source_id: str) -> list[dict[str, Any]]:
        rows = self.db.query_all(
            """
            SELECT o.id AS output_id, o.sha256, o.size, o.rel_path, o.health, o.out_no,
                   d.draw_no, a.attempt_no, s.source_order
            FROM selections sel
            JOIN outputs o ON o.id=sel.output_id
            JOIN attempts a ON a.id=o.attempt_id
            JOIN draws d ON d.id=a.draw_id
            JOIN sources s ON s.id=d.source_id
            WHERE s.job_id=? AND s.id=? AND sel.selected=1
              AND o.health='VERIFIED'
              AND d.status='SUCCEEDED' AND d.accepted_attempt_id=a.id
              AND a.status='SUCCEEDED'
              AND NOT EXISTS (SELECT 1 FROM output_retention ret
                              WHERE ret.output_id=o.id AND ret.state IN ('CLEANING','PURGED'))
            ORDER BY d.draw_no, o.out_no
            """,
            (job_id, source_id),
        )
        return [dict(row) for row in rows]

    def _assert_no_purged_selected(self, job_id: str) -> None:
        """A stale selection from an older database must block export."""
        row = self.db.query_one(
            """
            SELECT sel.output_id, COALESCE(ret.state,'RETAINED') AS retention_state
              FROM selections sel
              JOIN outputs o ON o.id=sel.output_id
              JOIN attempts a ON a.id=o.attempt_id
              JOIN draws d ON d.id=a.draw_id
              LEFT JOIN output_retention ret ON ret.output_id=o.id
             WHERE d.job_id=? AND sel.selected=1
               AND COALESCE(ret.state,'RETAINED') IN ('CLEANING','PURGED')
             ORDER BY sel.output_id LIMIT 1
            """,
            (job_id,),
        )
        if row is not None:
            raise GachaError(
                "OUTPUT_PURGED",
                "入选记录对应的图片已被永久清理；请先取消该失效入选，再创建或继续导出。",
                details={
                    "job_id": job_id,
                    "output_id": row["output_id"],
                    "retention_state": row["retention_state"],
                },
            )

    def _assert_maintenance_free(self, job_id: str) -> None:
        # Generation/export delivery is serialized with any result cleanup in
        # this instance, not only a cleanup of the same job.
        self.repo.assert_maintenance_free()

    # ---------------------------------------------------------- reviews
    def put_review(
        self,
        export_id: str,
        source_id: str,
        *,
        decision: str,
        expected_result_revision: Optional[int] = None,
        expected_selection_version: Optional[int] = None,
        selected_output_ids: Optional[list[str]] = None,
        reason: Optional[str] = None,
    ) -> dict[str, Any]:
        decision = decision.upper()
        if decision not in {ReviewDecision.SELECTED, ReviewDecision.SKIPPED}:
            raise GachaError("BAD_REQUEST", "审核决定无效。")
        export = self._draft_export(export_id)
        job = self.repo.get_job(export["job_id"])
        source = self.repo.get_source(source_id)
        if job is None or source is None or source["job_id"] != job["id"]:
            raise not_found("原图不属于该导出任务。", source_id=source_id)
        if expected_result_revision is not None and int(expected_result_revision) != int(source["result_revision"]):
            raise conflict(
                "原图结果版本已变化，请刷新审核面板。",
                current_result_revision=source["result_revision"],
            )
        if expected_selection_version is not None and int(expected_selection_version) != int(source["selection_version"]):
            raise conflict(
                "原图选择版本已变化，请刷新审核面板。",
                current_selection_version=source["selection_version"],
            )
        current_selected = [item["output_id"] for item in self._selected_outputs(job["id"], source_id)]
        if decision == ReviewDecision.SELECTED:
            if not current_selected:
                raise GachaError("EXPORT_REVIEW_REQUIRED", "该原图没有健康入选项，只能明确跳过。")
            if selected_output_ids is None:
                selected_output_ids = current_selected
            if set(selected_output_ids) != set(current_selected):
                raise conflict(
                    "审核的入选集合与当前选择不一致，请刷新后重新确认。",
                    current=current_selected,
                    reviewed=selected_output_ids,
                )
        else:
            selected_output_ids = []
        summary = self.repo.source_summary(source_id)
        now = utc_now()
        with self.db.tx() as conn:
            conn.execute(
                """
                INSERT INTO export_reviews(export_id, source_id, decision, selection_version, result_revision,
                                           plan_version, selected_output_ids_json, pending_draws, reason, confirmed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(export_id, source_id) DO UPDATE SET
                    decision=excluded.decision,
                    selection_version=excluded.selection_version,
                    result_revision=excluded.result_revision,
                    plan_version=excluded.plan_version,
                    selected_output_ids_json=excluded.selected_output_ids_json,
                    pending_draws=excluded.pending_draws,
                    reason=excluded.reason,
                    confirmed_at=excluded.confirmed_at
                """,
                (
                    export_id,
                    source_id,
                    decision,
                    int(source["selection_version"]),
                    int(source["result_revision"]),
                    int(job["plan_version"]),
                    json.dumps(selected_output_ids),
                    int(summary.get("pending", 0) + summary.get("inflight", 0) + summary.get("unknown", 0)),
                    reason,
                    now,
                ),
            )
            conn.execute(
                "UPDATE exports SET draft_version=draft_version+1, row_version=row_version+1, updated_at=? WHERE id=?",
                (now, export_id),
            )
        return self.get_export(export_id)

    def _draft_export(self, export_id: str) -> dict[str, Any]:
        export = self.repo_export(export_id)
        if export is None:
            raise not_found("导出草稿不存在。", export_id=export_id)
        if export["status"] != ExportStatus.DRAFT:
            raise GachaError("STATE_CONFLICT", "只能修改未冻结的导出草稿。", status=export["status"])
        return export

    def repo_export(self, export_id: str) -> Optional[dict[str, Any]]:
        row = self.db.query_one("SELECT * FROM exports WHERE id=?", (export_id,))
        if row is None:
            return None
        export = dict(row)
        export["preview"] = json.loads(export.get("preview_json") or "{}")
        export["cutoff_summary"] = json.loads(export.get("cutoff_summary_json") or "{}")
        return export

    # ------------------------------------------------------------ freeze
    def confirm(
        self,
        export_id: str,
        *,
        expected_draft_version: Optional[int] = None,
        expected_cutoff_event_id: Optional[int] = None,
        layout: Optional[str] = None,
        target_root: Optional[str] = None,
        confirm_cutoff: bool = False,
        confirm_zero: bool = False,
    ) -> dict[str, Any]:
        with self._copy_lock:
            export = self.repo_export(export_id)
            if export is None:
                raise not_found("导出草稿不存在。", export_id=export_id)
            if export["status"] != ExportStatus.DRAFT:
                return self.get_export(export_id)  # already frozen; idempotent view
            if expected_draft_version is not None and int(expected_draft_version) != int(export["draft_version"]):
                raise conflict(
                    "导出草稿版本已变化，请刷新。",
                    current_draft_version=export["draft_version"],
                    expected=expected_draft_version,
                )
            if expected_cutoff_event_id is not None and int(expected_cutoff_event_id) != int(
                export.get("cutoff_summary", {}).get("last_event_seq", -1)
            ):
                raise conflict("导出截止版本已变化，请刷新。", current=export["cutoff_summary"].get("last_event_seq"))
            job = self.repo.get_job(export["job_id"])
            if job is None:
                raise not_found("任务不存在。", job_id=export["job_id"])
            self._assert_maintenance_free(job["id"])
            self._assert_no_purged_selected(job["id"])
            layout = (layout or export["layout"]).upper()
            if layout not in ExportLayout.ALL:
                raise GachaError("BAD_REQUEST", "布局无效。")
            target = self._validate_target(target_root or export["target_root"])
            sources = self.repo.all_sources(job["id"])
            reviews = {
                row["source_id"]: dict(row)
                for row in self.db.query_all("SELECT * FROM export_reviews WHERE export_id=?", (export_id,))
            }
            self._assert_versions_unchanged(export, job, sources, reviews)
            items: list[dict[str, Any]] = []
            skipped: list[dict[str, Any]] = []
            if export["kind"] == ExportKind.FINAL:
                for source in sources:
                    review = reviews.get(source["id"])
                    if review is None:
                        raise GachaError(
                            "EXPORT_REVIEW_REQUIRED",
                            "仍有原图未审核或未明确跳过，不能冻结正式导出。",
                            details={"source_id": source["id"], "source_order": source["source_order"]},
                        )
                    if review["decision"] == ReviewDecision.SKIPPED:
                        skipped.append({"source_id": source["id"], "source_order": source["source_order"]})
                        continue
                    frozen_ids = json.loads(review["selected_output_ids_json"] or "[]")
                    current = self._selected_outputs(job["id"], source["id"])
                    current_ids = [item["output_id"] for item in current]
                    if set(frozen_ids) != set(current_ids):
                        raise conflict(
                            "审核后的选择发生变化，请刷新审核面板。",
                            source_id=source["id"],
                            current=current_ids,
                            reviewed=frozen_ids,
                        )
                    items.extend(current)
            else:
                for source in sources:
                    items.extend(self._selected_outputs(job["id"], source["id"]))
            if not items and export["kind"] == ExportKind.FINAL:
                if not confirm_zero:
                    raise GachaError(
                        "EXPORT_REVIEW_REQUIRED",
                        "正式导出将包含 0 张图片；请二次确认“仅审核清单”。",
                        details={"confirm_zero_required": True, "skipped": skipped},
                    )
            summary = self.repo.job_summary(job["id"])
            if (summary.get("pending", 0) or summary.get("inflight", 0) or summary.get("unknown", 0)) and not confirm_cutoff:
                raise GachaError(
                    "EXPORT_REVIEW_REQUIRED",
                    "任务仍有未完成 DRAW；请确认本次只导出截止版本。",
                    details={
                        "pending": summary.get("pending", 0),
                        "inflight": summary.get("inflight", 0),
                        "unknown": summary.get("unknown", 0),
                        "confirm_cutoff_required": True,
                    },
                )
            for item in items:
                item["target_rel_path"] = _export_item_name(
                    int(item["source_order"]),
                    int(item["draw_no"]),
                    int(item["attempt_no"]),
                    int(item["out_no"]),
                    layout,
                )
            if export["kind"] == ExportKind.FINAL and self.backup_manager is not None:
                # A final freeze is a checkpoint: if this consistent backup
                # cannot be made, do not freeze the review.
                self.backup_manager.create_backup(reason="final_export_freeze")
            # Exclusive batch directory.
            stamp = utc_now()[:10].replace("-", "")
            prefix = "QUICK" if export["kind"] == ExportKind.QUICK else "FINAL"
            batch_name = f"{prefix}-{stamp}-{export_id.replace('-', '')[:8]}"
            batch_dir = os.path.join(target, batch_name)
            try:
                os.makedirs(batch_dir, exist_ok=False)
            except FileExistsError:
                raise GachaError(
                    "EXPORT_FILE_CONFLICT",
                    "导出批次目录已存在，拒绝复用。",
                    details={"path": batch_dir},
                )
            now = utc_now()
            with self.db.tx() as conn:
                # Re-check the shared boundary inside the freeze transaction:
                # the earlier optimistic check is not proof against a cleanup
                # that started in between.
                self.repo.assert_maintenance_free(conn=conn)
                conn.execute(
                    """
                    UPDATE exports SET status=?, layout=?, target_root=?, batch_dir=?, batch_name=?,
                        frozen_plan_version=?, cutoff_event_id=?, cutoff_at=?, frozen_at=?,
                        item_count=?, cutoff_summary_json=?, row_version=row_version+1, updated_at=?
                    WHERE id=? AND status='DRAFT'
                    """,
                    (
                        ExportStatus.COPYING if items else ExportStatus.COMPLETED,
                        layout,
                        target,
                        batch_dir,
                        batch_name,
                        int(job["plan_version"]),
                        int(self.repo.last_event_seq()),
                        now,
                        now,
                        len(items),
                        json_text(
                            {
                                "plan_version": job["plan_version"],
                                "pending_draws": summary.get("pending", 0),
                                "inflight_draws": summary.get("inflight", 0),
                                "unknown_draws": summary.get("unknown", 0),
                                "skipped_sources": skipped,
                                "confirmed_cutoff": bool(confirm_cutoff),
                                "confirmed_zero": bool(confirm_zero),
                            }
                        ),
                        now,
                        export_id,
                    ),
                )
                for item in items:
                    item_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"gacha-master/export/{export_id}/{item['output_id']}"))
                    conn.execute(
                        """
                        INSERT INTO export_items(id, export_id, output_id, source_sha256, target_rel_path, status)
                        VALUES (?,?,?,?,?,'PENDING')
                        """,
                        (item_id, export_id, item["output_id"], item["sha256"], item["target_rel_path"]),
                    )
                self.repo._event(
                    conn,
                    job_id=job["id"],
                    entity_type="EXPORT",
                    entity_id=export_id,
                    action="EXPORT_FROZEN",
                    actor="user",
                    payload={
                        "kind": export["kind"],
                        "layout": layout,
                        "item_count": len(items),
                        "zero": len(items) == 0,
                    },
                )
            if items:
                self.write_manifest(export_id)
            else:
                self._finish(export_id, ExportStatus.COMPLETED)
                self.write_manifest(export_id)
            return self.get_export(export_id)

    def _assert_versions_unchanged(
        self,
        export: Mapping[str, Any],
        job: Mapping[str, Any],
        sources: Iterable[Mapping[str, Any]],
        reviews: Mapping[str, Mapping[str, Any]],
    ) -> None:
        preview = export.get("preview") or {}
        snapshot = preview.get("snapshot") or {}
        if int(snapshot.get("plan_version", -1)) != int(job["plan_version"]):
            raise conflict(
                "预览后计划版本已变化，请刷新审核面板。",
                current_plan_version=job["plan_version"],
                preview_plan_version=snapshot.get("plan_version"),
            )
        preview_sources = {item["source_id"]: item for item in preview.get("sources", [])}
        for source in sources:
            old = preview_sources.get(source["id"])
            if old is None:
                raise conflict("预览后原图清单发生变化，请刷新。", source_id=source["id"])
            if int(old["result_revision"]) != int(source["result_revision"]) or int(old["selection_version"]) != int(
                source["selection_version"]
            ):
                raise conflict(
                    "预览后原图结果或选择已变化，请刷新审核面板。",
                    source_id=source["id"],
                    current_result_revision=source["result_revision"],
                    current_selection_version=source["selection_version"],
                    preview=old,
                )
        for source_id, review in reviews.items():
            source = self.repo.get_source(source_id)
            if source is None:
                raise conflict("审核记录对应的原图已不存在。", source_id=source_id)
            if int(review["result_revision"]) != int(source["result_revision"]) or int(
                review["selection_version"]
            ) != int(source["selection_version"]):
                raise conflict("审核后原图结果或选择已变化，请重新审核。", source_id=source_id)

    # -------------------------------------------------------------- copy
    def list_exports(self, job_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.query_all(
            "SELECT * FROM exports WHERE job_id=? ORDER BY created_at DESC LIMIT ?",
            (job_id, max(1, min(int(limit), 200))),
        )
        result = []
        for row in rows:
            item = dict(row)
            item["preview"] = json.loads(item.pop("preview_json", "{}") or "{}")
            item["cutoff_summary"] = json.loads(item.pop("cutoff_summary_json", "{}") or "{}")
            item.pop("target_root", None)
            result.append(item)
        return result

    def get_export(self, export_id: str) -> dict[str, Any]:
        export = self.repo_export(export_id)
        if export is None:
            raise not_found("导出不存在。", export_id=export_id)
        items = [dict(row) for row in self.db.query_all(
            """
            SELECT ei.*, o.out_no, o.rel_path AS source_rel_path, d.draw_no, a.attempt_no,
                   s.source_order, s.id AS source_id, s.original_name
            FROM export_items ei
            JOIN outputs o ON o.id=ei.output_id
            JOIN attempts a ON a.id=o.attempt_id
            JOIN draws d ON d.id=a.draw_id
            JOIN sources s ON s.id=d.source_id
            WHERE ei.export_id=? ORDER BY ei.target_rel_path
            """,
            (export_id,),
        )]
        reviews = [dict(row) for row in self.db.query_all(
            "SELECT * FROM export_reviews WHERE export_id=? ORDER BY source_id", (export_id,)
        )]
        return {
            "id": export["id"],
            "job_id": export["job_id"],
            "kind": export["kind"],
            "layout": export["layout"],
            "status": export["status"],
            "target_root": export["target_root"],
            "batch_dir": export.get("batch_dir"),
            "batch_name": export.get("batch_name"),
            "draft_version": export["draft_version"],
            "row_version": export["row_version"],
            "frozen_plan_version": export.get("frozen_plan_version"),
            "cutoff_event_id": export.get("cutoff_event_id"),
            "cutoff_at": export.get("cutoff_at"),
            "item_count": export["item_count"],
            "verified_count": export["verified_count"],
            "manifest_hash": export.get("manifest_hash"),
            "error_code": export.get("error_code"),
            "error_message": export.get("error_message"),
            "created_at": export["created_at"],
            "frozen_at": export.get("frozen_at"),
            "completed_at": export.get("completed_at"),
            "preview": export.get("preview"),
            "cutoff_summary": export.get("cutoff_summary"),
            "items": items,
            "reviews": reviews,
            "progress": {
                "verified": sum(1 for item in items if item["status"] == ExportItemStatus.VERIFIED),
                "failed": sum(1 for item in items if item["status"] == ExportItemStatus.FAILED),
                "pending": sum(1 for item in items if item["status"] in (ExportItemStatus.PENDING, ExportItemStatus.COPYING)),
                "total": len(items),
            },
        }

    def copy_export(self, export_id: str) -> dict[str, Any]:
        with self._copy_lock:
            export = self.repo_export(export_id)
            if export is None:
                raise not_found("导出不存在。", export_id=export_id)
            if export["status"] not in (ExportStatus.FROZEN, ExportStatus.COPYING, ExportStatus.PARTIAL_FAILED, ExportStatus.RECOVERY_REQUIRED):
                return self.get_export(export_id)
            if export_id in self._copying:
                return self.get_export(export_id)
            self._copying.add(export_id)
        try:
            self._set_export_status(export_id, ExportStatus.COPYING)
            self._cleanup_stale_temp(export.get("batch_dir"))
            items = [dict(row) for row in self.db.query_all(
                "SELECT * FROM export_items WHERE export_id=? ORDER BY target_rel_path", (export_id,)
            )]
            for item in items:
                if item["status"] == ExportItemStatus.VERIFIED:
                    continue
                try:
                    self._copy_item(export, item)
                except Exception as exc:
                    self._mark_item_failed(
                        item["id"], "INTERNAL_ERROR", f"复制项异常，已保留待恢复：{type(exc).__name__}: {exc}"
                    )
            items = [dict(row) for row in self.db.query_all(
                "SELECT * FROM export_items WHERE export_id=? ORDER BY target_rel_path", (export_id,)
            )]
            all_verified = all(item["status"] == ExportItemStatus.VERIFIED for item in items)
            self._finish(export_id, ExportStatus.COMPLETED if all_verified else ExportStatus.PARTIAL_FAILED)
            self.write_manifest(export_id)
            return self.get_export(export_id)
        finally:
            with self._copy_lock:
                self._copying.discard(export_id)

    @staticmethod
    def _cleanup_stale_temp(batch_dir: Optional[str]) -> None:
        if not batch_dir or not os.path.isdir(batch_dir):
            return
        for dirpath, _dirnames, filenames in os.walk(batch_dir):
            for name in filenames:
                if name.startswith(".copy-") and name.endswith(".tmp"):
                    try:
                        os.unlink(os.path.join(dirpath, name))
                    except OSError:
                        pass

    def _copy_item(self, export: Mapping[str, Any], item: Mapping[str, Any]) -> None:
        batch_dir = export.get("batch_dir")
        if not batch_dir:
            raise GachaError("EXPORT_FILE_CONFLICT", "导出批次目录缺失。")
        output = self.repo.get_output(item["output_id"])
        if output is None:
            self._mark_item_failed(item["id"], "OUTPUT_MISSING", "冻结的输出记录不存在。")
            return
        retention = self.repo.get_retention(output["id"])
        if retention is not None and retention["state"] != "RETAINED":
            self._mark_item_failed(
                item["id"],
                "OUTPUT_PURGED",
                "冻结成员的结果图片已被永久清理，不能复制。",
            )
            return
        source = os.path.join(self.paths.results_root, *str(output["rel_path"]).split("/"))
        target = os.path.join(batch_dir, *str(item["target_rel_path"]).split("/"))
        ensure_dir(os.path.dirname(target))
        if os.path.exists(target):
            try:
                actual_hash, actual_size = sha256_file(target)
            except OSError as exc:
                self._mark_item_failed(item["id"], "EXPORT_FILE_CONFLICT", f"目标文件无法读取: {exc}")
                return
            if actual_hash == item["source_sha256"] and actual_size == int(output["size"]):
                self._mark_item_verified(item["id"], actual_hash, actual_size)
                return
            self._mark_item_failed(
                item["id"],
                "EXPORT_FILE_CONFLICT",
                "目标文件已存在且内容不同，拒绝覆盖。",
            )
            return
        if not os.path.isfile(source):
            # Source missing: only claim the item if a complete target copy exists.
            self._mark_item_failed(
                item["id"],
                "OUTPUT_MISSING",
                "源结果文件缺失；未找到可认领的目标副本。",
                source_missing=True,
            )
            return
        try:
            result = copy_file_verified(
                source, target, fault_name="F10", expected_sha256=item["source_sha256"]
            )
        except FileExistsError:
            try:
                actual_hash, actual_size = sha256_file(target)
            except OSError as exc:
                self._mark_item_failed(item["id"], "EXPORT_FILE_CONFLICT", f"目标文件无法读取: {exc}")
                return
            if actual_hash == item["source_sha256"]:
                self._mark_item_verified(item["id"], actual_hash, actual_size)
            else:
                self._mark_item_failed(item["id"], "EXPORT_FILE_CONFLICT", "目标文件冲突。")
            return
        except GachaError as exc:
            self._mark_item_failed(item["id"], exc.code, exc.message, detail=exc.details)
            return
        except Exception as exc:
            self._mark_item_failed(item["id"], "INTERNAL_ERROR", f"复制失败: {type(exc).__name__}: {exc}")
            return
        if result.sha256 != item["source_sha256"]:
            self._mark_item_failed(item["id"], "OUTPUT_CORRUPT", "复制后哈希与冻结源哈希不一致。")
            return
        # F11: file published, item status not yet committed.
        fault_point("F11")
        self._mark_item_verified(item["id"], result.sha256, result.size)
        # Remove the temp name if publish fell back to a copy.
        try:
            if os.path.exists(target + ".tmp"):
                os.unlink(target + ".tmp")
        except OSError:
            pass

    def _mark_item_verified(self, item_id: str, sha256: str, size: int) -> None:
        now = utc_now()
        with self.db.tx() as conn:
            row = conn.execute("SELECT * FROM export_items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                return
            if row["status"] == ExportItemStatus.VERIFIED:
                return
            conn.execute(
                "UPDATE export_items SET status='VERIFIED', actual_sha256=?, size=?, copied_at=?, verified_at=?, "
                "error_code=NULL, error_message=NULL WHERE id=?",
                (sha256, int(size), now, now, item_id),
            )
            conn.execute(
                "UPDATE exports SET verified_count=(SELECT COUNT(*) FROM export_items WHERE export_id=? AND status='VERIFIED'), "
                "row_version=row_version+1, updated_at=? WHERE id=?",
                (row["export_id"], now, row["export_id"]),
            )

    def _mark_item_failed(
        self,
        item_id: str,
        code: str,
        message: str,
        *,
        detail: Any = None,
        source_missing: bool = False,
    ) -> None:
        now = utc_now()
        with self.db.tx() as conn:
            row = conn.execute("SELECT * FROM export_items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                return
            conn.execute(
                "UPDATE export_items SET status='FAILED', error_code=?, error_message=?, verified_at=NULL WHERE id=?",
                (code, message, item_id),
            )
            conn.execute(
                "UPDATE exports SET row_version=row_version+1, updated_at=? WHERE id=?",
                (now, row["export_id"]),
            )

    def _set_export_status(self, export_id: str, status: str) -> None:
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE exports SET status=?, row_version=row_version+1, updated_at=? WHERE id=?",
                (status, utc_now(), export_id),
            )

    def _finish(self, export_id: str, status: str) -> None:
        now = utc_now()
        with self.db.tx() as conn:
            export = conn.execute("SELECT * FROM exports WHERE id=?", (export_id,)).fetchone()
            failed = int(
                conn.execute(
                    "SELECT COUNT(*) FROM export_items WHERE export_id=? AND status='FAILED'", (export_id,)
                ).fetchone()[0]
            )
            conn.execute(
                "UPDATE exports SET status=?, completed_at=?, error_code=?, error_message=?, row_version=row_version+1, "
                "updated_at=? WHERE id=?",
                (
                    status,
                    now if status in (ExportStatus.COMPLETED, ExportStatus.PARTIAL_FAILED) else None,
                    "EXPORT_FILE_CONFLICT" if failed else None,
                    f"{failed} 个文件未能导出。" if failed else None,
                    now,
                    export_id,
                ),
            )
            if export is not None:
                self.repo._event(
                    conn,
                    job_id=export["job_id"],
                    entity_type="EXPORT",
                    entity_id=export_id,
                    action="EXPORT_FINISHED",
                    to_state=status,
                    actor="system",
                    payload={"failed_items": failed},
                )

    def write_manifest(self, export_id: str) -> str:
        export = self.repo_export(export_id)
        if export is None:
            raise not_found("导出不存在。", export_id=export_id)
        if not export.get("batch_dir"):
            return ""
        items = [dict(row) for row in self.db.query_all(
            """
            SELECT ei.*, o.out_no, o.rel_path AS source_rel_path, d.draw_no, a.attempt_no,
                   s.source_order, s.id AS source_id, s.original_name
            FROM export_items ei
            JOIN outputs o ON o.id=ei.output_id
            JOIN attempts a ON a.id=o.attempt_id
            JOIN draws d ON d.id=a.draw_id
            JOIN sources s ON s.id=d.source_id
            WHERE ei.export_id=? ORDER BY ei.target_rel_path
            """,
            (export_id,),
        )]
        reviews = [dict(row) for row in self.db.query_all(
            "SELECT * FROM export_reviews WHERE export_id=? ORDER BY source_id", (export_id,)
        )]
        job = self.repo.get_job(export["job_id"])
        payload = {
            "schema_version": 1,
            "kind": "gacha_export",
            "export_id": export_id,
            "job_id": export["job_id"],
            "job_display_id": job["display_id"] if job else None,
            "export_kind": export["kind"],
            "layout": export["layout"],
            "status": export["status"],
            "created_at": export["created_at"],
            "frozen_at": export.get("frozen_at"),
            "completed_at": export.get("completed_at"),
            "cutoff_event_id": export.get("cutoff_event_id"),
            "cutoff": export.get("cutoff_summary"),
            "workflow": {
                "api_sha256": job["workflow_api_sha256"] if job else None,
                "ui_sha256": job["workflow_ui_sha256"] if job else None,
            },
            "reviews": [
                {
                    "source_id": review["source_id"],
                    "decision": review["decision"],
                    "selection_version": review["selection_version"],
                    "result_revision": review["result_revision"],
                    "plan_version": review["plan_version"],
                    "selected_output_ids": json.loads(review["selected_output_ids_json"] or "[]"),
                    "reason": review.get("reason"),
                    "confirmed_at": review["confirmed_at"],
                }
                for review in reviews
            ],
            "items": [
                {
                    "source_id": item.get("source_id"),
                    "source_order": item.get("source_order"),
                    "original_name": item.get("original_name"),
                    "draw_no": item.get("draw_no"),
                    "attempt_no": item.get("attempt_no"),
                    "out_no": item.get("out_no"),
                    "output_id": item["output_id"],
                    "target_rel_path": item["target_rel_path"],
                    "source_sha256": item["source_sha256"],
                    "actual_sha256": item.get("actual_sha256"),
                    "size": item.get("size"),
                    "status": item["status"],
                    "error_code": item.get("error_code"),
                    "error_message": item.get("error_message"),
                }
                for item in items
            ],
            "software_version": "1.0.0",
        }
        path = os.path.join(export["batch_dir"], "manifest.json")
        atomic_write_json(path, payload)
        checksum, size = sha256_file(path)
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE exports SET manifest_hash=?, row_version=row_version+1, updated_at=? WHERE id=?",
                (checksum, utc_now(), export_id),
            )
        return path

    def resume(self, export_id: str) -> dict[str, Any]:
        export = self.repo_export(export_id)
        if export is None:
            raise not_found("导出不存在。", export_id=export_id)
        if export["status"] == ExportStatus.COMPLETED:
            return self.get_export(export_id)
        if export["status"] == ExportStatus.DRAFT:
            raise GachaError("STATE_CONFLICT", "草稿尚未冻结，不能恢复复制。")
        self._assert_maintenance_free(export["job_id"])
        self._assert_no_purged_selected(export["job_id"])
        return self.copy_export(export_id)
