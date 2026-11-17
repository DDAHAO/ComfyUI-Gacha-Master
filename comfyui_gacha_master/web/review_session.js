// Client state machine for continuous, resumable source-group review.
//
// The server owns the session, position and decision receipt.  This class only
// keeps a local projection: it never treats localStorage, a component variable
// or a pager cache as the source of truth.  Every group decision flushes the
// pending position first, waits for in-flight selection writes and submits the
// complete version envelope captured from the server.

import { idempotencyKey } from "./api.js";
import { ResultPager, RESULT_PAGE_SIZE } from "./collections/result_pager.js";

const POSITION_DEBOUNCE_MS = 400;

function localError(message, code) {
  const error = new Error(message);
  error.code = code;
  return error;
}

export class ReviewSessionClient {
  constructor({ api, jobId, selection, notify, pagerFactory } = {}) {
    if (!api || !jobId) throw new Error("ReviewSessionClient requires api and jobId");
    this.api = api;
    this.jobId = jobId;
    this.selection = selection || null;
    this.notify = notify;
    this._pagerFactory = pagerFactory || null;
    this.scope = "pending";

    this.session = null;
    this.resume = null;
    this.summary = null;
    this.group = null;
    this.source = null;
    this.pager = null;
    this.currentIndex = 0;
    this.currentOutputId = null;
    this.nextSource = null;

    this.viewState = "idle"; // idle|loading|ready|finished|next_failed|error
    this.error = null;
    this.conflict = null;
    this.save = { position: "idle", decision: "idle", at: null, message: "" };

    this.listeners = new Set();
    this._destroyed = false;
    this._loadToken = 0;
    this._positionTimer = null;
    this._positionDirty = false;
    this._positionInFlight = null;
    this._flushing = null;
    this._pendingPosition = null;
    this._decisionBusy = false;
    this._navBusy = false;
    this._selectionFailures = new Set();
    this._groupRefreshTimer = null;
    this._groupRefreshPending = false;
    this._viewed = new Set();

    this._unsubscribeSelection = this.selection?.subscribe((event) => this._onSelection(event));
  }

