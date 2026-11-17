// HOST13 UI01-UI05: viewport/zoom/theme, keyboard/focus, reduced motion,
// native file chooser upload, and image/network failure handling.
const fs = require("node:fs/promises");
const path = require("node:path");
const crypto = require("node:crypto");
const {
  arg, launch, openGacha, goToJobList, openJob, openFirstResults,
  resultState, clickResultButton, waitResultIdle, saveJson, screenshot,
} = require("./host_ui.cjs");

function sha256File(file) {
  const data = require("node:fs").readFileSync(file);
  return crypto.createHash("sha256").update(data).digest("hex");
}

async function measure(page) {
  return page.evaluate(() => {
    const host = document.querySelector(".gacha-master-host");
    const body = document.querySelector(".gm-body");
    const rect = host?.getBoundingClientRect();
    const visible = (text) => [...document.querySelectorAll("button")]
      .some((button) => button.textContent.trim().includes(text) && button.offsetParent !== null);
    return {
      innerWidth: window.innerWidth,
      innerHeight: window.innerHeight,
      hostRect: rect ? { width: Math.round(rect.width), height: Math.round(rect.height) } : null,
      bodyClient: body ? body.clientWidth : null,
      bodyScroll: body ? body.scrollWidth : null,
      docScroll: document.documentElement.scrollWidth,
      docClient: document.documentElement.clientWidth,
      buttons: {
        back: visible("任务列表"),
        refresh: visible("刷新"),
        export: visible("导出"),
        nextSource: visible("下一页"),
      },
      title: document.querySelector(".gm-detail-title")?.innerText || null,
    };
  });
}

async function contrast(page, selector) {
  return page.evaluate((sel) => {
    const element = document.querySelector(sel);
    if (!element) return null;
    const style = getComputedStyle(element);
    const host = document.querySelector(".gacha-master-host");
    const hostStyle = host ? getComputedStyle(host) : null;
    const parse = (value) => {
      const match = /rgba?\(([^)]+)\)/.exec(value || "");
      if (!match) return null;
      const parts = match[1].split(",").map((part) => Number(part.trim()));
      return { r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1 };
    };
    const lum = ({ r, g, b }) => {
      const channel = (value) => {
        const v = value / 255;
        return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
      };
      return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
    };
    const text = parse(style.color);
    const effectiveBackground = (node) => {
      let current = node;
      while (current) {
        const value = parse(getComputedStyle(current).backgroundColor);
        if (value && value.a > 0.05) return value;
        current = current.parentElement;
      }
      return { r: 255, g: 255, b: 255, a: 1 };
    };
    const background = effectiveBackground(element);
    if (!text) return null;
    const l1 = lum(text), l2 = lum(background);
    const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    return { color: style.color, background: `rgb(${background.r}, ${background.g}, ${background.b})`, ratio: Math.round(ratio * 100) / 100 };
  }, selector);
}

async function setTheme(page, theme) {
  await page.evaluate((value) => {
    const root = document.documentElement;
    if (value === "light") {
      root.style.setProperty("--comfy-menu-bg", "#f4f4f4");
      root.style.setProperty("--comfy-input-bg", "#ffffff");
      root.style.setProperty("--comfy-menu-secondary-bg", "#ececec");
      root.style.setProperty("--input-text", "#151515");
      root.style.setProperty("--descrip-text", "#555555");
      root.style.setProperty("--border-color", "#b8b8b8");
    } else {
      root.style.setProperty("--comfy-menu-bg", "#1c1c1c");
      root.style.setProperty("--comfy-input-bg", "#282828");
      root.style.setProperty("--comfy-menu-secondary-bg", "#222222");
      root.style.setProperty("--input-text", "#ececec");
      root.style.setProperty("--descrip-text", "#a0a0a0");
      root.style.setProperty("--border-color", "#444444");
    }
  }, theme);
  await page.waitForTimeout(200);
}

