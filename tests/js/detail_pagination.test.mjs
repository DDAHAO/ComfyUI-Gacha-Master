import assert from "node:assert/strict";
import test from "node:test";

import {
  ResultPager, globalCachedResultCount, livePagerCount, MAX_CACHED_PAGES,
} from "../../comfyui_gacha_master/web/collections/result_pager.js";
import { SourceWindow } from "../../comfyui_gacha_master/web/collections/source_window.js";

// ---------------------------------------------------------------- fixtures
function makeOutputs(count, perDraw = 5) {
  const items = [];
  for (let index = 0; index < count; index += 1) {
    const draw = Math.floor(index / perDraw) + 1;
    const out = (index % perDraw) + 1;
    items.push({
      id: `OUT-${draw}-${out}`,
      draw_no: draw,
      out_no: out,
      selected: false,
      selection_version: 0,
      health: "VERIFIED",
    });
  }
  return items;
}

function cursorIndex(items, cursor) {
  if (!cursor) return 0;
  const found = items.findIndex((item) => item.draw_no === cursor.draw_no && item.out_no === cursor.out_no);
  return found < 0 ? items.length : found + 1;
}

function pageOf(items, cursor, limit) {
  const start = cursorIndex(items, cursor);
  const page = items.slice(start, start + limit);
  const last = page[page.length - 1];
  const max = items[items.length - 1];
  const next = page.length && (last.draw_no !== max.draw_no || last.out_no !== max.out_no)
    ? { draw_no: last.draw_no, out_no: last.out_no }
    : null;
  const first = page[0];
  const min = items[0];
  const prev = page.length && (first.draw_no !== min.draw_no || first.out_no !== min.out_no)
    ? { draw_no: first.draw_no, out_no: first.out_no }
    : null;
  return {
    items: page.map((item) => ({ ...item })),
    next_cursor: next,
    prev_cursor: prev,
  };
}

class FakeOutputApi {
  constructor(items, { delayForCursor = null, failOnceForCursor = null } = {}) {
    this.items = items;
    this.calls = [];
    this.delayForCursor = delayForCursor;
    this.failOnceForCursor = failOnceForCursor;
    this.failed = false;
  }
  async outputs(sourceId, { cursor, limit = 50, signal } = {}) {
    this.calls.push(cursor || null);
    const key = cursor ? `${cursor.draw_no}:${cursor.out_no}` : "start";
    if (this.delayForCursor && key === this.delayForCursor.key) {
      await new Promise((resolve, reject) => {
        const timer = setTimeout(resolve, this.delayForCursor.ms);
        signal?.addEventListener("abort", () => {
          clearTimeout(timer);
          reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
        }, { once: true });
      });
    }
    if (this.failOnceForCursor && !this.failed && key === this.failOnceForCursor) {
      this.failed = true;
      throw Object.assign(new Error("boom"), { status: 500 });
    }
    if (signal?.aborted) throw Object.assign(new Error("aborted"), { name: "AbortError" });
    return pageOf(this.items, cursor, limit);
  }
}

async function drain(pager) {
  const seen = [];
  for (const item of pager.items) seen.push(item);
  while (pager.hasNext) {
    await pager.next();
    for (const item of pager.items) seen.push(item);
  }
  return seen;
}

// ---------------------------------------------------------------- PG01
test("PG01: 300 OUT pages remain usable and every OUT appears once in order", async () => {
  const items = makeOutputs(300);
  const api = new FakeOutputApi(items);
  const pager = new ResultPager({ api, sourceId: "src", pageSize: 50 });
  await pager.loadFirst();
  const seen = await drain(pager);
  assert.equal(seen.length, 300);
  assert.deepEqual(seen.map((item) => item.id), items.map((item) => item.id));
  assert.equal(new Set(seen.map((item) => item.id)).size, 300);
  assert.equal(pager.hasNext, false);
  assert.equal(pager.currentIndex, 5);
  assert.ok(pager.cachedPages <= MAX_CACHED_PAGES);
  pager.destroy();
});

