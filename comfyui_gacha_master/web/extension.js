// ComfyUI extension entry for Gacha Master.
// The task center is registered through the official sidebar tab API; if an
// older frontend lacks it, a self-contained floating launcher is used instead
// of injecting into fragile DOM selectors.

import { GachaApp } from "./app.js";

function ensureStyles() {
  const href = new URL("./styles.css", import.meta.url).href;
  if (document.querySelector(`link[data-gacha-master-style="1"]`)) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = href;
  link.dataset.gachaMasterStyle = "1";
  document.head.appendChild(link);
}
ensureStyles();

const EXTENSION_NAME = "ComfyUI.GachaMaster";
const TAB_ID = "gacha-master";

async function resolveComfyModules() {
  try {
    const appModule = await import("../../scripts/app.js");
    const apiModule = await import("../../scripts/api.js");
    return { app: appModule.app, api: apiModule.api };
  } catch (error) {
    const comfy = globalThis.comfyAPI || globalThis.comfy;
    const app = comfy?.app?.app || comfy?.app || globalThis.app;
    const api = comfy?.api?.api || comfy?.api || globalThis.api;
    if (!app) throw error;
    return { app, api };
  }
}

function createMount(container, comfyApp, comfyApi) {
  const app = new GachaApp(container, comfyApi, comfyApp);
  app.mount().catch((error) => {
    console.error("[GachaMaster] mount failed", error);
    container.textContent = `抽卡大师加载失败：${error?.message || error}`;
  });
  return app;
}

(async () => {
  if (globalThis.__gachaMasterRegistered) return;
  let modules;
  try {
    modules = await resolveComfyModules();
  } catch (error) {
    console.error("[GachaMaster] ComfyUI frontend API unavailable", error);
    return;
  }
  const { app, api } = modules;
  if (!app?.registerExtension) {
    console.error("[GachaMaster] app.registerExtension unavailable");
    return;
  }
  globalThis.__gachaMasterRegistered = true;

  app.registerExtension({
    name: EXTENSION_NAME,
    async setup() {
      const manager = app.extensionManager;
      if (typeof manager?.registerSidebarTab === "function") {
        manager.registerSidebarTab({
          id: TAB_ID,
          icon: "pi pi-box",
          title: "抽卡大师",
          tooltip: "ComfyUI 抽卡大师：任务中心、对照筛选与导出",
          type: "custom",
          render: (element) => {
            element.classList.add("gacha-master-host");
            if (element.__gachaMounted) return;
            element.__gachaMounted = true;
            element.__gachaApp = createMount(element, app, api);
          },
          destroy: () => {
            const element = document.querySelector(".gacha-master-host");
            element?.__gachaApp?.destroy?.();
            if (element) {
              element.__gachaMounted = false;
              element.__gachaApp = null;
            }
          },
        });
      } else {
        console.warn("[GachaMaster] registerSidebarTab unavailable; using floating panel fallback");
        installFloatingLauncher(app, api);
      }
    },
  });
})();

function installFloatingLauncher(comfyApp, comfyApi) {
  if (document.getElementById("gacha-master-launcher")) return;
  const button = document.createElement("button");
  button.id = "gacha-master-launcher";
  button.type = "button";
  button.textContent = "抽卡大师";
  button.className = "gacha-master-launcher";
  let panel = null;
  let app = null;
  button.addEventListener("click", () => {
    if (panel) {
      app?.destroy?.();
      app = null;
      panel.remove();
      panel = null;
      return;
    }
    panel = document.createElement("div");
    panel.className = "gacha-master-floating";
    const header = document.createElement("div");
    header.className = "gacha-master-floating-header";
    const title = document.createElement("strong");
    title.textContent = "ComfyUI 抽卡大师";
    const close = document.createElement("button");
    close.type = "button";
    close.textContent = "×";
    close.addEventListener("click", () => button.click());
    header.append(title, close);
    const host = document.createElement("div");
    host.className = "gacha-master-host";
    panel.append(header, host);
    document.body.appendChild(panel);
    app = createMount(host, comfyApp, comfyApi);
  });
  document.body.appendChild(button);
}
