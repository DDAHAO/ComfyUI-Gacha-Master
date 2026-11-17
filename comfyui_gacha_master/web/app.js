import { ApiClient } from "./api.js";
import { EventBridge, Store } from "./store.js";
import { closeAllModals, clear, errorBox, fmtClock, h } from "./dom.js";
import { createToastStack } from "./components/toast.js";
import { skeletonJobCard } from "./components/skeleton.js";
import { watchContainerWidth } from "./layout.js";
import { mountJobs } from "./views/jobs.js";
import { mountCreate } from "./views/create.js";
import { mountDetail } from "./views/detail.js";
import { mountSettings } from "./views/settings.js";
import { installDebugProbe, registerResourceProvider } from "./resources.js";

export class GachaApp {
  constructor(container, comfyApi, comfyApp) {
    this.container = container;
    this.comfyApi = comfyApi;
    this.comfyApp = comfyApp;
    this.api = new ApiClient(comfyApi);
    installDebugProbe(globalThis);
    this.resourceProvider = registerResourceProvider(() => ({
      view: this.store.get().view,
      extra: { connection: this.store.get().connection, jobId: this.store.get().jobId },
    }));
    this.store = new Store({
      view: "jobs",
      jobId: null,
      lastSeq: 0,
      capabilities: null,
      settings: null,
      pollError: null,
      lastSyncAt: null,
      connection: { connected: true, error: null, lastSyncAt: null, failures: 0 },
    });
    this.current = null;
    this.root = null;
    this.toasts = null;
    this.started = false;
    this.stopContainerWatch = null;
    this.unsubscribe = null;
    this.leaveGuard = null;
    this.navigating = false;
    this.bridge = new EventBridge(this.api, this.store, {
      onEvents: (items) => this.current?.onEvents?.(items),
      onGap: () => {
        this.current?.onGap?.();
        this.updateConnectionChrome();
      },
      onConnection: (connection) => this.updateConnectionChrome(connection),
      onReconnect: () => {
        // Reconnection only refreshes snapshots. It never starts tasks.
        this.current?.onReconnect?.();
        this.updateConnectionChrome();
      },
    });
  }

  async mount() {
    if (this.started) return;
    this.started = true;
    this.root = h("div", { class: "gacha-master-root" });
    this.container.appendChild(this.root);
    this.toasts = createToastStack();
    this.renderShell();
    this.unsubscribe = this.store.subscribe(() => this.updateConnectionChrome());
    this.stopContainerWatch = watchContainerWidth(this.body, (size) => {
      this.root.dataset.gmContainer = size;
    });
    await this.loadBaseData();
  }

  destroy() {
    this.started = false;
    closeAllModals("app-destroy");
    this.bridge.stop();
    this.current?.destroy?.();
    this.current = null;
    this.unsubscribe?.();
    this.unsubscribe = null;
    this.stopContainerWatch?.();
    this.stopContainerWatch = null;
    this.toasts?.destroy();
    this.toasts = null;
    this.resourceProvider?.();
    this.resourceProvider = null;
    this.root?.remove();
    this.root = null;
  }

  renderShell() {
    clear(this.root);
    this.header = h("header", { class: "gm-header" },
      h("div", { class: "gm-brand" },
        h("span", { class: "gm-logo", "aria-hidden": "true" }, "抽"),
        h("div", { class: "gm-brand-text" },
          h("h1", null, "抽卡大师"),
          h("span", { class: "gm-sub" }, "ComfyUI Gacha Master V1.0"),
        ),
      ),
      h("div", { class: "gm-header-actions" },
        this.connectionPill = h("span", { class: "gm-connection-pill", role: "status", "aria-live": "polite" }, "正在连接…"),
        h("button", { class: "gm-icon-btn", type: "button", title: "刷新", "aria-label": "刷新当前页面", onclick: () => this.refreshCurrent() }, "↻"),
      ),
    );
    this.tabs = h("nav", { class: "gm-tabs", role: "tablist", "aria-label": "主要页面" });
    this.tabs.addEventListener("keydown", (event) => {
      const buttons = [...this.tabs.querySelectorAll(".gm-tab")];
      const index = buttons.indexOf(document.activeElement);
      if (index < 0) return;
      let next = null;
      if (event.key === "ArrowRight") next = (index + 1) % buttons.length;
      else if (event.key === "ArrowLeft") next = (index - 1 + buttons.length) % buttons.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = buttons.length - 1;
      if (next === null) return;
      event.preventDefault();
      buttons[next].focus();
      buttons[next].click();
    });
    this.body = h("main", { class: "gm-body", tabindex: "-1" });
    this.root.append(this.header, this.tabs, this.body);
    this.renderTabs();
  }

