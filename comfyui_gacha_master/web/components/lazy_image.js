// Shared image request budget.
//
// A result page can contain hundreds of thumbnails.  Native loading=lazy does
// not cap simultaneous network requests, so this loader only assigns `src`
// when the element is near the viewport and at most MAX_CONCURRENT images are
// in flight.  Releasing a node clears its observer and src reference.

export const MAX_CONCURRENT_IMAGES = 8;

export class LazyImageLoader {
  constructor({ maxConcurrent = MAX_CONCURRENT_IMAGES, rootMargin = "200px" } = {}) {
    this.maxConcurrent = Math.max(1, Number(maxConcurrent) || MAX_CONCURRENT_IMAGES);
    this.rootMargin = rootMargin;
    this.active = 0;
    this.peak = 0;
    this.queue = [];
    this.stats = { requested: 0, loaded: 0, failed: 0, released: 0, cancelled: 0 };
    this._observer = null;
    this._entries = new WeakMap();
    this._pending = new Set();
    this._scanTimer = null;
  }

  _scheduleScan() {
    if (this._scanTimer !== null || typeof setTimeout === "undefined") return;
    // Some hosts (background tabs, paused compositors, automated panes) do not
    // deliver IntersectionObserver callbacks even for attached, laid-out
    // images.  A bounded geometric sweep is the fallback; it never raises the
    // concurrency limit or starts an offscreen request.
    this._scanTimer = setTimeout(() => {
      this._scanTimer = null;
      this._scan();
    }, 700);
  }

  _scan() {
    if (typeof window === "undefined" || typeof document === "undefined") return;
    const height = window.innerHeight || 0;
    const width = window.innerWidth || 0;
    for (const data of [...this._pending]) {
      if (data.released || data.started) {
        this._pending.delete(data);
        continue;
      }
      if (!data.img.isConnected) {
        this._pending.delete(data);
        data.released = true;
        continue;
      }
      const rect = data.img.getBoundingClientRect();
      const visible = rect.width > 0 && rect.height > 0
        && rect.bottom >= -200 && rect.top <= height + 200
        && rect.right >= -200 && rect.left <= width + 200;
      if (visible) {
        this._pending.delete(data);
        this._observer?.unobserve(data.img);
        this._enqueue(data);
      }
    }
    if (this._pending.size) this._scheduleScan();
  }

  _ensureObserver() {
    if (this._observer || typeof IntersectionObserver === "undefined") return this._observer;
    this._observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const entryData = this._entries.get(entry.target);
        if (!entryData || entryData.started) continue;
        this._observer.unobserve(entry.target);
        this._enqueue(entryData);
      }
    }, { rootMargin: this.rootMargin });
    return this._observer;
  }

  /**
   * Create an <img> whose src is assigned only near the viewport.
   * Returns the element; callers may release it when unmounting.
   */
  create(url, alt = "", className = "", options = {}) {
    const img = document.createElement("img");
    img.className = className;
    img.alt = alt;
    img.loading = "lazy";
    img.decoding = "async";
    img.draggable = false;
    this._installListeners(img);
    this.assign(img, url, options);
    return img;
  }

  /**
   * Re-use an existing <img> for a different URL.  The element and its two
   * listeners stay stable across result pages, so page navigation does not
   * create a new element/listener pair per thumbnail.
   */
  assign(img, url, options = {}) {
    if (!img) return;
    this.release(img);
    const data = {
      img,
      url,
      started: false,
      settled: false,
      released: false,
      onLoad: options.onLoad || null,
      onError: options.onError || null,
      priority: options.priority || 0,
    };
    this._entries.set(img, data);
    this._pending.add(data);
    const observer = this._ensureObserver();
    if (observer) observer.observe(img);
    this._scheduleScan();
  }

  _installListeners(img) {
    if (img.__gachaLazyBound) return;
    img.__gachaLazyBound = true;
    img.addEventListener("load", () => {
      const data = this._entries.get(img);
      if (!data || data.released || data.settled) return;
      this.stats.loaded += 1;
      data.settled = true;
      this.active = Math.max(0, this.active - 1);
      this._pump();
      data.onLoad?.(img);
    });
    img.addEventListener("error", () => {
      const data = this._entries.get(img);
      if (!data || data.released || data.settled) return;
      this.stats.failed += 1;
      data.settled = true;
      this.active = Math.max(0, this.active - 1);
      this._pump();
      data.onError?.(img);
    });
  }

  _enqueue(data) {
    if (data.released || data.started) return;
    data.started = true;
    this.queue.push(data);
    this.queue.sort((a, b) => b.priority - a.priority);
    this._pump();
  }

  _pump() {
    while (this.active < this.maxConcurrent && this.queue.length) {
      const data = this.queue.shift();
      if (!data || data.released || data.settled) continue;
      this.active += 1;
      this.peak = Math.max(this.peak, this.active);
      this.stats.requested += 1;
      data.img.src = data.url;
    }
  }

  release(img) {
    if (!img) return;
    const data = this._entries.get(img);
    if (data) {
      this._pending.delete(data);
      data.released = true;
      if (!data.settled) {
        this.stats.cancelled += 1;
        if (data.started) {
          this.active = Math.max(0, this.active - 1);
        }
        data.settled = true;
      }
      this.queue = this.queue.filter((item) => item !== data);
      this._entries.delete(img);
    }
    this._observer?.unobserve(img);
    img.removeAttribute("src");
    this.stats.released += 1;
    this._pump();
  }

  resetStats() {
    this.stats = { requested: 0, loaded: 0, failed: 0, released: 0, cancelled: 0 };
    this.peak = 0;
  }

  snapshot() {
    return {
      active: this.active,
      peak: this.peak,
      queued: this.queue.length,
      pending: this._pending.size,
      maxConcurrent: this.maxConcurrent,
      ...this.stats,
    };
  }

  destroy() {
    this._observer?.disconnect();
    this._observer = null;
    for (const data of this._pending) data.released = true;
    this._pending.clear();
    clearTimeout(this._scanTimer);
    this._scanTimer = null;
    this.queue = [];
  }
}

// One budget is shared by every view so two mounted pages cannot each open 8.
export const thumbnailLoader = new LazyImageLoader({ maxConcurrent: MAX_CONCURRENT_IMAGES });
