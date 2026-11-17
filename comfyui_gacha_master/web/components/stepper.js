// Step navigation. Status is derived from real validation in the caller; the
// component only decides how a step looks and whether a click is allowed.

import { h, clear } from "../dom.js";

export function computeStepStates(steps, currentIndex, { completed = [], invalid = [], maxUnlocked = 0 } = {}) {
  const done = new Set(completed);
  const bad = new Set(invalid);
  return steps.map((step, index) => {
    let state;
    if (index === currentIndex) state = "current";
    else if (bad.has(step.id)) state = "error";
    else if (index > maxUnlocked) state = "locked";
    else if (done.has(step.id)) state = "complete";
    else state = "available";
    return { ...step, index, state };
  });
}

export function createStepper({ steps = [], currentStep = 0, states = null, onRequestStep } = {}) {
  const element = h("nav", { class: "gm-stepper", "aria-label": "创建步骤" });
  const compact = h("div", { class: "gm-stepper-compact" });
  const track = h("ol", { class: "gm-stepper-track" });
  element.append(compact, track);
  let currentStates = states || computeStepStates(steps, currentStep);
  let globallyDisabled = false;

  function render() {
    clear(track);
    clear(compact);
    const current = currentStates[currentStep] || currentStates[0];
    compact.append(
      h("span", { class: "gm-stepper-compact-label" }, `第 ${currentStep + 1}/${currentStates.length} 步 · ${current?.label || ""}`),
      h("span", { class: "gm-stepper-dots", "aria-hidden": "true" },
        ...currentStates.map((step, index) => h("span", { class: `gm-stepper-dot ${step.state} ${index === currentStep ? "on" : ""}` }))),
    );
    for (const step of currentStates) {
      const isCurrent = step.index === currentStep;
      const disabled = globallyDisabled || step.state === "locked";
      const item = h("li", { class: `gm-step ${step.state}` });
      const control = h("button", {
        class: "gm-step-btn",
        type: "button",
        disabled,
        "aria-current": isCurrent ? "step" : undefined,
        title: globallyDisabled ? "当前请求处理中，步骤暂时锁定" : step.state === "locked" ? "完成前面步骤后可进入" : step.label,
        onclick: () => {
          if (disabled || isCurrent) return;
          onRequestStep?.(step.index);
        },
      },
        h("span", { class: "gm-step-index", "aria-hidden": "true" },
          step.state === "complete" ? "✓" : step.state === "error" ? "!" : String(step.index + 1)),
        h("span", { class: "gm-step-label" }, step.label),
        h("span", { class: "gm-sr-only" }, `（${step.state === "complete" ? "已完成" : step.state === "error" ? "存在问题" : step.state === "current" ? "当前步骤" : step.state === "locked" ? "尚未解锁" : "可进入"}）`),
      );
      item.appendChild(control);
      track.appendChild(item);
    }
  }

  function update({ steps: nextSteps = steps, currentStep: nextCurrent = currentStep, states: nextStates = null, disabled = globallyDisabled } = {}) {
    if (nextSteps !== steps) steps = nextSteps;
    currentStep = Math.max(0, Math.min(Number(nextCurrent) || 0, steps.length - 1));
    currentStates = nextStates || computeStepStates(steps, currentStep);
    globallyDisabled = Boolean(disabled);
    render();
  }

  function destroy() {
    element.remove();
  }

  render();
  return { element, update, destroy };
}
