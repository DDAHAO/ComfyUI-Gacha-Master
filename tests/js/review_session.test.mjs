import assert from "node:assert/strict";
import test from "node:test";

import { ResultPager } from "../../comfyui_gacha_master/web/collections/result_pager.js";
import { ReviewSessionClient } from "../../comfyui_gacha_master/web/review_session.js";
import { SelectionController } from "../../comfyui_gacha_master/web/selection_controller.js";

// ------------------------------------------------------------ fixtures
function source(order, id = `src-${order}`) {
  return {
    id,
    job_id: "job",
    source_order: order,
    original_name: `photo-${order}.png`,
    status: "ACTIVE",
    size: 100,
    sha256: "a".repeat(64),
    width: 100,
    height: 80,
    image_format: "PNG",
    result_revision: 1,
    selection_version: 0,
    created_at: "2026-11-16T00:00:00Z",
    removed_at: null,
    summary: { available: 2, selected: 0 },
    review: { status: "available_unreviewed", decision: null, confirmed_at: null },
    image_url: `/sources/${id}/image`,
    thumbnail_url: `/sources/${id}/thumbnail`,
  };
}

function outputsFor(sourceId, count = 2) {
  return Array.from({ length: count }, (_, index) => ({
    id: `${sourceId}-out-${index + 1}`,
    source_id: sourceId,
    draw_no: index + 1,
    out_no: 1,
    attempt_no: 1,
    health: "VERIFIED",
    selected: false,
    selection_version: 0,
    width: 100,
    height: 80,
    size: 50,
    sha256: "b".repeat(64),
    image_url: `/outputs/${sourceId}-out-${index + 1}/image`,
    thumbnail_url: `/outputs/${sourceId}-out-${index + 1}/thumbnail`,
  }));
}

function sessionFor(current, { version = 1, history = [] } = {}) {
  return {
    id: "sess-1",
    job_id: "job",
    scope: "pending",
    version,
    break_type: "IN_GROUP",
    current: {
      source_id: current.id,
      source_order: current.source_order,
      output_id: null,
      result_offset: 0,
    },
    confirmed: null,
    history,
    finished: false,
    created_at: "2026-11-16T00:00:00Z",
    updated_at: "2026-11-16T00:00:00Z",
  };
}

class FakeReviewApi {
  constructor({ sources, outputs, session = null }) {
    this.sources = sources;
    this.outputs = outputs;
    this.session = session;
    this.summary = {
      total_sources: sources.length,
      active_sources: sources.length,
      reviewable_sources: sources.length,
      pending_sources: sources.length,
      confirmed_sources: 0,
      selected_groups: 0,
      skipped_groups: 0,
      no_result_sources: 0,
      selected_outputs: 0,
      available_outputs: sources.length * 2,
      pending_draws: 0,
    };
    this.positionCalls = [];
    this.decisionCalls = [];
    this.selectionCalls = [];
    this.sessionReads = 0;
    this.positionConflict = false;
    this.decisionHandler = null;
    this.selectionGate = null;
  }

  async reviewSession() {
    this.sessionReads += 1;
    return {
      session: this.session,
      resume: this.resume(),
      summary: { ...this.summary },
      plan_version: 1,
      job_id: "job",
    };
  }

  resume() {
    if (!this.session) return { mode: "NONE", source: this.sources[0] };
    if (this.session.break_type === "AFTER_CONFIRMED") {
      const after = this.session.confirmed?.source_order || 0;
      const next = this.sources.find((item) => item.source_order > after);
      return next
        ? { mode: "NEXT_PENDING", source: next }
        : { mode: "FINISHED", source: null, requeue_available: false };
    }
    const current = this.sources.find((item) => item.id === this.session.current?.source_id);
    return current
      ? { mode: "IN_GROUP", source: current }
      : { mode: "NONE", source: null };
  }

  async openReviewSession() {
    if (!this.session) this.session = sessionFor(this.sources[0]);
    return this.reviewSession();
  }

