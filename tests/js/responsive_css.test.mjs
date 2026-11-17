// Static contract tests for responsive layout rules that cannot be covered by
// the DOM-free JS suite.  The narrow source-row layout previously regressed
// because grid placement was applied to `.gm-source-thumb` (an <img> nested in
// `.gm-source-thumb-host`) instead of the wrapper that is the actual grid item.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const css = readFileSync(
  new URL("../../comfyui_gacha_master/web/styles.css", import.meta.url),
  "utf8",
);

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// Return the body of the first `{...}` that follows `header`, honouring nested
// braces (media/container/supports blocks contain their own rules).
function extractBlock(source, header) {
  const start = source.indexOf(header);
  assert.notEqual(start, -1, `missing CSS block: ${header}`);
  const open = source.indexOf("{", start + header.length);
  assert.notEqual(open, -1, `missing opening brace after: ${header}`);
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    else if (source[index] === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(open + 1, index);
    }
  }
  throw new Error(`unbalanced CSS block: ${header}`);
}

function ruleBody(source, selector) {
  const match = new RegExp(`${escapeRegExp(selector)}\\s*\\{([^}]*)\\}`, "m").exec(source);
  return match ? match[1] : "";
}

const SOURCE_ROW_AREAS = [["thumb", ".gm-source-thumb-host"], ["index", ".gm-source-index"],
  ["info", ".gm-source-info"], ["actions", ".gm-source-actions"]];

function assertSourceRowAreas(block, selectorFor, label) {
  const meta = ruleBody(block, selectorFor(".gm-source-meta"));
  assert.match(meta, /grid-template-columns:\s*56px minmax\(0, 1fr\)/, `${label}: two-column grid`);
  assert.match(meta, /"thumb index"\s*"thumb info"\s*"actions actions"/, `${label}: named areas`);
  assert.match(meta, /align-items:\s*start/, `${label}: rows align to the thumbnail top`);
  for (const [area, selector] of SOURCE_ROW_AREAS) {
    assert.match(
      ruleBody(block, selectorFor(selector)),
      new RegExp(`grid-area:\\s*${area}`),
      `${label}: ${selector} occupies the ${area} area`,
    );
  }
}

test("narrow container layout places the thumbnail wrapper, not the image, in its grid area", () => {
  const narrow = extractBlock(css, "@container gm-app (max-width: 479px)");
  assertSourceRowAreas(narrow, (selector) => selector, "gm-app (max-width: 479px)");
  // The image is not a grid item; placing it must never stand in for the wrapper.
  assert.doesNotMatch(ruleBody(narrow, ".gm-source-thumb"), /grid-(area|row|column)/);
});

test("hosts without container queries keep the same narrow source-row layout", () => {
  const fallback = extractBlock(css, "@supports not (container-type: inline-size)");
  const prefix = ".gm-body[data-gm-size=\"narrow\"] ";
  assertSourceRowAreas(fallback, (selector) => prefix + selector, "data-gm-size=narrow");
  assert.match(ruleBody(fallback, prefix + ".gm-source-thumb"), /width:\s*56px/, "thumbnail matches the 56px column");
});