// ---------------------------------------------------------------- PG02
test("PG02: empty, exact-page and one-past-page boundaries end correctly", async () => {
  for (const count of [0, 49, 50, 51, 100, 101]) {
    const api = new FakeOutputApi(makeOutputs(count));
    const pager = new ResultPager({ api, sourceId: "src", pageSize: 50 });
    await pager.loadFirst();
    const seen = await drain(pager);
    assert.equal(seen.length, count, `count=${count}`);
    assert.equal(pager.hasNext, false, `count=${count}`);
    assert.equal(pager.error, null);
    pager.destroy();
  }
});

test("PG02: 1000 OUT complete traversal has no gaps", async () => {
  const items = makeOutputs(1000);
  const api = new FakeOutputApi(items);
  const pager = new ResultPager({ api, sourceId: "src", pageSize: 50 });
  await pager.loadFirst();
  const seen = await drain(pager);
  assert.equal(seen.length, 1000);
  assert.equal(seen[0].id, "OUT-1-1");
  assert.equal(seen[999].id, items[999].id);
  assert.equal(new Set(seen.map((item) => item.id)).size, 1000);
  pager.destroy();
});

// ---------------------------------------------------------------- PG03
test("PG03: a failed next page keeps cards, does not advance and retries the same cursor", async () => {
  const items = makeOutputs(300);
  const api = new FakeOutputApi(items, { failOnceForCursor: "10:5" });
  const pager = new ResultPager({ api, sourceId: "src", pageSize: 50 });
  await pager.loadFirst();
  const firstPageIds = pager.items.map((item) => item.id);
  assert.equal(await pager.next(), null);
  assert.equal(pager.error.message, "boom");
  assert.equal(pager.currentIndex, 0);
  assert.deepEqual(pager.items.map((item) => item.id), firstPageIds);
  const before = api.calls.length;
  await pager.retry();
  assert.equal(pager.error, null);
  assert.equal(pager.currentIndex, 1);
  assert.equal(api.calls[before].draw_no, 10);
  assert.ok(api.calls[before].out_no === 5);
  assert.equal(pager.items.length, 50);
  const seen = [firstPageIds, pager.items.map((item) => item.id)];
  const more = await drain(pager);
  const all = [...seen[0], ...seen[1], ...more.slice(50)];
  assert.equal(new Set(all).size, 300);
  pager.destroy();
});

// ---------------------------------------------------------------- PG04
test("PG04: rapid clicks create one in-flight request and never skip a page", async () => {
  const items = makeOutputs(300);
  const api = new FakeOutputApi(items, { delayForCursor: { key: "10:5", ms: 60 } });
  const pager = new ResultPager({ api, sourceId: "src", pageSize: 50 });
  await pager.loadFirst();
  assert.equal(api.calls.length, 1);
  const first = pager.next();
  const second = pager.next();
  const third = pager.prev();
  const fourth = pager.loadFirst();
  await Promise.all([first, second, third, fourth]);
  assert.equal(api.calls.length, 2, "only the first next() may issue a request");
  assert.equal(pager.currentIndex, 1);
  assert.equal(pager.items[0].id, "OUT-11-1");
  await pager.next();
  assert.equal(pager.items[0].id, "OUT-21-1");
  pager.destroy();
});

// ---------------------------------------------------------------- PG05
test("PG05: a delayed old page cannot enter a new filter or a destroyed view", async () => {
  const items = makeOutputs(300);
  const api = new FakeOutputApi(items, { delayForCursor: { key: "10:5", ms: 80 } });
  const pager = new ResultPager({ api, sourceId: "src", pageSize: 50 });
  await pager.loadFirst();
  const pending = pager.next();
  pager.setIncludeUnhealthy(true);
  await pending;
  assert.equal(pager.currentIndex, 0);
  assert.equal(pager.items.length, 0);
  assert.equal(pager.loading, false);
  await pager.loadFirst();
  assert.equal(pager.items.length, 50);
  pager.destroy();
});