  async saveReviewPosition(id, body) {
    this.positionCalls.push(body);
    if (this.positionConflict) {
      const error = new Error("session version changed");
      error.status = 409;
      error.code = "VERSION_CONFLICT";
      throw error;
    }
    if (body.expected_version !== this.session.version) {
      const error = new Error("session version changed");
      error.status = 409;
      error.code = "VERSION_CONFLICT";
      throw error;
    }
    const target = this.sources.find((item) => item.id === body.current_source_id);
    let offset = body.result_offset || 0;
    if (body.current_output_id) {
      const list = this.outputs[body.current_source_id] || [];
      const found = list.findIndex((item) => item.id === body.current_output_id);
      if (found >= 0) offset = found;
    }
    const same = this.session.current?.source_id === body.current_source_id;
    const history = [...(this.session.history || [])];
    if (!same && this.session.current?.source_id) {
      history.push({
        source_id: this.session.current.source_id,
        source_order: this.session.current.source_order,
      });
    }
    this.session = {
      ...this.session,
      version: this.session.version + 1,
      break_type: "IN_GROUP",
      current: {
        source_id: body.current_source_id,
        source_order: target?.source_order || 0,
        output_id: body.current_output_id || null,
        result_offset: offset,
      },
      history,
    };
    return this.reviewSession();
  }

  async reviewSourceState(sourceId) {
    const selected = this.selectedBySource?.[sourceId] || 0;
    return {
      source: this.sources.find((item) => item.id === sourceId),
      versions: { result_revision: 1, selection_version: selected, plan_version: 1 },
      available: 2,
      selected,
      selected_output_ids: [],
      selected_truncated: false,
      pending_draws: 0,
      reviewable: true,
      job_id: "job",
    };
  }

  async reviewDecision(id, body, key) {
    this.decisionCalls.push({ body, key });
    if (this.decisionHandler) return this.decisionHandler(this, body);
    const target = this.sources.find((item) => item.id === body.source_id);
    this.session = {
      ...this.session,
      version: this.session.version + 1,
      break_type: "AFTER_CONFIRMED",
      confirmed: {
        source_id: target.id,
        source_order: target.source_order,
        decision: body.decision,
        result_revision: 1,
        selection_version: 0,
        plan_version: 1,
        confirmed_at: "2026-11-16T00:00:00Z",
      },
    };
    const next = this.sources.find((item) => item.source_order > target.source_order) || null;
    this.summary = {
      ...this.summary,
      pending_sources: next ? this.summary.pending_sources - 1 : 0,
      confirmed_sources: (this.summary.confirmed_sources || 0) + 1,
      selected_groups: body.decision === "SELECTED" ? 1 : 0,
      skipped_groups: body.decision === "SKIPPED" ? 1 : 0,
    };
    return {
      session: this.session,
      review: { ...body, selected_count: 1 },
      next: { mode: next ? "NEXT_PENDING" : "FINISHED", source: next },
      summary: this.summary,
      idempotent: false,
    };
  }

  async reviewBack(id, body) {
    assert.equal(body.expected_version, this.session.version);
    const history = [...(this.session.history || [])];
    const previous = history.pop();
    assert.ok(previous, "history must contain a previous group");
    const target = this.sources.find((item) => item.id === previous.source_id);
    this.session = {
      ...this.session,
      version: this.session.version + 1,
      break_type: "IN_GROUP",
      current: {
        source_id: target.id,
        source_order: target.source_order,
        output_id: null,
        result_offset: 0,
      },
      history,
    };
    return this.reviewSession();
  }

  async selection(outputId, selected, expectedVersion) {
    this.selectionCalls.push({ outputId, selected, expectedVersion });
    await this.selectionGate;
    return { output_id: outputId, selected, version: (expectedVersion || 0) + 1, idempotent: false };
  }

  async viewed() { return { output_id: null }; }
}

function pagerFactoryFor(api, { failOnceFor = null, offsets = {} } = {}) {
  const failed = new Set();
  const calls = [];
  const factory = (sourceId, onEvent) => {
    if (failOnceFor === sourceId && !failed.has(sourceId)) {
      failed.add(sourceId);
      throw new Error("next group load failed");
    }
    const allItems = (api.outputs[sourceId] || []).map((item) => ({ ...item }));
    const pager = {
      sourceId,
      allItems,
      items: allItems.slice(0, 50),
      currentIndex: 0,
      currentOffset: offsets[sourceId] || 0,
      hasNext: false,
      hasPrev: Boolean(offsets[sourceId]),
      loading: false,
      cachedPages: 1,
      total: allItems.length,
      error: null,
      restoreCalls: [],
      nextCalls: 0,
      prevCalls: 0,
      destroyed: false,
      async restore({ offset = 0, outputId = null } = {}) {
        pager.restoreCalls.push({ offset, outputId });
        pager.currentOffset = Math.floor(offset / 50) * 50;
        pager.items = allItems.slice(pager.currentOffset, pager.currentOffset + 50);
        const found = outputId ? pager.items.findIndex((item) => item.id === outputId) : 0;
        pager.currentIndex = found < 0 ? offset - pager.currentOffset : found;
        return { page: { items: pager.items }, index: 0, outputIndex: pager.currentIndex };
      },
      async next() { pager.nextCalls += 1; return null; },
      async prev() { pager.prevCalls += 1; return null; },
      async goTo() { return { items: pager.items }; },
      async retry() { return { items: pager.items }; },
      destroy() { pager.destroyed = true; },
      onEvent,
    };
    calls.push(pager);
    return pager;
  };
  factory.calls = calls;
  return factory;
}

