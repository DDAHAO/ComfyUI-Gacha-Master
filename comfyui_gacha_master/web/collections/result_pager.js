// Bounded, cancellable result pagination shared by the detail view and the
// compare modal.
//
// One instance owns the cursor state for one (source, filter) pair.  It keeps:
//   * at most MAX_CACHED_PAGES result pages (DTO cache),
//   * a lightweight page-start cursor list for navigation (no item arrays),
//   * one in-flight request token, so retries and rapid clicks cannot advance
//     the cursor twice or let an old response enter a newer view.
//
// Consumers render only the current page; DOM bounds are the view's concern.

export const RESULT_PAGE_SIZE = 50;
export const MAX_CACHED_PAGES = 3;
export const GLOBAL_RESULT_DTO_LIMIT = 1000;
export const MAX_TRACKED_PAGES = 2000;

const ALL_PAGERS = new Set();

function countCached(pager) {
  let total = 0;
  for (const page of pager._pages.values()) total += page.items.length;
  return total;
}

function trimGlobal(requireUnderLimit = true) {
  if (!requireUnderLimit) return;
  let total = 0;
  for (const pager of ALL_PAGERS) total += countCached(pager);
  if (total <= GLOBAL_RESULT_DTO_LIMIT) return;
  const candidates = [...ALL_PAGERS].sort((a, b) => a._lastAccess - b._lastAccess);
  for (const pager of candidates) {
    while (total > GLOBAL_RESULT_DTO_LIMIT) {
      const evicted = pager._evictOnePage();
      if (!evicted) break;
      total -= evicted;
    }
    if (total <= GLOBAL_RESULT_DTO_LIMIT) return;
  }
}

export class ResultPager {
  constructor({ api, sourceId, pageSize = RESULT_PAGE_SIZE, includeUnhealthy = false, maxPages = MAX_CACHED_PAGES, onEvent } = {}) {
    if (!api || !sourceId) throw new Error("ResultPager requires api and sourceId");
    this.api = api;
    this.sourceId = sourceId;
    this.pageSize = Math.max(1, Math.min(200, Number(pageSize) || RESULT_PAGE_SIZE));
    this.includeUnhealthy = Boolean(includeUnhealthy);
    this.maxPages = Math.max(1, Math.min(MAX_CACHED_PAGES, Number(maxPages) || MAX_CACHED_PAGES));
    this.onEvent = onEvent;
    this._pages = new Map();
    this._starts = [null];       // page index -> forward cursor (null = beginning)
    this._offsets = new Map();   // page index -> filtered ordinal start
    this._current = 0;
    this._nextCursor = null;     // known continuation after the last tracked page
    this._loading = false;
    this._error = null;
    this._errorIndex = null;
    this._errorCursor = null;
    this._errorOffset = null;
    this._token = 0;
    this._abort = null;
    this._destroyed = false;
    this._lastAccess = Date.now();
    this._revision = null;
    this._listeners = new Set();
    ALL_PAGERS.add(this);
  }

  // ---------------------------------------------------------------- state
  get currentIndex() { return this._current; }
  get loaded() { return this._pages.has(this._current); }
  get loading() { return this._loading; }
  get error() { return this._error; }
  get hasPrev() { return this._current > 0; }
  get hasNext() {
    const page = this._pages.get(this._current);
    return Boolean((page && page.nextCursor) || this._starts[this._current + 1]);
  }
  get cachedPages() { return this._pages.size; }
  get cachedItems() { return countCached(this); }
  get items() { return this._pages.get(this._current)?.items || []; }
  get pageCount() { return Math.max(this._starts.length, this._current + 1); }
  /** Filtered ordinal of this page start (when known). */
  get currentOffset() {
    const offset = this._offsets.get(this._current);
    return offset === undefined ? null : offset;
  }
  get total() {
    const total = this._pages.get(this._current)?.total;
    return total === undefined ? null : total;
  }

  subscribe(listener) {
    this._listeners.add(listener);
    return () => this._listeners.delete(listener);
  }

  emit(type = "state", detail = {}) {
    const payload = { type, pager: this, ...detail };
    try { this.onEvent?.(payload); } catch (error) { console.error(error); }
    for (const listener of this._listeners) {
      try { listener(payload); } catch (error) { console.error(error); }
    }
  }

  _assertAlive() {
    if (this._destroyed) throw Object.assign(new Error("pager destroyed"), { name: "AbortError" });
  }

  _requestParams(cursor, offset = null) {
    const params = {
      includeUnhealthy: this.includeUnhealthy,
      limit: this.pageSize,
    };
    if (offset !== null && offset !== undefined) params.offset = Math.max(0, Math.floor(offset));
    else params.cursor = cursor || undefined;
    return params;
  }

  async _fetch(startCursor, offset = null) {
    return this.api.outputs(this.sourceId, this._requestParams(startCursor, offset));
  }