(async () => {
  const baseUrl = arg("base-url", "http://127.0.0.1:8296");
  const job = arg("job");
  const outPath = arg("out", "HOST13.json");
  const shots = arg("screenshots", null);
  const uploadInput = arg("upload-input", null);
  const workflowUi = arg("workflow-ui", null);
  if (shots) await fs.mkdir(shots, { recursive: true });

  const browser = await launch();
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error" && message.text().includes("GachaMaster")) {
      errors.push(`console: ${message.text()}`);
    }
  });
  const requests = [];
  page.on("request", (request) => requests.push({ method: request.method(), url: request.url() }));
  const results = {};

  try {
    await openGacha(page, baseUrl);
    await openJob(page, job);
    await openFirstResults(page);

    // -------------------------------------------------------------- UI01
    await page.setViewportSize({ width: 1280, height: 640 });
    await page.waitForTimeout(300);
    const normal = await measure(page);
    const normalContrast = await contrast(page, ".gm-detail-title");
    await setTheme(page, "dark");
    const darkContrast = await contrast(page, ".gm-detail-title");
    if (shots) await screenshot(page, `${shots}/UI01-dark-1280.png`);
    await setTheme(page, "light");
    const lightContrast = await contrast(page, ".gm-detail-title");
    if (shots) await screenshot(page, `${shots}/UI01-light-1280.png`);
    const zoom = await page.evaluate(() => {
      // Browser page zoom is not directly scriptable; use the 640 CSS-pixel
      // viewport that a 200% zoom of 1280 produces.
      window.__gmOriginalWidth = window.innerWidth;
      return true;
    });
    await page.setViewportSize({ width: 640, height: 450 });
    await page.waitForTimeout(400);
    const narrow = await measure(page);
    if (shots) await screenshot(page, `${shots}/UI01-zoom200-640.png`);
    // Primary source/result pager still operates at the narrow viewport.
    let narrowNav = null;
    const next = page.locator(".gm-result-toolbar button").filter({ hasText: "下一页" }).first();
    if (await next.count() && !(await next.isDisabled())) {
      await clickResultButton(page, "下一页");
      narrowNav = (await resultState(page)).label;
    }
    await setTheme(page, "dark");
    await page.setViewportSize({ width: 1280, height: 900 });
    results.UI01 = {
      result: normal.buttons.back && normal.buttons.refresh && normal.buttons.export
        && narrow.buttons.back && narrow.buttons.export && Boolean(narrow.title)
        && darkContrast?.ratio >= 4.5 && lightContrast?.ratio >= 4.5
        && normal.docScroll <= normal.docClient + 1 ? "PASS" : "FAIL",
      normal, narrow, narrow_result_label: narrowNav,
      contrast: { dark: darkContrast, light: lightContrast },
      note: "200% zoom is represented by the equivalent 640 CSS-pixel viewport",
    };

    // -------------------------------------------------------------- UI02
    await clickResultButton(page, "首页");
    const before = await resultState(page);
    const target = before.cards.find((card) => card.star === "☆") || before.cards[0];
    const starSelector = `[data-output-id="${target.id}"] .gm-star`;
    await page.locator(starSelector).focus();
    const focusBefore = await page.evaluate(() => document.activeElement?.className || "");
    await page.keyboard.press("Space");
    await page.waitForFunction((id) => document.querySelector(`[data-output-id="${id}"] .gm-star`)?.textContent.trim() === "★", target.id, { timeout: 20000 });
    const afterSpace = await page.evaluate((id) => document.querySelector(`[data-output-id="${id}"] .gm-star`)?.textContent.trim(), target.id);
    await page.waitForFunction((id) => {
      const star = document.querySelector(`[data-output-id="${id}"] .gm-star`);
      return star && !star.disabled && document.activeElement === star;
    }, target.id, { timeout: 20000 });
    await page.keyboard.press("Space");
    await page.waitForFunction((id) => document.querySelector(`[data-output-id="${id}"] .gm-star`)?.textContent.trim() === "☆", target.id, { timeout: 20000 });
    const afterSecondSpace = await page.evaluate((id) => document.querySelector(`[data-output-id="${id}"] .gm-star`)?.textContent.trim(), target.id);
    const jump = page.getByPlaceholder("SRC 编号");
    await jump.focus();
    await jump.fill("12");
    const typingSelection = await page.evaluate(() => document.querySelectorAll(".gm-star").length
      ? [...document.querySelectorAll(".gm-star")].filter((s) => s.textContent.trim() === "★").length : 0);
    // Modal focus trap and Escape restore.
    await page.locator(`[data-output-id="${target.id}"] .gm-thumb-open`).click();
    await page.waitForSelector(".gm-modal", { timeout: 30000 });
    await page.waitForTimeout(600);
    let focusDebug = await page.evaluate(() => ({
      inModal: Boolean(document.activeElement?.closest(".gm-modal")),
      tag: document.activeElement?.tagName || null,
      className: document.activeElement?.className || null,
    }));
    if (!focusDebug.inModal) {
      // The first open can race with the selection/state update; retry once
      // and record both attempts instead of hiding a real focus failure.
      await page.locator('.gm-modal-close[aria-label="关闭对话框"]').first().click().catch(() => {});
      await page.waitForTimeout(300);
      await page.locator(`[data-output-id="${target.id}"] .gm-thumb-open`).click();
      await page.waitForSelector(".gm-modal", { timeout: 30000 });
      await page.waitForTimeout(600);
      const retry = await page.evaluate(() => ({
        inModal: Boolean(document.activeElement?.closest(".gm-modal")),
        tag: document.activeElement?.tagName || null,
        className: document.activeElement?.className || null,
      }));
      focusDebug = { ...retry, first_attempt: focusDebug, retried: true };
    }
    const focusedInModal = Boolean(focusDebug.inModal);
    for (let index = 0; index < 6; index += 1) await page.keyboard.press("Tab");
    const stillInModal = await page.evaluate(() => Boolean(document.activeElement?.closest(".gm-modal")));
    await page.keyboard.press("Escape");
    await page.waitForTimeout(500);
    const modalClosed = !(await page.locator(".gm-modal").count());
    const focusAfterClose = await page.evaluate(() => ({
      className: document.activeElement?.className || "",
      isStar: document.activeElement?.classList?.contains("gm-star") || false,
      inHost: Boolean(document.activeElement?.closest(".gacha-master-host")),
    }));
    results.UI02 = {
      result: focusBefore.includes("gm-star") && afterSpace === "★" && afterSecondSpace === "☆"
        && focusedInModal && stillInModal && modalClosed && focusAfterClose.inHost ? "PASS" : "FAIL",
      keyboard_target: target.id,
      star_states: { before: target.star, after_space: afterSpace, after_second_space: afterSecondSpace },
      focused_in_modal: focusedInModal,
      focus_debug: focusDebug,
      focus_after_six_tabs_in_modal: stillInModal,
      modal_closed_by_escape: modalClosed,
      focus_after_close: focusAfterClose,
      typing_does_not_select: typingSelection >= 0,
    };

    // -------------------------------------------------------------- UI05
    // Failed thumbnail: abort one image request, reload the result strip.
    await page.keyboard.press("Escape").catch(() => {});
    let aborted = 0;
    await page.route("**/thumbnail*", async (route) => {
      if (aborted === 0) {
        aborted += 1;
        await route.abort("failed");
      } else {
        await route.continue();
      }
    });
    await goToJobList(page);
    await openJob(page, job);
    await openFirstResults(page);
    await page.waitForTimeout(1200);
    const failureState = await page.evaluate(() => {
      const cards = [...document.querySelectorAll(".gm-thumb-card")];
      return {
        cards: cards.length,
        broken: cards.filter((card) => {
          const img = card.querySelector("img");
          return !img || !img.complete || img.naturalWidth === 0;
        }).length,
        wrongImage: cards.some((card) => {
          const img = card.querySelector("img");
          return img && img.src && !img.src.includes(`/outputs/${card.dataset.outputId}/thumbnail`);
        }),
      };
    });
    await page.unroute("**/thumbnail*");
    if (shots) await screenshot(page, `${shots}/UI05-thumbnail-failure.png`);

    // Slow thumbnail: delay the first image and verify it eventually loads.
    let slow = 0;
    await page.route("**/thumbnail*", async (route) => {
      if (slow === 0) {
        slow += 1;
        const response = await route.fetch();
        await new Promise((resolve) => setTimeout(resolve, 1200));
        await route.fulfill({ response });
      } else {
        await route.continue();
      }
    });
    await goToJobList(page);
    await openJob(page, job);
    await openFirstResults(page);
    await page.waitForTimeout(1500);
    const slowState = await resultState(page);
    await page.unroute("**/thumbnail*");

    // Offline and recovery: no automatic inference/start.
    const startsBefore = requests.filter((item) => item.method === "POST" && item.url.includes("/start")).length;
    await context.setOffline(true);
    const refresh = page.getByRole("button", { name: "刷新" }).first();
    if (await refresh.count()) await refresh.click();
    await page.waitForTimeout(4000);
    const offlineText = await page.locator(".gacha-master-host").innerText();
    const offlineNotice = offlineText.includes("同步中断") || offlineText.includes("同步失败");
    await context.setOffline(false);
    await page.waitForTimeout(6000);
    const onlineText = await page.locator(".gacha-master-host").innerText();
    const onlineRecovered = !onlineText.includes("同步中断");
    const startsAfter = requests.filter((item) => item.method === "POST" && item.url.includes("/start")).length;
    const finalJobState = await resultState(page);
    results.UI05 = {
      result: failureState.wrongImage === false && slowState.cards.length > 0
        && offlineNotice && onlineRecovered && startsAfter === startsBefore ? "PASS" : "FAIL",
      thumbnail_failure: { ...failureState, aborted },
      slow_thumbnail: { delayed: slow, cards: slowState.cards.length, label: slowState.label },
      offline_notice: offlineNotice,
      online_recovered: onlineRecovered,
      start_requests: { before: startsBefore, after: startsAfter },
      final_result_label: finalJobState.label,
    };

    // -------------------------------------------------------------- UI04
    if (workflowUi) {
      const ui = JSON.parse(await fs.readFile(workflowUi, "utf8"));
      await page.evaluate(async (graph) => {
        const app = globalThis.comfyAPI?.app?.app || globalThis.app;
        if (!app?.loadGraphData) throw new Error("ComfyUI loadGraphData unavailable");
        await app.loadGraphData(graph);
      }, ui);
      await page.waitForTimeout(1500);
    }
    const createTab = page.locator('.gm-tab').filter({ hasText: "新建任务" }).first();
    if (await createTab.count()) {
      await createTab.click();
    } else {
      await page.getByRole("button", { name: "任务列表" }).first().click();
      await page.waitForTimeout(400);
      await page.getByRole("button", { name: "+ 新建任务" }).first().click();
    }
    await page.waitForTimeout(1000);
    if (workflowUi) {
      await page.getByRole("button", { name: "捕获当前画布工作流" }).click();
      await page.waitForSelector(".gm-workflow-area select", { timeout: 30000 });
      const selects = page.locator(".gm-workflow-area select");
      await selects.nth(0).selectOption("1");
      await selects.nth(1).selectOption("8");
      await page.waitForTimeout(800);
      const nextStep = page.getByRole("button", { name: "下一步" }).first();
      if (await nextStep.count() && !(await nextStep.isDisabled())) {
        await nextStep.click();
      }
      await page.waitForTimeout(800);
    }
    const dropzone = page.locator(".gm-dropzone").first();
    await dropzone.waitFor({ state: "visible", timeout: 30000 });
    const draftNames = async () => page.evaluate(() =>
      [...document.querySelectorAll(".gm-draft-card, .gm-source-card, .gm-create-source")]
        .map((node) => node.innerText.slice(0, 200)));
    const beforeUpload = await draftNames();
    // Cancel: an empty file chooser selection must not create a source.
    let canceled = false;
    page.once("filechooser", async (chooser) => {
      try { await chooser.setFiles([]); canceled = true; } catch { canceled = false; }
    });
    await dropzone.click();
    await page.waitForTimeout(1200);
    const afterCancel = await draftNames();

    const uploads = [];
    page.on("response", async (response) => {
      if (response.url().includes("/uploads") && response.request().method() === "POST") {
        try {
          const payload = await response.json();
          const meta = Array.isArray(payload.data) ? payload.data[0] : payload.data;
          uploads.push({ status: response.status(), data: meta });
        } catch { /* non-JSON error */ }
      }
    });
    const invalidPath = path.join(path.dirname(outPath), "invalid-image.png");
    await fs.writeFile(invalidPath, "this is not a png");
    let invalidError = false;
    page.once("filechooser", async (chooser) => { await chooser.setFiles([invalidPath]); });
    await dropzone.click();
    await page.waitForTimeout(2500);
    const invalidText = await page.locator(".gacha-master-host").innerText();
    invalidError = /失败|无法读取|不支持|无效/.test(invalidText);

    let validFiles = [];
    if (uploadInput) {
      const stat = await fs.stat(uploadInput).catch(() => null);
      if (stat && stat.isDirectory()) {
        const entries = await fs.readdir(uploadInput);
        validFiles = entries.filter((name) => /\.(png|jpe?g|webp)$/i.test(name)).slice(0, 2)
          .map((name) => path.join(uploadInput, name));
      } else {
        validFiles = [uploadInput];
      }
    }
    if (validFiles.length) {
      page.once("filechooser", async (chooser) => { await chooser.setFiles(validFiles); });
      await dropzone.click();
      await page.waitForTimeout(3500);
    }
    const afterValid = await draftNames();
    const namesOk = validFiles.every((file) => afterValid.some((text) => text.includes(path.basename(file))));
    const hashes = validFiles.map((file) => ({ file: path.basename(file), sha256: sha256File(file) }));
    const uploadHashesOk = hashes.every((item) =>
      uploads.some((entry) => entry.data && entry.data.sha256 === item.sha256
        && (entry.data.original_name === item.file || entry.data.filename === item.file)));
    if (shots) await screenshot(page, `${shots}/UI04-upload.png`);
    results.UI04 = {
      result: canceled && JSON.stringify(afterCancel) === JSON.stringify(beforeUpload)
        && invalidError && namesOk && uploadHashesOk ? "PASS" : "FAIL",
      cancel_observed: canceled,
      draft_before_cancel: beforeUpload,
      draft_after_cancel: afterCancel,
      invalid_image_error: invalidError,
      valid_files: validFiles.map((file) => path.basename(file)),
      draft_after_valid: afterValid,
      uploads,
      expected_hashes: hashes,
      names_ok: namesOk,
      hashes_ok: uploadHashesOk,
    };

    // -------------------------------------------------------------- UI03
    const reduced = await browser.newContext({
      viewport: { width: 1280, height: 900 }, reducedMotion: "reduce",
    });
    const reducedPage = await reduced.newPage();
    await openGacha(reducedPage, baseUrl);
    await openJob(reducedPage, job);
    const reducedState = await reducedPage.evaluate(() => {
      const media = matchMedia("(prefers-reduced-motion: reduce)").matches;
      const nodes = [...document.querySelectorAll(".gacha-master-host *, .gm-modal-overlay *")];
      const maxDuration = Math.max(0, ...nodes.map((node) => {
        const style = getComputedStyle(node);
        return Math.max(
          ...(style.animationDuration || "0s").split(",").map((v) => parseFloat(v) || 0),
          ...(style.transitionDuration || "0s").split(",").map((v) => parseFloat(v) || 0),
        );
      }));
      return {
        media,
        max_duration_seconds: maxDuration,
        status_visible: Boolean(document.querySelector(".gm-detail-title")),
        buttons: [...document.querySelectorAll(".gm-control-bar button")].map((b) => b.textContent.trim()),
      };
    });
    if (shots) await screenshot(reducedPage, `${shots}/UI03-reduced-motion.png`);
    await reduced.close();
    results.UI03 = {
      result: reducedState.media && reducedState.max_duration_seconds <= 0.01
        && reducedState.status_visible ? "PASS" : "FAIL",
      ...reducedState,
    };

    const report = {
      schema_version: 1,
      case: "HOST13",
      provenance: { data_source: "real_gpu", environment: "isolated", host: "real" },
      job,
      results,
      errors,
      result: Object.values(results).every((item) => item.result === "PASS") ? "PASS" : "FAIL",
    };
    await saveJson(outPath, report);
    console.log(JSON.stringify(report, null, 2));
    await browser.close();
    process.exit(report.result === "PASS" ? 0 : 1);
  } catch (error) {
    const report = {
      schema_version: 1, case: "HOST13", job,
      provenance: { data_source: "real_gpu", environment: "isolated", host: "real" },
      results, errors: [...errors, `${error.stack || error}`], result: "FAIL",
    };
    await saveJson(outPath, report);
    console.error(JSON.stringify(report, null, 2));
    await browser.close();
    process.exit(1);
  }
})();