function makeClient(api, { sources, outputs, session = null, pagerOptions = {} } = {}) {
  const selection = new SelectionController({ api });
  const factory = pagerFactoryFor(api, pagerOptions);
  const client = new ReviewSessionClient({
    api,
    jobId: "job",
    selection,
    pagerFactory: factory,
  });
  return { client, selection, factory };
}

async function openReady(api, options = {}) {
  const { client, selection, factory } = makeClient(api, options);
  await client.open();
  assert.equal(client.viewState, "ready");
  return { client, selection, factory };
}

// ------------------------------------------------------------- tests
test("opens a new durable session and saves the first preview position", async () => {
  const sources = [source(1), source(2)];
  const outputs = { "src-1": outputsFor("src-1"), "src-2": outputsFor("src-2") };
  const api = new FakeReviewApi({ sources, outputs });
  const { client } = await openReady(api, { sources, outputs });
  assert.equal(client.session.version, 1);
  assert.equal(client.source.id, "src-1");
  assert.equal(client.currentOutput.id, "src-1-out-1");
  const flushed = await client.flushPosition();
  assert.equal(flushed, true);
  assert.equal(api.positionCalls.length, 1);
  assert.equal(api.positionCalls[0].current_source_id, "src-1");
  assert.equal(api.positionCalls[0].current_output_id, "src-1-out-1");
  assert.equal(client.session.version, 2);
  client.close();
});

test("opening from a thumbnail positions the session at that exact result", async () => {
  const sources = [source(1), source(2)];
  const outputs = { "src-1": outputsFor("src-1", 4), "src-2": outputsFor("src-2") };
  const api = new FakeReviewApi({ sources, outputs });
  api.session = sessionFor(sources[1], { version: 4, history: [{ source_id: "src-1", source_order: 1 }] });
  const { client } = makeClient(api, { sources, outputs });
  await client.open({ startSourceId: "src-1", startOutputId: "src-1-out-3" });
  assert.equal(client.viewState, "ready");
  assert.equal(client.source.id, "src-1");
  assert.equal(client.currentOutput.id, "src-1-out-3");
  assert.equal(client.currentIndex, 2);
  const jump = api.positionCalls.at(-1);
  assert.equal(jump.current_source_id, "src-1");
  assert.equal(jump.current_output_id, "src-1-out-3");
  assert.equal(Object.prototype.hasOwnProperty.call(jump, "result_offset"), false);
  assert.equal(client.session.current.output_id, "src-1-out-3");
  assert.equal(client.session.current.result_offset, 2);
  client.close();
});

test("restores the saved result page offset and current output identity", async () => {
  const sources = [source(1), source(2)];
  const outputs = { "src-1": outputsFor("src-1", 60), "src-2": outputsFor("src-2") };
  const api = new FakeReviewApi({ sources, outputs });
  api.session = sessionFor(sources[0]);
  api.session.current.output_id = "src-1-out-52";
  api.session.current.result_offset = 51;
  const { client, factory } = makeClient(api, { sources, outputs, pagerOptions: { offsets: { "src-1": 50 } } });
  await client.open();
  assert.equal(factory.calls[0].restoreCalls[0].offset, 51);
  assert.equal(factory.calls[0].restoreCalls[0].outputId, "src-1-out-52");
  assert.equal(client.currentOutput.id, "src-1-out-52");
  assert.equal(client.currentIndex, 1);
  client.close();
});