  _storePage(index, startCursor, payload, token) {
    const items = (payload.items || []).filter((item) => item && item.id);
    if (token !== this._token || this._destroyed) return null;
    // De-duplicate against other cached pages.  A page range should not
    // overlap, but a revision change can expose the same OUT twice.
    const seen = new Set();
    for (const [pageIndex, page] of this._pages) {
      if (pageIndex === index) continue;
      for (const item of page.items) seen.add(item.id);
    }
    const unique = [];
    for (const item of items) {
      if (seen.has(item.id)) continue;
      seen.add(item.id);
      unique.push(item);
    }
    const page = {
      index,
      startCursor: startCursor || null,
      items: unique,
      nextCursor: payload.next_cursor || null,
      prevCursor: payload.prev_cursor || null,
      offset: Number.isFinite(Number(payload.offset)) ? Number(payload.offset) : null,
      total: Number.isFinite(Number(payload.total)) ? Number(payload.total) : null,
      eventSeq: payload.event_seq,
      loadedAt: Date.now(),
    };
    this._pages.set(index, page);
    if (page.offset !== null) this._offsets.set(index, page.offset);
    this._starts[index] = startCursor || null;
    if (payload.next_cursor) {
      this._starts[index + 1] = payload.next_cursor;
      this._nextCursor = payload.next_cursor;
    } else {
      // The server proved there is no newer row.  Any page-start cursors we
      // had tracked after this page are stale.
      this._nextCursor = null;
      if (index + 1 < this._starts.length) this._starts.splice(index + 1);
    }
    this._trimPages();
    trimGlobal();
    return page;
  }

  _trimPages() {
    while (this._pages.size > this.maxPages) {
      const evicted = this._evictOnePage();
      if (!evicted) break;
    }
  }

  /** Evict the cached page farthest from the current page (never the current). */
  _evictOnePage() {
    let victim = null;
    let distance = -1;
    for (const [index, page] of this._pages) {
      if (index === this._current) continue;
      const d = Math.abs(index - this._current);
      if (d > distance) {
        distance = d;
        victim = index;
      }
    }
    if (victim === null) return 0;
    const page = this._pages.get(victim);
    const count = page ? page.items.length : 0;
    this._pages.delete(victim);
    return count;
  }

  _markAccess() {
    this._lastAccess = Date.now();
  }

  /** Fetch and show a page by index using its tracked start cursor. */
  async goTo(index, { force = false } = {}) {
    this._assertAlive();
    const target = Math.max(0, Math.floor(index));
    this._markAccess();
    if (!force && target === this._current && this._pages.has(target)) {
      this.emit("state");
      return this._pages.get(target);
    }
    const cached = !force ? this._pages.get(target) : null;
    if (cached) {
      this._current = target;
      this.emit("state");
      return cached;
    }
    if (this._loading) {
      // One in-flight read per pager.  A second click is a no-op, not a
      // second cursor advance.
      return this._pages.get(this._current) || null;
    }
    const startCursor = this._starts[target] ?? null;
    if (target > 0 && startCursor === null) {
      // We do not know how to reach this page without a stable boundary.
      return this._pages.get(this._current) || null;
    }
    return this._load(target, startCursor, { mode: "replace" });
  }

  async loadFirst({ force = false } = {}) {
    return this.goTo(0, { force });
  }

  async next() {
    this._assertAlive();
    this._markAccess();
    if (this._loading) return this.items;
    const page = this._pages.get(this._current);
    let target = this._current + 1;
    let startCursor = page?.nextCursor ?? this._starts[target];
    if (!startCursor) return this.items;
    if (!this._pages.has(target) && this._starts[target] === undefined) {
      this._starts[target] = startCursor;
    }
    return this._load(target, startCursor, { mode: "next" });
  }

  async prev() {
    this._assertAlive();
    this._markAccess();
    if (this._loading || this._current <= 0) return this.items;
    const target = this._current - 1;
    const startCursor = this._starts[target] ?? null;
    if (startCursor === null && target === 0) {
      return this.goTo(0);
    }
    if (startCursor === null) {
      // A restored page may have no forward start cursor yet.  The same
      // filtered ordinal range is still reachable with an explicit offset.
      const currentOffset = this._offsets.get(this._current);
      if (currentOffset !== undefined && currentOffset > 0) {
        return this._load(target, null, {
          mode: "prev",
          offset: Math.max(0, currentOffset - this.pageSize),
        });
      }
      return this.items;
    }
    return this._load(target, startCursor, { mode: "prev" });
  }

  /** Retry exactly the page that failed, with the same boundary. */
  async retry() {
    if (this._loading) return this.items;
    const index = this._errorIndex ?? this._current;
    if (this._errorOffset !== null && this._errorOffset !== undefined) {
      return this._load(index, null, { mode: "replace", force: true, offset: this._errorOffset });
    }
    const started = this._errorCursor ?? this._starts[index] ?? null;
    const offset = started === null ? this._offsets.get(index) : undefined;
    return this._load(index, started, {
      mode: "replace",
      force: true,
      ...(offset === undefined ? {} : { offset }),
    });
  }

