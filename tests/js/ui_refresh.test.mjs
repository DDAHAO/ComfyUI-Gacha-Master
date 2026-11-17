import assert from "node:assert/strict";
import test from "node:test";

import {
  buildCreateBody, evaluateDraft, fingerprintBody, isUncertainError, modeConflict,
  parseCount, sourceCounts,
} from "../../comfyui_gacha_master/web/create_draft.js";
import { computeStepStates } from "../../comfyui_gacha_master/web/components/stepper.js";
import { ToastQueue } from "../../comfyui_gacha_master/web/components/toast.js";
import { clampPosition, ratiosMatch } from "../../comfyui_gacha_master/web/components/image_compare.js";
import { containerSize, modalSize } from "../../comfyui_gacha_master/web/layout.js";
import { SelectionController } from "../../comfyui_gacha_master/web/selection_controller.js";
import { EventBridge, Store } from "../../comfyui_gacha_master/web/store.js";

const CAPS = { limits: { plan_limit: 100, upload_max_file: 1024 } };

function baseDraft(overrides = {}) {
  return {
    name: "",
    countRaw: "2",
    mode: "snapshot",
    sources: [],
    workflow: {
      api: { "1": { class_type: "LoadImage" } },
      bindings: { load_image_node_id: "1", save_image_node_id: "6" },
    },
    workflowRevision: 3,
    bindingsRevision: 2,
    preflight: { state: "ok", workflowRevision: 3, bindingsRevision: 2 },
    sources: [{ key: "a", kind: "upload", status: "ready", uploadId: "u1", sha256: "abc" }],
    create: { state: "idle" },
    ...overrides,
  };
}

test("count parsing keeps invalid text as an error instead of a fallback", () => {
  assert.deepEqual(parseCount("5"), { valid: true, value: 5, error: "" });
  assert.equal(parseCount("").valid, false);
  assert.equal(parseCount("0").valid, false);
  assert.equal(parseCount("-2").valid, false);
  assert.equal(parseCount("1.5").valid, false);
  assert.equal(parseCount("abc").valid, false);
});

test("source counting and mode conflicts use only draft facts", () => {
  const counts = sourceCounts([
    { status: "ready" }, { status: "queued" }, { status: "uploading" }, { status: "error" },
  ]);
  assert.deepEqual(counts, { total: 4, ready: 1, queued: 1, uploading: 1, error: 1 });
  assert.equal(modeConflict("reference", [{ kind: "upload" }]), true);
  assert.equal(modeConflict("reference", [{ kind: "path" }]), false);
  assert.equal(modeConflict("snapshot", [{ kind: "upload" }]), false);
});

test("draft evaluation unlocks steps only from real completion", () => {
  const ready = evaluateDraft(baseDraft(), CAPS);
  assert.equal(ready.workflowValid, true);
  assert.equal(ready.sourcesComplete, true);
  assert.equal(ready.paramsComplete, true);
  assert.equal(ready.canCreate, true);
  assert.equal(ready.maxUnlocked, 3);
  assert.deepEqual(ready.completed, ["workflow", "sources", "params"]);

  const pending = evaluateDraft(baseDraft({ sources: [{ key: "a", kind: "upload", status: "uploading" }] }), CAPS);
  assert.equal(pending.sourcesComplete, false);
  assert.equal(pending.canCreate, false);
  assert.equal(pending.maxUnlocked, 1);

  const missingBinding = evaluateDraft(baseDraft({
    workflow: { api: { "1": {} }, bindings: { load_image_node_id: "1", save_image_node_id: "" } },
  }), CAPS);
  assert.equal(missingBinding.workflowValid, false);
  assert.ok(missingBinding.invalid.includes("workflow"));
  assert.equal(missingBinding.maxUnlocked, 0);
});