test("decide waits for an in-flight selection save and flushes the position first", async () => {
  const sources = [source(1), source(2)];
  const outputs = { "src-1": outputsFor("src-1"), "src-2": outputsFor("src-2") };
  const api = new FakeReviewApi({ sources, outputs });
  let release;
  api.selectionGate = new Promise((resolve) => { release = resolve; });
  api.selectedBySource = { "src-1": 1 };
  const { client } = await openReady(api, { sources, outputs });
  client.toggleCurrent();
  assert.equal(api.selectionCalls.length, 1);
  const pendingDecision = client.decide("SELECTED");
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(api.decisionCalls.length, 0, "no decision before the selection write settles");
  release();
  const ok = await pendingDecision;
  assert.equal(ok, true);
  assert.equal(api.decisionCalls.length, 1);
  const [decision] = api.decisionCalls;
  assert.equal(decision.body.decision, "SELECTED");
  assert.equal(decision.body.expected_result_revision, 1);
  assert.equal(decision.body.expected_plan_version, 1);
  assert.ok(decision.body.expected_session_version >= 2);
  // The next group is opened in the same workspace and its position saved.
  assert.equal(client.source.id, "src-2");
  await client.flushPosition();
  assert.equal(client.session.break_type, "IN_GROUP");
  const versions = api.positionCalls.map((call) => call.expected_version);
  assert.deepEqual(versions, [...versions].sort((a, b) => a - b));
  client.close();
});

test("double activation produces one decision and one advance", async () => {
  const sources = [source(1), source(2), source(3)];
  const outputs = {
    "src-1": outputsFor("src-1"), "src-2": outputsFor("src-2"), "src-3": outputsFor("src-3"),
  };
  const api = new FakeReviewApi({ sources, outputs });
  api.selectedBySource = { "src-1": 1 };
  const { client } = await openReady(api, { sources, outputs });
  const first = client.decide("SELECTED");
  const second = client.decide("SELECTED");
  const results = await Promise.all([first, second]);
  assert.deepEqual(results, [true, false]);
  assert.equal(api.decisionCalls.length, 1);
  assert.equal(client.source.id, "src-2");
  client.close();
});

test("a position conflict stops the decision and never reports saved", async () => {
  const sources = [source(1), source(2)];
  const outputs = { "src-1": outputsFor("src-1"), "src-2": outputsFor("src-2") };
  const api = new FakeReviewApi({ sources, outputs });
  api.selectedBySource = { "src-1": 1 };
  const { client } = await openReady(api, { sources, outputs });
  api.positionConflict = true;
  const ok = await client.decide("SELECTED");
  assert.equal(ok, false);
  assert.equal(api.decisionCalls.length, 0);
  assert.equal(client.conflict.kind, "position");
  assert.notEqual(client.save.position, "saved");
  assert.equal(client.source.id, "src-1");
  client.close();
});

test("a failed next-group load only retries navigation, not the decision", async () => {
  const sources = [source(1), source(2), source(3)];
  const outputs = {
    "src-1": outputsFor("src-1"), "src-2": outputsFor("src-2"), "src-3": outputsFor("src-3"),
  };
  const api = new FakeReviewApi({ sources, outputs });
  api.selectedBySource = { "src-1": 1 };
  const { client } = await openReady(api, {
    sources, outputs, pagerOptions: { failOnceFor: "src-2" },
  });
  const ok = await client.decide("SELECTED");
  assert.equal(ok, true);
  assert.equal(client.viewState, "next_failed");
  assert.equal(client.nextSource.id, "src-2");
  assert.equal(api.decisionCalls.length, 1);
  await client.retryNext();
  assert.equal(client.viewState, "ready");
  assert.equal(client.source.id, "src-2");
  assert.equal(api.decisionCalls.length, 1, "retry must not confirm again");
  client.close();
});

