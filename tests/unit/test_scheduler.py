import asyncio
import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.join(ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tests"))

from harness import Harness


async def wait_until(predicate, timeout=20.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(interval)
    return predicate()


def attempt_order(harness):
    rows = harness.runtime.db.query_all(
        "SELECT s.source_order, d.draw_no, a.attempt_no, a.id FROM attempts a "
        "JOIN draws d ON d.id=a.draw_id JOIN sources s ON s.id=d.source_id ORDER BY a.rowid"
    )
    return [(int(row["source_order"]), int(row["draw_no"])) for row in rows]


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def harness(self, **kwargs):
        harness = Harness(**kwargs)
        await harness.start()
        self.addAsyncCleanup(harness.cleanup)
        return harness

    async def test_3x5_strict_order_seeds_and_editor_isolation(self):
        h = await self.harness()
        job = h.create_job(count=5, names=("a.png", "b.png", "c.png"))
        before = {}
        for name in ("a.png", "b.png", "c.png"):
            path = os.path.join(h.input_root, name)
            before[name] = open(path, "rb").read()
        workflow_before = open(os.path.join(h.data_root, "jobs", job["id"], "workflow_api.json"), "rb").read()
        h.runtime.service.start_job(job["id"])
        final = h.wait_job(job["id"], timeout=40)
        self.assertEqual(final["status"], "COMPLETED", final)
        self.assertEqual(final["summary"]["succeeded"], 15)
        expected = [(1, i) for i in range(1, 6)] + [(2, i) for i in range(1, 6)] + [(3, i) for i in range(1, 6)]
        self.assertEqual(attempt_order(h), expected)
        seeds = h.runtime.db.query_all("SELECT job_id, seed FROM seed_allocations")
        self.assertEqual(len(seeds), 15)
        self.assertEqual(len({int(row["seed"]) for row in seeds}), 15)
        for name in ("a.png", "b.png", "c.png"):
            self.assertEqual(open(os.path.join(h.input_root, name), "rb").read(), before[name])
        workflow_after = open(os.path.join(h.data_root, "jobs", job["id"], "workflow_api.json"), "rb").read()
        self.assertEqual(workflow_after, workflow_before)
        self.assertEqual(h.runtime.repo.get_slot()["attempt_id"], None)

    async def test_multi_out_success_and_partial_save_failure(self):
        h = await self.harness(outputs_per_attempt=4)
        job = h.create_job(count=1, names=("a.png",))
        h.runtime.service.start_job(job["id"])
        final = h.wait_job(job["id"], timeout=20)
        self.assertEqual(final["status"], "COMPLETED")
        self.assertEqual(final["summary"]["succeeded"], 1)
        self.assertEqual(final["summary"]["out_available"], 4)
        self.assertEqual(final["summary"]["out_total"], 4)

        h2 = await self.harness(outputs_per_attempt=4)
        h2.engine.fail_save_at = 2
        job2 = h2.create_job(count=1, names=("a.png",))
        h2.runtime.service.start_job(job2["id"])
        failed = h2.wait_job(job2["id"], timeout=20)
        self.assertEqual(failed["status"], "BLOCKED", failed)
        self.assertEqual(failed["summary"]["succeeded"], 0)
        self.assertEqual(failed["summary"]["failed"], 1)
        self.assertEqual(failed["summary"]["out_available"], 0)
        self.assertEqual(failed["summary"]["out_total"], 2)
        self.assertEqual(len(h2.engine.submissions), 1)
        attempt_dir = h2.runtime.db.query_one("SELECT attempt_dir FROM attempts").__getitem__(0)
        self.assertTrue(os.path.exists(os.path.join(attempt_dir, "OUT-0001.png")))
        self.assertTrue(os.path.exists(os.path.join(attempt_dir, "OUT-0002.png")))
        self.assertFalse(os.path.exists(os.path.join(attempt_dir, "outputs.complete.json")))
        source = h2.runtime.service.list_sources(job2["id"])["items"][0]
        self.assertEqual(len(h2.runtime.service.list_outputs(source["id"])["items"]), 0)
        abnormal = h2.runtime.service.list_outputs(source["id"], include_unhealthy=True)["items"]
        self.assertEqual(len(abnormal), 2)
        self.assertTrue(all(item["health"] == "PARTIAL" for item in abnormal))

    async def test_safe_pause_finishes_current_then_resumes(self):
        h = await self.harness(auto_complete=False)
        job = h.create_job(count=2, names=("a.png", "b.png"))
        h.runtime.service.start_job(job["id"])
        await wait_until(lambda: len(h.engine.submissions) >= 1, timeout=10)
        result = h.runtime.service.pause_job(job["id"], mode="safe")
        self.assertIn(result["stage"], ("waiting_current",))
        h.engine.auto_complete = True
        paused = h.wait_job(job["id"], statuses=("PAUSED", "COMPLETED", "BLOCKED"), timeout=15)
        self.assertEqual(paused["status"], "PAUSED", paused)
        self.assertGreaterEqual(paused["summary"]["succeeded"], 1)
        self.assertGreater(paused["summary"]["pending"], 0)
        h.runtime.service.start_job(job["id"])
        final = h.wait_job(job["id"], timeout=20)
        self.assertEqual(final["status"], "COMPLETED")

    async def test_immediate_pause_targeted_cancel_isolated(self):
        h = await self.harness(auto_complete=False, targeted_cancel=True)
        h.engine.add_external("external-task", "queued")
        job = h.create_job(count=2, names=("a.png", "b.png"))
        h.runtime.service.start_job(job["id"])
        await wait_until(lambda: len(h.engine.submissions) >= 1, timeout=10)
        result = h.runtime.service.pause_job(job["id"], mode="now")
        self.assertEqual(result["stage"], "cancel_requested")
        paused = h.wait_job(job["id"], statuses=("PAUSED", "COMPLETED", "BLOCKED"), timeout=15)
        self.assertEqual(paused["status"], "PAUSED", paused)
        self.assertEqual(len(h.engine.cancel_calls), 1)
        self.assertEqual(h.engine.cancel_calls[0], h.engine.submissions[0]["expected_prompt_id"])
        self.assertEqual(h.engine.external_jobs["external-task"].state, "queued")
        self.assertEqual(len(h.engine.submissions), 1)
        self.assertEqual(paused["summary"]["failed"], 1)

    async def test_immediate_pause_queued_cancel_acknowledged_settles_paused(self):
        h = await self.harness(auto_complete=False, targeted_cancel=True)
        h.engine.auto_run = False
        h.engine.cancel_removes_pending = True
        job = h.create_job(count=1, names=("a.png",))
        h.runtime.service.start_job(job["id"])
        await wait_until(lambda: len(h.engine.submissions) >= 1, timeout=10)
        result = h.runtime.service.pause_job(job["id"], mode="now")
        self.assertEqual(result["stage"], "cancel_requested")
        paused = h.wait_job(job["id"], statuses=("PAUSED", "COMPLETED", "BLOCKED", "RECOVERY_REQUIRED"), timeout=15)
        self.assertEqual(paused["status"], "PAUSED", paused)
        self.assertEqual(h.engine.cancel_calls, [h.engine.submissions[0]["expected_prompt_id"]])
        attempt = h.runtime.db.query_one("SELECT status, remote_state FROM attempts")
        self.assertEqual(attempt["status"], "INTERRUPTED")
        self.assertEqual(attempt["remote_state"], "cancel_acknowledged")
        self.assertIsNone(h.runtime.repo.get_slot()["attempt_id"])
        self.assertEqual(len(h.engine.submissions), 1)

    async def test_immediate_pause_degrades_without_targeted_cancel(self):
        h = await self.harness(auto_complete=False, targeted_cancel=False)
        job = h.create_job(count=2, names=("a.png", "b.png"))
        h.runtime.service.start_job(job["id"])
        await wait_until(lambda: len(h.engine.submissions) >= 1, timeout=10)
        result = h.runtime.service.pause_job(job["id"], mode="now")
        self.assertEqual(result["stage"], "degraded_waiting_current")
        self.assertIn("无法安全定向中断", result["message"])
        h.engine.auto_complete = True
        paused = h.wait_job(job["id"], statuses=("PAUSED", "COMPLETED", "BLOCKED"), timeout=15)
        self.assertEqual(paused["status"], "PAUSED")
        self.assertEqual(h.engine.cancel_calls, [])
        self.assertEqual(len(h.engine.submissions), 1)

    async def test_dropped_response_never_resubmits_and_recovers(self):
        h = await self.harness(auto_complete=True)
        job = h.create_job(count=1, names=("a.png", "b.png"))
        h.engine.drop_response_next = True
        h.runtime.service.start_job(job["id"])
        recovered = h.wait_job(job["id"], statuses=("RECOVERY_REQUIRED",), timeout=15)
        self.assertEqual(recovered["status"], "RECOVERY_REQUIRED")
        self.assertEqual(len(h.engine.submissions), 1)
        self.assertIsNotNone(h.runtime.repo.get_slot()["attempt_id"])
        # The scheduler keeps monitoring the same attempt and finds its success.
        await wait_until(lambda: h.runtime.service.job_detail(job["id"])["summary"]["succeeded"] == 1, timeout=15)
        self.assertEqual(len(h.engine.submissions), 1)
        self.assertIsNone(h.runtime.repo.get_slot()["attempt_id"])
        detail = h.runtime.service.job_detail(job["id"])
        h.runtime.service.start_job(
            job["id"], acknowledge_recovery=True, recovery_version=detail["recovery_version"]
        )
        final = h.wait_job(job["id"], timeout=20)
        self.assertEqual(final["status"], "COMPLETED")
        self.assertEqual(len(h.engine.submissions), 2)

    async def test_dropped_response_without_client_id_stays_unknown(self):
        h = await self.harness(auto_complete=True, honor_client_id=False)
        job = h.create_job(count=1, names=("a.png",))
        h.engine.drop_response_next = True
        h.runtime.service.start_job(job["id"])
        detail = h.wait_job(job["id"], statuses=("RECOVERY_REQUIRED",), timeout=15)
        self.assertEqual(detail["status"], "RECOVERY_REQUIRED")
        self.assertEqual(detail["summary"]["unknown"], 1)
        self.assertEqual(len(h.engine.submissions), 1)
        await asyncio.sleep(0.5)
        self.assertEqual(len(h.engine.submissions), 1)
        self.assertIsNotNone(h.runtime.repo.get_slot()["attempt_id"])

    async def test_failure_blocks_without_automatic_retry(self):
        h = await self.harness(auto_complete=True)
        job = h.create_job(count=1, names=("a.png", "b.png"))
        h.engine.fail_next = True
        h.runtime.service.start_job(job["id"])
        blocked = h.wait_job(job["id"], statuses=("BLOCKED",), timeout=15)
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertEqual(blocked["summary"]["failed"], 1)
        self.assertGreater(blocked["summary"]["pending"], 0)
        self.assertEqual(len(h.engine.submissions), 1)

    async def test_retry_preserves_seeds_and_creates_new_attempt(self):
        h = await self.harness(auto_complete=True)
        job = h.create_job(count=1, names=("a.png",))
        h.engine.fail_next = True
        h.runtime.service.start_job(job["id"])
        blocked = h.wait_job(job["id"], statuses=("BLOCKED",), timeout=15)
        self.assertEqual(blocked["status"], "BLOCKED")
        attempt = h.runtime.db.query_one("SELECT * FROM attempts")
        seeds_before = h.runtime.repo.seed_rows(attempt["draw_id"])
        await h.runtime.recovery.resolve(str(attempt["id"]), h.engine, decision="retry", reason="test retry", confirm_inactive=True)
        h.runtime.service.start_job(job["id"])
        final = h.wait_job(job["id"], timeout=20)
        self.assertEqual(final["status"], "COMPLETED", final)
        attempts = h.runtime.repo.get_attempt_for_draw(str(attempt["draw_id"]))
        self.assertEqual(len(attempts), 2)
        self.assertNotEqual(attempts[0]["id"], attempts[1]["id"])
        seeds_after = h.runtime.repo.seed_rows(str(attempt["draw_id"]))
        self.assertEqual([row["seed"] for row in seeds_before], [row["seed"] for row in seeds_after])

    async def test_single_inflight_and_no_parallel_submissions(self):
        h = await self.harness(auto_complete=False)
        job = h.create_job(count=2, names=("a.png", "b.png", "c.png"))
        h.runtime.service.start_job(job["id"])
        await wait_until(lambda: len(h.engine.submissions) >= 1, timeout=10)
        await asyncio.sleep(0.4)
        self.assertEqual(len(h.engine.submissions), 1)
        active = [attempt for attempt in h.runtime.repo.list_active_attempts() if attempt["status"] in ("DISPATCHING", "QUEUED", "RUNNING")]
        self.assertEqual(len(active), 1)
        self.assertIsNotNone(h.runtime.repo.get_slot()["attempt_id"])
        h.engine.auto_complete = True
        final = h.wait_job(job["id"], statuses=("COMPLETED",), timeout=30)
        self.assertEqual(final["status"], "COMPLETED")

    async def test_append_priority_tail_and_idempotent_key(self):
        h = await self.harness(auto_complete=False)
        job = h.create_job(count=3, names=("a.png", "b.png"))
        h.runtime.service.start_job(job["id"])
        await wait_until(lambda: len(h.engine.submissions) >= 1, timeout=10)
        h.runtime.service.pause_job(job["id"], mode="safe")
        h.engine.auto_complete = True
        paused = h.wait_job(job["id"], statuses=("PAUSED",), timeout=15)
        self.assertEqual(paused["status"], "PAUSED")
        sources = h.runtime.service.list_sources(job["id"])["items"]
        detail = h.runtime.service.job_detail(job["id"])
        first = h.runtime.service.append_block(
            sources[1]["id"], count=2, priority="priority", expected_version=detail["row_version"], idempotency_key="append-key"
        )
        second = h.runtime.service.append_block(
            sources[1]["id"], count=2, priority="priority", expected_version=detail["row_version"], idempotency_key="append-key"
        )
        self.assertEqual(first["block_id"], second["block_id"])
        self.assertEqual(first["draw_start"], 4)
        self.assertEqual(first["draw_end"], 5)
        self.assertEqual(h.runtime.db.query_one("SELECT COUNT(*) FROM draws WHERE block_id=?", (first["block_id"],))[0], 2)
        h.runtime.service.start_job(job["id"])
        final = h.wait_job(job["id"], timeout=30)
        self.assertEqual(final["status"], "COMPLETED")
        order = attempt_order(h)
        # The priority append runs before the remaining initial B1-B3.
        self.assertEqual(order[:2], [(1, 1), (2, 4)])

    async def test_started_append_block_keeps_continuity(self):
        h = await self.harness(auto_complete=False)
        job = h.create_job(count=3, names=("a.png", "b.png"))
        h.runtime.service.start_job(job["id"])
        await wait_until(lambda: len(h.engine.submissions) >= 1, timeout=10)
        h.runtime.service.pause_job(job["id"], mode="safe")
        h.engine.auto_complete = True
        h.wait_job(job["id"], statuses=("PAUSED",), timeout=15)
        sources = h.runtime.service.list_sources(job["id"])["items"]
        detail = h.runtime.service.job_detail(job["id"])
        a = h.runtime.service.append_block(
            sources[0]["id"], count=3, priority="priority", expected_version=detail["row_version"], idempotency_key="cont-a"
        )
        # Hold the next attempt open so the second append provably lands while
        # the job is still running; otherwise the fast fake engine can finish
        # the whole block first and the append correctly falls back to PAUSED.
        h.engine.auto_complete = False
        h.runtime.service.start_job(job["id"])
        await wait_until(
            lambda: (h.runtime.db.query_one("SELECT status FROM draws WHERE block_id=?", (a["block_id"],)) or {})["status"]
            in ("DISPATCHING", "QUEUED", "RUNNING"),
            timeout=15,
        )
        detail = h.runtime.service.job_detail(job["id"])
        self.assertEqual(detail["status"], "RUNNING", detail)
        b = h.runtime.service.append_block(
            sources[1]["id"], count=2, priority="priority", expected_version=detail["row_version"], idempotency_key="cont-b"
        )
        h.engine.auto_complete = True
        final = h.wait_job(job["id"], timeout=30)
        self.assertEqual(final["status"], "COMPLETED", final)
        sequence = attempt_order(h)
        self.assertEqual(sequence[:6], [(1, 1), (1, 4), (1, 5), (1, 6), (2, 4), (2, 5)])
        self.assertEqual(sequence[6:], [(1, 2), (1, 3), (2, 1), (2, 2), (2, 3)])

    async def test_remove_unexecuted_source_and_preserve_executed(self):
        h = await self.harness(auto_complete=True)
        job = h.create_job(count=1, names=("a.png", "b.png"))
        sources = h.runtime.service.list_sources(job["id"])["items"]
        detail = h.runtime.service.job_detail(job["id"])
        removed = h.runtime.service.remove_source(sources[1]["id"], expected_version=detail["row_version"])
        self.assertEqual(removed["status"], "REMOVED_BY_USER")
        remaining = h.runtime.service.list_sources(job["id"])["items"]
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["source_order"], 1)
        h.runtime.service.start_job(job["id"])
        final = h.wait_job(job["id"], timeout=20)
        self.assertEqual(final["status"], "COMPLETED")
        self.assertEqual(final["summary"]["cancelled"], 1)
        self.assertEqual(final["summary"]["effective"], 1)
        with self.assertRaises(Exception):
            h.runtime.service.remove_source(sources[0]["id"], expected_version=h.runtime.service.job_detail(job["id"])["row_version"])

    async def test_reference_mode_detects_changed_input_and_snapshot_does_not(self):
        h = await self.harness(auto_complete=True)
        snapshot_job = h.create_job(count=1, names=("a.png",), protection_mode="snapshot")
        reference_job = h.create_job(count=1, names=("b.png",), protection_mode="reference")
        # Snapshot survives deleting the original.
        os.unlink(os.path.join(h.input_root, "a.png"))
        h.runtime.service.start_job(snapshot_job["id"])
        self.assertEqual(h.wait_job(snapshot_job["id"], timeout=20)["status"], "COMPLETED")
        # Reference detects a content change at start.
        from harness import make_png

        make_png(os.path.join(h.input_root, "b.png"), color=(1, 2, 3))
        with self.assertRaises(Exception) as ctx:
            h.runtime.service.start_job(reference_job["id"])
        self.assertIn(getattr(ctx.exception, "code", ""), ("INPUT_CHANGED", "INPUT_MISSING"))


if __name__ == "__main__":
    unittest.main()