test("plan limit and stale preflight block creation", () => {
  const over = evaluateDraft(baseDraft({ countRaw: "101" }), CAPS);
  assert.equal(over.planExceeded, true);
  assert.equal(over.canCreate, false);
  assert.ok(over.blockers.some((item) => item.includes("超过后端上限")));

  const stale = evaluateDraft(baseDraft({ bindingsRevision: 9 }), CAPS);
  assert.equal(stale.preflightCurrent, false);
  assert.equal(stale.canCreate, false);
  assert.ok(stale.invalid.includes("review") === false);

  const unknownLimit = evaluateDraft(baseDraft(), {});
  assert.equal(unknownLimit.planLimitUnknown, true);
  assert.equal(unknownLimit.canCreate, false);
});

test("uncertain creation state freezes the draft and blocks a second create", () => {
  const frozen = evaluateDraft(baseDraft({ create: { state: "uncertain" } }), CAPS);
  assert.equal(frozen.frozen, true);
  assert.equal(frozen.canCreate, false);
  assert.ok(frozen.blockers.some((item) => item.includes("结果未知")));
  const verify = evaluateDraft(baseDraft({ create: { state: "verify" } }), CAPS);
  assert.equal(verify.frozen, true);
  assert.equal(verify.canCreate, false);
});

test("create body carries upload and path identities without inventing fields", () => {
  const body = buildCreateBody(baseDraft({
    name: "  set A  ",
    count: 2,
    sources: [
      { kind: "upload", uploadId: "u1", sha256: "a" },
      { kind: "path", root: "D:\\in", rel_path: "b.png", sha256: "b" },
    ],
  }));
  assert.equal(body.name, "set A");
  assert.equal(body.count_per_source, 2);
  assert.deepEqual(body.sources, [
    { upload_id: "u1", sha256: "a" },
    { root: "D:\\in", rel_path: "b.png", sha256: "b" },
  ]);
  assert.equal(fingerprintBody(body), JSON.stringify(body));
});

test("network and 5xx errors are uncertain; 4xx validation is definitive", () => {
  assert.equal(isUncertainError({ status: 0, code: "NETWORK_ERROR" }), true);
  assert.equal(isUncertainError({ status: 502 }), true);
  assert.equal(isUncertainError({ status: 400 }), false);
  assert.equal(isUncertainError({ status: 409 }), false);
});

test("step states distinguish current, complete, error and locked", () => {
  const steps = [{ id: "a" }, { id: "b" }, { id: "c" }, { id: "d" }];
  const states = computeStepStates(steps, 2, { completed: ["a"], invalid: ["b"], maxUnlocked: 2 });
  assert.deepEqual(states.map((item) => item.state), ["complete", "error", "current", "locked"]);
});

test("toast queue caps visible messages, merges duplicates and promotes queued", () => {
  const queue = new ToastQueue({ limit: 2, queueLimit: 2 });
  assert.equal(queue.push({ id: "a", message: "A", kind: "info" }).type, "show");
  assert.equal(queue.push({ id: "b", message: "B", kind: "info" }).type, "show");
  assert.equal(queue.push({ id: "c", message: "C", kind: "info" }).type, "queue");
  assert.equal(queue.push({ id: "d", message: "D", kind: "info" }).type, "queue");
  assert.equal(queue.push({ id: "e", message: "E", kind: "info" }).type, "queue");
  assert.deepEqual(queue.queued.map((item) => item.id), ["d", "e"]);
  assert.equal(queue.push({ id: "a", message: "A2", kind: "warn" }).type, "update");
  const result = queue.dismiss("a");
  assert.equal(result.type, "promote");
  assert.equal(result.item.id, "d");
  assert.deepEqual(queue.visible.map((item) => item.id), ["b", "d"]);
});

test("image ratios tolerate rounding but never rescale mismatched pictures", () => {
  assert.equal(ratiosMatch(1024, 1024, 512, 512), true);
  assert.equal(ratiosMatch(832, 1216, 416, 608), true);
  assert.equal(ratiosMatch(1024, 1023, 1024, 1024), true);
  assert.equal(ratiosMatch(1024, 1024, 768, 1024), false);
  assert.equal(ratiosMatch(0, 0, 10, 10), false);
  assert.equal(clampPosition(-1), 0);
  assert.equal(clampPosition(2), 1);
  assert.equal(clampPosition("x"), 0.5);
});