  // ------------------------------------------------------------- events
  subscribe(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  emit(type, detail = {}) {
    const payload = { type, client: this, ...detail };
    for (const listener of [...this.listeners]) {
      try { listener(payload); } catch (error) { console.error(error); }
    }
  }

  // ------------------------------------------------------------ getters
  get busy() {
    return this._decisionBusy || this._navBusy || this.save.decision === "saving";
  }

  get currentOutput() {
    return this.pager?.items?.[this.currentIndex] || null;
  }

  get selectedCount() {
    return Number(this.group?.selected || 0);
  }

  get pendingSelectionCount() {
    return Number(this.selection?.pendingCount || 0);
  }

  get canRetain() {
    return this.viewState === "ready" && !this.busy && !this._groupRefreshPending
      && this.selectedCount > 0 && this.pendingSelectionCount === 0
      && !this.conflict && this.save.position !== "failed";
  }

  get canSkip() {
    return this.viewState === "ready" && !this.busy && !this._groupRefreshPending
      && this.selectedCount === 0 && this.pendingSelectionCount === 0
      && !this.conflict && this.save.position !== "failed";
  }

  get canBack() {
    return Boolean(this.session?.history?.length) && !this.busy
      && this.viewState === "ready";
  }

  get canGoNextPage() {
    return Boolean(this.pager?.hasNext) && !this.pager?.loading && !this.busy;
  }

  get canGoPrevPage() {
    return Boolean(this.pager?.hasPrev) && !this.pager?.loading && !this.busy;
  }

  // -------------------------------------------------------------- open
  async open({ startSourceId = null, startOutputId = null, scope = "pending" } = {}) {
    this.scope = scope;
    this.viewState = "loading";
    this.error = null;
    this.conflict = null;
    this.emit("state");
    try {
      let payload = await this.api.reviewSession(this.jobId);
      if (!payload.session) {
        payload = await this.api.openReviewSession(this.jobId, {
          scope,
          start_source_id: startSourceId || undefined,
        });
      }
      payload = await this._jumpToStart(payload, { startSourceId, startOutputId, scope });
      this._apply(payload);
      await this._materialize(payload);
    } catch (error) {
      this._fail(error);
    }
  }

  /**
   * Open the workspace at a specific result.  The server derives the exact
   * result page offset from the OUT identity, so a thumbnail click never
   * guesses a page from an array index.
   */
  async _jumpToStart(payload, { startSourceId = null, startOutputId = null, scope = "pending" }) {
    const session = payload?.session;
    if (!session) return payload;
    const current = session.current || {};
    const targetSourceId = startSourceId || (startOutputId ? current.source_id : null);
    if (!targetSourceId) return payload;
    const sourceChanged = current.source_id !== targetSourceId;
    const outputChanged = Boolean(startOutputId)
      && (sourceChanged || current.output_id !== startOutputId);
    if (!sourceChanged && !outputChanged) return payload;
    const body = {
      expected_version: session.version,
      current_source_id: targetSourceId,
      scope,
    };
    if (startOutputId) body.current_output_id = startOutputId;
    else body.result_offset = 0;
    return this.api.saveReviewPosition(session.id, body);
  }

  /** Re-read the server session after a version conflict or external change. */
  async reload({ startSourceId = null } = {}) {
    this.conflict = null;
    this.error = null;
    this.save.position = "idle";
    this.save.message = "";
    this.viewState = "loading";
    this._positionDirty = false;
    this._pendingPosition = null;
    if (this._positionTimer) {
      clearTimeout(this._positionTimer);
      this._positionTimer = null;
    }
    this.emit("state");
    try {
      let payload = await this.api.reviewSession(this.jobId);
      if (!payload.session) {
        await this.open({ startSourceId });
        return;
      }
      if (startSourceId && payload.session.current?.source_id !== startSourceId) {
        payload = await this.api.saveReviewPosition(payload.session.id, {
          expected_version: payload.session.version,
          current_source_id: startSourceId,
          result_offset: 0,
          scope: this.scope,
        });
      }
      this._apply(payload);
      await this._materialize(payload);
    } catch (error) {
      this._fail(error);
    }
  }

  _apply(payload) {
    if (!payload) return;
    if (payload.session !== undefined) this.session = payload.session;
    if (payload.summary) this.summary = payload.summary;
    if (payload.resume) this.resume = payload.resume;
    this.emit("state");
  }

  async _materialize(payload) {
    const resume = payload?.resume || this.resume;
    if (!resume || resume.mode === "FINISHED" || !resume.source) {
      this.viewState = "finished";
      this.emit("state");
      return;
    }
    const target = resume.source;
    const same = this.session?.current?.source_id === target.id;
    await this._enterSource(target, {
      offset: same ? Number(this.session?.current?.result_offset || 0) : 0,
      outputId: same ? this.session?.current?.output_id || null : null,
      persist: !same || resume.mode === "NEXT_PENDING",
    });
  }

  // ------------------------------------------------------------- group
  _createPager(sourceId) {
    if (this._pagerFactory) {
      return this._pagerFactory(sourceId, (event) => this._onPager(event));
    }
    return new ResultPager({
      api: this.api,
      sourceId,
      pageSize: RESULT_PAGE_SIZE,
      includeUnhealthy: false,
      onEvent: (event) => this._onPager(event),
    });
  }

  _onPager(event) {
    if (this._destroyed) return;
    if (event?.type === "error") this.error = event.error;
    this.emit("pager", { event });
    this.emit("state");
  }

  async _enterSource(source, { offset = 0, outputId = null, persist = false, nextFailure = false } = {}) {
    const token = ++this._loadToken;
    this.source = source;
    this.group = null;
    this.currentIndex = 0;
    this.currentOutputId = outputId;
    this.error = null;
    this.viewState = "loading";
    this.emit("group", { source });
    this.emit("state");
    this.pager?.destroy();
    this.pager = null;
    try {
      const state = await this.api.reviewSourceState(source.id);
      if (this._destroyed || token !== this._loadToken) return false;
      this.group = state;
      const pager = this._createPager(source.id);
      this.pager = pager;
      const restored = await pager.restore({ offset, outputId });
      if (this._destroyed || token !== this._loadToken) return false;
      if (!restored) {
        this.error = pager.error || localError("结果读取失败。", "RESULTS_UNAVAILABLE");
        this.viewState = nextFailure ? "next_failed" : "error";
        this.emit("state");
        return false;
      }
      this.currentIndex = Math.max(0, Math.min(restored.outputIndex ?? 0, pager.items.length - 1));
      this.currentOutputId = this.currentOutput?.id || null;
      this.viewState = "ready";
      this.save.position = "saved";
      this.emit("group", { source });
      this.emit("state");
      this._markViewed(this.currentOutput);
      this._markPosition();
      // Persist the restored/first preview position, but not when the server
      // already points exactly here (reloads must not churn the version).
      if (persist || offset > 0 || outputId) this._schedulePosition(0);
      else this._schedulePosition();
      return true;
    } catch (error) {
      if (this._destroyed || token !== this._loadToken) return false;
      this.error = error;
      this.viewState = nextFailure ? "next_failed" : "error";
      this.emit("error", { error });
      this.emit("state");
      return false;
    }
  }

  async refreshGroupState() {
    if (!this.source) return;
    const sourceId = this.source.id;
    try {
      const state = await this.api.reviewSourceState(sourceId);
      if (this._destroyed || this.source?.id !== sourceId) return;
      this.group = state;
      this._groupRefreshPending = false;
      this.emit("state");
    } catch (error) {
      if (this._destroyed) return;
      this._groupRefreshPending = false;
      this.error = error;
      this.emit("state");
    }
  }

  _scheduleGroupRefresh(delay = 120) {
    if (this._groupRefreshTimer) return;
    this._groupRefreshTimer = setTimeout(() => {
      this._groupRefreshTimer = null;
      this.refreshGroupState();
    }, delay);
  }

  _onSelection(event) {
    if (this._destroyed || !event) return;
    if (event.state === "saved") {
      this._selectionFailures.delete(event.outputId);
      // The server count is authoritative; keep group actions disabled until
      // the refreshed whole-group count has actually arrived.
      this._groupRefreshPending = true;
      this._scheduleGroupRefresh();
    } else if (event.state === "failed") {
      this._selectionFailures.add(event.outputId);
      this.error = event.error;
    }
    this.emit("state");
  }

  // ----------------------------------------------------------- preview
  _markViewed(output) {
    if (!output?.id || this._viewed.has(output.id)) return;
    this._viewed.add(output.id);
    if (this._viewed.size > 2000) this._viewed.clear();
    this.api.viewed(output.id).catch(() => {});
  }

  selectOutput(outputId) {
    if (!this.pager) return;
    const index = this.pager.items.findIndex((item) => item.id === outputId);
    if (index < 0) return;
    this.currentIndex = index;
    this.currentOutputId = outputId;
    this._markViewed(this.currentOutput);
    this._markPosition();
    this._schedulePosition();
    this.emit("state");
  }

  async moveResult(delta) {
    if (!this.pager || this._navBusy) return;
    const next = this.currentIndex + delta;
    if (next >= 0 && next < this.pager.items.length) {
      this.currentIndex = next;
      this.currentOutputId = this.currentOutput?.id || null;
      this._markViewed(this.currentOutput);
      this._markPosition();
      this._schedulePosition();
      this.emit("state");
      return;
    }
    this._navBusy = true;
    try {
      const loaded = delta > 0 ? await this.pager.next() : await this.pager.prev();
      if (this._destroyed || !loaded || this.pager.error || !this.pager.items.length) {
        if (this.pager.error) this.error = this.pager.error;
        return;
      }
      this.currentIndex = delta > 0 ? 0 : this.pager.items.length - 1;
      this.currentOutputId = this.currentOutput?.id || null;
      this._markViewed(this.currentOutput);
      this._markPosition();
      this._schedulePosition();
    } finally {
      this._navBusy = false;
      this.emit("state");
    }
  }

  async firstPage() {
    if (!this.pager || this._navBusy || !this.pager.hasPrev) return;
    this._navBusy = true;
    try {
      const page = await this.pager.goTo(0);
      if (this._destroyed || !page) {
        if (this.pager.error) this.error = this.pager.error;
        return;
      }
      this.currentIndex = 0;
      this.currentOutputId = this.currentOutput?.id || null;
      this._markViewed(this.currentOutput);
      this._markPosition();
      this._schedulePosition(0);
    } finally {
      this._navBusy = false;
      this.emit("state");
    }
  }

  async nextPage() {
    if (!this.pager || this._navBusy || !this.pager.hasNext) return;
    this._navBusy = true;
    try {
      const page = await this.pager.next();
      if (this._destroyed || !page) {
        if (this.pager.error) this.error = this.pager.error;
        return;
      }
      this.currentIndex = 0;
      this.currentOutputId = this.currentOutput?.id || null;
      this._navBusy = false;
      this._markViewed(this.currentOutput);
      this._markPosition();
      this._schedulePosition(0);
    } finally {
      this._navBusy = false;
      this.emit("state");
    }
  }

  async prevPage() {
    if (!this.pager || this._navBusy || !this.pager.hasPrev) return;
    this._navBusy = true;
    try {
      const page = await this.pager.prev();
      if (this._destroyed || !page) {
        if (this.pager.error) this.error = this.pager.error;
        return;
      }
      this.currentIndex = Math.max(0, this.pager.items.length - 1);
      this.currentOutputId = this.currentOutput?.id || null;
      this._markViewed(this.currentOutput);
      this._markPosition();
      this._schedulePosition(0);
    } finally {
      this._navBusy = false;
      this.emit("state");
    }
  }

  async retryPage() {
    if (!this.pager) return;
    try {
      await this.pager.retry();
      if (!this.pager.error && this.pager.items.length) {
        this.currentIndex = Math.min(this.currentIndex, this.pager.items.length - 1);
        this.currentOutputId = this.currentOutput?.id || null;
        this._markPosition();
        this._schedulePosition(0);
      }
    } finally {
      this.emit("state");
    }
  }

  toggleCurrent() {
    const output = this.currentOutput;
    if (!output || !this.selection) return null;
    if (this.selection.isPending(output.id)) return null;
    return this.selection.toggle(output);
  }

  toggleOutput(outputId) {
    const output = this.pager?.items.find((item) => item.id === outputId);
    if (!output || !this.selection) return null;
    if (this.selection.isPending(output.id)) return null;
    return this.selection.toggle(output);
  }

  // ---------------------------------------------------------- position
  _markPosition() {
    const offset = (this.pager?.currentOffset ?? 0) + this.currentIndex;
    this._pendingPosition = {
      sourceId: this.source?.id || null,
      outputId: this.currentOutputId || null,
      offset: Math.max(0, offset),
    };
  }

  _buildPositionBody() {
    if (!this.session || !this.source) return null;
    const pending = this._pendingPosition;
    if (!pending) return null;
    const current = this.session.current || {};
    const sameSource = current.source_id === pending.sourceId;
    const sameOutput = (current.output_id || null) === (pending.outputId || null);
    const sameOffset = Number(current.result_offset || 0) === Number(pending.offset || 0);
    if (sameSource && sameOutput && sameOffset) return null;
    return {
      expected_version: this.session.version,
      current_source_id: pending.sourceId,
      current_output_id: pending.outputId || null,
      result_offset: Math.max(0, Number(pending.offset) || 0),
      scope: this.scope,
    };
  }

  _schedulePosition(delay = POSITION_DEBOUNCE_MS) {
    if (this._destroyed) return;
    this._positionDirty = true;
    if (this._positionTimer) clearTimeout(this._positionTimer);
    this._positionTimer = setTimeout(() => {
      this._positionTimer = null;
      this.flushPosition().catch(() => {});
    }, Math.max(0, delay));
    this.emit("state");
  }

  /**
   * Send every pending position write in order.  Resolves true only after the
   * server confirmed the last one; a conflict/failure leaves the last server
   * breakpoint intact and is never reported as "saved".
   */
  flushPosition() {
    if (this._flushing) return this._flushing;
    this._flushing = this._drainPosition().finally(() => { this._flushing = null; });
    return this._flushing;
  }

  async _drainPosition() {
    if (this._positionTimer) {
      clearTimeout(this._positionTimer);
      this._positionTimer = null;
    }
    if (this._destroyed || !this.session) return true;
    while (this._positionDirty) {
      if (this._positionInFlight) {
        await this._positionInFlight;
        if (this.conflict) return false;
      }
      if (!this._positionDirty) break;
      const body = this._buildPositionBody();
      this._positionDirty = false;
      if (!body) continue;
      this._positionInFlight = this._sendPosition(body);
      const ok = await this._positionInFlight;
      this._positionInFlight = null;
      if (!ok) return false;
    }
    return true;
  }

  async _sendPosition(body) {
    this.save.position = "saving";
    this.save.message = "";
    this.emit("save");
    try {
      const payload = await this.api.saveReviewPosition(this.session.id, body);
      this.session = payload.session;
      this.summary = payload.summary || this.summary;
      if (payload.resume) this.resume = payload.resume;
      this.save.position = "saved";
      this.save.at = Date.now();
      this.conflict = null;
      this.emit("save");
      return true;
    } catch (error) {
      if (error?.status === 409 || error?.code === "VERSION_CONFLICT") {
        this.save.position = "conflict";
        this.conflict = { kind: "position", error };
      } else {
        this.save.position = "failed";
        this.save.message = error?.message || String(error);
      }
      // Keep the desired position dirty so an explicit retry can send it; the
      // server still holds the last confirmed breakpoint in the meantime.
      this._positionDirty = true;
      this.error = error;
      this.emit("save");
      return false;
    }
  }

  // ---------------------------------------------------------- decisions
  async decide(decision) {
    if (this._decisionBusy || this._destroyed) return false;
    if (!this.session || !this.source || this.viewState !== "ready") return false;
    // A locally stale count is not a rejection reason: an in-flight selection
    // write is awaited below, then the server count decides.  Double activation
    // is still impossible because only one decision may be in flight.
    this._decisionBusy = true;
    this.save.decision = "saving";
    this.error = null;
    this.emit("state");
    try {
      if (this.selection) await this.selection.whenIdle();
      if (this._selectionFailures.size) {
        throw localError("本组有入选状态保存失败；请先重试该图片，再确认本组。", "SELECTION_SAVE_FAILED");
      }
      const flushed = await this.flushPosition();
      if (!flushed) {
        throw localError("筛选进度尚未保存到服务器；请重试保存后再确认本组。", "POSITION_SAVE_FAILED");
      }
      const state = await this.api.reviewSourceState(this.source.id);
      this.group = state;
      if (decision === "SELECTED" && state.selected < 1) {
        throw localError("本组没有已保存的入选项，不能保留。", "STATE_CONFLICT");
      }
      if (decision === "SKIPPED" && state.selected > 0) {
        throw localError(`本组已选 ${state.selected} 张；取消入选后可跳过，或选择保留并继续。`, "STATE_CONFLICT");
      }
      if (this.session.current?.source_id !== this.source.id) {
        // The server session must point at the group being decided; this is a
        // position write, never a second selection write.
        const payload = await this.api.saveReviewPosition(this.session.id, {
          expected_version: this.session.version,
          current_source_id: this.source.id,
          current_output_id: this.currentOutputId || null,
          result_offset: Math.max(0, (this.pager?.currentOffset ?? 0) + this.currentIndex),
          scope: this.scope,
        });
        this.session = payload.session;
        this.summary = payload.summary || this.summary;
      }
      const body = {
        source_id: this.source.id,
        decision,
        expected_session_version: this.session.version,
        expected_result_revision: state.versions.result_revision,
        expected_selection_version: state.versions.selection_version,
        expected_plan_version: state.versions.plan_version,
      };
      const payload = await this.api.reviewDecision(
        this.session.id, body, idempotencyKey(),
      );
      this.session = payload.session;
      this.summary = payload.summary || this.summary;
      this.save.decision = "saved";
      this.save.at = Date.now();
      this.conflict = null;
      this.emit("state");
      if (payload.idempotent) {
        // A retried response may be stale; re-read the authoritative resume and
        // navigate from it instead of executing the old decision again.
        const fresh = await this.api.reviewSession(this.jobId);
        this._apply(fresh);
        await this._followResume(fresh.resume);
      } else {
        await this._followDecision(payload);
      }
      return true;
    } catch (error) {
      this.save.decision = "failed";
      this.error = error;
      if (error?.status === 409 || error?.code === "VERSION_CONFLICT") {
        this.conflict = { kind: "decision", error };
        this.save.decision = "conflict";
      } else if (error?.code === "STATE_CONFLICT") {
        this.save.decision = "blocked";
      }
      this.emit("error", { error });
      this.emit("state");
      return false;
    } finally {
      this._decisionBusy = false;
      this.emit("state");
    }
  }

  async _followDecision(payload) {
    const next = payload?.next;
    if (!next || next.mode === "FINISHED" || !next.source) {
      this.viewState = "finished";
      this.emit("state");
      return;
    }
    this.nextSource = next.source;
    await this._loadNext(next.source);
  }

  async _followResume(resume) {
    if (!resume || resume.mode === "FINISHED" || !resume.source) {
      this.viewState = "finished";
      this.emit("state");
      return;
    }
    const same = this.session?.current?.source_id === resume.source.id;
    await this._enterSource(resume.source, {
      offset: same ? Number(this.session?.current?.result_offset || 0) : 0,
      outputId: same ? this.session?.current?.output_id || null : null,
      persist: !same || resume.mode === "NEXT_PENDING",
    });
  }

  async _loadNext(source) {
    const ok = await this._enterSource(source, { offset: 0, outputId: null, persist: true, nextFailure: true });
    this.nextSource = ok ? null : source;
    return ok;
  }

  /** Retry only the next-group navigation; never re-submits the decision. */
  async retryNext() {
    if (!this.nextSource) {
      await this.reload();
      return;
    }
    const target = this.nextSource;
    this.viewState = "loading";
    this.error = null;
    this.emit("state");
    await this._loadNext(target);
  }

  async back() {
    if (this._decisionBusy || this._destroyed || !this.session) return false;
    if (!this.canBack) return false;
    this._decisionBusy = true;
    this.save.decision = "saving";
    this.error = null;
    this.emit("state");
    try {
      const flushed = await this.flushPosition();
      if (!flushed) {
        throw localError("筛选进度尚未保存；请重试后再返回上一组。", "POSITION_SAVE_FAILED");
      }
      const payload = await this.api.reviewBack(this.session.id, {
        expected_version: this.session.version,
      });
      this._apply(payload);
      if (payload.resume?.source) {
        await this._enterSource(payload.resume.source, {
          offset: Number(payload.session?.current?.result_offset || 0),
          outputId: payload.session?.current?.output_id || null,
          persist: false,
        });
      } else {
        this.viewState = "finished";
      }
      this.save.decision = "saved";
      return true;
    } catch (error) {
      this.error = error;
      this.save.decision = "failed";
      if (error?.status === 409 || error?.code === "VERSION_CONFLICT") {
        this.conflict = { kind: "decision", error };
        this.save.decision = "conflict";
      }
      this.emit("error", { error });
      this.emit("state");
      return false;
    } finally {
      this._decisionBusy = false;
      this.emit("state");
    }
  }

  /** Start a new sweep when earlier groups became pending again. */
  async requeue() {
    if (!this.session || this._decisionBusy) return false;
    const target = this.resume?.requeue_source || this.resume?.source;
    if (!target) {
      await this.reload();
      return false;
    }
    this._decisionBusy = true;
    this.viewState = "loading";
    this.emit("state");
    try {
      const payload = await this.api.saveReviewPosition(this.session.id, {
        expected_version: this.session.version,
        current_source_id: target.id,
        result_offset: 0,
        scope: this.scope,
      });
      this._apply(payload);
      await this._materialize(payload);
      return true;
    } catch (error) {
      this._fail(error);
      return false;
    } finally {
      this._decisionBusy = false;
      this.emit("state");
    }
  }

  _fail(error) {
    this.error = error;
    this.viewState = "error";
    this.save.position = error?.status === 409 ? "conflict" : this.save.position;
    if (error?.status === 409) this.conflict = { kind: "session", error };
    this.emit("error", { error });
    this.emit("state");
  }

  close() {
    if (this._positionTimer) {
      clearTimeout(this._positionTimer);
      this._positionTimer = null;
    }
    if (this._groupRefreshTimer) {
      clearTimeout(this._groupRefreshTimer);
      this._groupRefreshTimer = null;
    }
    // Normal exit flushes the preview position once.  Fire-and-forget: closing
    // the modal must not block on the network, and the last server-confirmed
    // breakpoint stays authoritative.
    const flush = this._positionDirty ? this.flushPosition().catch(() => {}) : Promise.resolve();
    this._destroyed = true;
    this._loadToken += 1;
    this._unsubscribeSelection?.();
    this._unsubscribeSelection = null;
    this.pager?.destroy();
    this.pager = null;
    this.listeners.clear();
    return flush;
  }
}
