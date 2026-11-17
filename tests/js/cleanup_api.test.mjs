import assert from "node:assert/strict";
import test from "node:test";

import { ApiClient } from "../../comfyui_gacha_master/web/api.js";

function response(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function clientWith(handler) {
  const calls = [];
  const api = new ApiClient({
    fetchApi: async (path, init) => {
      calls.push({ path, init });
      return handler(path, init);
    },
  });
  return { api, calls };
}

test("cleanup preview uses POST with an optional idempotency key", async () => {
  const { api, calls } = clientWith(() =>
    response({ schema_version: 1, data: { id: "c1", plan_token: "t", candidate_count: 3 } }),
  );
  const preview = await api.cleanupPreview("job-1", "key-1");
  assert.equal(preview.id, "c1");
  assert.equal(calls[0].path, "/gacha_master/v1/jobs/job-1/cleanup/preview");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.headers["Idempotency-Key"], "key-1");
});

test("cleanup confirm posts the explicit permanent-delete contract", async () => {
  const { api, calls } = clientWith(() =>
    response({ schema_version: 1, data: { id: "c1", status: "RUNNING" } }, 202),
  );
  await api.cleanupConfirm(
    "c1",
    { plan_token: "t", expected_candidate_count: 3, confirm_permanent_delete: true },
    "key-2",
  );
  assert.equal(calls[0].path, "/gacha_master/v1/storage-cleanups/c1/confirm");
  assert.equal(calls[0].init.method, "POST");
  const body = JSON.parse(calls[0].init.body);
  assert.deepEqual(body, {
    plan_token: "t",
    expected_candidate_count: 3,
    confirm_permanent_delete: true,
  });
  assert.equal(calls[0].init.headers["Idempotency-Key"], "key-2");
});

test("cleanup detail, items and history use bounded GET queries", async () => {
  const { api, calls } = clientWith((path) => {
    if (path.includes("/items")) {
      return response({ schema_version: 1, data: { items: [], next_cursor: null } });
    }
    if (path.includes("/cleanups")) {
      return response({ schema_version: 1, data: { items: [] } });
    }
    return response({ schema_version: 1, data: { id: "c1", status: "COMPLETED" } });
  });
  await api.cleanupDetail("c1");
  await api.cleanupItems("c1", { limit: 25, cursor: '{"a":1}' });
  await api.jobCleanups("job-9");
  assert.equal(calls[0].path, "/gacha_master/v1/storage-cleanups/c1");
  assert.ok(calls[1].path.startsWith("/gacha_master/v1/storage-cleanups/c1/items?"));
  assert.ok(calls[1].path.includes("limit=25"));
  assert.ok(calls[1].path.includes("cursor="));
  assert.equal(calls[2].path, "/gacha_master/v1/jobs/job-9/cleanups?limit=20");
});

test("purged image errors keep OUTPUT_PURGED and status 410", async () => {
  const { api } = clientWith(() =>
    response(
      {
        schema_version: 1,
        error: {
          code: "OUTPUT_PURGED",
          message: "结果图片已被永久清理，不能读取。",
          retryable: false,
          details: { output_id: "o1" },
        },
      },
      410,
    ),
  );
  await assert.rejects(
    () => api.get("/outputs/o1/image"),
    (error) => {
      assert.equal(error.code, "OUTPUT_PURGED");
      assert.equal(error.status, 410);
      assert.equal(error.details.output_id, "o1");
      return true;
    },
  );
});
