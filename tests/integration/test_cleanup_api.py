"""HTTP contract for storage cleanup and cache reporting."""

from __future__ import annotations

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

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from harness import Harness, workflow_fixture  # noqa: E402


class CleanupApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness(outputs_per_attempt=2)
        await self.h.start()
        app = web.Application()
        routes = web.RouteTableDef()
        self.h.runtime.api.register(routes)
        app.add_routes(routes)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.job = await self.create_job()
        response, payload = await self.post(
            f"/jobs/{self.job['id']}/start", {}, headers={"Idempotency-Key": "start"}
        )
        self.assertEqual(response.status, 200, payload)
        self.job = await self.wait_job(self.job["id"])
        self.assertEqual(self.job["status"], "COMPLETED", self.job)
        _, payload = await self.get(f"/jobs/{self.job['id']}/sources")
        self.sources = payload["data"]["items"]

    async def asyncTearDown(self):
        await self.client.close()
        self.h.cleanup()

    # ------------------------------------------------------------- helpers
    async def get(self, path):
        response = await self.client.get("/gacha_master/v1" + path)
        return response, await response.json()

    async def post(self, path, body=None, headers=None):
        response = await self.client.post(
            "/gacha_master/v1" + path, json=body if body is not None else {}, headers=headers
        )
        return response, await response.json()

    async def create_job(self, key="cleanup-api-job"):
        sources = self.h.make_sources(("a.png", "b.png", "c.png"))
        fixture = workflow_fixture()
        response, payload = await self.post(
            "/jobs",
            {
                "name": "cleanup API job",
                "protection_mode": "snapshot",
                "count_per_source": 1,
                "sources": sources,
                "workflow_ui": fixture["ui"],
                "workflow_api": fixture["api"],
                "bindings": {"load_image_node_id": "1", "save_image_node_id": "6"},
            },
            headers={"Idempotency-Key": key},
        )
        self.assertEqual(response.status, 201, payload)
        job = payload["data"]
        deadline = time.time() + 60
        while time.time() < deadline and job["status"] == "CREATING":
            _, detail = await self.get(f"/jobs/{job['id']}")
            job = detail["data"]
            await asyncio.sleep(0.01)
        return job

    async def wait_job(self, job_id, timeout=30):
        deadline = time.time() + timeout
        job = None
        while time.time() < deadline:
            _, payload = await self.get(f"/jobs/{job_id}")
            job = payload["data"]
            if job["status"] in ("COMPLETED", "BLOCKED", "RECOVERY_REQUIRED", "PAUSED"):
                return job
            await asyncio.sleep(0.05)
        return job

    async def outputs(self, source):
        _, payload = await self.get(f"/sources/{source['id']}/outputs?limit=50")
        return payload["data"]["items"]

    async def confirm_groups(self):
        for source in self.sources:
            response, payload = await self.post(
                f"/sources/{source['id']}/review", {"decision": "SKIPPED"}
            )
            self.assertEqual(response.status, 200, payload)

    async def preview(self):
        response, payload = await self.post(
            f"/jobs/{self.job['id']}/cleanup/preview", {}, headers={"Idempotency-Key": f"preview-{time.time()}"}
        )
        return response, payload

    async def wait_cleanup(self, cleanup_id, timeout=30):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            _, payload = await self.get(f"/storage-cleanups/{cleanup_id}")
            last = payload["data"]
            if last["status"] not in ("RUNNING", "PREVIEW"):
                return last
            await asyncio.sleep(0.03)
        return last

    # --------------------------------------------------------------- tests
    async def test_preview_confirm_progress_and_history_over_http(self):
        first = await self.outputs(self.sources[0])
        response = await self.client.put(
            f"/gacha_master/v1/outputs/{first[0]['id']}/selection",
            json={"selected": True, "expected_version": first[0]["selection_version"]},
        )
        self.assertEqual(response.status, 200)
        await self.confirm_groups()
        response, blocked = await self.preview()
        # A selected group with no export still allows cleanup of unselected OUTs.
        self.assertEqual(response.status, 200, blocked)
        preview = blocked["data"]
        self.assertEqual(preview["candidate_count"], 5)
        self.assertEqual(preview["protected_selected_count"], 1)
        self.assertIn("plan_token", preview)
        response, page = await self.get(
            f"/storage-cleanups/{preview['id']}/items?limit=2"
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(len(page["data"]["items"]), 2)
        self.assertIsNotNone(page["data"]["next_cursor"])
        body = {
            "plan_token": preview["plan_token"],
            "expected_candidate_count": preview["candidate_count"],
            "confirm_permanent_delete": True,
        }
        response, started = await self.post(
            f"/storage-cleanups/{preview['id']}/confirm",
            body,
            headers={"Idempotency-Key": "confirm-1"},
        )
        self.assertEqual(response.status, 202, started)
        run = await self.wait_cleanup(preview["id"])
        self.assertEqual(run["status"], "COMPLETED", run)
        self.assertEqual(run["deleted_files"], 5)
        self.assertGreater(run["deleted_bytes"], 0)
        # The same key/body returns the same run without deleting twice.
        response, replay = await self.post(
            f"/storage-cleanups/{preview['id']}/confirm",
            body,
            headers={"Idempotency-Key": "confirm-1"},
        )
        self.assertEqual(response.status, 202)
        self.assertEqual(replay["data"]["id"], run["id"])
        self.assertEqual(replay["data"]["deleted_files"], 5)
        # Same key with a different request body conflicts.
        changed = dict(body, expected_candidate_count=4)
        response, conflict = await self.post(
            f"/storage-cleanups/{preview['id']}/confirm",
            changed,
            headers={"Idempotency-Key": "confirm-1"},
        )
        self.assertEqual(response.status, 409)
        self.assertEqual(conflict["error"]["code"], "IDEMPOTENCY_CONFLICT")
        # History survives a page reload.
        response, history = await self.get(f"/jobs/{self.job['id']}/cleanups")
        self.assertEqual(response.status, 200)
        self.assertTrue(any(item["id"] == run["id"] for item in history["data"]["items"]))
        # Purged image and thumbnail are explicitly gone, not "corrupt".
        purged = page["data"]["items"][0]["output_id"]
        response, payload = await self.get(f"/outputs/{purged}/image")
        self.assertEqual(response.status, 410)
        self.assertEqual(payload["error"]["code"], "OUTPUT_PURGED")
        response, payload = await self.get(f"/outputs/{purged}/thumbnail")
        self.assertEqual(response.status, 410)
        self.assertEqual(payload["error"]["code"], "OUTPUT_PURGED")
        # Selected image still loads.
        response = await self.client.get(
            f"/gacha_master/v1/outputs/{first[0]['id']}/image"
        )
        self.assertEqual(response.status, 200)
        response.close()
        # Counts are consistent.
        _, payload = await self.get(f"/jobs/{self.job['id']}")
        summary = payload["data"]["summary"]
        self.assertEqual(summary["out_purged"], 5)
        self.assertEqual(summary["out_available"], 1)
        self.assertEqual(summary["selected"], 1)

    async def test_preview_requires_every_group_confirmation(self):
        for source in self.sources[:2]:
            response, payload = await self.post(
                f"/sources/{source['id']}/review", {"decision": "SKIPPED"}
            )
            self.assertEqual(response.status, 200, payload)
        response, payload = await self.preview()
        self.assertEqual(response.status, 409)
        self.assertEqual(payload["error"]["code"], "CLEANUP_NOT_ELIGIBLE")
        self.assertEqual(payload["error"]["details"]["next_action"], "continue_review")
        self.assertEqual(payload["error"]["details"]["pending_count"], 1)

    async def test_confirm_validation_and_stale_token(self):
        await self.confirm_groups()
        response, payload = await self.preview()
        self.assertEqual(response.status, 200)
        preview = payload["data"]
        response, payload = await self.post(
            f"/storage-cleanups/{preview['id']}/confirm",
            {
                "plan_token": preview["plan_token"],
                "expected_candidate_count": preview["candidate_count"],
                "confirm_permanent_delete": False,
            },
        )
        self.assertEqual(response.status, 400)
        response, payload = await self.post(
            f"/storage-cleanups/{preview['id']}/confirm",
            {
                "plan_token": "not-the-token",
                "expected_candidate_count": preview["candidate_count"],
                "confirm_permanent_delete": True,
            },
        )
        self.assertEqual(response.status, 409)
        self.assertEqual(payload["error"]["code"], "CLEANUP_PREVIEW_STALE")
        self.assertTrue(
            all(
                os.path.isfile(
                    os.path.join(
                        self.h.runtime.paths.results_root,
                        *self.h.runtime.repo.get_output(item["id"])["rel_path"].split("/"),
                    )
                )
                for item in (await self.outputs(self.sources[0]))
            )
        )

    async def test_storage_breakdown_and_cache_clear_contract(self):
        _, payload = await self.get("/storage")
        storage = payload["data"]
        for key in (
            "results_bytes",
            "result_image_bytes",
            "thumbnails_bytes",
            "results_other_bytes",
            "exports_bytes",
            "thumbnails_root",
            "results_includes_thumbnails",
            "breakdown_note",
            "retention",
        ):
            self.assertIn(key, storage)
        self.assertTrue(storage["results_includes_thumbnails"])
        self.assertGreaterEqual(storage["results_bytes"], storage["result_image_bytes"])
        response, payload = await self.post("/cache/clear", {})
        self.assertEqual(response.status, 200)
        data = payload["data"]
        for key in ("removed", "bytes", "scanned", "failed", "skipped", "errors", "root"):
            self.assertIn(key, data)
        self.assertEqual(data["failed"], 0)

    async def test_second_preview_expires_the_previous_one(self):
        await self.confirm_groups()
        _, first = await self.preview()
        _, second = await self.preview()
        preview1 = first["data"]
        preview2 = second["data"]
        self.assertNotEqual(preview1["id"], preview2["id"])
        _, payload = await self.get(f"/storage-cleanups/{preview1['id']}")
        self.assertEqual(payload["data"]["status"], "EXPIRED")
        response, payload = await self.post(
            f"/storage-cleanups/{preview1['id']}/confirm",
            {
                "plan_token": preview1["plan_token"],
                "expected_candidate_count": preview1["candidate_count"],
                "confirm_permanent_delete": True,
            },
        )
        self.assertEqual(response.status, 409)
        self.assertEqual(payload["error"]["code"], "CLEANUP_PREVIEW_STALE")


if __name__ == "__main__":
    unittest.main()