test("PG05: invalidation after a revision change reloads the same page, not page one", async () => {
  const api = new FakeOutputApi(makeOutputs(300));
  const pager = new ResultPager({ api, sourceId: "src", pageSize: 50 });
  await pager.loadFirst();
  await pager.next();
  await pager.next();
  assert.equal(pager.currentIndex, 2);
  api.items = makeOutputs(301);
  const page = await pager.invalidate({ reload: true, revision: 7 });
  assert.equal(pager.currentIndex, 2);
  assert.equal(page.items[0].id, "OUT-21-1");
  pager.destroy();
});

// ---------------------------------------------------------------- bounded cache
test("cache keeps at most three pages per source and 1000 DTOs globally", async () => {
  const items = makeOutputs(500);
  const pagers = [];
  for (let index = 0; index < 10; index += 1) {
    const pager = new ResultPager({ api: new FakeOutputApi(items), sourceId: `src-${index}`, pageSize: 50 });
    pagers.push(pager);
    await pager.loadFirst();
    for (let page = 0; page < 4; page += 1) {
      if (pager.hasNext) await pager.next();
    }
    assert.ok(pager.cachedPages <= MAX_CACHED_PAGES, `pager ${index} cached ${pager.cachedPages}`);
  }
  assert.ok(globalCachedResultCount() <= 1000, `global ${globalCachedResultCount()}`);
  assert.equal(livePagerCount(), 10);
  for (const pager of pagers) pager.destroy();
  assert.equal(livePagerCount(), 0);
  assert.equal(globalCachedResultCount(), 0);
});

// ---------------------------------------------------------------- selection state
test("authoritative OUT state never lets an older version undo a newer write", async () => {
  const items = makeOutputs(60);
  const api = new FakeOutputApi(items);
  const pager = new ResultPager({ api, sourceId: "src", pageSize: 50 });
  await pager.loadFirst();
  const output = pager.items[0];
  output.selected = true;
  output.selection_version = 5;
  const changed = pager.applyStates({
    items: [{ output_id: output.id, selected: false, selection_version: 4, health: "VERIFIED" }],
  });
  assert.equal(changed.length, 0);
  assert.equal(output.selected, true);
  const changed2 = pager.applyStates({
    items: [{ output_id: output.id, selected: false, selection_version: 6, health: "CORRUPT" }],
  });
  assert.equal(changed2.length, 1);
  assert.equal(output.selected, false);
  assert.equal(output.health, "CORRUPT");
  const pendingChanged = pager.applyStates({
    items: [{ output_id: output.id, selected: true, selection_version: 7, health: "VERIFIED" }],
  }, { isPending: () => true });
  assert.equal(pendingChanged.length, 0);
  pager.destroy();
});

// ---------------------------------------------------------------- source window
function makeSources(count, { removeEvery = 0 } = {}) {
  const rows = [];
  for (let order = 1; order <= count; order += 1) {
    if (removeEvery && order % removeEvery === 0) continue;
    rows.push({
      id: `SRC-${order}`,
      source_order: order,
      original_name: `src-${order}.png`,
      status: "ACTIVE",
      summary: {},
      review: {},
    });
  }
  return rows;
}

class FakeSourceApi {
  constructor(rows, { delayForAfter = null } = {}) {
    this.rows = rows;
    this.calls = [];
    this.delayForAfter = delayForAfter;
  }
  async sources(jobId, afterOrder = 0, limit = 40, options = {}) {
    this.calls.push(afterOrder);
    if (this.delayForAfter && this.delayForAfter.after === afterOrder) {
      await new Promise((resolve) => setTimeout(resolve, this.delayForAfter.ms));
    }
    const remaining = this.rows.filter((row) => row.source_order > afterOrder);
    const items = remaining.slice(0, limit);
    const hasMore = remaining.length > items.length;
    return {
      items,
      next_after_order: hasMore && items.length ? items[items.length - 1].source_order : null,
      total_sources: this.rows.length,
      active_sources: this.rows.length,
      removed_sources: 0,
    };
  }
}

