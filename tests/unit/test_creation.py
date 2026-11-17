import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PYTHON = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
if not os.path.exists(PYTHON):
    PYTHON = sys.executable
RUNNER = os.path.join(ROOT, "tests", "fault_injection", "run_job.py")
for path in (ROOT, os.path.join(ROOT, "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

from harness import Harness, workflow_fixture

FAST_SETTINGS = {"plan_insert_batch": 1, "backup_draws": 1000000, "backup_interval": 1000000}


class CreationPlanTests(unittest.IsolatedAsyncioTestCase):
    def run_fault(self, fault: str, *, mode: str = "create", count: int = 5, batch: int = 1):
        root = tempfile.mkdtemp(prefix=f"gacha-plan-{fault.replace(':', '_')}-")
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        log = os.path.join(root, "fault.log")
        env = dict(os.environ)
        env["GACHA_FAULT"] = fault
        env["GACHA_FAULT_LOG"] = log
        env["GACHA_CREATE_COUNT"] = str(count)
        env["GACHA_CREATE_BATCH"] = str(batch)
        process = subprocess.run(
            [PYTHON, RUNNER, root, mode], env=env, capture_output=True, text=True, timeout=120
        )
        self.assertEqual(process.returncode, 91, process.stderr[-3000:])
        self.assertTrue(os.path.exists(log), process.stderr[-3000:])
        with open(os.path.join(root, "state.json"), encoding="utf-8") as handle:
            state = json.load(handle)
        return root, state

    async def reopen(self, root, batch: int = 1):
        harness = Harness(
            root=root,
            settings_overrides={"plan_insert_batch": batch, "backup_draws": 1000000, "backup_interval": 1000000},
        )
        await harness.start()
        self.addAsyncCleanup(harness.cleanup)
        return harness

    def assert_seed_mapping(self, h, job_id, draw_count, sampler_nodes=1, count_per_source=None):
        draws = h.runtime.db.query_all(
            "SELECT * FROM draws WHERE job_id=? ORDER BY source_id, draw_no", (job_id,)
        )
        self.assertEqual(len(draws), draw_count)
        # DRAW identity is unique per (source_id, draw_no); different sources
        # intentionally restart at draw_no 1.
        pairs = {(row["source_id"], int(row["draw_no"])) for row in draws}
        self.assertEqual(len(pairs), len(draws))
        if count_per_source is not None:
            for source_id in {row["source_id"] for row in draws}:
                numbers = sorted(
                    int(row["draw_no"]) for row in draws if row["source_id"] == source_id
                )
                self.assertEqual(numbers, list(range(1, count_per_source + 1)))
        seeds = h.runtime.db.query_all(
            "SELECT * FROM seed_allocations WHERE job_id=? ORDER BY draw_id, node_id", (job_id,)
        )
        self.assertEqual(len(seeds), draw_count * sampler_nodes)
        self.assertEqual(len({int(row["seed"]) for row in seeds}), len(seeds))
        for draw in draws:
            rows = h.runtime.repo.seed_rows(draw["id"])
            self.assertEqual(len(rows), sampler_nodes)
            self.assertEqual(len({(row["node_id"], row["input_name"]) for row in rows}), sampler_nodes)
            if count_per_source is not None:
                self.assertGreaterEqual(int(draw["draw_no"]), 1)
                self.assertLessEqual(int(draw["draw_no"]), count_per_source)
        return draws, seeds

    # ------------------------------------------------------------- PLAN01
    async def test_plan01_crash_before_and_after_batches_leaves_consistent_resume(self):
        for fault in ("F17_DRAW_0_1", "F18_DRAW_2", "F17_DRAW_4_1", "F19_5"):
            with self.subTest(fault=fault):
                root, state = self.run_fault(fault, count=5, batch=1)
                h = await self.reopen(root, batch=1)
                job = h.runtime.service.job_detail(state["job_id"])
                self.assertEqual(job["status"], "CREATING", fault)
                self.assertEqual(job["creation"]["stage"], "PAUSED", fault)
                committed = h.runtime.repo.count_draws(state["job_id"])
                self.assertEqual(committed, job["creation"]["draws_committed"], fault)
                expected = 5 if fault == "F19_5" else None
                if expected is None:
                    self.assertLess(committed, 5, fault)
                else:
                    self.assertEqual(committed, expected, fault)
                seeds = h.runtime.db.query_one(
                    "SELECT COUNT(*) FROM seed_allocations WHERE job_id=?", (state["job_id"],)
                )
                self.assertEqual(int(seeds[0]), committed, fault)
                self.assertEqual(job["run_intent"], "STOPPED")
                # Continue from committed facts; only missing DRAWs are made.
                resumed = h.runtime.service.start_job(state["job_id"])
                self.assertEqual(resumed["status"], "CREATING")
                ready = h.wait_creation(state["job_id"])
                self.assertEqual(ready["status"], "READY", ready)
                self.assertEqual(ready["summary"]["total_planned"], 5)
                draws, seeds_rows = self.assert_seed_mapping(h, state["job_id"], 5, 1, 5)
                self.assertEqual(sorted(int(row["draw_no"]) for row in draws), [1, 2, 3, 4, 5])

    # ------------------------------------------------------------- PLAN02
    async def test_plan02_concurrent_same_key_and_lost_response_cannot_duplicate(self):
        h = Harness(settings_overrides=FAST_SETTINGS)
        await h.start()
        self.addAsyncCleanup(h.cleanup)
        h.runtime.creation.stop()
        request = h.create_request(count=2, names=("a.png", "b.png"))
        barrier = __import__("threading").Barrier(2)

        def create():
            barrier.wait()
            return h.runtime.service.create_job(request, idempotency_key="plan02-key")

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: create(), range(2)))
        self.assertEqual(results[0]["id"], results[1]["id"])
        job_id = results[0]["id"]
        self.assertEqual(len(h.runtime.repo.all_jobs()), 1)
        # Registration does not copy sources; the worker does.
        self.assertEqual(len(h.runtime.repo.all_sources(job_id)), 0)
        self.assertEqual(h.runtime.repo.count_draws(job_id), 0)
        h.runtime.creation.start()
        ready = h.wait_creation(job_id)
        self.assertEqual(ready["status"], "READY")
        self.assertEqual(ready["summary"]["total_planned"], 4)
        self.assert_seed_mapping(h, job_id, 4, 1, 2)
        # Replay after completion returns the same identity and never adds a
        # second source set or plan.
        replay = h.runtime.service.create_job(request, idempotency_key="plan02-key")
        self.assertEqual(replay["id"], job_id)
        self.assertEqual(h.runtime.repo.count_draws(job_id), 4)
        self.assertEqual(len(h.runtime.repo.all_sources(job_id)), 2)

    # ------------------------------------------------------------- PLAN03
    async def test_plan03_pause_and_abandon_beat_late_worker_work(self):
        h = Harness(settings_overrides={"plan_insert_batch": 1, "backup_draws": 1000000, "backup_interval": 1000000})
        await h.start()
        self.addAsyncCleanup(h.cleanup)
        h.runtime.creation.stop()
        job = h.create_job(count=200, names=("a.png",), wait=False)
        h.runtime.creation.start()
        result = h.runtime.service.pause_job(job["id"], mode="safe")
        self.assertIn(result["stage"], ("pausing_creation", "paused_creation"))
        deadline = time.time() + 20
        while time.time() < deadline:
            progress = h.runtime.repo.get_creation(job["id"])
            if progress["stage"] == "PAUSED":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(h.runtime.repo.get_creation(job["id"])["stage"], "PAUSED")
        committed = h.runtime.repo.get_creation(job["id"])["draws_committed"]
        self.assertLess(committed, 200)
        self.assertEqual(h.runtime.repo.get_job(job["id"])["status"], "CREATING")
        # Late work cannot flip the job to READY after the stop intent.
        await asyncio.sleep(0.3)
        self.assertEqual(h.runtime.repo.get_job(job["id"])["status"], "CREATING")
        self.assertEqual(h.runtime.repo.get_creation(job["id"])["stage"], "PAUSED")
        resumed = h.runtime.service.start_job(job["id"])
        self.assertEqual(resumed["status"], "CREATING")
        ready = h.wait_creation(job["id"], timeout=120)
        self.assertEqual(ready["status"], "READY")
        self.assert_seed_mapping(h, job["id"], 200, 1, 200)

        # Abandon while the worker is active: after it settles the job stays
        # BLOCKED and can never be started as a plan or as inference.
        job2 = h.create_job(count=200, names=("b.png",), wait=False)
        abandoned = h.runtime.service.abandon_creation(job2["id"], reason="PLAN03")
        self.assertEqual(abandoned["status"], "BLOCKED")
        self.assertEqual(h.runtime.repo.get_creation(job2["id"])["stage"], "ABANDONED")
        await asyncio.sleep(0.3)
        self.assertEqual(h.runtime.repo.get_job(job2["id"])["status"], "BLOCKED")
        with self.assertRaises(Exception) as ctx:
            h.runtime.service.start_job(job2["id"])
        self.assertEqual(getattr(ctx.exception, "code", None), "STATE_CONFLICT")

    # ------------------------------------------------------------- PLAN04
    async def test_plan04_status_queries_and_pause_stay_responsive_during_planning(self):
        h = Harness(settings_overrides={"plan_insert_batch": 50, "backup_draws": 1000000, "backup_interval": 1000000})
        await h.start()
        self.addAsyncCleanup(h.cleanup)
        h.runtime.creation.stop()
        job = h.create_job(count=50000, names=("a.png",), wait=False)
        h.runtime.creation.start()
        latencies = []
        deadline = time.time() + 20
        while time.time() < deadline:
            start = time.perf_counter()
            detail = h.runtime.service.job_detail(job["id"])
            latencies.append(time.perf_counter() - start)
            if detail["status"] == "READY":
                break
            if len(latencies) >= 100 and detail["creation"]["draws_committed"] > 0:
                break
            await asyncio.sleep(0.005)
        self.assertGreaterEqual(len(latencies), 10)
        latencies.sort()
        p95 = latencies[int(len(latencies) * 0.95) - 1]
        self.assertLess(p95, 1.0, f"p95 status latency {p95:.3f}s")
        self.assertIsNone(h.runtime.repo.get_slot()["attempt_id"])
        start = time.perf_counter()
        result = h.runtime.service.pause_job(job["id"], mode="safe")
        pause_latency = time.perf_counter() - start
        self.assertLess(pause_latency, 1.0)
        self.assertIn(result["stage"], ("pausing_creation", "paused_creation"))
        deadline = time.time() + 20
        while time.time() < deadline:
            if h.runtime.repo.get_creation(job["id"])["stage"] == "PAUSED":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(h.runtime.repo.get_creation(job["id"])["stage"], "PAUSED")
        self.assertLess(h.runtime.repo.get_creation(job["id"])["draws_committed"], 50000)
        self.assertEqual(h.runtime.repo.get_job(job["id"])["run_intent"], "STOPPED")
        print(f"\nPLAN04 status p95 {p95 * 1000:.1f}ms, pause {pause_latency * 1000:.1f}ms")

    # ------------------------------------------------------------- PLAN05
    async def test_plan05_runtime_close_stops_worker_before_db_close(self):
        h = Harness(settings_overrides=FAST_SETTINGS)
        await h.start()
        root = h.root
        job = h.create_job(count=20000, names=("a.png",), wait=False)
        # Let the worker commit at least one batch before shutting down, while
        # ensuring the plan is still incomplete.
        deadline = time.time() + 20
        while time.time() < deadline:
            progress = h.runtime.repo.get_creation(job["id"])
            if progress and 0 < int(progress["draws_committed"]) < int(progress["draws_total"]):
                break
            time.sleep(0.005)
        h.runtime.close()
        self.assertIsNone(h.runtime.creation)
        # Reopening the same root must not auto-continue, and must see the
        # exact committed counters.
        h2 = await self.reopen(root, batch=1)
        reopened = h2.runtime.service.job_detail(job["id"])
        self.assertEqual(reopened["status"], "CREATING")
        self.assertEqual(reopened["creation"]["stage"], "PAUSED")
        self.assertEqual(
            h2.runtime.repo.count_draws(job["id"]),
            reopened["creation"]["draws_committed"],
        )
        # Do not spend the test budget finishing 20k DRAWs; abandon is the
        # explicit terminal path and must keep the partial evidence.
        abandoned = h2.runtime.service.abandon_creation(job["id"], reason="PLAN05 cleanup")
        self.assertEqual(abandoned["status"], "BLOCKED")
        self.assertEqual(h2.runtime.repo.get_creation(job["id"])["stage"], "ABANDONED")

    # ------------------------------------------------------------- PLAN06
    async def test_plan06_legacy_creating_job_is_adopted_paused_with_same_ids_and_seeds(self):
        h = Harness(settings_overrides=FAST_SETTINGS)
        await h.start()
        root = h.root
        job = h.create_job(count=2, names=("a.png", "b.png"))
        seeds_before = {
            (row["draw_id"], row["node_id"]): int(row["seed"])
            for row in h.runtime.db.query_all("SELECT * FROM seed_allocations")
        }
        draws_before = [row["id"] for row in h.runtime.db.query_all("SELECT * FROM draws ORDER BY id")]
        # Simulate a schema-v1 CREATING job with no creation_progress row.
        h.runtime.repo.update_job(job["id"], status="CREATING", actor="test", reason="legacy fixture")
        h.runtime.db.execute("DELETE FROM creation_progress WHERE job_id=?", (job["id"],))
        h.runtime.close()
        h2 = await self.reopen(root, batch=1)
        detail = h2.runtime.service.job_detail(job["id"])
        self.assertEqual(detail["status"], "CREATING")
        self.assertEqual(detail["creation"]["stage"], "PAUSED")
        self.assertEqual(detail["creation"]["sources_committed"], 2)
        self.assertEqual(detail["creation"]["draws_committed"], 4)
        resumed = h2.runtime.service.start_job(job["id"])
        self.assertEqual(resumed["status"], "CREATING")
        ready = h2.wait_creation(job["id"])
        self.assertEqual(ready["status"], "READY")
        self.assertEqual(
            [row["id"] for row in h2.runtime.db.query_all("SELECT * FROM draws ORDER BY id")],
            draws_before,
        )
        seeds_after = {
            (row["draw_id"], row["node_id"]): int(row["seed"])
            for row in h2.runtime.db.query_all("SELECT * FROM seed_allocations")
        }
        self.assertEqual(seeds_after, seeds_before)

    # ------------------------------------------------------------- PLAN07
    async def test_plan07_two_samplers_survive_crash_resume_per_draw(self):
        root, state = self.run_fault("F18_DRAW_2", mode="create2", count=4, batch=1)
        h = await self.reopen(root, batch=1)
        detail = h.runtime.service.job_detail(state["job_id"])
        self.assertEqual(detail["status"], "CREATING")
        self.assertEqual(detail["creation"]["stage"], "PAUSED")
        self.assertEqual(detail["creation"]["draws_committed"], 2)
        h.runtime.service.start_job(state["job_id"])
        ready = h.wait_creation(state["job_id"])
        self.assertEqual(ready["status"], "READY")
        self.assertEqual(ready["summary"]["total_planned"], 4)
        draws, seeds = self.assert_seed_mapping(h, state["job_id"], 4, 2, 4)
        for draw in draws:
            rows = h.runtime.repo.seed_rows(draw["id"])
            self.assertEqual({row["node_id"] for row in rows}, {"2", "20"})
        self.assertEqual(
            h.runtime.db.query_one(
                "SELECT COUNT(DISTINCT seed) FROM seed_allocations WHERE job_id=?", (state["job_id"],)
            )[0],
            8,
        )


if __name__ == "__main__":
    unittest.main()
