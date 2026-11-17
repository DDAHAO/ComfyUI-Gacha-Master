// Shared helpers for real-host Playwright scenarios.
// CommonJS is deliberate: Playwright is installed outside the repository and
// resolved through NODE_PATH, which ESM import does not honour.
const { chromium } = require("playwright");

function arg(name, fallback = null) {
  const index = process.argv.indexOf(`--${name}`);
  if (index === -1) return fallback;
  const value = process.argv[index + 1];
  if (!value || value.startsWith("--")) return fallback;
  return value;
}

function hasFlag(name) {
  return process.argv.includes(`--${name}`);
}

async function launch(options = {}) {
  return chromium.launch({
    headless: !hasFlag("headed"),
    args: ["--disable-gpu", "--no-sandbox", "--js-flags=--expose-gc"],
  });
}

async function openGacha(page, baseUrl) {
  await page.goto(baseUrl, { waitUntil: "domcontentloaded", timeout: 60000 });
  // A first-run/update dialog can cover the sidebar.  Dismiss overlays
  // without relying on their wording.
  for (let attempt = 0; attempt < 4; attempt += 1) {
    if (!(await page.locator('[data-testid="dialog-overlay"]').count())) break;
    await page.keyboard.press("Escape").catch(() => {});
    await page.waitForTimeout(300);
    if (await page.locator('[data-testid="dialog-overlay"]').count()) {
      const close = page.locator('[data-testid="dialog-overlay"] button').last();
      if (await close.count()) await close.click({ force: true }).catch(() => {});
      await page.waitForTimeout(300);
    }
  }
  await page.waitForSelector('button[aria-label*="抽卡大师"]', { timeout: 60000 });
  await page.locator('button[aria-label*="抽卡大师"]').first().click();
  await page.waitForSelector(".gacha-master-host", { timeout: 30000 });
  await page.waitForTimeout(600);
}

async function goToJobList(page) {
  const back = page.getByRole("button", { name: "← 任务列表" });
  if (await back.count()) {
    await back.first().click();
    await page.waitForTimeout(500);
  }
  await page.waitForSelector(".gm-job-card", { timeout: 30000 });
}

async function openJob(page, displayId) {
  const card = page.locator(".gm-job-card").filter({ hasText: displayId }).first();
  await card.waitFor({ state: "visible", timeout: 30000 });
  await card.click();
  await page.waitForSelector(".gm-detail-title", { timeout: 30000 });
  await page.waitForTimeout(800);
}

async function openFirstResults(page) {
  const button = page.getByRole("button", { name: "查看结果" }).first();
  await button.waitFor({ state: "visible", timeout: 30000 });
  await button.click();
  await page.waitForSelector(".gm-thumb-card", { timeout: 60000 });
  await page.waitForTimeout(600);
}

async function waitResultIdle(page, timeout = 60000) {
  await page.waitForFunction(() => {
    const bar = document.querySelector(".gm-result-toolbar");
    if (!bar) return false;
    const status = bar.querySelector('[role="status"]');
    return !status || status.hidden;
  }, { timeout });
  await page.waitForTimeout(250);
}

async function resultState(page) {
  return page.evaluate(() => {
    const bar = document.querySelector(".gm-result-toolbar");
    const cards = [...document.querySelectorAll(".gm-thumb-card")].map((card) => {
      const star = card.querySelector(".gm-star");
      const img = card.querySelector("img");
      return {
        id: card.dataset.outputId || null,
        title: card.title || "",
        text: card.querySelector(".gm-thumb-foot")?.innerText || "",
        star: star?.textContent || "",
        starAria: star?.getAttribute("aria-label") || null,
        imageSrc: img?.currentSrc || img?.src || null,
        imageLoaded: Boolean(img && img.complete && img.naturalWidth > 0),
        imageWidth: img?.naturalWidth || 0,
      };
    });
    return {
      cards,
      label: bar?.querySelector(".gm-pager-position")?.textContent || null,
      buttons: bar ? [...bar.querySelectorAll("button")].map((b) => ({
        text: b.textContent.trim(), disabled: b.disabled, hidden: b.hidden,
      })) : [],
      retryVisible: Boolean(bar && !bar.querySelector("button")?.hidden),
      sourceRows: document.querySelectorAll(".gm-source-row").length,
      sourceLabel: document.querySelector(".gm-source-pager .gm-pager-position")?.textContent || null,
      sourceCounts: document.querySelector(".gm-sources-counts")?.innerText || null,
      bodyText: document.querySelector(".gacha-master-host")?.innerText.slice(0, 4000) || "",
    };
  });
}

function drawNo(card) {
  const match = /DRAW\s+(\d+)/.exec(card.title || "");
  return match ? Number(match[1]) : null;
}

function outNo(card) {
  const match = /OUT\s+(\d+)/.exec(card.title || "");
  return match ? Number(match[1]) : null;
}

async function clickResultButton(page, label) {
  const bar = page.locator(".gm-result-toolbar").first();
  const button = bar.locator("button").filter({ hasText: label }).first();
  await button.click();
  await waitResultIdle(page);
}

async function openCompare(page, cardIndex = 0) {
  const card = page.locator(".gm-thumb-card").nth(cardIndex);
  await card.locator(".gm-thumb-open").click();
  await page.waitForSelector(".gm-compare-grid", { timeout: 30000 });
  await page.waitForFunction(() => {
    const images = [...document.querySelectorAll(".gm-compare-image")];
    return images.length >= 2 && images.every((img) => img.complete && img.naturalWidth > 0);
  }, { timeout: 30000 });
  const state = await page.evaluate(() => {
    const images = [...document.querySelectorAll(".gm-compare-image")];
    return {
      imageCount: images.length,
      sources: images.map((img) => img.getAttribute("src")),
      widths: images.map((img) => img.naturalWidth),
      heights: images.map((img) => img.naturalHeight),
      mode: document.querySelector(".gm-compare-mode-btn.active")?.textContent || null,
      position: document.querySelector(".gm-compare-position")?.textContent || null,
      meta: document.querySelector(".gm-compare-meta")?.innerText || null,
    };
  });
  await page.locator('.gm-modal-close[aria-label="关闭对话框"]').first().click();
  await page.waitForTimeout(400);
  return state;
}

async function saveJson(path, payload) {
  const fs = require("node:fs/promises");
  await fs.writeFile(path, JSON.stringify(payload, null, 2));
}

async function screenshot(page, path) {
  await page.screenshot({ path, fullPage: false });
}

module.exports = {
  arg, hasFlag, launch, openGacha, goToJobList, openJob, openFirstResults,
  waitResultIdle, resultState, drawNo, outNo, clickResultButton, openCompare,
  saveJson, screenshot,
};
