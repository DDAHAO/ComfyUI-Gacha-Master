// Bounded source-list window.
//
// The server is authoritative for the full source list; this class keeps only
// one page of source DTOs (default 40 rows) plus a small list of page-start
// cursors.  Page starts are stable source_order values, never array indexes,
// so logically removed sources leave holes without renumbering.

export const SOURCE_PAGE_SIZE = 40;
export const MAX_SOURCE_PAGE_STARTS = 5000;

export class SourceWindow {
  constructor({ api, jobId, pageSize = SOURCE_PAGE_SIZE, onEvent } = {}) {
    if (!api || !jobId) throw new Error("SourceWindow requires api and jobId");
    this.api = api;
    this.jobId = jobId;
    this.pageSize = Math.max(1, Math.min(200, Number(pageSize) || SOURCE_PAGE_SIZE));
    this.onEvent = onEvent;
    this.items = [];
    this.totalSources = 0;
    this.activeSources = 0;
    this.removedSources = 0;
    this.loaded = false;
    this.loading = false;
    this.error = null;
    this.pageIndex = 0;
    this.jumpOrigin = null;
    this._starts = [0];
    this._hasNext = false;
    this._token = 0;
    this._abort = null;
    this._destroyed = false;
    this._listeners = new Set();
    this._lastPayload = null;
  }

  get hasPrev() { return this.pageIndex > 0 || Boolean(this.jumpOrigin); }
  get hasNext() { return this._hasNext; }
  get pageLabel() {
    return this.jumpOrigin
      ? `从 SRC-${String(this.jumpOrigin).padStart(4, "0")} 起`
      : `第 ${this.pageIndex + 1} 页`;
  }

  subscribe(listener) {
    this._listeners.add(listener);
    return () => this._listeners.delete(listener);
  }

  emit(type = "state") {
    const payload = { type, window: this };
    try { this.onEvent?.(payload); } catch (error) { console.error(error); }
    for (const listener of this._listeners) {
      try { listener(payload); } catch (error) { console.error(error); }
    }
  }

  async _fetch(afterOrder) {
    return this.api.sources(this.jobId, afterOrder, this.pageSize);
  }

  async loadPage(index, { afterOrder = null, force = false } = {}) {
    if (this._destroyed) return;
    if (this.loading && !force) return;
    this._abort?.abort();
    const controller = new AbortController();
    this._abort = controller;
    this._token += 1;
    const token = this._token;
    const start = afterOrder === null ? (this._starts[index] ?? 0) : afterOrder;
    if (index === 0) this._starts = [0];
    this.loading = true;
    this.error = null;
    this.emit("loading");
    try {
      const payload = await this._fetch(start);
      if (this._destroyed || token !== this._token) return;
      this.items = payload.items || [];
      this.pageIndex = index;
      this._starts[index] = start;
      if (index === 0 && start === 0) this.jumpOrigin = null;
      this._hasNext = Boolean(payload.next_after_order);
      if (this._hasNext) {
        this._starts[index + 1] = payload.next_after_order;
        if (this._starts.length > MAX_SOURCE_PAGE_STARTS) this._starts.length = MAX_SOURCE_PAGE_STARTS;
      } else {
        this._starts.splice(index + 1);
      }
      this.totalSources = Number(payload.total_sources ?? this.totalSources ?? 0);
      this.activeSources = Number(payload.active_sources ?? this.items.length);
      this.removedSources = Number(payload.removed_sources ?? 0);
      this.loaded = true;
      this.loading = false;
      this._lastPayload = payload;
      this.emit("state");
    } catch (error) {
      if (this._destroyed || token !== this._token || error?.name === "AbortError") return;
      this.error = error;
      this.loading = false;
      this.emit("error");
    } finally {
      if (token === this._token) {
        this.loading = false;
        this._abort = null;
      }
    }
  }

  async loadFirst({ force = false } = {}) {
    return this.loadPage(0, { afterOrder: 0, force });
  }

  async next() {
    if (this.loading || !this._hasNext) return;
    return this.loadPage(this.pageIndex + 1, { afterOrder: this._starts[this.pageIndex + 1] });
  }

  async prev() {
    if (this.loading) return;
    if (this.pageIndex <= 0) {
      if (this.jumpOrigin) return this.first();
      return;
    }
    return this.loadPage(this.pageIndex - 1, { afterOrder: this._starts[this.pageIndex - 1] });
  }

  async first() {
    if (this.loading) return;
    return this.loadPage(0, { afterOrder: 0 });
  }

  /**
   * Jump to the page containing a stable source_order.  The window restarts
   * at that cursor; it does not load every preceding source into memory.
   */
  async jumpToOrder(order) {
    const target = Math.max(1, Math.floor(Number(order) || 1));
    if (this.loading) return;
    this.jumpOrigin = target > 1 ? target : null;
    await this.loadPage(0, { afterOrder: target - 1 });
  }

  /** Reload the current page in place; used by polling and selection sync. */
  async refresh() {
    if (this.loading) return;
    const start = this._starts[this.pageIndex] ?? 0;
    return this.loadPage(this.pageIndex, { afterOrder: start, force: true });
  }

  /** Apply a fresh page payload without another request (e.g. after append). */
  apply(payload) {
    if (!payload) return;
    this.items = payload.items || [];
    this.totalSources = Number(payload.total_sources ?? this.totalSources ?? 0);
    this.activeSources = Number(payload.active_sources ?? this.items.length);
    this.removedSources = Number(payload.removed_sources ?? 0);
    this._hasNext = Boolean(payload.next_after_order);
    this.loaded = true;
    this._lastPayload = payload;
    this.emit("state");
  }

  destroy() {
    this._destroyed = true;
    this._abort?.abort();
    this._abort = null;
    this._listeners.clear();
  }
}
