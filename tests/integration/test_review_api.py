"""HTTP layer for the resumable review workspace."""

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

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from harness import Harness, workflow_fixture


class ReviewApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness(outputs_per_attempt=2)
        await self.h.start()
        app = web.Application()
        routes = web.RouteTableDef()
        self.h.runtime.api.register(routes)
        app.add_routes(routes)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.job = await self.create_and_run_job()

    async def asyncTearDown(self):
        await self.client.close()
        self.h.cleanup()

    async def get(self, path):
        response = await self.client.get("/gacha_master/v1" + path)
        return response, await response.json()

    async def post(self, path, body=None, headers=None):
        response = await self.client.post(
            "/gacha_master/v1" + path, json=body or {}, headers=headers
        )
        return response, await response.json()

    async def patch(self, path, body):
        response = await self.client.patch("/gacha_master/v1" + path, json=body)
        return response, await response.json()

    async def put(self, path, body):
        response = await self.client.put("/gacha_master/v1" + path, json=body)
        return response, await response.json()

    async def create_and_run_job(self):
        sources = self.h.make_sources(("a.png", "b.png"))
        fixture = workflow_fixture()
        response, payload = await self.post(
            "/jobs",
            {
                "name": "review API job",
                "protection_mode": "snapshot",
                "count_per_source": 1,
                "sources": sources,
                "workflow_ui": fixture["ui"],
                "workflow_api": fixture["api"],
                "bindings": {
                    "load_image_node_id": "1",
                    "save_image_node_id": "6",
                },
            },
            headers={"Idempotency-Key": "review-api-job"},
        )
        self.assertEqual(response.status, 201, payload)
        job = payload["data"]
        deadline = time.time() + 60
        while time.time() < deadline and job["status"] == "CREATING":
            _, detail = await self.get(f"/jobs/{job['id']}")
            job = detail["data"]
            await asyncio.sleep(0.01)
        response, payload = await self.post(
            f"/jobs/{job['id']}/start", {"expected_version": job["row_version"]}
        )
        self.assertEqual(response.status, 200, payload)
        deadline = time.time() + 60
        while time.time() < deadline:
            _, detail = await self.get(f"/jobs/{job['id']}")
            job = detail["data"]
            if job["status"] in ("COMPLETED", "BLOCKED", "PAUSED"):
                break
            await asyncio.sleep(0.02)
        self.assertEqual(job["status"], "COMPLETED", job)
        return job

    async def test_full_review_api_flow_and_version_conflicts(self):
        response, payload = await self.get(f"/jobs/{self.job['id']}/review-session")
        self.assertEqual(response.status, 200)
        self.assertIsNone(payload["data"]["session"])
        self.assertEqual(payload["data"]["resume"]["mode"], "NONE")
        first_source = payload["data"]["resume"]["source"]
        self.assertEqual(first_source["source_order"], 1)

        response, payload = await self.post(
            f"/jobs/{self.job['id']}/review-session", {"scope": "pending"}
        )
        self.assertEqual(response.status, 200, payload)
        session = payload["data"]["session"]
        self.assertEqual(session["version"], 1)
        self.assertEqual(payload["data"]["resume"]["mode"], "IN_GROUP")

        response, payload = await self.get(
            f"/jobs/{self.job['id']}/review-sources?scope=pending&limit=40"
        )
        self.assertEqual(response.status, 200, payload)
        self.assertEqual([item["source_order"] for item in payload["data"]["items"]], [1, 2])
        self.assertEqual(payload["data"]["summary"]["pending_sources"], 2)

        response, payload = await self.get(f"/sources/{first_source['id']}/review-state")
        self.assertEqual(response.status, 200, payload)
        state = payload["data"]
        self.assertEqual(state["available"], 2)
        self.assertEqual(state["selected"], 0)

        response, payload = await self.get(
            f"/sources/{first_source['id']}/outputs?limit=1&offset=1"
        )
        self.assertEqual(response.status, 200, payload)
        self.assertEqual(payload["data"]["offset"], 1)
        self.assertEqual(payload["data"]["total"], 2)
        output = payload["data"]["items"][0]
        response, payload = await self.put(
            f"/outputs/{output['id']}/selection", {"selected": True}
        )
        self.assertEqual(response.status, 200, payload)

        response, payload = await self.patch(
            f"/review-sessions/{session['id']}/position",
            {
                "expected_version": session["version"],
                "current_source_id": first_source["id"],
                "current_output_id": output["id"],
                "result_offset": 1,
                "scope": "pending",
            },
        )
        self.assertEqual(response.status, 200, payload)
        session = payload["data"]["session"]
        self.assertEqual(session["version"], 2)
        self.assertEqual(session["current"]["output_id"], output["id"])
        self.assertEqual(session["current"]["result_offset"], 1)

        response, payload = await self.patch(
            f"/review-sessions/{session['id']}/position",
            {"expected_version": 1, "current_source_id": first_source["id"]},
        )
        self.assertEqual(response.status, 409, payload)
        self.assertEqual(payload["error"]["code"], "VERSION_CONFLICT")
        self.assertEqual(payload["error"]["details"]["current_version"], 2)

        response, payload = await self.get(f"/sources/{first_source['id']}/review-state")
        versions = payload["data"]["versions"]
        body = {
            "source_id": first_source["id"],
            "decision": "SELECTED",
            "expected_session_version": session["version"],
            "expected_result_revision": versions["result_revision"],
            "expected_selection_version": versions["selection_version"],
            "expected_plan_version": versions["plan_version"],
        }
        response, payload = await self.post(
            f"/review-sessions/{session['id']}/decisions",
            body,
            headers={"Idempotency-Key": "decision-http-1"},
        )
        self.assertEqual(response.status, 200, payload)
        decision = payload["data"]
        self.assertFalse(decision["idempotent"])
        self.assertEqual(decision["review"]["selected_count"], 1)
        self.assertEqual(decision["next"]["source"]["source_order"], 2)
        self.assertEqual(decision["session"]["break_type"], "AFTER_CONFIRMED")

        replay_response, replay = await self.post(
            f"/review-sessions/{session['id']}/decisions",
            body,
            headers={"Idempotency-Key": "decision-http-1"},
        )
        self.assertEqual(replay_response.status, 200, replay)
        self.assertTrue(replay["data"]["idempotent"])
        self.assertEqual(
            replay["data"]["session"]["version"], decision["session"]["version"]
        )

        # The server session alone resumes at group 2 (no browser cache).
        response, payload = await self.get(f"/jobs/{self.job['id']}/review-session")
        self.assertEqual(payload["data"]["resume"]["mode"], "NEXT_PENDING")
        self.assertEqual(payload["data"]["resume"]["source"]["source_order"], 2)

        # Opening the next group writes a resumable position and remembers the
        # previous group, so "back" works after a restart.
        response, payload = await self.patch(
            f"/review-sessions/{session['id']}/position",
            {
                "expected_version": decision["session"]["version"],
                "current_source_id": decision["next"]["source"]["id"],
                "result_offset": 0,
            },
        )
        self.assertEqual(response.status, 200, payload)
        advanced = payload["data"]["session"]
        self.assertEqual(advanced["current"]["source_order"], 2)
        self.assertEqual([item["source_order"] for item in advanced["history"]], [1])

        response, payload = await self.post(
            f"/review-sessions/{session['id']}/back",
            {"expected_version": advanced["version"]},
        )
        self.assertEqual(response.status, 200, payload)
        self.assertEqual(payload["data"]["session"]["current"]["source_order"], 1)
        self.assertEqual(payload["data"]["resume"]["mode"], "IN_GROUP")
        self.assertEqual(
            payload["data"]["resume"]["source"]["review"]["decision"], "SELECTED"
        )

    async def test_missing_version_envelope_is_rejected(self):
        _, payload = await self.post(f"/jobs/{self.job['id']}/review-session")
        session = payload["data"]["session"]
        source = payload["data"]["resume"]["source"]
        response, payload = await self.post(
            f"/review-sessions/{session['id']}/decisions",
            {
                "source_id": source["id"],
                "decision": "SKIPPED",
                "expected_session_version": session["version"],
            },
            headers={"Idempotency-Key": "missing-versions"},
        )
        self.assertEqual(response.status, 400, payload)
        self.assertEqual(payload["error"]["code"], "BAD_REQUEST")


if __name__ == "__main__":
    unittest.main()