test("SRC01/SRC02: 201 and 500 sources are reachable with stable order and no duplicates", async () => {
  for (const count of [201, 500]) {
    const rows = makeSources(count);
    const api = new FakeSourceApi(rows);
    const window = new SourceWindow({ api, jobId: "job", pageSize: 40 });
    await window.loadFirst();
    const seen = window.items.map((row) => row.id);
    while (window.hasNext) {
      await window.next();
      seen.push(...window.items.map((row) => row.id));
    }
    assert.equal(seen.length, rows.length);
    assert.deepEqual(seen, rows.map((row) => row.id));
    assert.equal(new Set(seen).size, rows.length);
    assert.equal(window.items[window.items.length - 1].source_order, count);
    window.destroy();
  }
});

test("SRC03: logical removals leave holes and never renumber later sources", async () => {
  const rows = makeSources(120, { removeEvery: 7 });
  const window = new SourceWindow({ api: new FakeSourceApi(rows), jobId: "job", pageSize: 40 });
  await window.loadFirst();
  const seen = [];
  while (true) {
    seen.push(...window.items.map((row) => row.source_order));
    if (!window.hasNext) break;
    await window.next();
  }
  assert.deepEqual(seen, rows.map((row) => row.source_order));
  assert.ok(seen.includes(120));
  const orders = new Set(seen);
  for (const removed of [7, 14, 21, 28, 119]) assert.equal(orders.has(removed), false);
  window.destroy();
});

test("SRC04: jump uses a stable source_order cursor without loading earlier rows", async () => {
  const rows = makeSources(500);
  const api = new FakeSourceApi(rows);
  const window = new SourceWindow({ api, jobId: "job", pageSize: 40 });
  await window.loadFirst();
  await window.jumpToOrder(201);
  assert.equal(window.items[0].source_order, 201);
  assert.equal(api.calls.at(-1), 200);
  assert.ok(window.items.length <= 40);
  assert.ok(window.items.every((row) => row.source_order >= 201));
  await window.next();
  assert.ok(window.items[0].source_order > 201);
  await window.prev();
  assert.equal(window.items[0].source_order, 201);
  await window.prev();
  assert.equal(window.items[0].source_order, 1);
  assert.equal(window.jumpOrigin, null);
  window.destroy();
});

test("SRC04: refresh keeps the current page instead of snapping to the first page", async () => {
  const rows = makeSources(200);
  const window = new SourceWindow({ api: new FakeSourceApi(rows), jobId: "job", pageSize: 40 });
  await window.loadFirst();
  await window.next();
  await window.next();
  const firstOrder = window.items[0].source_order;
  await window.refresh();
  assert.equal(window.pageIndex, 2);
  assert.equal(window.items[0].source_order, firstOrder);
  window.destroy();
});

test("SRC04: a stale response from another job cannot overwrite this window", async () => {
  const a = new SourceWindow({ api: new FakeSourceApi(makeSources(60)), jobId: "A", pageSize: 40 });
  const b = new SourceWindow({ api: new FakeSourceApi(makeSources(90)), jobId: "B", pageSize: 40 });
  await Promise.all([a.loadFirst(), b.loadFirst()]);
  assert.equal(a.items.length, 40);
  assert.equal(b.items.length, 40);
  await a.next();
  assert.ok(a.items.every((row) => row.id.startsWith("SRC-")));
  assert.equal(a.items[0].source_order, 41);
  assert.equal(b.items[0].source_order, 1);
  a.destroy();
  b.destroy();
});
