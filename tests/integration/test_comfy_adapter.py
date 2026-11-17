import asyncio
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from aiohttp import web
from aiohttp.test_utils import TestServer

from comfyui_gacha_master.backend.adapters.comfy import ComfyAdapter


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.jobs = {}
        self.accepted = []
        self.cancelled = []
        self.mode = "success"
        app = web.Application()
        app.router.add_get("/system_stats", self.system_stats)
        app.router.add_get("/queue", self.queue)
        app.router.add_get("/history", self.history_list)
        app.router.add_get("/history/{prompt_id}", self.history)
        app.router.add_post("/prompt", self.prompt)
        app.router.add_post("/api/jobs/{job_id}/cancel", self.cancel)
        app.router.add_get("/object_info/{class_type}", self.object_info)
        self.server = TestServer(app)
        await self.server.start_server()
        self.base = str(self.server.make_url("/")).rstrip("/")
        self.adapter = ComfyAdapter(base_url=self.base, submit_timeout=2.0, targeted_cancel_verified=True)

    async def asyncTearDown(self):
        await self.adapter.close()
        await self.server.close()

    async def system_stats(self, request):
        return web.json_response(
            {"system": {"comfyui_version": "0.45.0-test", "required_frontend_version": "1.58.5"}}
        )

    async def queue(self, request):
        running = [job for job in self.jobs.values() if job["state"] == "running"]
        pending = [job for job in self.jobs.values() if job["state"] == "queued"]
        return web.json_response(
            {
                "queue_running": [[0, job["id"], {}, {}, []] for job in running],
                "queue_pending": [[1, job["id"], {}, {}, []] for job in pending],
            }
        )

    async def history_list(self, request):
        return web.json_response({})

    async def history(self, request):
        prompt_id = request.match_info["prompt_id"]
        job = self.jobs.get(prompt_id)
        if not job or job["state"] not in ("success", "error", "interrupted"):
            return web.json_response({})
        status = (
            {"status_str": "success", "completed": True, "messages": [["execution_success", {}]]}
            if job["state"] == "success"
            else {
                "status_str": "error",
                "completed": True,
                "messages": [["execution_interrupted" if job["state"] == "interrupted" else "execution_error", {}]],
            }
        )
        return web.json_response({prompt_id: {"status": status, "outputs": {}}})

    async def prompt(self, request):
        data = await request.json()
        expected = data.get("prompt_id")
        self.accepted.append(data)
        if self.mode == "reject":
            return web.json_response({"error": {"type": "prompt_outputs_failed_validation"}, "node_errors": {}}, status=400)
        if self.mode == "drop":
            request.transport.close()
            return web.Response()
        prompt_id = expected or "server-generated"
        self.jobs[prompt_id] = {"id": prompt_id, "state": "queued"}
        return web.json_response({"prompt_id": prompt_id, "number": 0, "node_errors": {}})

    async def cancel(self, request):
        job_id = request.match_info["job_id"]
        self.cancelled.append(job_id)
        job = self.jobs.get(job_id)
        if job and job["state"] in ("queued", "running"):
            job["state"] = "interrupted"
            return web.json_response({"cancelled": True})
        return web.json_response({"cancelled": False})

    async def object_info(self, request):
        class_type = request.match_info["class_type"]
        if class_type == "KSampler":
            return web.json_response({class_type: {"name": class_type, "input": {"required": {}}}})
        return web.json_response({}, status=404)

    async def test_detect_submit_lookup_success_and_cancel(self):
        caps = await self.adapter.detect()
        self.assertEqual(caps["comfy_version"], "0.45.0-test")
        self.assertTrue(caps["queue_lookup"])
        self.assertTrue(caps["history_lookup"])
        self.assertFalse(caps["client_supplied_prompt_id"])  # not yet proven by a response
        again = await self.adapter.capabilities()
        self.assertTrue(again["queue_lookup"])
        self.assertEqual(again["comfy_version"], "0.45.0-test")
        submitted = await self.adapter.submit({"1": {"class_type": "LoadImage", "inputs": {}}}, expected_prompt_id="11111111-1111-4111-8111-111111111111", metadata={"attempt_id": "a"})
        self.assertEqual(submitted.status, "accepted")
        self.assertEqual(submitted.prompt_id, "11111111-1111-4111-8111-111111111111")
        self.jobs[submitted.prompt_id]["state"] = "success"
        lookup = await self.adapter.lookup(submitted.prompt_id)
        self.assertEqual(lookup.state, "history_success")
        self.assertTrue(lookup.terminal)
        info = await self.adapter.object_info("KSampler")
        self.assertIsNotNone(info)
        self.assertIsNone(await self.adapter.object_info("MissingNode"))
        self.jobs["66666666-6666-4666-8666-666666666666"] = {
            "id": "66666666-6666-4666-8666-666666666666",
            "state": "running",
        }
        cancel = await self.adapter.cancel("66666666-6666-4666-8666-666666666666")
        self.assertTrue(cancel.dispatched)
        self.assertFalse(cancel.verified)  # request accepted, evidence still required
        self.assertEqual(self.cancelled, ["66666666-6666-4666-8666-666666666666"])

    async def test_dropped_response_is_unknown_and_single_submission(self):
        self.mode = "drop"
        result = await self.adapter.submit({}, expected_prompt_id="22222222-2222-4222-8222-222222222222", metadata={})
        self.assertEqual(result.status, "unknown")
        self.assertEqual(len(self.accepted), 1)
        # The mock accepted the bytes but the client has no response.  The
        # adapter must not have retried.
        self.assertEqual(len(self.accepted), 1)

    async def test_rejection_and_unsupported_cancel(self):
        self.mode = "reject"
        result = await self.adapter.submit({}, expected_prompt_id="33333333-3333-4333-8333-333333333333", metadata={})
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.http_status, 400)
        unverified = ComfyAdapter(base_url=self.base, targeted_cancel_verified=False)
        try:
            cancel = await unverified.cancel("some-id")
            self.assertFalse(cancel.dispatched)
            self.assertEqual(cancel.capability, "unverified")
            self.assertEqual(self.cancelled, [])
        finally:
            await unverified.close()

    async def test_interrupted_history_is_distinguished(self):
        self.jobs["44444444-4444-4444-8444-444444444444"] = {
            "id": "44444444-4444-4444-8444-444444444444",
            "state": "interrupted",
        }
        lookup = await self.adapter.lookup("44444444-4444-4444-8444-444444444444")
        self.assertEqual(lookup.state, "history_interrupted")

    async def test_absent_lookup_is_not_success(self):
        lookup = await self.adapter.lookup("55555555-5555-4555-8555-555555555555")
        self.assertEqual(lookup.state, "absent")


if __name__ == "__main__":
    unittest.main()
