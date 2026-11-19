import assert from "node:assert/strict";
import test from "node:test";

import { ApiClient, ApiError, idempotencyKey } from "../../comfyui_gacha_master/web/api.js";
import { EventBridge, Store } from "../../comfyui_gacha_master/web/store.js";
import { fmtBytes } from "../../comfyui_gacha_master/web/dom.js";

function response(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

test("idempotency keys are unique", () => {
  const values = new Set(Array.from({ length: 50 }, () => idempotencyKey()));
  assert.equal(values.size, 50);
});

test("api client unwraps data and sends relative routes through fetchApi", async () => {
  const calls = [];
  const comfyApi = {
    fetchApi: async (path, init) => {
      calls.push({ path, init });
      return response({ schema_version: 1, data: { ok: true }, event_seq: 7 });
    },
  };
  const client = new ApiClient(comfyApi);
  const data = await client.get("/health");
  assert.deepEqual(data, { ok: true });
  assert.equal(calls[0].path, "/gacha_master/v1/health");
  assert.equal(calls[0].init.method, "GET");
});

test("api errors carry code, retryable and details", async () => {
  const comfyApi = {
    fetchApi: async () =>
      response(
        {
          schema_version: 1,
          error: {
            code: "SUBMISSION_UNKNOWN",
            message: "未知",
            trace_id: "trace-1",
            retryable: false,
            details: { attempt_id: "a" },
          },
        },
        409,
      ),
  };
  const client = new ApiClient(comfyApi);
  await assert.rejects(
    () => client.get("/jobs/x"),
    (error) => {
      assert.ok(error instanceof ApiError);
      assert.equal(error.code, "SUBMISSION_UNKNOWN");
      assert.equal(error.status, 409);
      assert.equal(error.traceId, "trace-1");
      assert.equal(error.retryable, false);
      return true;
    },
  );
});

test("selection uses PUT with a target boolean and no toggle semantics", async () => {
  const calls = [];
  const comfyApi = {
    fetchApi: async (path, init) => {
      calls.push({ path, init });
      return response({ schema_version: 1, data: { output_id: "o", selected: true, version: 2 } });
    },
  };
  const client = new ApiClient(comfyApi);
  const result = await client.selection("o", true, 1);
  assert.equal(result.selected, true);
  assert.equal(calls[0].init.method, "PUT");
  assert.deepEqual(JSON.parse(calls[0].init.body), { selected: true, expected_version: 1 });
});

test("project deletion sends the typed name with the chosen job ID", async () => {
  const client = new ApiClient({
    fetchApi: async (path, init) => {
      assert.equal(path, "/gacha_master/v1/jobs/job-id");
      assert.equal(init.method, "DELETE");
      assert.deepEqual(JSON.parse(init.body), { confirmation_name: "测试项目" });
      return response({ data: { id: "job-id", deleted: true } });
    },
  });
  assert.deepEqual(await client.deleteJob("job-id", "测试项目"), { id: "job-id", deleted: true });
});

test("upload sends multipart to /uploads and validates one item", async () => {
  const calls = [];
  const comfyApi = {
    fetchApi: async (path, init) => {
      calls.push({ path, init });
      return response({ schema_version: 1, data: [{ upload_id: "u1", sha256: "abc" }] });
    },
  };
  const client = new ApiClient(comfyApi);
  const file = new File([new Uint8Array([1, 2, 3])], "a.png", { type: "image/png" });
  const uploaded = await client.upload(file);
  assert.equal(uploaded.upload_id, "u1");
  assert.equal(calls[0].path, "/gacha_master/v1/uploads");
  assert.ok(calls[0].init.body instanceof FormData);
});

test("event bridge advances seq, reports gaps and handles empty pages", async () => {
  globalThis.document = { hidden: false, addEventListener() {}, removeEventListener() {} };
  const store = new Store({ lastSeq: 0 });
  const events = [];
  const gaps = [];
  const api = {
    events: async (after) => {
      events.push(after);
      if (after === 0) return { items: [{ seq: 5 }], last_seq: 5 };
      return { items: [], last_seq: 5 };
    },
  };
  const bridge = new EventBridge(api, store, {
    onEvents: (items) => {},
    onGap: (gap) => gaps.push(gap),
  });
  bridge.stopped = false;
  await bridge.poll();
  assert.equal(store.get().lastSeq, 5);
  assert.equal(gaps.length, 1);
  assert.deepEqual(gaps[0], { expected: 1, got: 5 });
  await bridge.poll();
  assert.equal(events.length, 2);
  assert.equal(events[1], 5);
  bridge.stop();
});

test("byte formatting is deterministic", () => {
  assert.equal(fmtBytes(512), "512 B");
  assert.equal(fmtBytes(2048), "2.0 KiB");
  assert.equal(fmtBytes(3 * 1024 * 1024), "3.0 MiB");
});