test("an idempotent decision response re-reads the server resume instead of advancing blindly", async () => {
  const sources = [source(1), source(2), source(3)];
  const outputs = {
    "src-1": outputsFor("src-1"), "src-2": outputsFor("src-2"), "src-3": outputsFor("src-3"),
  };
  const api = new FakeReviewApi({ sources, outputs });
  api.selectedBySource = { "src-1": 1 };
  const { client } = await openReady(api, { sources, outputs });
  api.decisionHandler = (self, body) => {
    const target = self.sources.find((item) => item.id === body.source_id);
    self.session = {
      ...self.session,
      version: self.session.version + 1,
      break_type: "AFTER_CONFIRMED",
      confirmed: {
        source_id: target.id, source_order: target.source_order, decision: body.decision,
        result_revision: 1, selection_version: 1, plan_version: 1,
      },
    };
    // The retried response points at group 3, while the authoritative resume
    // is group 2 after another tab already processed the boundary.
    return {
      session: self.session,
      review: { ...body, selected_count: 1 },
      next: { mode: "NEXT_PENDING", source: self.sources[2] },
      summary: self.summary,
      idempotent: true,
    };
  };
  const before = api.sessionReads;
  const ok = await client.decide("SELECTED");
  assert.equal(ok, true);
  assert.ok(api.sessionReads > before, "idempotent path must re-read the session");
  assert.equal(client.source.id, "src-2");
  client.close();
});

test("back reopens the previous group from the persisted history", async () => {
  const sources = [source(1), source(2)];
  const outputs = { "src-1": outputsFor("src-1"), "src-2": outputsFor("src-2") };
  const api = new FakeReviewApi({ sources, outputs });
  api.session = sessionFor(sources[1], {
    version: 3,
    history: [{ source_id: "src-1", source_order: 1 }],
  });
  const { client } = await openReady(api, { sources, outputs });
  await client.flushPosition();
  assert.equal(client.source.id, "src-2");
  const ok = await client.back();
  assert.equal(ok, true);
  assert.equal(client.source.id, "src-1");
  assert.equal(client.session.current.source_id, "src-1");
  client.close();
});

// ---------------------------------------------------- ResultPager offset
class FakeOutputApi {
  constructor(items, { failOnce = false } = {}) {
    this.items = items;
    this.failOnce = failOnce;
    this.calls = [];
  }
  async outputs(sourceId, { cursor, limit = 50, offset, signal } = {}) {
    this.calls.push({ cursor, offset });
    if (this.failOnce) {
      this.failOnce = false;
      throw Object.assign(new Error("boom"), { status: 500 });
    }
    const start = offset !== undefined && offset !== null
      ? Number(offset)
      : (cursor
        ? this.items.findIndex((item) => item.draw_no === cursor.draw_no && item.out_no === cursor.out_no) + 1
        : 0);
    const page = this.items.slice(start, start + limit);
    const last = page[page.length - 1];
    const max = this.items[this.items.length - 1];
    return {
      items: page,
      offset: start,
      total: this.items.length,
      next_cursor: page.length && (last.draw_no !== max.draw_no || last.out_no !== max.out_no)
        ? { draw_no: last.draw_no, out_no: last.out_no }
        : null,
      prev_cursor: start > 0 ? { draw_no: page[0].draw_no, out_no: page[0].out_no } : null,
    };
  }
}

function manyOutputs(count) {
  return Array.from({ length: count }, (_, index) => ({
    id: `OUT-${index + 1}`,
    draw_no: Math.floor(index / 5) + 1,
    out_no: (index % 5) + 1,
    selected: false,
    selection_version: 0,
    health: "VERIFIED",
  }));
}

test("ResultPager.restore reaches a saved page by offset and prev uses the offset fallback", async () => {
  const items = manyOutputs(120);
  const api = new FakeOutputApi(items);
  const pager = new ResultPager({ api, sourceId: "src", pageSize: 50 });
  const restored = await pager.restore({ offset: 51, outputId: "OUT-52" });
  assert.equal(restored.index, 1);
  assert.equal(restored.outputIndex, 1);
  assert.equal(pager.currentOffset, 50);
  assert.equal(pager.items[0].id, "OUT-51");
  assert.equal(pager.total, 120);
  await pager.prev();
  assert.equal(pager.currentOffset, 0);
  assert.equal(pager.items[0].id, "OUT-1");
  assert.equal(pager.hasPrev, false);
  pager.destroy();
});

test("ResultPager.retry after an offset restore keeps the same ordinal page", async () => {
  const items = manyOutputs(120);
  const api = new FakeOutputApi(items, { failOnce: true });
  const pager = new ResultPager({ api, sourceId: "src", pageSize: 50 });
  const failed = await pager.restore({ offset: 60 });
  assert.equal(failed, null);
  assert.ok(pager.error);
  const page = await pager.retry();
  assert.ok(page);
  assert.equal(pager.currentOffset, 50);
  assert.equal(pager.items[0].id, "OUT-51");
  pager.destroy();
});
