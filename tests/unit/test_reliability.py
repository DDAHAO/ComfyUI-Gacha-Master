import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.join(ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tests"))

from comfyui_gacha_master.backend.db import Database
from comfyui_gacha_master.backend.errors import GachaError
from comfyui_gacha_master.backend.runtime import Runtime
from comfyui_gacha_master.backend.util import FileLock
from comfyui_gacha_master.backend.workflow import preflight
from harness import Harness, make_png, workflow_fixture


class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def harness(self, **kwargs):
        h = Harness(**kwargs)
        await h.start()
        self.addAsyncCleanup(h.cleanup)
        return h

    async def test_multiple_samplers_get_unique_persisted_seeds(self):
        h = await self.harness()
        fixture = workflow_fixture()
        api = json.loads(json.dumps(fixture["api"]))
        ui = json.loads(json.dumps(fixture["ui"]))
        api["20"] = {
            "class_type": "KSampler",
            "inputs": {
                "model": ["3", 0], "seed": 99, "steps": 10, "cfg": 7.0,
                "sampler_name": "euler", "scheduler": "normal",
                "positive": ["7", 0], "negative": ["8", 0], "latent_image": ["2", 0], "denoise": 0.3,
            },
        }
        api["5"]["inputs"]["samples"] = ["20", 0]
        ui["nodes"].append({"id": 20, "type": "KSampler"})
        result = preflight(ui, api, {"load_image_node_id": "1", "save_image_node_id": "6"})
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(len(result.seed_bindings), 2)
        request = {
            "name": "two samplers",
            "protection_mode": "snapshot",
            "count_per_source": 2,
            "sources": h.make_sources(("a.png",)),
            "workflow_ui": ui,
            "workflow_api": api,
            "bindings": {"load_image_node_id": "1", "save_image_node_id": "6"},
        }
        created = h.runtime.service.create_job(request)
        job = h.wait_creation(created["id"])
        self.assertEqual(job["status"], "READY")
        rows = h.runtime.db.query_all("SELECT * FROM seed_allocations ORDER BY draw_id")
        self.assertEqual(len(rows), 4)
        self.assertEqual(len({int(row["seed"]) for row in rows}), 4)
        self.assertEqual({row["node_id"] for row in rows}, {"2", "20"})
        h.runtime.service.start_job(job["id"])
        final = h.wait_job(job["id"], timeout=30)
        self.assertEqual(final["status"], "COMPLETED", final)
        # Every DRAW still stores its full node+input -> seed mapping.
        draws = h.runtime.db.query_all("SELECT id FROM draws")
        for draw in draws:
            mapping = h.runtime.repo.seed_rows(draw["id"])
            self.assertEqual({row["node_id"] for row in mapping}, {"2", "20"})

    async def test_second_runtime_same_data_root_is_locked(self):
        h = await self.harness()
        second = Runtime(
            data_root=h.data_root,
            results_root=h.results_root,
            comfy_input_root=h.input_root,
            comfy_output_root=h.output_root,
            engine_factory=lambda: h.engine,
            api_engine_factory=lambda: h.engine,
        )
        with self.assertRaises(GachaError) as ctx:
            await second.ensure_started()
        self.assertEqual(ctx.exception.code, "INSTANCE_LOCKED")
        # The first instance remains healthy.
        self.assertTrue(h.runtime.started)
        self.assertEqual(h.runtime.service.health()["db"]["ok"], True)

    def test_file_lock_is_cross_process(self):
        import shutil
        import subprocess
        import time

        root = tempfile.mkdtemp(prefix="gacha-lock-")
        try:
            lock_path = os.path.join(root, "gacha.lock")
            ready = os.path.join(root, "ready")
            script = (
                "import time, sys; from comfyui_gacha_master.backend.util import FileLock; "
                f"l=FileLock(r'{lock_path}'); assert l.acquire(); "
                f"open(r'{ready}','w').write('1'); time.sleep(8)"
            )
            process = subprocess.Popen([sys.executable, "-c", script], cwd=ROOT)
            try:
                for _ in range(100):
                    if os.path.exists(ready):
                        break
                    time.sleep(0.05)
                self.assertTrue(os.path.exists(ready), "child did not acquire lock")
                second = FileLock(lock_path)
                self.assertFalse(second.acquire())
            finally:
                process.terminate()
                process.wait(timeout=10)
                # Windows may release the terminated child's handle a moment
                # after wait() returns; retry the cleanup instead of failing
                # the assertion on filesystem timing.
                for _ in range(100):
                    try:
                        os.unlink(lock_path)
                        break
                    except FileNotFoundError:
                        break
                    except PermissionError:
                        time.sleep(0.05)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    async def test_backup_rotation_restore_and_rollback(self):
        h = await self.harness(settings_overrides={"backup_retention": 2})
        job = h.create_job(count=1, names=("a.png",))
        for index in range(4):
            h.runtime.service.create_backup(f"manual-{index}")
        verified = [row for row in h.runtime.service.list_backups() if row["status"] == "VERIFIED"]
        self.assertEqual(len(verified), 2)
        files = [name for name in os.listdir(h.runtime.paths.backups_root) if name.endswith(".sqlite3") and not name.startswith("pre-restore")]
        self.assertEqual(len(files), 2)
        # Prepare a restart checkpoint then modify with an append, then restore.
        backup = verified[0]
        detail = h.runtime.service.job_detail(job["id"])
        source = h.runtime.service.list_sources(job["id"])["items"][0]
        h.runtime.service.append_block(source["id"], count=1, priority="tail", expected_version=detail["row_version"])
        self.assertEqual(h.runtime.service.job_detail(job["id"])["plan_version"], 2)
        restored = h.runtime.service.restore_backup(backup["id"])
        self.assertTrue(os.path.exists(restored["pre_restore_path"]))
        restored_job = h.runtime.service.job_detail(job["id"])
        self.assertEqual(restored_job["plan_version"], 1)
        self.assertEqual(restored_job["run_intent"], "STOPPED")
        self.assertEqual(restored_job["status"], "READY")

    async def test_newer_schema_is_refused_without_writing(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "newer.sqlite3")
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT, checksum TEXT)")
            conn.execute("INSERT INTO schema_migrations VALUES (2, 'test', 'x')")
            conn.commit()
            conn.close()
            db = Database(path)
            with self.assertRaises(GachaError) as ctx:
                db.open()
            self.assertEqual(ctx.exception.code, "DB_READ_ONLY")
            # The file was not replaced or rebuilt.
            conn = sqlite3.connect(path)
            self.assertEqual(conn.execute("SELECT version FROM schema_migrations").fetchone()[0], 2)
            conn.close()

    async def test_failed_initial_migration_is_atomic(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "half.sqlite3")
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE idx_blocks_job(x INTEGER)")
            conn.commit()
            conn.close()
            db = Database(path)
            with self.assertRaises(GachaError) as ctx:
                db.open()
            self.assertEqual(ctx.exception.code, "DB_CORRUPT")
            conn = sqlite3.connect(path)
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            conn.close()
            self.assertIn("idx_blocks_job", tables)
            self.assertNotIn("jobs", tables)
            self.assertNotIn("schema_migrations", tables)

    async def test_readonly_db_open_is_classified_db_read_only(self):
        class ReadOnlyConn:
            row_factory = None

            def execute(self, *args, **kwargs):
                raise sqlite3.DatabaseError("attempt to write a readonly database")

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as root:
            db = Database(os.path.join(root, "readonly.sqlite3"))
            with patch("comfyui_gacha_master.backend.db.sqlite3.connect", return_value=ReadOnlyConn()):
                with self.assertRaises(GachaError) as ctx:
                    db.open()
            self.assertEqual(ctx.exception.code, "DB_READ_ONLY")
            self.assertIsNone(db._conn)

    async def test_old_events_or_responses_cannot_rewind_states(self):
        h = await self.harness()
        job = h.create_job(count=1, names=("a.png",))
        h.runtime.service.start_job(job["id"])
        final = h.wait_job(job["id"], timeout=20)
        self.assertEqual(final["status"], "COMPLETED")
        attempt = h.runtime.db.query_one("SELECT * FROM attempts")
        with self.assertRaises(GachaError) as ctx:
            h.runtime.repo.update_attempt(attempt["id"], status="RUNNING")
        self.assertEqual(ctx.exception.code, "STATE_CONFLICT")
        draw = h.runtime.repo.get_draw(attempt["draw_id"])
        with self.assertRaises(GachaError) as ctx:
            h.runtime.repo.set_draw_status(draw["id"], "PENDING")
        self.assertEqual(ctx.exception.code, "STATE_CONFLICT")
        self.assertEqual(h.runtime.repo.get_draw(draw["id"])["status"], "SUCCEEDED")

    async def test_disk_low_blocks_before_submit(self):
        h = await self.harness()
        job = h.create_job(count=1, names=("a.png",))
        error = GachaError("DISK_LOW", "injected disk low")
        with patch("comfyui_gacha_master.backend.scheduler.require_disk_space", side_effect=error):
            h.runtime.service.start_job(job["id"])
            blocked = h.wait_job(job["id"], statuses=("BLOCKED",), timeout=15)
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertEqual(blocked["summary"]["succeeded"], 0)
        self.assertEqual(h.engine.submissions, [])
        self.assertIsNone(h.runtime.repo.get_slot()["attempt_id"])

    async def test_readonly_results_dir_blocks_before_submit(self):
        h = await self.harness()
        job = h.create_job(count=1, names=("a.png",))
        error = GachaError("READ_ONLY", "injected read-only results dir")
        with patch("comfyui_gacha_master.backend.scheduler.require_writable_dir", side_effect=error):
            h.runtime.service.start_job(job["id"])
            blocked = h.wait_job(job["id"], statuses=("BLOCKED",), timeout=15)
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertEqual(blocked["summary"]["succeeded"], 0)
        self.assertEqual(h.engine.submissions, [])
        self.assertIsNone(h.runtime.repo.get_slot()["attempt_id"])

    async def test_folder_scan_does_not_follow_symlinked_files(self):
        h = await self.harness()
        outside = os.path.join(h.root, "outside.png")
        make_png(outside)
        link = os.path.join(h.input_root, "linked.png")
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation not permitted on this Windows account")
        scan = h.runtime.inputs.scan_folder(h.input_root)
        self.assertNotIn("linked.png", [item["name"] for item in scan["items"]])

    async def test_recovery_marks_missing_and_corrupt_completed_outputs(self):
        h = await self.harness(outputs_per_attempt=2)
        job = h.create_job(count=1, names=("a.png",))
        h.runtime.service.start_job(job["id"])
        final = h.wait_job(job["id"], timeout=20)
        self.assertEqual(final["status"], "COMPLETED")
        source = h.runtime.service.list_sources(job["id"])["items"][0]
        outputs = h.runtime.service.list_outputs(source["id"])["items"]
        first = h.runtime.repo.get_output(outputs[0]["id"])
        second = h.runtime.repo.get_output(outputs[1]["id"])
        os.unlink(os.path.join(h.runtime.paths.results_root, *str(first["rel_path"]).split("/")))
        with open(os.path.join(h.runtime.paths.results_root, *str(second["rel_path"]).split("/")), "ab") as handle:
            handle.write(b"corrupt")
        report = h.runtime.recovery.verify_completed_outputs(h.runtime.repo.get_job(job["id"]))
        self.assertEqual(report["checked"], 2)
        self.assertEqual(report["missing"], 1)
        self.assertEqual(report["corrupt"], 1)
        self.assertEqual(h.runtime.repo.get_output(first["id"])["health"], "MISSING")
        self.assertEqual(h.runtime.repo.get_output(second["id"])["health"], "CORRUPT")
        # Completion history is retained; no automatic redraw/count change.
        self.assertEqual(h.runtime.service.job_detail(job["id"])["summary"]["succeeded"], 1)

    def _simulate_paused_creation(self, h, job):
        """Turn a READY plan into a genuine CREATING/PAUSED fixture.

        The facts already exist, so continuing must reuse the same sources,
        DRAW ids and seeds rather than allocate a second plan.
        """
        h.runtime.repo.update_job(job["id"], status="CREATING", actor="test", reason="simulate crash")
        with h.runtime.db.tx() as conn:
            conn.execute(
                "UPDATE creation_progress SET stage='PAUSED', control_intent='STOP', "
                "generation=generation+1, progress_version=progress_version+1 WHERE job_id=?",
                (job["id"],),
            )

    async def test_creating_job_can_resume_or_be_abandoned(self):
        h = await self.harness()
        job = h.create_job(count=1, names=("a.png",))
        original_seeds = {
            row["draw_id"]: int(row["seed"])
            for row in h.runtime.db.query_all("SELECT * FROM seed_allocations")
        }
        self._simulate_paused_creation(h, job)
        resumed = h.runtime.service.start_job(job["id"])
        self.assertEqual(resumed["status"], "CREATING")
        self.assertEqual(resumed["creation"]["stage"], "WAITING")
        restored = h.wait_creation(job["id"])
        self.assertEqual(restored["status"], "READY")
        self.assertEqual(restored["summary"]["total_planned"], 1)
        self.assertEqual(
            original_seeds,
            {
                row["draw_id"]: int(row["seed"])
                for row in h.runtime.db.query_all("SELECT * FROM seed_allocations")
            },
        )
        job2 = h.create_job(count=1, names=("b.png",))
        self._simulate_paused_creation(h, job2)
        abandoned = h.runtime.service.abandon_creation(job2["id"])
        self.assertEqual(abandoned["status"], "BLOCKED")
        self.assertIn("放弃创建", abandoned["error_message"])
        self.assertEqual(h.runtime.repo.get_job(job2["id"])["status"], "BLOCKED")
        self.assertEqual(h.runtime.repo.get_creation(job2["id"])["stage"], "ABANDONED")

    async def test_folder_scan_reports_invalid_files_without_silently_skipping(self):
        h = await self.harness()
        make_png(os.path.join(h.input_root, "good.png"))
        with open(os.path.join(h.input_root, "broken.png"), "wb") as handle:
            handle.write(b"not a png")
        scan = h.runtime.inputs.scan_folder(h.input_root)
        self.assertEqual(scan["valid_count"], 1)
        self.assertEqual(scan["invalid_count"], 1)
        invalid = [item for item in scan["items"] if not item["valid"]][0]
        self.assertEqual(invalid["name"], "broken.png")
        self.assertEqual(invalid["error"]["code"], "UNSUPPORTED_MEDIA")
        self.assertFalse(scan["recursive"])


if __name__ == "__main__":
    unittest.main()