test("container and modal breakpoints match the design document", () => {
  assert.equal(containerSize(320), "narrow");
  assert.equal(containerSize(479), "narrow");
  assert.equal(containerSize(480), "medium");
  assert.equal(containerSize(719), "medium");
  assert.equal(containerSize(720), "wide");
  assert.equal(modalSize(799), "stacked");
  assert.equal(modalSize(800), "wide");
});

function fakeOutput(overrides = {}) {
  return { id: "o1", selected: false, selection_version: 1, health: "VERIFIED", ...overrides };
}

test("selection controller applies exactly one optimistic save per OUT", async () => {
  let calls = 0;
  let release;
  const api = {
    selection: (id, selected, version) => {
      calls += 1;
      assert.equal(id, "o1");
      assert.equal(selected, true);
      assert.equal(version, 1);
      return new Promise((resolve) => { release = () => resolve({ selected: true, version: 2 }); });
    },
  };
  const events = [];
  const controller = new SelectionController({ api, onChange: (event) => events.push(event) });
  const output = fakeOutput();
  const first = controller.set(output, true);
  assert.equal(output.selected, true);
  const second = controller.set(output, false);
  assert.equal(calls, 1);
  assert.equal(controller.isPending("o1"), true);
  release();
  await first;
  await second;
  assert.equal(output.selected, true);
  assert.equal(output.selection_version, 2);
  assert.equal(controller.isPending("o1"), false);
  assert.deepEqual(events.map((event) => event.state), ["saving", "saved"]);
});

test("selection failure restores the confirmed state and emits one failure", async () => {
  const api = { selection: async () => { throw Object.assign(new Error("offline"), { status: 0, code: "NETWORK_ERROR" }); } };
  const events = [];
  const controller = new SelectionController({ api, onChange: (event) => events.push(event) });
  const output = fakeOutput({ selected: true, selection_version: 4 });
  await controller.set(output, false);
  assert.equal(output.selected, true);
  assert.equal(output.selection_version, 4);
  assert.equal(events.at(-1).state, "failed");
});

test("version conflict reports once and does not rewrite server state", async () => {
  let conflicts = 0;
  const api = { selection: async () => { throw Object.assign(new Error("conflict"), { status: 409, code: "VERSION_CONFLICT" }); } };
  const controller = new SelectionController({
    api,
    onChange: () => {},
    onConflict: () => { conflicts += 1; },
  });
  const output = fakeOutput();
  await controller.set(output, true);
  assert.equal(output.selected, false);
  assert.equal(conflicts, 1);
});

test("different OUTs may save concurrently while one OUT stays disabled", async () => {
  const releases = [];
  const api = {
    selection: (id) => new Promise((resolve) => releases.push(() => resolve({ selected: true, version: 2 }))),
  };
  const controller = new SelectionController({ api });
  const a = fakeOutput({ id: "a" });
  const b = fakeOutput({ id: "b" });
  const first = controller.set(a, true);
  const second = controller.set(b, true);
  assert.equal(controller.isPending("a"), true);
  assert.equal(controller.isPending("b"), true);
  releases[1]();
  await second;
  assert.equal(b.selected, true);
  assert.equal(a.selected, true);
  releases[0]();
  await first;
});

test("event bridge clears pollError and reports reconnection without starting work", async () => {
  globalThis.document = { hidden: false, addEventListener() {}, removeEventListener() {} };
  const store = new Store({ lastSeq: 0 });
  let attempt = 0;
  const api = {
    events: async () => {
      attempt += 1;
      if (attempt === 1) throw new Error("offline");
      return { items: [], last_seq: 3 };
    },
  };
  let reconnects = 0;
  const bridge = new EventBridge(api, store, { onReconnect: () => { reconnects += 1; } });
  bridge.stopped = false;
  await bridge.poll();
  assert.equal(store.get().pollError, "offline");
  await bridge.poll();
  assert.equal(store.get().pollError, null);
  assert.equal(store.get().lastSeq, 3);
  assert.equal(reconnects, 1);
  bridge.stop();
});
