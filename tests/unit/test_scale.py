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


class ScaleTests(unittest.IsolatedAsyncioTestCase):
    async def harness(self, **kwargs):
        h = Harness(**kwargs)
        await h.start()
        self.addAsyncCleanup(h.cleanup)
        return h

    async def test_555_fake_draws_111_sources_5_each(self):
        """L0 scale run; this is NOT real ComfyUI sampling."""
        h = await self.harness(outputs_per_attempt=1)
        names = [f"src{index:03d}.png" for index in range(111)]
        start = time.time()
        job = h.create_job(count=5, names=names)
        self.assertEqual(job["status"], "READY")
        self.assertEqual(job["summary"]["total_planned"], 555)
        h.runtime.service.start_job(job["id"])
        final = h.wait_job(job["id"], timeout=180)
        elapsed = time.time() - start
        self.assertEqual(final["status"], "COMPLETED", final)
        self.assertEqual(final["summary"]["succeeded"], 555)
        self.assertEqual(final["summary"]["out_available"], 555)
        self.assertEqual(len(h.engine.submissions), 555)
        rows = h.runtime.db.query_all(
            "SELECT s.source_order, d.draw_no FROM attempts a JOIN draws d ON d.id=a.draw_id "
            "JOIN sources s ON s.id=d.source_id ORDER BY a.rowid"
        )
        sequence = [(int(row["source_order"]), int(row["draw_no"])) for row in rows]
        expected = []
        for source_order in range(1, 112):
            for draw_no in range(1, 6):
                expected.append((source_order, draw_no))
        self.assertEqual(sequence, expected)
        seed_stats = h.runtime.db.query_one("SELECT COUNT(*) n, COUNT(DISTINCT seed) d FROM seed_allocations")
        self.assertEqual(int(seed_stats["n"]), 555)
        self.assertEqual(int(seed_stats["d"]), 555)
        # Every accepted DRAW has exactly one accepted attempt and one output.
        accepted = h.runtime.db.query_one(
            "SELECT COUNT(*) n FROM draws WHERE status='SUCCEEDED' AND accepted_attempt_id IS NOT NULL"
        )
        self.assertEqual(int(accepted["n"]), 555)
        self.assertEqual(h.runtime.service.list_sources(job["id"])["items"][0]["source_order"], 1)
        print(f"\n555 fake DRAW elapsed {elapsed:.2f}s (L0, fake engine)")

    async def test_1000_outputs_pagination(self):
        h = await self.harness(outputs_per_attempt=4)
        job = h.create_job(count=250, names=("a.png",))
        h.runtime.service.start_job(job["id"])
        final = h.wait_job(job["id"], timeout=180)
        self.assertEqual(final["status"], "COMPLETED", final)
        self.assertEqual(final["summary"]["succeeded"], 250)
        self.assertEqual(final["summary"]["out_available"], 1000)
        source = h.runtime.service.list_sources(job["id"])["items"][0]
        cursor = None
        seen = 0
        pages = 0
        while True:
            payload = h.runtime.service.list_outputs(
                source["id"],
                limit=50,
                cursor_draw_no=(cursor or {}).get("draw_no", 0),
                cursor_out_no=(cursor or {}).get("out_no", 0),
            )
            seen += len(payload["items"])
            pages += 1
            cursor = payload["next_cursor"]
            if not cursor:
                break
            self.assertLess(pages, 30)
        self.assertEqual(seen, 1000)

    async def test_10000_draw_plan_creation(self):
        h = await self.harness()
        start = time.time()
        job = h.create_job(count=10000, names=("a.png",))
        elapsed = time.time() - start
        self.assertEqual(job["status"], "READY")
        self.assertEqual(job["summary"]["total_planned"], 10000)
        self.assertEqual(h.runtime.db.query_one("SELECT COUNT(*) FROM draws")[0], 10000)
        stats = h.runtime.db.query_one("SELECT COUNT(*) n, COUNT(DISTINCT seed) d FROM seed_allocations")
        self.assertEqual(int(stats["n"]), 10000)
        self.assertEqual(int(stats["d"]), 10000)
        self.assertEqual(h.runtime.repo.next_executable_draw(job["id"])["draw_no"], 1)
        print(f"\n10000 DRAW plan creation {elapsed:.2f}s")


if __name__ == "__main__":
    unittest.main()
