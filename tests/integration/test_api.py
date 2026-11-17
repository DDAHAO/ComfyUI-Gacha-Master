import asyncio
import io
import json
import os
import sys
import time
import unittest
from urllib.parse import quote

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.join(ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tests"))

from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from harness import Harness, make_png, workflow_fixture


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness(outputs_per_attempt=2)
        await self.h.start()
        app = web.Application()
        routes = web.RouteTableDef()
        self.h.runtime.api.register(routes)
        app.add_routes(routes)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.h.cleanup()

    async def get(self, path, **kwargs):
        response = await self.client.get("/gacha_master/v1" + path, **kwargs)
        return response, await response.json()

    async def post(self, path, body=None, headers=None):
        response = await self.client.post("/gacha_master/v1" + path, json=body or {}, headers=headers)
        return response, await response.json()

    async def put(self, path, body=None):
        response = await self.client.put("/gacha_master/v1" + path, json=body or {})
        return response, await response.json()

    async def create_job(self, key="api-key", count=1, names=("a.png", "b.png")):
        sources = self.h.make_sources(names)
        fixture = workflow_fixture()
        response, payload = await self.post(
            "/jobs",
            {
                "name": "API job",
                "protection_mode": "snapshot",
                "count_per_source": count,
                "sources": sources,
                "workflow_ui": fixture["ui"],
                "workflow_api": fixture["api"],
                "bindings": {"load_image_node_id": "1", "save_image_node_id": "6"},
            },
            headers={"Idempotency-Key": key},
        )
        self.assertEqual(response.status, 201, payload)
        job = payload["data"]
        # POST proves the request checkpoint is recorded; the plan is built by
        # the bounded worker.  Tests that need a runnable plan wait for READY.
        deadline = time.time() + 60
        while time.time() < deadline and job["status"] == "CREATING":
            _, detail = await self.get(f"/jobs/{job['id']}")
            job = detail["data"]
            await asyncio.sleep(0.01)
        return job

    async def wait_job(self, job_id, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            response, payload = await self.get(f"/jobs/{job_id}")
            job = payload["data"]
            if job["status"] in ("COMPLETED", "BLOCKED", "RECOVERY_REQUIRED", "PAUSED"):
                return job
            await asyncio.sleep(0.05)
        return job

    async def test_capabilities_health_settings_and_origin(self):
        response, payload = await self.get("/capabilities")
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["data"]["schema_version"], 1)
        self.assertEqual(payload["data"]["limits"]["max_inflight"], 1)
        response, payload = await self.get("/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["data"]["db"]["pragmas"]["journal_mode"], "wal")
        response, payload = await self.get("/settings")
        self.assertIn("data_root", payload["data"])
        # Cross-site Origin is rejected before any state change.
        response, payload = await self.post(
            "/jobs", {"sources": []}, headers={"Origin": "https://evil.example"}
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(payload["error"]["code"], "BAD_REQUEST")

    async def test_upload_scan_preflight_validation(self):
        buffer = io.BytesIO()
        Image.new("RGB", (30, 20), (4, 5, 6)).save(buffer, "PNG")
        form = FormData()
        form.add_field("image", buffer.getvalue(), filename="uploaded.png", content_type="image/png")
        response = await self.client.post("/gacha_master/v1/uploads", data=form)
        payload = await response.json()
        self.assertEqual(response.status, 200, payload)
        upload = payload["data"][0]
        self.assertEqual(upload["width"], 30)
        response = await self.client.get(f"/gacha_master/v1/uploads/{upload['upload_id']}/image")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Content-Type"], "image/png")
        # Bad upload is rejected, not accepted as a blank image.
        form = FormData()
        form.add_field("image", b"not-an-image", filename="bad.png", content_type="image/png")
        response = await self.client.post("/gacha_master/v1/uploads", data=form)
        self.assertEqual(response.status, 415)
        # Allowed root scan with natural ordering.
        make_png(os.path.join(self.h.input_root, "img10.png"))
        make_png(os.path.join(self.h.input_root, "img2.png"))
        response, payload = await self.post("/inputs/scan", {"root": self.h.input_root})
        self.assertEqual(response.status, 200)
        names = [item["name"] for item in payload["data"]["items"] if item["valid"]]
        self.assertIn("img2.png", names)
        self.assertLess(names.index("img2.png"), names.index("img10.png"))
        # Arbitrary root is rejected.
        response, payload = await self.post("/inputs/scan", {"root": "C:\\Windows"})
        self.assertEqual(response.status, 403)
        self.assertEqual(payload["error"]["code"], "PATH_NOT_ALLOWED")
        # Preflight errors are structured.
        fixture = workflow_fixture()
        response, payload = await self.post(
            "/preflight",
            {
                "workflow_ui": fixture["ui"],
                "workflow_api": fixture["api"],
                "bindings": {"load_image_node_id": "1", "save_image_node_id": "99"},
            },
        )
        self.assertEqual(response.status, 200)
        self.assertFalse(payload["data"]["ok"])
        self.assertEqual(payload["data"]["errors"][0]["code"], "NODE_MISSING")

    async def test_job_lifecycle_selection_and_events(self):
        job = await self.create_job()
        _, detail = await self.get(f"/jobs/{job['id']}")
        self.assertEqual(detail["data"]["status"], "READY")
        _, started = await self.post(f"/jobs/{job['id']}/start", {})
        self.assertEqual(started["data"]["status"], "RUNNING")
        final = await self.wait_job(job["id"])
        self.assertEqual(final["status"], "COMPLETED", final)
        self.assertEqual(final["summary"]["succeeded"], 2)
        self.assertEqual(final["summary"]["out_available"], 4)
        _, sources = await self.get(f"/jobs/{job['id']}/sources")
        source = sources["data"]["items"][0]
        self.assertEqual(source["source_order"], 1)
        _, outputs = await self.get(f"/sources/{source['id']}/outputs")
        output = outputs["data"]["items"][0]
        self.assertEqual(len(outputs["data"]["items"]), 2)
        selection, payload = await self.put(
            f"/outputs/{output['id']}/selection", {"selected": True, "expected_version": 0}
        )
        self.assertEqual(selection.status, 200)
        self.assertTrue(payload["data"]["selected"])
        # Replaying the target boolean is idempotent (not a toggle).
        selection, payload = await self.put(f"/outputs/{output['id']}/selection", {"selected": True})
        self.assertTrue(payload["data"]["selected"])
        self.assertTrue(payload["data"]["idempotent"])
        # Stale version is a visible conflict.
        selection, payload = await self.put(
            f"/outputs/{output['id']}/selection", {"selected": False, "expected_version": 0}
        )
        self.assertEqual(selection.status, 409)
        self.assertEqual(payload["error"]["code"], "VERSION_CONFLICT")
        response = await self.client.get(f"/gacha_master/v1/outputs/{output['id']}/thumbnail")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Content-Type"], "image/jpeg")
        response = await self.client.get(f"/gacha_master/v1/outputs/{output['id']}/image")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Content-Type"], "image/png")
        await response.read()
        # A file deleted after success keeps the DRAW history but is marked
        # MISSING and cannot be served/exported as healthy.
        row = self.h.runtime.repo.get_output(output["id"])
        os.unlink(os.path.join(self.h.runtime.paths.results_root, *str(row["rel_path"]).split("/")))
        response = await self.client.get(f"/gacha_master/v1/outputs/{output['id']}/image")
        self.assertEqual(response.status, 409)
        self.assertEqual(self.h.runtime.repo.get_output(output["id"])["health"], "MISSING")
        _, events = await self.get("/events?after=0")
        actions = [event["action"] for event in events["data"]["items"]]
        self.assertIn("JOB_COMPLETED", actions)
        self.assertIn("SELECTION_SET", actions)

    async def test_output_state_and_backward_pagination(self):
        job = await self.create_job(key="state-job", count=2, names=("a.png", "b.png"))
        await self.post(f"/jobs/{job['id']}/start", {})
        final = await self.wait_job(job["id"])
        self.assertEqual(final["status"], "COMPLETED", final)
        _, sources = await self.get(f"/jobs/{job['id']}/sources")
        source = sources["data"]["items"][0]
        other_source = sources["data"]["items"][1]
        _, page1 = await self.get(f"/sources/{source['id']}/outputs?limit=2")
        self.assertEqual(len(page1["data"]["items"]), 2)
        self.assertIsNotNone(page1["data"]["next_cursor"])
        next_cursor = json.dumps(page1["data"]["next_cursor"])
        _, page2 = await self.get(
            f"/sources/{source['id']}/outputs?limit=2&cursor={quote(next_cursor)}"
        )
        self.assertEqual(len(page2["data"]["items"]), 2)
        backward_cursor = json.dumps(page2["data"]["prev_cursor"])
        _, back = await self.get(
            f"/sources/{source['id']}/outputs?limit=2&direction=backward&cursor={quote(backward_cursor)}"
        )
        self.assertEqual(
            [item["id"] for item in back["data"]["items"]],
            [item["id"] for item in page1["data"]["items"]],
        )
        _, other_outputs = await self.get(f"/sources/{other_source['id']}/outputs?limit=1")
        foreign_id = other_outputs["data"]["items"][0]["id"]
        first_id = page1["data"]["items"][0]["id"]
        response, state = await self.post(
            f"/sources/{source['id']}/outputs/state",
            {"output_ids": [first_id, foreign_id, "OUT-does-not-exist"]},
        )
        self.assertEqual(response.status, 200, state)
        self.assertEqual([item["output_id"] for item in state["data"]["items"]], [first_id])
        self.assertEqual(
            set(state["data"]["missing_ids"]), {foreign_id, "OUT-does-not-exist"}
        )
        self.assertEqual(state["data"]["items"][0]["health"], "VERIFIED")
        # A real selection write is reflected by the lightweight state read.
        await self.put(f"/outputs/{first_id}/selection", {"selected": True, "expected_version": 0})
        _, state2 = await self.post(
            f"/sources/{source['id']}/outputs/state", {"output_ids": [first_id]}
        )
        self.assertTrue(state2["data"]["items"][0]["selected"])
        self.assertEqual(state2["data"]["items"][0]["selection_version"], 1)
        response, payload = await self.post(
            f"/sources/{source['id']}/outputs/state",
            {"output_ids": [f"OUT-{index}" for index in range(201)]},
        )
        self.assertEqual(response.status, 400, payload)

    async def test_idempotent_create_and_version_conflict(self):
        job = await self.create_job(key="same-key")
        _, detail = await self.get(f"/jobs/{job['id']}")
        self.assertEqual(detail["data"]["plan_version"], 1)
        # Same key + same body returns the same job.
        fixture = workflow_fixture()
        sources = self.h.make_sources(("a.png", "b.png"))
        body = {
            "name": "API job",
            "protection_mode": "snapshot",
            "count_per_source": 1,
            "sources": sources,
            "workflow_ui": fixture["ui"],
            "workflow_api": fixture["api"],
            "bindings": {"load_image_node_id": "1", "save_image_node_id": "6"},
        }
        response, payload = await self.post("/jobs", body, headers={"Idempotency-Key": "same-key"})
        self.assertEqual(response.status, 201)
        self.assertEqual(payload["data"]["id"], job["id"])
        # Same key + different body is a conflict.
        altered = json.loads(json.dumps(body))
        altered["count_per_source"] = 9
        response, payload = await self.post("/jobs", altered, headers={"Idempotency-Key": "same-key"})
        self.assertEqual(response.status, 409)
        self.assertEqual(payload["error"]["code"], "IDEMPOTENCY_CONFLICT")

    async def test_append_pause_resume_and_recovery_report(self):
        job = await self.create_job(key="append-job")
        self.h.engine.auto_complete = False
        await self.post(f"/jobs/{job['id']}/start", {})
        for _ in range(200):
            if self.h.engine.submissions:
                break
            await asyncio.sleep(0.02)
        response, payload = await self.post(f"/jobs/{job['id']}/pause", {"mode": "safe"})
        self.assertEqual(response.status, 200)
        self.h.engine.auto_complete = True
        paused = await self.wait_job(job["id"], timeout=15)
        self.assertEqual(paused["status"], "PAUSED", paused)
        _, sources = await self.get(f"/jobs/{job['id']}/sources")
        source = sources["data"]["items"][0]
        _, detail = await self.get(f"/jobs/{job['id']}")
        response, payload = await self.post(
            f"/sources/{source['id']}/append",
            {"count": 2, "priority": "priority", "expected_version": detail["data"]["row_version"]},
            headers={"Idempotency-Key": "append-fixed"},
        )
        self.assertEqual(response.status, 200, payload)
        self.assertEqual(payload["data"]["draw_start"], 2)
        response, payload2 = await self.post(
            f"/sources/{source['id']}/append",
            {"count": 2, "priority": "priority", "expected_version": detail["data"]["row_version"]},
            headers={"Idempotency-Key": "append-fixed"},
        )
        self.assertEqual(payload2["data"]["block_id"], payload["data"]["block_id"])
        _, report = await self.get(f"/jobs/{job['id']}/recovery")
        self.assertEqual(report["data"]["job_id"], job["id"])
        _, started = await self.post(f"/jobs/{job['id']}/start", {})
        self.assertEqual(started["data"]["status"], "RUNNING")
        final = await self.wait_job(job["id"])
        self.assertEqual(final["status"], "COMPLETED", final)

    async def test_export_endpoints_and_backups(self):
        job = await self.create_job(key="export-job")
        await self.post(f"/jobs/{job['id']}/start", {})
        final = await self.wait_job(job["id"])
        self.assertEqual(final["status"], "COMPLETED")
        _, sources = await self.get(f"/jobs/{job['id']}/sources")
        source = sources["data"]["items"][0]
        _, outputs = await self.get(f"/sources/{source['id']}/outputs")
        output = outputs["data"]["items"][0]
        await self.put(f"/outputs/{output['id']}/selection", {"selected": True})
        response, payload = await self.post(
            f"/jobs/{job['id']}/exports/preview",
            {"kind": "QUICK", "layout": "FLAT", "target_root": self.h.export_root},
            headers={"Idempotency-Key": "preview-fixed"},
        )
        self.assertEqual(response.status, 200)
        draft = payload["data"]
        response, payload = await self.post(
            f"/export-drafts/{draft['id']}/confirm",
            {"expected_draft_version": draft["draft_version"], "confirm_cutoff": True},
            headers={"Idempotency-Key": "confirm-fixed"},
        )
        self.assertEqual(response.status, 202)
        export = payload["data"]
        for _ in range(200):
            _, latest = await self.get(f"/exports/{export['id']}")
            if latest["data"]["status"] in ("COMPLETED", "PARTIAL_FAILED"):
                break
            await asyncio.sleep(0.05)
        self.assertEqual(latest["data"]["status"], "COMPLETED", latest)
        self.assertTrue(os.path.isfile(os.path.join(latest["data"]["batch_dir"], "manifest.json")))
        _, listed = await self.get(f"/jobs/{job['id']}/exports")
        self.assertEqual(len(listed["data"]), 1)
        response, payload = await self.post("/backups", {})
        self.assertEqual(response.status, 200, payload)
        backup = payload["data"]
        self.assertEqual(backup["status"], "VERIFIED")
        _, backups = await self.get("/backups")
        self.assertGreaterEqual(len(backups["data"]), 1)
        self.assertTrue(all(item["status"] == "VERIFIED" for item in backups["data"]))
        _, storage = await self.get("/storage")
        self.assertIn("data_bytes", storage["data"])
        response, payload = await self.post("/cache/clear", {})
        self.assertEqual(response.status, 200)


if __name__ == "__main__":
    unittest.main()
