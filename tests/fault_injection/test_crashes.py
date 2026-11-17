import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PYTHON = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
if not os.path.exists(PYTHON):
    PYTHON = sys.executable
RUNNER = os.path.join(ROOT, "tests", "fault_injection", "run_job.py")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.join(ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tests"))

from harness import Harness


class FaultInjectionTests(unittest.IsolatedAsyncioTestCase):
    def run_fault(self, fault: str, mode: str = "job"):
        root = tempfile.mkdtemp(prefix=f"gacha-fault-{fault}-")
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        log = os.path.join(root, "fault.log")
        env = dict(os.environ)
        env["GACHA_FAULT"] = fault
        env["GACHA_FAULT_LOG"] = log
        process = subprocess.run(
            [PYTHON, RUNNER, root, mode],
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        self.assertEqual(process.returncode, 91, process.stderr[-2000:])
        self.assertTrue(os.path.exists(log), process.stderr[-2000:])
        state = json.load(open(os.path.join(root, "state.json"), encoding="utf-8"))
        return root, state

    async def reopen(self, root):
        harness = Harness(root=root, settings_overrides={"backup_draws": 1000000, "backup_interval": 1000000})
        await harness.start()
        self.addAsyncCleanup(harness.cleanup)
        return harness

    async def test_f01_claim_transaction_rolls_back(self):
        root, state = self.run_fault("F01")
        h = await self.reopen(root)
        self.assertEqual(len(h.runtime.repo.list_active_attempts()), 0)
        self.assertIsNone(h.runtime.repo.get_slot()["attempt_id"])
        detail = h.runtime.service.job_detail(state["job_id"])
        self.assertEqual(detail["status"], "RECOVERY_REQUIRED")
        self.assertEqual(detail["summary"]["pending"], 2)
        h.runtime.service.start_job(
            state["job_id"], acknowledge_recovery=True, recovery_version=detail["recovery_version"]
        )
        final = h.wait_job(state["job_id"], timeout=30)
        self.assertEqual(final["status"], "COMPLETED", final)

    async def test_f02_dispatching_without_send_proof_is_unknown(self):
        root, state = self.run_fault("F02")
        h = await self.reopen(root)
        detail = h.runtime.service.job_detail(state["job_id"])
        self.assertEqual(detail["status"], "RECOVERY_REQUIRED")
        self.assertEqual(detail["summary"]["unknown"], 1)
        self.assertIsNotNone(h.runtime.repo.get_slot()["attempt_id"])
        self.assertEqual(h.engine.submissions, [])
        attempt = h.runtime.repo.list_unknown_attempts()[0]
        seeds_before = h.runtime.repo.seed_rows(attempt["draw_id"])
        await h.runtime.recovery.resolve(
            attempt["id"], h.engine, decision="retry", reason="F02", confirm_inactive=True
        )
        h.runtime.service.start_job(state["job_id"])
        final = h.wait_job(state["job_id"], timeout=30)
        self.assertEqual(final["status"], "COMPLETED", final)
        seeds_after = h.runtime.repo.seed_rows(attempt["draw_id"])
        self.assertEqual([row["seed"] for row in seeds_before], [row["seed"] for row in seeds_after])

    async def test_f04_and_f05_partial_files_are_not_success(self):
        for fault in ("F04", "F05"):
            root, state = self.run_fault(fault)
            h = await self.reopen(root)
            detail = h.runtime.service.job_detail(state["job_id"])
            self.assertEqual(detail["status"], "RECOVERY_REQUIRED", fault)
            self.assertEqual(detail["summary"]["succeeded"], 0, fault)
            self.assertEqual(detail["summary"]["unknown"], 1, fault)
            attempt = h.runtime.repo.list_unknown_attempts()[0]
            files = os.listdir(attempt["attempt_dir"])
            if fault == "F05":
                self.assertIn("OUT-0001.png", files)
                self.assertNotIn("outputs.complete.json", files)
            self.assertIsNotNone(h.runtime.repo.get_slot()["attempt_id"], fault)

    async def test_f06_manifest_without_terminal_is_unknown(self):
        root, state = self.run_fault("F06")
        h = await self.reopen(root)
        detail = h.runtime.service.job_detail(state["job_id"])
        self.assertEqual(detail["status"], "RECOVERY_REQUIRED")
        self.assertEqual(detail["summary"]["succeeded"], 0)
        self.assertEqual(detail["summary"]["unknown"], 1)
        attempt = h.runtime.repo.list_unknown_attempts()[0]
        self.assertEqual(attempt["remote_state"], "files_found_terminal_unknown")
        self.assertTrue(os.path.isfile(os.path.join(attempt["attempt_dir"], "outputs.complete.json")))
        self.assertFalse(os.path.exists(os.path.join(h.data_root, "jobs", state["job_id"], "receipts", attempt["id"], "terminal.json")))

    async def test_f07_receipt_backfills_success_without_repst(self):
        root, state = self.run_fault("F07")
        h = await self.reopen(root)
        detail = h.runtime.service.job_detail(state["job_id"])
        self.assertEqual(detail["summary"]["succeeded"], 1)
        self.assertEqual(detail["summary"]["unknown"], 0)
        self.assertEqual(h.engine.submissions, [])
        self.assertIsNone(h.runtime.repo.get_slot()["attempt_id"])

    async def test_f08_committed_success_is_counted_once(self):
        root, state = self.run_fault("F08")
        h = await self.reopen(root)
        detail = h.runtime.service.job_detail(state["job_id"])
        self.assertEqual(detail["summary"]["succeeded"], 1)
        self.assertIsNone(h.runtime.repo.get_slot()["attempt_id"])
        self.assertEqual(len(h.runtime.repo.list_active_attempts()), 0)

    async def test_f09_append_transaction_rolls_back(self):
        root, state = self.run_fault("F09", mode="append")
        h = await self.reopen(root)
        job = h.runtime.service.job_detail(state["job_id"])
        self.assertEqual(job["plan_version"], 1)
        self.assertEqual(h.runtime.db.query_one("SELECT COUNT(*) FROM draws")[0], 2)
        self.assertEqual(h.runtime.db.query_one("SELECT COUNT(*) FROM blocks WHERE kind='APPEND'")[0], 0)

    async def test_f10_f11_export_resume_never_overwrites(self):
        for fault in ("F10", "F11"):
            root, state = self.run_fault(fault, mode="export")
            h = await self.reopen(root)
            export = h.runtime.exports.get_export(state["export_id"])
            self.assertEqual(export["status"], "COPYING", fault)
            before = None
            if fault == "F11":
                target = os.path.join(export["batch_dir"], *export["items"][0]["target_rel_path"].split("/"))
                before = open(target, "rb").read()
            resumed = h.runtime.exports.resume(state["export_id"])
            self.assertEqual(resumed["status"], "COMPLETED", fault)
            self.assertEqual(resumed["items"][0]["status"], "VERIFIED", fault)
            if before is not None:
                target = os.path.join(resumed["batch_dir"], *resumed["items"][0]["target_rel_path"].split("/"))
                self.assertEqual(open(target, "rb").read(), before)

    async def test_f12_half_backup_is_not_accepted(self):
        root, state = self.run_fault("F12", mode="backup")
        h = await self.reopen(root)
        backups = h.runtime.service.list_backups()
        self.assertTrue(all(backup["status"] == "VERIFIED" for backup in backups))
        backup = h.runtime.service.create_backup("after-crash")
        self.assertEqual(backup["status"], "VERIFIED")


if __name__ == "__main__":
    unittest.main()
