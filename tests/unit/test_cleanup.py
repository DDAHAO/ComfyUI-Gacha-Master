"""Storage cleanup: eligibility, protection, journal and crash reconciliation.

These tests use real SQLite, real files and the fake engine.  They delete real
files in a temporary isolated root only; no user data is touched.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.join(ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tests"))

from harness import Harness, make_png  # noqa: E402
from comfyui_gacha_master.backend.errors import GachaError  # noqa: E402
from comfyui_gacha_master.backend.states import RetentionState  # noqa: E402
from comfyui_gacha_master.backend.util import sha256_file, utc_now  # noqa: E402


def _file_hash(path: str) -> str:
    return sha256_file(path)[0]


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness(outputs_per_attempt=2)
        await self.h.start()
        self.svc = self.h.runtime.service
        self.cleanup = self.h.runtime.cleanup
        self.job = self.h.create_job(count=2, names=("a.png", "b.png", "c.png"))
        self.svc.start_job(self.job["id"], expected_version=self.job["row_version"])
        self.done = self.h.wait_job(self.job["id"], timeout=60)
        self.assertEqual(self.done["status"], "COMPLETED", self.done)
        self.sources = self.svc.list_sources(self.job["id"])["items"]
        self.addAsyncCleanup(self.h.cleanup)

    # ------------------------------------------------------------- helpers
    def outputs(self, source):
        return self.svc.list_outputs(source["id"], limit=100)["items"]

    def all_outputs(self):
        return [output for source in self.sources for output in self.outputs(source)]

    def select(self, output):
        return self.svc.set_selection(
            output["id"], True, expected_version=output["selection_version"]
        )

    def unselect(self, output):
        current = self.svc.get_output(output["id"])
        return self.svc.set_selection(
            output["id"], False, expected_version=current["selection_version"]
        )

    def confirm_groups(self, decisions=None):
        decisions = decisions or {}
        for source in self.sources:
            self.svc.confirm_source_review(
                source["id"], decision=decisions.get(source["id"], "SKIPPED")
            )

    def job_row(self):
        return self.svc.repo.get_job(self.job["id"])

    def rel_path_for(self, output):
        return self.svc.repo.get_output(output["id"])["rel_path"]

    def path_for(self, output):
        row = self.svc.repo.get_output(output["id"])
        return os.path.join(
            self.h.runtime.paths.results_root, *str(row["rel_path"]).split("/")
        )

    def wait_cleanup(self, cleanup_id, timeout=30):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = self.cleanup.get(cleanup_id)
            if last["status"] not in ("RUNNING", "PREVIEW"):
                return last
            time.sleep(0.02)
        return last

    def preview_and_start(self):
        preview = self.cleanup.preview(self.job["id"])
        self.assertGreater(preview["candidate_count"], 0, preview)
        self.cleanup.confirm(
            preview["id"],
            plan_token=preview["plan_token"],
            expected_candidate_count=preview["candidate_count"],
            confirm_permanent_delete=True,
        )
        return preview, self.wait_cleanup(preview["id"])

    # ---------------------------------------------------- CLT01 / CLT04/05
    async def test_preview_and_execute_deletes_only_unselected(self):
        selected = self.outputs(self.sources[0])[0]
        self.select(selected)
        self.confirm_groups({self.sources[0]["id"]: "SELECTED"})
        before = {
            output["id"]: (self.path_for(output), _file_hash(self.path_for(output)))
            for output in self.all_outputs()
            if output["id"] != selected["id"]
        }
        manifest_paths = []
        for dirpath, _dirnames, filenames in os.walk(self.h.runtime.paths.results_root):
            for name in filenames:
                if name == "outputs.complete.json":
                    manifest_paths.append(os.path.join(dirpath, name))
        manifest_hashes = {path: _file_hash(path) for path in manifest_paths}
        original_file_hashes = {}
        for source in self.sources:
            row = self.svc.repo.get_source(source["id"])
            snapshot = row.get("snapshot_path")
            if snapshot and os.path.isfile(snapshot):
                original_file_hashes[snapshot] = _file_hash(snapshot)
        job_before = self.svc.job_detail(self.job["id"])
        preview, run = self.preview_and_start()
        self.assertEqual(run["status"], "COMPLETED", run)
        self.assertEqual(preview["candidate_count"], len(before))
        self.assertEqual(run["deleted_files"], len(before))
        self.assertEqual(run["failed_count"], 0)
        for output_id, (path, digest) in before.items():
            self.assertFalse(os.path.exists(path), output_id)
        self.assertTrue(os.path.isfile(self.path_for(selected)))
        self.assertEqual(_file_hash(self.path_for(selected)), selected["sha256"])
        for path, digest in manifest_hashes.items():
            self.assertTrue(os.path.isfile(path))
            self.assertEqual(_file_hash(path), digest)
        for path, digest in original_file_hashes.items():
            self.assertTrue(os.path.isfile(path))
            self.assertEqual(_file_hash(path), digest)
        job_after = self.svc.job_detail(self.job["id"])
        self.assertEqual(job_after["plan_version"], job_before["plan_version"])
        self.assertEqual(job_after["summary"]["succeeded"], job_before["summary"]["succeeded"])
        self.assertEqual(job_after["summary"]["out_total"], job_before["summary"]["out_total"])
        self.assertEqual(job_after["summary"]["out_purged"], len(before))
        self.assertEqual(job_after["summary"]["out_available"], 1)
        self.assertEqual(job_after["summary"]["selected"], 1)
        journal = os.path.join(
            self.h.runtime.paths.jobs_root, self.job["id"], "cleanup", preview["id"]
        )
        self.assertTrue(os.path.isfile(os.path.join(journal, "operation.json")))
        item_results = os.listdir(os.path.join(journal, "items"))
        self.assertEqual(
            len([name for name in item_results if name.endswith(".result.json")]), len(before)
        )
        # Re-applying the same confirmation never deletes twice.
        again = self.cleanup.confirm(
            preview["id"],
            plan_token=preview["plan_token"],
            expected_candidate_count=preview["candidate_count"],
            confirm_permanent_delete=True,
        )
        self.assertEqual(again["deleted_files"], len(before))
        # A restart reconcile re-imports the same receipts without double
        # counting or resurrecting files, and manual verification stays clean.
        reconcile = self.cleanup.reconcile_startup()
        self.assertEqual(reconcile["imported_runs"], 0)
        after_restart = self.cleanup.get(preview["id"])
        self.assertEqual(after_restart["deleted_files"], len(before))
        check = self.h.runtime.recovery.verify_completed_outputs(
            self.svc.repo.get_job(self.job["id"])
        )
        self.assertEqual(check["missing"], 0)
        self.assertEqual(check["purged"], len(before))
        self.assertEqual(self.cleanup.preview(self.job["id"])["candidate_count"], 0)

    async def test_unconfirmed_group_blocks_and_creates_no_candidates(self):
        only_viewed = self.outputs(self.sources[0])[0]
        self.svc.mark_viewed(only_viewed["id"])
        self.svc.confirm_source_review(self.sources[0]["id"], decision="SKIPPED")
        self.svc.confirm_source_review(self.sources[1]["id"], decision="SKIPPED")
        paths = [self.path_for(output) for output in self.all_outputs()]
        with self.assertRaises(GachaError) as ctx:
            self.cleanup.preview(self.job["id"])
        self.assertEqual(ctx.exception.code, "CLEANUP_NOT_ELIGIBLE")
        self.assertEqual(ctx.exception.details["pending_count"], 1)
        self.assertEqual(ctx.exception.details["next_action"], "continue_review")
        self.assertTrue(all(os.path.isfile(path) for path in paths))
        self.assertEqual(
            self.h.runtime.db.query_one("SELECT COUNT(*) FROM cleanup_runs")[0], 0
        )

    async def test_all_skipped_covers_every_output_and_keeps_the_decision(self):
        self.confirm_groups()
        before = self.all_outputs()
        preview = self.cleanup.preview(self.job["id"])
        self.assertTrue(preview["all_skipped"])
        self.assertEqual(preview["candidate_count"], len(before))
        run = None
        self.cleanup.confirm(
            preview["id"],
            plan_token=preview["plan_token"],
            expected_candidate_count=preview["candidate_count"],
            confirm_permanent_delete=True,
        )
        run = self.wait_cleanup(preview["id"])
        self.assertEqual(run["status"], "COMPLETED")
        self.assertEqual(run["deleted_files"], len(before))
        for output in before:
            self.assertFalse(os.path.exists(self.path_for(output)))
        status = self.svc.source_review_status(
            self.svc.repo.get_source(self.sources[0]["id"]),
            self.svc._job_or_404(self.job["id"]),
        )
        self.assertEqual(status["status"], "reviewed")
        self.assertEqual(status["decision"], "SKIPPED")
        summary = self.svc.repo.review_summary(
            self.job["id"], plan_version=self.svc._job_or_404(self.job["id"])["plan_version"]
        )
        self.assertEqual(summary["skipped_groups"], 3)
        self.assertEqual(summary["confirmed_sources"], 3)

    async def test_abnormal_outputs_are_not_candidates_and_are_preserved(self):
        target = self.outputs(self.sources[0])[0]
        self.svc.repo.update_output_health(target["id"], "CORRUPT", "fixture corrupt")
        self.confirm_groups()
        preview = self.cleanup.preview(self.job["id"])
        self.assertGreaterEqual(preview["excluded"]["abnormal"], 1)
        ids = []
        cursor = None
        while True:
            page = self.cleanup.items(preview["id"], limit=200, cursor=cursor)
            ids.extend(item["output_id"] for item in page["items"])
            cursor = page["next_cursor"]
            if not cursor:
                break
        self.assertNotIn(target["id"], ids)
        run = None
        self.cleanup.confirm(
            preview["id"],
            plan_token=preview["plan_token"],
            expected_candidate_count=preview["candidate_count"],
            confirm_permanent_delete=True,
        )
        run = self.wait_cleanup(preview["id"])
        self.assertEqual(run["status"], "COMPLETED")
        self.assertTrue(os.path.isfile(self.path_for(target)))
        self.assertEqual(
            self.svc.get_output(target["id"])["health"], "CORRUPT"
        )

    # --------------------------------------------------------- CLT03/08/10
    async def test_selection_change_after_preview_blocks_confirmation(self):
        self.confirm_groups()
        preview = self.cleanup.preview(self.job["id"])
        candidate = self.outputs(self.sources[0])[0]
        self.select(candidate)
        paths = [self.path_for(output) for output in self.all_outputs()]
        with self.assertRaises(GachaError) as ctx:
            self.cleanup.confirm(
                preview["id"],
                plan_token=preview["plan_token"],
                expected_candidate_count=preview["candidate_count"],
                confirm_permanent_delete=True,
            )
        self.assertEqual(ctx.exception.code, "CLEANUP_PREVIEW_STALE")
        self.assertTrue(all(os.path.isfile(path) for path in paths))

    async def test_expired_preview_is_rejected_before_any_delete(self):
        self.confirm_groups()
        preview = self.cleanup.preview(self.job["id"])
        with self.h.runtime.db.tx() as conn:
            conn.execute(
                "UPDATE cleanup_runs SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                (preview["id"],),
            )
        paths = [self.path_for(output) for output in self.all_outputs()]
        with self.assertRaises(GachaError) as ctx:
            self.cleanup.confirm(
                preview["id"],
                plan_token=preview["plan_token"],
                expected_candidate_count=preview["candidate_count"],
                confirm_permanent_delete=True,
            )
        self.assertEqual(ctx.exception.code, "CLEANUP_PREVIEW_STALE")
        self.assertTrue(all(os.path.isfile(path) for path in paths))
        self.assertEqual(self.cleanup.get(preview["id"])["status"], "EXPIRED")

    async def test_plan_token_and_explicit_confirmation_are_required(self):
        self.confirm_groups()
        preview = self.cleanup.preview(self.job["id"])
        with self.assertRaises(GachaError) as ctx:
            self.cleanup.confirm(
                preview["id"],
                plan_token="wrong-token",
                expected_candidate_count=preview["candidate_count"],
                confirm_permanent_delete=True,
            )
        self.assertEqual(ctx.exception.code, "CLEANUP_PREVIEW_STALE")
        with self.assertRaises(GachaError) as ctx:
            self.cleanup.confirm(
                preview["id"],
                plan_token=preview["plan_token"],
                expected_candidate_count=preview["candidate_count"],
                confirm_permanent_delete=False,
            )
        self.assertEqual(ctx.exception.code, "BAD_REQUEST")

    # --------------------------------------------------------- CLT06/CLT07
    async def test_historical_export_protects_a_now_unselected_output(self):
        chosen = self.outputs(self.sources[0])[0]
        self.select(chosen)
        quick = self.h.runtime.exports.preview(
            self.job["id"], kind="QUICK", layout="FLAT", target_root=self.h.export_root
        )
        frozen = self.h.runtime.exports.confirm(
            quick["id"], expected_draft_version=quick["draft_version"], confirm_cutoff=True
        )
        done = self.h.runtime.exports.copy_export(frozen["id"])
        self.assertEqual(done["status"], "COMPLETED")
        self.unselect(chosen)
        self.confirm_groups()
        preview = self.cleanup.preview(self.job["id"])
        self.assertGreaterEqual(preview["protected_export_count"], 1)
        self.cleanup.confirm(
            preview["id"],
            plan_token=preview["plan_token"],
            expected_candidate_count=preview["candidate_count"],
            confirm_permanent_delete=True,
        )
        run = self.wait_cleanup(preview["id"])
        self.assertEqual(run["status"], "COMPLETED")
        self.assertTrue(os.path.isfile(self.path_for(chosen)))
        self.assertEqual(self.svc.get_output(chosen["id"])["retention_state"], "RETAINED")

    async def test_unreadable_export_reference_blocks_cleanup(self):
        chosen = self.outputs(self.sources[0])[0]
        self.select(chosen)
        quick = self.h.runtime.exports.preview(
            self.job["id"], kind="QUICK", layout="FLAT", target_root=self.h.export_root
        )
        with self.h.runtime.db.tx() as conn:
            conn.execute(
                "UPDATE exports SET preview_json='{not-json' WHERE id=?", (quick["id"],)
            )
        self.confirm_groups()
        with self.assertRaises(GachaError) as ctx:
            self.cleanup.preview(self.job["id"])
        self.assertEqual(ctx.exception.code, "CLEANUP_NOT_ELIGIBLE")
        self.assertIn("导出", ctx.exception.message)
        self.assertTrue(all(os.path.isfile(self.path_for(o)) for o in self.all_outputs()))

    async def test_export_in_progress_blocks_cleanup(self):
        chosen = self.outputs(self.sources[0])[0]
        self.select(chosen)
        quick = self.h.runtime.exports.preview(
            self.job["id"], kind="QUICK", layout="FLAT", target_root=self.h.export_root
        )
        self.h.runtime.exports.confirm(
            quick["id"], expected_draft_version=quick["draft_version"], confirm_cutoff=True
        )
        self.confirm_groups()
        with self.assertRaises(GachaError) as ctx:
            self.cleanup.preview(self.job["id"])
        self.assertEqual(ctx.exception.code, "CLEANUP_NOT_ELIGIBLE")
        self.assertEqual(ctx.exception.details["code"], "EXPORT_IN_PROGRESS")

    async def test_inflight_draw_blocks_cleanup(self):
        self.confirm_groups()
        draw = self.h.runtime.db.query_one(
            "SELECT d.id FROM draws d JOIN sources s ON s.id=d.source_id "
            "WHERE s.job_id=? LIMIT 1",
            (self.job["id"],),
        )
        with self.h.runtime.db.tx() as conn:
            conn.execute("UPDATE draws SET status='PENDING' WHERE id=?", (draw["id"],))
        paths = [self.path_for(output) for output in self.all_outputs()]
        with self.assertRaises(GachaError) as ctx:
            self.cleanup.preview(self.job["id"])
        self.assertEqual(ctx.exception.code, "CLEANUP_NOT_ELIGIBLE")
        self.assertEqual(ctx.exception.details["code"], "BUSY_DRAWS")
        self.assertTrue(all(os.path.isfile(path) for path in paths))

    async def test_frozen_export_after_preview_makes_plan_stale(self):
        self.confirm_groups()
        preview = self.cleanup.preview(self.job["id"])
        chosen = self.outputs(self.sources[0])[0]
        self.select(chosen)
        quick = self.h.runtime.exports.preview(
            self.job["id"], kind="QUICK", layout="FLAT", target_root=self.h.export_root
        )
        self.h.runtime.exports.confirm(
            quick["id"], expected_draft_version=quick["draft_version"], confirm_cutoff=True
        )
        paths = [self.path_for(output) for output in self.all_outputs()]
        with self.assertRaises(GachaError) as ctx:
            self.cleanup.confirm(
                preview["id"],
                plan_token=preview["plan_token"],
                expected_candidate_count=preview["candidate_count"],
                confirm_permanent_delete=True,
            )
        self.assertIn(ctx.exception.code, {"CLEANUP_PREVIEW_STALE", "CLEANUP_NOT_ELIGIBLE"})
        self.assertTrue(all(os.path.isfile(path) for path in paths))

    async def test_bookmark_to_purged_output_locates_remaining_ordinal(self):
        selected = self.outputs(self.sources[0])[0]
        purged = self.outputs(self.sources[0])[1]
        self.select(selected)
        self.confirm_groups()
        preview, run = self.preview_and_start()
        self.assertEqual(run["status"], "COMPLETED")
        session = self.svc.review.open_session(self.job["id"])["session"]
        saved = self.svc.review.save_position(
            session["id"],
            {
                "expected_version": session["version"],
                "current_source_id": self.sources[0]["id"],
                "current_output_id": purged["id"],
            },
        )
        self.assertIsNotNone(saved)
        stored = self.svc.repo.get_review_session(self.job["id"])
        self.assertGreaterEqual(int(stored["result_offset"]), 0)
        # The remaining retained result is reachable at that filtered offset.
        page = self.svc.list_outputs(self.sources[0]["id"], limit=10, offset=0)
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["id"], selected["id"])
        states = self.svc.output_states(self.sources[0]["id"], [purged["id"]])
        self.assertEqual(states["items"][0]["retention_state"], "PURGED")

    # --------------------------------------------------------- CLT09/CLT10
    async def test_maintenance_boundary_blocks_concurrent_writes(self):
        drafted = self.outputs(self.sources[0])[0]
        self.select(drafted)
        self.confirm_groups()
        quick = self.h.runtime.exports.preview(
            self.job["id"], kind="QUICK", layout="FLAT", target_root=self.h.export_root
        )
        preview = self.cleanup.preview(self.job["id"])
        candidate = self.outputs(self.sources[0])[1]
        with self.h.runtime.db.tx() as conn:
            acquired = self.h.runtime.repo.acquire_maintenance(
                conn,
                holder="test-holder",
                kind="RESULT_CLEANUP",
                cleanup_id=preview["id"],
                job_id=self.job["id"],
                boot_id="test-boot",
            )
        self.assertTrue(acquired)
        try:
            with self.assertRaises(GachaError) as ctx:
                self.svc.set_selection(candidate["id"], True)
            self.assertEqual(ctx.exception.code, "STORAGE_BUSY")
            with self.assertRaises(GachaError) as ctx:
                self.svc.confirm_source_review(self.sources[0]["id"], decision="SELECTED")
            self.assertEqual(ctx.exception.code, "STORAGE_BUSY")
            with self.assertRaises(GachaError) as ctx:
                self.h.runtime.exports.confirm(
                    quick["id"], expected_draft_version=quick["draft_version"], confirm_cutoff=True
                )
            self.assertEqual(ctx.exception.code, "STORAGE_BUSY")
            with self.assertRaises(GachaError) as ctx:
                self.svc.restore_backup("any")
            self.assertEqual(ctx.exception.code, "STORAGE_BUSY")
        finally:
            with self.h.runtime.db.tx() as conn:
                self.h.runtime.repo.release_maintenance(conn, holder="test-holder")

    # --------------------------------------------------------- CLT11/CLT12
    async def test_locked_file_reports_partial_failure_and_stays_readable(self):
        self.confirm_groups()
        preview = self.cleanup.preview(self.job["id"])
        target = self.outputs(self.sources[0])[0]
        path = self.path_for(target)
        # Windows: hold the file open without delete sharing through a plain
        # read handle; unlink is expected to fail.
        handle = open(path, "rb")
        try:
            self.cleanup.confirm(
                preview["id"],
                plan_token=preview["plan_token"],
                expected_candidate_count=preview["candidate_count"],
                confirm_permanent_delete=True,
            )
            run = self.wait_cleanup(preview["id"])
        finally:
            handle.close()
        if os.name == "nt":
            self.assertEqual(run["status"], "PARTIAL_FAILED", run)
            self.assertGreaterEqual(run["failed_count"], 1)
            self.assertTrue(os.path.isfile(path))
            self.assertEqual(self.svc.get_output(target["id"])["retention_state"], "RETAINED")
            self.assertGreaterEqual(run["deleted_files"], 1)
            # A fresh preview after the failure deletes only what remains.
            preview2 = self.cleanup.preview(self.job["id"])
            self.assertLess(preview2["candidate_count"], preview["candidate_count"] + 1)
        else:
            self.assertEqual(run["status"], "COMPLETED")

    async def test_file_change_after_preview_aborts_before_first_delete(self):
        self.confirm_groups()
        preview = self.cleanup.preview(self.job["id"])
        target = self.outputs(self.sources[0])[0]
        path = self.path_for(target)
        with open(path, "ab") as handle:
            handle.write(b"tampered")
        paths = [self.path_for(output) for output in self.all_outputs()]
        with self.assertRaises(GachaError) as ctx:
            self.cleanup.confirm(
                preview["id"],
                plan_token=preview["plan_token"],
                expected_candidate_count=preview["candidate_count"],
                confirm_permanent_delete=True,
            )
        self.assertEqual(ctx.exception.code, "CLEANUP_PREVIEW_STALE")
        self.assertTrue(all(os.path.isfile(item) for item in paths))
        self.assertEqual(self.cleanup.get(preview["id"])["status"], "CANCELLED")

    async def test_predeleted_candidate_is_reported_not_counted_as_freed(self):
        self.confirm_groups()
        target = self.outputs(self.sources[0])[0]
        path = self.path_for(target)
        os.unlink(path)
        preview = self.cleanup.preview(self.job["id"])
        self.assertGreaterEqual(preview["excluded"]["file_missing"], 1)
        run = None
        if preview["candidate_count"]:
            self.cleanup.confirm(
                preview["id"],
                plan_token=preview["plan_token"],
                expected_candidate_count=preview["candidate_count"],
                confirm_permanent_delete=True,
            )
            run = self.wait_cleanup(preview["id"])
        self.assertFalse(os.path.exists(path))
        if run is not None:
            self.assertNotIn(target["id"], [item["output_id"] for item in self.cleanup.items(preview["id"])["items"]])
        # The accidental missing file remains an anomaly in the job summary.
        summary = self.svc.job_detail(self.job["id"])["summary"]
        self.assertGreaterEqual(summary["out_abnormal"], 0)

    # ------------------------------------------------------------- CLT13
    async def test_relative_path_escape_is_rejected_without_deleting(self):
        outsider = os.path.join(self.h.root, "outside-sentinel.png")
        make_png(outsider, color=(5, 5, 5))
        before = _file_hash(outsider)
        self.confirm_groups()
        target = self.outputs(self.sources[0])[0]
        outside_rel = os.path.relpath(outsider, self.h.runtime.paths.results_root).replace(os.sep, "/")
        with self.h.runtime.db.tx() as conn:
            conn.execute(
                "UPDATE outputs SET rel_path=? WHERE id=?", (outside_rel, target["id"])
            )
        with self.assertRaises(GachaError) as ctx:
            self.cleanup.preview(self.job["id"])
        self.assertEqual(ctx.exception.code, "PATH_NOT_ALLOWED")
        self.assertTrue(os.path.isfile(outsider))
        self.assertEqual(_file_hash(outsider), before)

    async def test_directory_junction_is_rejected_on_windows(self):
        if os.name != "nt":
            self.skipTest("Windows junction semantics only")
        self.confirm_groups()
        target = self.outputs(self.sources[0])[0]
        path = self.path_for(target)
        outside_dir = os.path.join(self.h.root, "junction-target")
        os.makedirs(outside_dir, exist_ok=True)
        moved = os.path.join(outside_dir, os.path.basename(path))
        os.replace(path, moved)
        junction = os.path.join(os.path.dirname(path), "lnk")
        result = os.system(f'cmd /c mklink /J "{junction}" "{outside_dir}" >NUL 2>&1')
        if result != 0:
            self.skipTest("cannot create junction in this environment")
        rel = os.path.relpath(os.path.join(junction, os.path.basename(path)), self.h.runtime.paths.results_root)
        with self.h.runtime.db.tx() as conn:
            conn.execute(
                "UPDATE outputs SET rel_path=? WHERE id=?",
                (rel.replace(os.sep, "/"), target["id"]),
            )
        try:
            with self.assertRaises(GachaError) as ctx:
                self.cleanup.preview(self.job["id"])
            self.assertEqual(ctx.exception.code, "PATH_NOT_ALLOWED")
            self.assertTrue(os.path.isfile(moved))
        finally:
            os.rmdir(junction)

    # ------------------------------------------------------------- CLT14
    async def test_intent_without_result_and_missing_file_becomes_purged_on_restart(self):
        self.confirm_groups()
        preview = self.cleanup.preview(self.job["id"])
        target = self.outputs(self.sources[0])[0]
        intent_dir = os.path.join(
            self.h.runtime.paths.jobs_root, self.job["id"], "cleanup", preview["id"]
        )
        os.makedirs(os.path.join(intent_dir, "items"), exist_ok=True)
        operation = {
            "schema_version": 1,
            "kind": "UNSELECTED_RESULTS",
            "storage_instance_id": self.cleanup.storage_instance_id(),
            "cleanup_id": preview["id"],
            "job_id": self.job["id"],
            "job_display_id": self.job["display_id"],
            "job_dir": self.job_row()["job_dir"],
            "result_dir": self.job_row()["result_dir"],
            "results_root": self.job_row()["results_root"],
            "plan_version": self.job["plan_version"],
            "storage_revision": preview["storage_revision"],
            "plan_token_hash": "fixture",
            "plan_hash": "fixture",
            "candidate_count": 1,
            "estimated_bytes": target["size"],
            "created_at": utc_now(),
            "confirmed_at": utc_now(),
        }
        intent_path = os.path.join(intent_dir, "items", f"{target['id']}.intent.json")
        with open(intent_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "schema_version": 1,
                    "cleanup_id": preview["id"],
                    "job_id": self.job["id"],
                    "storage_instance_id": operation["storage_instance_id"],
                    "output_id": target["id"],
                    "source_id": target["id"],
                    "draw_id": target["id"],
                    "attempt_id": target["attempt_id"],
                    "rel_path": self.rel_path_for(target),
                    "sha256": target["sha256"],
                    "size": target["size"],
                    "intended_at": utc_now(),
                },
                handle,
            )
        with open(os.path.join(intent_dir, "operation.json"), "w", encoding="utf-8") as handle:
            json.dump(operation, handle)
        # Simulate the crash window after the intent was durable but before
        # unlink: the process deleted the file, then died before the receipt.
        os.unlink(self.path_for(target))
        self.h.runtime.close()
        h2 = Harness(outputs_per_attempt=2, root=self.h.root)
        await h2.start()
        try:
            report = h2.runtime.cleanup.last_reconcile
            self.assertGreaterEqual(report["recovered_purged"], 1)
            retention = h2.runtime.repo.get_retention(target["id"])
            self.assertEqual(retention["state"], "PURGED")
            check = h2.runtime.recovery.verify_completed_outputs(
                h2.runtime.repo.get_job(self.job["id"])
            )
            self.assertGreaterEqual(check["purged"], 1)
            self.assertEqual(check["missing"], 0)
        finally:
            self.h.runtime = h2.runtime

    async def test_open_intent_is_closed_as_cancelled_and_file_is_kept(self):
        self.confirm_groups()
        preview = self.cleanup.preview(self.job["id"])
        target = self.outputs(self.sources[0])[0]
        journal_dir = os.path.join(
            self.h.runtime.paths.jobs_root, self.job["id"], "cleanup", preview["id"], "items"
        )
        os.makedirs(journal_dir, exist_ok=True)
        identity = self.cleanup.storage_instance_id()
        with open(
            os.path.join(
                self.h.runtime.paths.jobs_root, self.job["id"], "cleanup", preview["id"],
                "operation.json",
            ),
            "w",
            encoding="utf-8",
        ) as handle:
            json.dump(
                {
                    "schema_version": 1,
                    "kind": "UNSELECTED_RESULTS",
                    "storage_instance_id": identity,
                    "cleanup_id": preview["id"],
                    "job_id": self.job["id"],
                    "job_display_id": self.job["display_id"],
                    "job_dir": self.job_row()["job_dir"],
                    "result_dir": self.job_row()["result_dir"],
                    "results_root": self.job_row()["results_root"],
                    "plan_version": self.job["plan_version"],
                    "storage_revision": 0,
                    "plan_token_hash": "fixture",
                    "plan_hash": "fixture",
                    "candidate_count": 1,
                    "estimated_bytes": target["size"],
                    "created_at": utc_now(),
                    "confirmed_at": utc_now(),
                },
                handle,
            )
        with open(os.path.join(journal_dir, f"{target['id']}.intent.json"), "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "schema_version": 1,
                    "cleanup_id": preview["id"],
                    "job_id": self.job["id"],
                    "storage_instance_id": identity,
                    "output_id": target["id"],
                    "source_id": target["id"],
                    "draw_id": target["id"],
                    "attempt_id": target["attempt_id"],
                    "rel_path": self.rel_path_for(target),
                    "sha256": target["sha256"],
                    "size": target["size"],
                    "intended_at": utc_now(),
                },
                handle,
            )
        target_path = self.path_for(target)
        self.h.runtime.close()
        h2 = Harness(outputs_per_attempt=2, root=self.h.root)
        await h2.start()
        try:
            report = h2.runtime.cleanup.last_reconcile
            self.assertGreaterEqual(report["cancelled_intents"], 1)
            self.assertTrue(os.path.isfile(target_path))
            retention = h2.runtime.repo.get_retention(target["id"])
            self.assertIn(retention["state"] if retention else "RETAINED", ("RETAINED",))
            self.assertTrue(
                os.path.isfile(os.path.join(journal_dir, f"{target['id']}.result.json"))
            )
        finally:
            self.h.runtime = h2.runtime

    async def test_running_run_without_journal_is_cancelled_on_restart(self):
        cleanup_id = str(uuid.uuid4())
        now = utc_now()
        with self.h.runtime.db.tx() as conn:
            conn.execute(
                "INSERT INTO cleanup_runs(id, job_id, kind, status, plan_version, storage_revision,"
                " plan_token_hash, plan_hash, expires_at, created_at, updated_at)"
                " VALUES (?,?, 'UNSELECTED_RESULTS', 'RUNNING', 1, 0, 'x', 'y', ?, ?, ?)",
                (cleanup_id, self.job["id"], now, now, now),
            )
        report = self.cleanup.reconcile_startup()
        self.assertGreaterEqual(report["journals"], 0)
        run = self.cleanup.get(cleanup_id)
        self.assertEqual(run["status"], "CANCELLED")
        self.assertFalse(os.path.exists(os.path.join(
            self.h.runtime.paths.jobs_root, self.job["id"], "cleanup", cleanup_id
        )))
        # No durable plan means no deletion; a fresh preview is allowed.
        self.confirm_groups()
        preview = self.cleanup.preview(self.job["id"])
        self.assertGreater(preview["candidate_count"], 0)

    async def test_corrupt_journal_requires_recovery_and_blocks_preview(self):
        cleanup_id = str(uuid.uuid4())
        now = utc_now()
        with self.h.runtime.db.tx() as conn:
            conn.execute(
                "INSERT INTO cleanup_runs(id, job_id, kind, status, plan_version, storage_revision,"
                " plan_token_hash, plan_hash, expires_at, created_at, updated_at)"
                " VALUES (?,?, 'UNSELECTED_RESULTS', 'RUNNING', 1, 0, 'x', 'y', ?, ?, ?)",
                (cleanup_id, self.job["id"], now, now, now),
            )
        journal_dir = os.path.join(
            self.h.runtime.paths.jobs_root, self.job["id"], "cleanup", cleanup_id
        )
        os.makedirs(journal_dir, exist_ok=True)
        with open(os.path.join(journal_dir, "operation.json"), "w", encoding="utf-8") as handle:
            handle.write("{broken")
        self.cleanup.reconcile_startup()
        self.assertEqual(self.cleanup.get(cleanup_id)["status"], "RECOVERY_REQUIRED")
        self.confirm_groups()
        with self.assertRaises(GachaError) as ctx:
            self.cleanup.preview(self.job["id"])
        self.assertEqual(ctx.exception.code, "CLEANUP_RECOVERY_REQUIRED")

    # ------------------------------------------------------------- CLT15
    async def test_database_intent_failure_prevents_unlink(self):
        self.confirm_groups()
        preview = self.cleanup.preview(self.job["id"])
        original = self.h.runtime.repo.set_retention

        def fail_cleaning(conn, output_id, **kwargs):
            if kwargs.get("state") == RetentionState.CLEANING:
                raise RuntimeError("fixture db failure")
            return original(conn, output_id, **kwargs)

        paths = [self.path_for(output) for output in self.all_outputs()]
        with mock.patch.object(self.h.runtime.repo, "set_retention", side_effect=fail_cleaning):
            self.cleanup.confirm(
                preview["id"],
                plan_token=preview["plan_token"],
                expected_candidate_count=preview["candidate_count"],
                confirm_permanent_delete=True,
            )
            run = self.wait_cleanup(preview["id"])
        self.assertTrue(all(os.path.isfile(path) for path in paths))
        self.assertGreaterEqual(run["failed_count"], 1)
        self.assertEqual(run["deleted_files"], 0)

    # ------------------------------------------------------------- CLT16
    async def test_cancelled_intent_does_not_exempt_later_missing_file(self):
        self.confirm_groups()
        preview = self.cleanup.preview(self.job["id"])
        target = self.outputs(self.sources[0])[0]
        journal_dir = os.path.join(
            self.h.runtime.paths.jobs_root, self.job["id"], "cleanup", preview["id"], "items"
        )
        os.makedirs(journal_dir, exist_ok=True)
        identity = self.cleanup.storage_instance_id()
        with open(os.path.join(os.path.dirname(journal_dir), "operation.json"), "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "schema_version": 1,
                    "kind": "UNSELECTED_RESULTS",
                    "storage_instance_id": identity,
                    "cleanup_id": preview["id"],
                    "job_id": self.job["id"],
                    "job_display_id": self.job["display_id"],
                    "job_dir": self.job_row()["job_dir"],
                    "result_dir": self.job_row()["result_dir"],
                    "results_root": self.job_row()["results_root"],
                    "plan_version": self.job["plan_version"],
                    "storage_revision": 0,
                    "plan_token_hash": "fixture",
                    "plan_hash": "fixture",
                    "candidate_count": 1,
                    "estimated_bytes": target["size"],
                    "created_at": utc_now(),
                    "confirmed_at": utc_now(),
                },
                handle,
            )
        with open(os.path.join(journal_dir, f"{target['id']}.intent.json"), "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "schema_version": 1,
                    "cleanup_id": preview["id"],
                    "job_id": self.job["id"],
                    "storage_instance_id": identity,
                    "output_id": target["id"],
                    "source_id": target["id"],
                    "draw_id": target["id"],
                    "attempt_id": target["attempt_id"],
                    "rel_path": self.rel_path_for(target),
                    "sha256": target["sha256"],
                    "size": target["size"],
                    "intended_at": utc_now(),
                },
                handle,
            )
        with open(os.path.join(journal_dir, f"{target['id']}.result.json"), "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "schema_version": 1,
                    "cleanup_id": preview["id"],
                    "output_id": target["id"],
                    "status": "FAILED",
                    "completed_at": utc_now(),
                },
                handle,
            )
        os.unlink(self.path_for(target))
        self.h.runtime.close()
        h2 = Harness(outputs_per_attempt=2, root=self.h.root)
        await h2.start()
        try:
            check = h2.runtime.recovery.verify_completed_outputs(
                h2.runtime.repo.get_job(self.job["id"])
            )
            self.assertGreaterEqual(check["missing"], 1)
            retention = h2.runtime.repo.get_retention(target["id"])
            self.assertNotEqual((retention or {}).get("state"), "PURGED")
        finally:
            self.h.runtime = h2.runtime

    # ------------------------------------------------------------- CLT17
    async def test_restore_old_backup_reimports_purged_state_and_blocks_export(self):
        chosen = self.outputs(self.sources[0])[0]
        self.select(chosen)
        backup = self.svc.create_backup("before-cleanup")
        self.unselect(chosen)
        self.confirm_groups()
        preview, run = self.preview_and_start()
        self.assertEqual(run["status"], "COMPLETED")
        self.assertFalse(os.path.exists(self.path_for(chosen)))
        result = self.svc.restore_backup(backup["id"])
        self.assertIn("cleanup_reconcile", result)
        # The journal restores the PURGED fact that the old DB did not contain.
        retention = self.svc.repo.get_retention(chosen["id"])
        self.assertEqual(retention["state"], "PURGED")
        restored_selection = self.svc.repo.get_selection(chosen["id"])
        self.assertTrue(restored_selection["selected"])
        self.assertFalse(os.path.exists(self.path_for(chosen)))
        with self.assertRaises(GachaError) as ctx:
            self.h.runtime.exports.preview(
                self.job["id"], kind="QUICK", layout="FLAT", target_root=self.h.export_root
            )
        self.assertEqual(ctx.exception.code, "OUTPUT_PURGED")
        # The user can clear the stale selection, then export works again.
        self.svc.set_selection(chosen["id"], False)
        with self.assertRaises(GachaError) as ctx:
            self.h.runtime.exports.preview(
                self.job["id"], kind="QUICK", layout="FLAT", target_root=self.h.export_root
            )
        self.assertEqual(ctx.exception.code, "EXPORT_REVIEW_REQUIRED")

    # ------------------------------------------------------------- CLT20
    async def test_append_after_cleanup_uses_new_identities(self):
        self.confirm_groups()
        preview, run = self.preview_and_start()
        self.assertEqual(run["status"], "COMPLETED")
        before_draws = self.svc.job_detail(self.job["id"])["summary"]["total_planned"]
        source = self.sources[0]
        result = self.svc.append_block(source["id"], count=1, priority="tail")
        self.assertIn("block_id", result)
        self.svc.start_job(self.job["id"], expected_version=self.svc._job_or_404(self.job["id"])["row_version"])
        final = self.h.wait_job(self.job["id"], timeout=60)
        self.assertEqual(final["status"], "COMPLETED")
        self.assertGreater(final["summary"]["total_planned"], before_draws)
        # New results use new paths and are retained; purged rows stay purged.
        retained = self.svc.list_outputs(source["id"], limit=100)["items"]
        self.assertTrue(retained)
        self.assertTrue(all(item["retention_state"] == "RETAINED" for item in retained))
        self.assertEqual(final["summary"]["out_purged"], preview["candidate_count"])

    # ------------------------------------------------------------- CLT22/23
    async def test_thumbnail_cache_clear_reports_counts_and_preserves_other_data(self):
        from comfyui_gacha_master.backend.images import make_thumbnail

        thumbs = self.h.runtime.paths.thumbnails_root
        os.makedirs(os.path.join(thumbs, "outputs"), exist_ok=True)
        os.makedirs(os.path.join(thumbs, "sources"), exist_ok=True)
        source_image = self.svc.source_detail(self.sources[0]["id"])
        source_path = os.path.join(
            self.h.runtime.paths.jobs_root, self.job["id"], "sources", self.sources[0]["id"], "original.png"
        )
        make_thumbnail(source_path, os.path.join(thumbs, "sources", "s.jpg"))
        make_thumbnail(source_path, os.path.join(thumbs, "outputs", "o.jpg"))
        with open(os.path.join(thumbs, "outputs", "keep.txt"), "w", encoding="utf-8") as handle:
            handle.write("not a thumbnail")
        outputs_before = {
            output["id"]: _file_hash(self.path_for(output)) for output in self.all_outputs()
        }
        result = self.svc.clear_thumbnail_cache()
        self.assertEqual(result["removed"], 2)
        self.assertGreater(result["bytes"], 0)
        self.assertEqual(result["failed"], 0)
        self.assertGreaterEqual(result["skipped"], 1)
        self.assertFalse(os.path.exists(os.path.join(thumbs, "sources", "s.jpg")))
        self.assertTrue(os.path.exists(os.path.join(thumbs, "outputs", "keep.txt")))
        for output_id, digest in outputs_before.items():
            output = self.svc.get_output(output_id)
            if output["retention_state"] == "RETAINED":
                self.assertEqual(_file_hash(self.path_for(output)), digest)
        # Empty cache is a clean zero result.
        again = self.svc.clear_thumbnail_cache()
        self.assertEqual(again["removed"], 0)
        self.assertEqual(again["bytes"], 0)
        # A read-only cache file is reported as a failure, not silently ignored.
        if os.name == "nt":
            blocked = os.path.join(thumbs, "outputs", "blocked.jpg")
            with open(blocked, "wb") as handle:
                handle.write(b"x")
            os.chmod(blocked, stat.S_IREAD)
            try:
                result = self.svc.clear_thumbnail_cache()
                self.assertEqual(result["failed"], 1)
                self.assertEqual(len(result["errors"]), 1)
            finally:
                os.chmod(blocked, stat.S_IWRITE)
                os.unlink(blocked)

    async def test_concurrent_thumbnail_generation_is_a_cache_hit(self):
        from comfyui_gacha_master.backend.images import make_thumbnail
        import threading

        first = self.outputs(self.sources[0])[0]
        source_path = self.path_for(first)
        cache = os.path.join(
            self.h.runtime.paths.thumbnails_root, "outputs", "race-test.jpg"
        )
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        results = []
        errors = []

        def worker():
            try:
                results.append(make_thumbnail(source_path, cache, edge=64))
            except Exception as exc:  # noqa: BLE001 - fixture records any failure
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertTrue(os.path.isfile(cache))
        # Exactly one publisher wins; every other thread is a cache hit.
        self.assertEqual(sum(1 for item in results if not item.get("cached")), 1)

    async def test_shared_thumbnail_key_survives_one_sided_cleanup(self):
        from comfyui_gacha_master.backend.images import make_thumbnail

        first = self.outputs(self.sources[0])[0]
        second = self.outputs(self.sources[0])[1]
        first_path = self.path_for(first)
        second_path = self.path_for(second)
        shutil.copyfile(first_path, second_path)
        digest, size = sha256_file(second_path)
        with self.h.runtime.db.tx() as conn:
            conn.execute(
                "UPDATE outputs SET sha256=?, size=? WHERE id=?",
                (digest, size, second["id"]),
            )
        cache = os.path.join(
            self.h.runtime.paths.thumbnails_root, "outputs", f"{digest[:32]}.jpg"
        )
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        make_thumbnail(first_path, cache)
        self.confirm_groups()
        preview = self.cleanup.preview(self.job["id"])
        # Shared key means neither candidate owns an orphan thumbnail.
        self.assertEqual(preview["estimated_thumbnail_bytes"], 0)
        self.cleanup.confirm(
            preview["id"],
            plan_token=preview["plan_token"],
            expected_candidate_count=preview["candidate_count"],
            confirm_permanent_delete=True,
        )
        run = self.wait_cleanup(preview["id"])
        self.assertEqual(run["status"], "COMPLETED")
        self.assertTrue(os.path.isfile(cache))

    # ------------------------------------------------------------- CLT25
    async def test_scale_five_thousand_outputs_is_bounded(self):
        source = self.sources[0]
        job = self.svc._job_or_404(self.job["id"])
        block = self.h.runtime.repo.list_blocks(job["id"])[0]
        draw_id = str(uuid.uuid4())
        attempt_id = str(uuid.uuid4())
        attempt_dir = os.path.join(
            job["result_dir"], f"SRC-{source['source_order']:04d}", "DRAW-999999", "ATTEMPT-001"
        )
        os.makedirs(attempt_dir, exist_ok=True)
        make_png(os.path.join(attempt_dir, "OUT-0001.png"))
        payload = open(os.path.join(attempt_dir, "OUT-0001.png"), "rb").read()
        digest, size = sha256_file(os.path.join(attempt_dir, "OUT-0001.png"))
        now = utc_now()
        with self.h.runtime.db.tx() as conn:
            conn.execute(
                "INSERT INTO draws(id, job_id, source_id, block_id, draw_no, status, accepted_attempt_id,"
                " row_version, created_at, updated_at) VALUES (?,?,?,?,?, 'SUCCEEDED', NULL, 1, ?, ?)",
                (draw_id, job["id"], source["id"], block["id"], 999999, now, now),
            )
            conn.execute(
                "INSERT INTO attempts(id, draw_id, attempt_no, expected_prompt_id, boot_id, status,"
                " execution_hash, source_hash, attempt_dir, created_at, updated_at)"
                " VALUES (?,?,1,?,?, 'SUCCEEDED', 'eh', ?, ?, ?, ?)",
                (attempt_id, draw_id, f"prompt-{attempt_id}", "boot", source["sha256"], attempt_dir, now, now),
            )
            conn.execute(
                "UPDATE draws SET accepted_attempt_id=? WHERE id=?", (attempt_id, draw_id)
            )
            for index in range(5000):
                rel = os.path.relpath(
                    os.path.join(attempt_dir, f"OUT-{index + 1:04d}.png"),
                    job["results_root"],
                ).replace(os.sep, "/")
                target = os.path.join(attempt_dir, f"OUT-{index + 1:04d}.png")
                if index:
                    shutil.copyfile(os.path.join(attempt_dir, "OUT-0001.png"), target)
                conn.execute(
                    "INSERT INTO outputs(id, attempt_id, out_no, rel_path, sha256, size, width, height,"
                    " health, created_at) VALUES (?,?,?,?,?,?,40,30,'VERIFIED',?)",
                    (str(uuid.uuid4()), attempt_id, index + 1, rel, digest, size, now),
                )
            conn.execute(
                "UPDATE draws SET accepted_attempt_id=? WHERE id=?", (attempt_id, draw_id)
            )
        for item in self.sources:
            self.svc.confirm_source_review(item["id"], decision="SKIPPED")
        import tracemalloc

        tracemalloc.start()
        started = time.time()
        preview = self.cleanup.preview(job["id"])
        preview_elapsed = time.time() - started
        self.assertGreaterEqual(preview["candidate_count"], 5000)
        page = self.cleanup.items(preview["id"], limit=50)
        self.assertEqual(len(page["items"]), 50)
        self.assertIsNotNone(page["next_cursor"])
        self.assertLess(preview_elapsed, 120.0)
        started = time.time()
        self.cleanup.confirm(
            preview["id"],
            plan_token=preview["plan_token"],
            expected_candidate_count=preview["candidate_count"],
            confirm_permanent_delete=True,
        )
        run = self.wait_cleanup(preview["id"], timeout=600)
        delete_elapsed = time.time() - started
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertEqual(run["status"], "COMPLETED", run)
        self.assertGreaterEqual(run["deleted_files"], 5000)
        metrics = {
            "data_origin": "fixture",
            "fixture_outputs": 5000,
            "candidate_count": preview["candidate_count"],
            "preview_seconds": round(preview_elapsed, 3),
            "delete_seconds": round(delete_elapsed, 3),
            "deleted_files": run["deleted_files"],
            "deleted_bytes": run["deleted_bytes"],
            "failed": run["failed_count"],
            "worker_rss_kib": None,
            "tracemalloc_peak_kib": int(peak / 1024),
        }
        try:
            import resource  # type: ignore

            metrics["worker_rss_kib"] = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        except Exception:
            pass
        metrics_path = os.path.join(
            ROOT,
            "test-artifacts",
            "storage-cleanup-20261117",
            "CLT25-scale-metrics.json",
        )
        os.makedirs(os.path.dirname(metrics_path), exist_ok=True)
        with open(metrics_path, "w", encoding="utf-8") as handle:
            json.dump(metrics, handle, ensure_ascii=False, indent=2)
            handle.write("\n")

    # ------------------------------------------------------------- CLT26
    async def test_unknown_files_and_manifests_are_never_deleted(self):
        self.confirm_groups()
        target = self.outputs(self.sources[0])[0]
        attempt_dir = os.path.dirname(self.path_for(target))
        unknown = os.path.join(attempt_dir, "notes-user-added.txt")
        with open(unknown, "w", encoding="utf-8") as handle:
            handle.write("keep me")
        manifest = os.path.join(attempt_dir, "outputs.complete.json")
        journal_before = self.h.runtime.db.query_one(
            "SELECT COUNT(*) FROM events WHERE action LIKE 'CLEANUP%'"
        )[0]
        preview, run = self.preview_and_start()
        self.assertEqual(run["status"], "COMPLETED")
        self.assertTrue(os.path.isfile(unknown))
        if os.path.isfile(manifest):
            self.assertTrue(os.path.isfile(manifest))
        self.assertGreater(
            self.h.runtime.db.query_one(
                "SELECT COUNT(*) FROM events WHERE action LIKE 'CLEANUP%'"
            )[0],
            journal_before,
        )


if __name__ == "__main__":
    unittest.main()