  renderTabs() {
    clear(this.tabs);
    const current = this.store.get().view;
    const entries = [
      ["jobs", "任务列表"],
      ["create", "新建任务"],
      ["settings", "设置与存储"],
    ];
    for (const [id, label] of entries) {
      this.tabs.appendChild(h("button", {
        class: `gm-tab ${current === id ? "active" : ""}`,
        type: "button",
        role: "tab",
        "aria-selected": current === id ? "true" : "false",
        tabindex: current === id ? "0" : "-1",
        onclick: () => this.navigate(id),
      }, label));
    }
  }

  async loadBaseData() {
    clear(this.body);
    const skeleton = h("div", { class: "gm-job-list", "aria-busy": "true" },
      skeletonJobCard(), skeletonJobCard(), skeletonJobCard());
    this.body.append(
      h("p", { class: "gm-help", role: "status" }, "正在连接抽卡大师后端…"),
      skeleton,
    );
    try {
      const [settings, capabilities] = await Promise.all([this.api.settings(), this.api.capabilities()]);
      this.store.set({ settings, capabilities });
      this.store.set({ view: "jobs" });
      this.renderTabs();
      this.navigate("jobs");
      this.bridge.start(this.store.get().lastSeq || 0);
    } catch (error) {
      this.store.set({ connection: { connected: false, error: error?.message || String(error), lastSyncAt: null, failures: 0 } });
      this.updateConnectionChrome();
      clear(this.body);
      this.body.appendChild(errorBox(error));
      this.body.appendChild(h("p", { class: "gm-help" },
        "如果 ComfyUI 刚刚启动，请稍候再点右上角刷新。任务中心后端尚未初始化时不会提交任何任务。"));
    }
  }

  async navigate(view, params = {}) {
    if (!this.root || this.navigating) return;
    if (this.leaveGuard && this.current) {
      this.navigating = true;
      let allowed = false;
      try {
        allowed = await this.leaveGuard(view, params);
      } catch (error) {
        console.error("[GachaMaster] leave guard failed", error);
        allowed = false;
      } finally {
        this.navigating = false;
      }
      if (!allowed) return;
      this.leaveGuard = null;
    }
    this.navigating = true;
    try {
      closeAllModals("navigate");
      this.current?.destroy?.();
      this.current = null;
      this.bridge.setJobActive(false);
      clear(this.body);
      const previous = this.store.get().view;
      this.store.set({ view, jobId: params.jobId || null, ...params });
      this.renderTabs();
      const ctx = this.context();
      let cleanup = null;
      try {
        if (view === "jobs") cleanup = mountJobs(this.body, ctx);
        else if (view === "create") cleanup = mountCreate(this.body, ctx);
        else if (view === "detail") cleanup = mountDetail(this.body, ctx, params.jobId);
        else if (view === "settings") cleanup = mountSettings(this.body, ctx);
        else cleanup = mountJobs(this.body, ctx);
      } catch (error) {
        this.body.appendChild(errorBox(error));
      }
      this.current = cleanup || null;
      if (previous !== view) this.body.scrollTop = 0;
      this.updateConnectionChrome();
    } finally {
      this.navigating = false;
    }
  }

  refreshCurrent() {
    if (this.current?.refresh) this.current.refresh();
    else this.loadBaseData();
  }

  notify(message, kind = "info", options = {}) {
    return this.toasts?.show(message, kind, options) || null;
  }

  isActive() {
    return this.started && !document.hidden;
  }

  updateConnectionChrome(connection = null) {
    if (!this.connectionPill) return;
    const state = connection || this.store.get().connection || {};
    const connected = state.connected !== false;
    this.connectionPill.className = `gm-connection-pill ${connected ? "ok" : "bad"}`;
    if (connected) {
      this.connectionPill.textContent = state.lastSyncAt ? `已同步 ${fmtClock(state.lastSyncAt)}` : "已连接";
      this.connectionPill.title = state.lastSyncAt ? `最后成功同步：${new Date(state.lastSyncAt).toLocaleString()}` : "事件通道已连接";
    } else {
      this.connectionPill.textContent = "同步中断";
      this.connectionPill.title = `${state.error || "无法连接后端"}；最后成功同步：${state.lastSyncAt ? new Date(state.lastSyncAt).toLocaleString() : "尚未成功"}`;
    }
  }

  context() {
    return {
      api: this.api,
      comfyApp: this.comfyApp,
      comfyApi: this.comfyApi,
      store: this.store,
      navigate: (view, params) => this.navigate(view, params),
      notify: (message, kind, options) => this.notify(message, kind, options),
      dismissNotice: (id) => this.toasts?.dismiss(id),
      refreshBase: () => this.loadBaseData(),
      isActive: () => this.isActive(),
      setJobActive: (active) => this.bridge.setJobActive(active),
      setLeaveGuard: (fn) => { this.leaveGuard = fn; },
      clearLeaveGuard: () => { this.leaveGuard = null; },
      root: this.root,
      containerSize: () => this.root?.dataset.gmContainer || "wide",
    };
  }
}