  /**
   * Restore a saved result page by filtered ordinal.  The current output is
   * returned by identity; if it vanished, the first item of that page is used.
   */
  async restore({ offset = 0, outputId = null } = {}) {
    this._assertAlive();
    this._abort?.abort();
    this._token += 1;
    this._loading = false;
    const start = Math.max(0, Math.floor(Number(offset) || 0));
    const index = Math.floor(start / this.pageSize);
    this._pages.clear();
    this._starts = [];
    this._offsets.clear();
    this._current = index;
    this._nextCursor = null;
    this._error = null;
    this._errorIndex = null;
    this._errorCursor = null;
    this._errorOffset = null;
    const page = await this._load(index, null, {
      mode: "restore",
      offset: index * this.pageSize,
    });
    if (!page) return null;
    let outputIndex = 0;
    if (outputId) {
      const found = page.items.findIndex((item) => item.id === outputId);
      if (found >= 0) outputIndex = found;
    }
    return { page, index, outputIndex };
  }

  async _load(index, startCursor, { mode, offset = null }) {
    this._assertAlive();
    this._abort?.abort();
    const controller = new AbortController();
    this._abort = controller;
    this._token += 1;
    const token = this._token;
    this._loading = true;
    this._error = null;
    this._errorIndex = null;
    this._errorCursor = null;
    this._errorOffset = null;
    this.emit("loading", { index });
    try {
      const payload = await this.api.outputs(this.sourceId, {
        ...this._requestParams(startCursor, offset),
        signal: controller.signal,
      });
      if (this._destroyed || token !== this._token) return null;
      const page = this._storePage(index, startCursor, payload || {}, token);
      if (!page) return null;
      if (offset !== null && offset !== undefined && page.offset === null) {
        page.offset = Math.max(0, Math.floor(offset));
        this._offsets.set(index, page.offset);
      }
      this._current = index;
      this._loading = false;
      this.emit("state", { index });
      return page;
    } catch (error) {
      if (this._destroyed || token !== this._token || error?.name === "AbortError") return null;
      this._error = error;
      this._errorIndex = index;
      this._errorCursor = startCursor || null;
      this._errorOffset = offset;
      this._loading = false;
      // Keep previously loaded pages usable; the failed page remains retryable
      // with its original boundary.
      this.emit("error", { index, error });
      return null;
    } finally {
      if (token === this._token) {
        // Only the current token may clear the loading state.
        this._loading = false;
        this._abort = null;
      }
    }
  }

  /**
   * Server facts changed.  Cursor starts remain stable, so only the DTO pages
   * are dropped; the current page is reloaded in place (no jump to page 1).
   */
  async invalidate({ reload = true, revision = null } = {}) {
    if (this._destroyed) return [];
    this._pages.clear();
    this._revision = revision ?? this._revision;
    this._nextCursor = null;
    if (reload) return this.retry();
    this.emit("state");
    return [];
  }

  setIncludeUnhealthy(value) {
    const next = Boolean(value);
    if (next === this.includeUnhealthy) return;
    this.includeUnhealthy = next;
    this._abort?.abort();
    this._token += 1;
    this._loading = false;
    this._error = null;
    this._errorIndex = null;
    this._errorCursor = null;
    this._errorOffset = null;
    this._pages.clear();
    this._starts = [null];
    this._offsets.clear();
    this._current = 0;
    this._nextCursor = null;
    this.emit("state");
  }

  /** Apply authoritative per-OUT state to every cached page. */
  applyStates(statePayload, { isPending } = {}) {
    const byId = new Map((statePayload.items || []).map((item) => [item.output_id, item]));
    const changed = [];
    for (const page of this._pages.values()) {
      for (const output of page.items) {
        const fresh = byId.get(output.id);
        if (!fresh) continue;
        if (isPending?.(output.id)) continue;
        const version = Number(fresh.selection_version ?? 0);
        const currentVersion = Number(output.selection_version ?? 0);
        // A read older than the last confirmed write must not undo it.
        if (version < currentVersion) continue;
        const nextSelected = Boolean(fresh.selected);
        const nextHealth = fresh.health || output.health;
        if (
          nextSelected !== Boolean(output.selected)
          || version !== currentVersion
          || nextHealth !== output.health
        ) {
          output.selected = nextSelected;
          output.selection_version = version;
          output.health = nextHealth;
          changed.push(output);
        }
      }
    }
    return changed;
  }

  allCachedItems() {
    const result = [];
    for (const [index, page] of this._pages) {
      for (const item of page.items) result.push({ pageIndex: index, item });
    }
    return result;
  }

  destroy() {
    this._destroyed = true;
    this._abort?.abort();
    this._abort = null;
    this._pages.clear();
    this._listeners.clear();
    ALL_PAGERS.delete(this);
  }
}

/** Total DTOs currently retained across every live pager (diagnostics/tests). */
export function globalCachedResultCount() {
  let total = 0;
  for (const pager of ALL_PAGERS) total += countCached(pager);
  return total;
}

export function livePagerCount() {
  return ALL_PAGERS.size;
}
