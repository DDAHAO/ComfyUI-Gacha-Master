// Minimal observable state and a bounded event poller.
// The backend is authoritative; views refresh snapshots after gaps and on
// relevant event sequences. Closing/hiding the page never stops the backend.

export class Store {
  constructor(initial) {
    this.state = { ...initial };
    this.listeners = new Set();
  }
  get() { return this.state; }
  set(patch) {
    this.state = { ...this.state, ...patch };
    for (const listener of this.listeners) {
      try { listener(this.state); } catch (error) { console.error(error); }
    }
    return this.state;
  }
  subscribe(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
}

export class EventBridge {
  constructor(api, store, { onEvents, onGap, onReconnect, onConnection } = {}) {
    this.api = api;
    this.store = store;
    this.onEvents = onEvents;
    this.onGap = onGap;
    this.onReconnect = onReconnect;
    this.onConnection = onConnection;
    this.lastSeq = 0;
    this.timer = null;
    this.stopped = true;
    this.failures = 0;
    this.inFlight = false;
    this.lastSyncAt = null;
    this.onVisibility = () => {
      if (!document.hidden && !this.stopped) this.schedule(0);
    };
    document.addEventListener("visibilitychange", this.onVisibility);
  }

  start(seq = 0) {
    this.lastSeq = seq || this.store.get().lastSeq || 0;
    this.stopped = false;
    this.schedule(0);
  }

  stop() {
    this.stopped = true;
    clearTimeout(this.timer);
    this.timer = null;
    document.removeEventListener("visibilitychange", this.onVisibility);
  }

  setJobActive(active) {
    this.jobActive = Boolean(active);
    if (this.jobActive && !this.stopped) this.schedule(50);
  }

  schedule(delay) {
    if (this.stopped || document.hidden) return;
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.poll(), delay);
  }

  reportConnection(connected, error = null) {
    const payload = {
      connected,
      lastSyncAt: this.lastSyncAt,
      error: error ? (error?.message || String(error)) : null,
      failures: this.failures,
    };
    this.store.set({
      pollError: error ? payload.error : null,
      lastSyncAt: this.lastSyncAt,
      connection: payload,
    });
    try { this.onConnection?.(payload); } catch (exception) { console.error(exception); }
  }

  async poll() {
    if (this.stopped || this.inFlight || document.hidden) return;
    this.inFlight = true;
    try {
      const payload = await this.api.events(this.lastSeq);
      const wasOffline = this.failures > 0;
      this.failures = 0;
      this.lastSyncAt = Date.now();
      const items = payload?.items || [];
      const last = Number(payload?.last_seq || this.lastSeq);
      if (items.length && Number(items[0].seq) > this.lastSeq + 1) {
        // Gap: the view must refetch a full snapshot.
        this.onGap?.({ expected: this.lastSeq + 1, got: Number(items[0].seq) });
      }
      if (items.length) {
        this.lastSeq = Number(items[items.length - 1].seq);
      } else if (last > this.lastSeq) {
        // Events were rotated/cleared beyond our cursor.
        this.onGap?.({ expected: this.lastSeq + 1, got: last + 1 });
        this.lastSeq = last;
      }
      this.store.set({ lastSeq: this.lastSeq });
      this.reportConnection(true);
      this.onEvents?.(items, payload);
      if (wasOffline) {
        // Reconnecting must not start/resume anything; views only refetch.
        try { this.onReconnect?.(); } catch (exception) { console.error(exception); }
      }
      this.schedule(this.jobActive ? 1200 : 4000);
    } catch (error) {
      this.failures += 1;
      const backoff = Math.min(15000, 1000 * 2 ** Math.min(4, this.failures));
      this.reportConnection(false, error);
      this.schedule(backoff);
    } finally {
      this.inFlight = false;
    }
  }
}
