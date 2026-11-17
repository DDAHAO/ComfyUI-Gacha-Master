// Shared selection persistence. Detail thumbnails and the compare modal both
// use this controller so there is exactly one optimistic update per OUT and no
// response can be applied to a different image.
//
// One OUT may be in flight at a time; different OUTs may proceed independently.

export class SelectionController {
  constructor({ api, onChange, onConflict, notify } = {}) {
    this.api = api;
    this.onChange = onChange;
    this.onConflict = onConflict;
    this.notify = notify;
    this.pending = new Map();
    this.listeners = new Set();
    this.destroyed = false;
  }

  subscribe(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  emit(event) {
    try { this.onChange?.(event); } catch (error) { console.error(error); }
    for (const listener of this.listeners) {
      try { listener(event); } catch (error) { console.error(error); }
    }
  }

  isPending(outputId) {
    return this.pending.has(outputId);
  }

  get pendingCount() {
    return this.pending.size;
  }

  pendingIds() {
    return [...this.pending.keys()];
  }

  /** Wait for every selection write already in flight (settled, not thrown). */
  whenIdle() {
    const promises = [...this.pending.values()]
      .map((entry) => entry.promise)
      .filter(Boolean);
    return Promise.allSettled(promises);
  }

  pendingTarget(outputId) {
    return this.pending.get(outputId)?.target ?? null;
  }

  async toggle(output) {
    if (!output) return null;
    return this.set(output, !output.selected);
  }

  async set(output, target) {
    if (!output || this.destroyed) return null;
    if (output.health && output.health !== "VERIFIED") {
      this.notify?.("异常输出不能入选；请保留其证据身份。", "warn");
      return null;
    }
    if (this.pending.has(output.id)) return this.pending.get(output.id).promise;
    const previous = Boolean(output.selected);
    const next = Boolean(target);
    if (previous === next) return null;

    const pending = { target: next, previous, outputId: output.id, promise: null };
    output.selected = next;
    this.pending.set(output.id, pending);
    this.emit({ output, outputId: output.id, state: "saving", target: next });

    pending.promise = (async () => {
      try {
        const result = await this.api.selection(output.id, next, output.selection_version);
        if (this.destroyed) return null;
        // Apply by the OUT identity captured above, never by list position.
        output.selected = Boolean(result.selected);
        if (result.version !== undefined && result.version !== null) output.selection_version = result.version;
        this.pending.delete(output.id);
        this.emit({ output, outputId: output.id, state: "saved", result, target: output.selected });
        return result;
      } catch (error) {
        if (this.destroyed) return null;
        output.selected = previous;
        this.pending.delete(output.id);
        const conflict = error?.status === 409 || error?.code === "VERSION_CONFLICT";
        this.emit({ output, outputId: output.id, state: "failed", error, conflict, target: next });
        this.notify?.(
          conflict ? "入选状态已在其他页面变化，已恢复并重新读取权威状态。" : `入选状态保存失败：${error?.message || error}`,
          conflict ? "warn" : "error",
          { id: `selection-${output.id}`, detail: conflict ? error?.message : "当前图片附近保留失败提示。" },
        );
        if (conflict) this.onConflict?.({ output, error });
        return null;
      }
    })();
    return pending.promise;
  }

  destroy() {
    this.destroyed = true;
    this.pending.clear();
  }
}
