// Pure create-draft validation. The view feeds it real draft state and the
// backend capabilities; tests can exercise the same rules without a DOM.

export const CREATE_STEPS = [
  { id: "workflow", label: "工作流", short: "工作流" },
  { id: "sources", label: "原图", short: "原图" },
  { id: "params", label: "参数", short: "参数" },
  { id: "review", label: "检查并创建", short: "检查创建" },
];

export function parseCount(raw) {
  const text = String(raw ?? "").trim();
  if (!text) return { valid: false, value: null, error: "请输入每张原图的抽卡次数。" };
  if (!/^\d+$/.test(text)) return { valid: false, value: null, error: "抽卡次数只能是正整数。" };
  const value = Number(text);
  if (!Number.isSafeInteger(value) || value < 1) {
    return { valid: false, value: null, error: "抽卡次数必须大于 0。" };
  }
  return { valid: true, value, error: "" };
}

export function sourceCounts(sources = []) {
  const counts = { total: sources.length, ready: 0, queued: 0, uploading: 0, error: 0 };
  for (const source of sources) {
    if (source.status === "ready") counts.ready += 1;
    else if (source.status === "error") counts.error += 1;
    else if (source.status === "queued") counts.queued += 1;
    else counts.uploading += 1;
  }
  return counts;
}

export function modeConflict(mode, sources = []) {
  return mode === "reference" && sources.some((source) => source.kind === "upload");
}

export function planLimitOf(capabilities) {
  const raw = capabilities?.limits?.plan_limit;
  const value = Number(raw);
  return Number.isFinite(value) && value > 0 ? value : null;
}

export function evaluateDraft(draft, capabilities) {
  const workflow = draft.workflow || {};
  const workflowValid = Boolean(workflow.api && workflow.bindings?.load_image_node_id && workflow.bindings?.save_image_node_id);
  const capturedWithoutBindings = Boolean(workflow.api) && !workflowValid;
  const counts = sourceCounts(draft.sources);
  const conflict = modeConflict(draft.mode, draft.sources);
  const sourcesComplete = counts.total > 0 && counts.ready === counts.total && !conflict;
  const count = parseCount(draft.countRaw);
  const planLimit = planLimitOf(capabilities);
  const planLimitUnknown = planLimit === null;
  const totalPlanned = count.valid ? counts.total * count.value : null;
  const planExceeded = Boolean(count.valid && planLimit !== null && totalPlanned > planLimit);
  const paramsComplete = count.valid && !planExceeded && counts.total > 0;
  const preflight = draft.preflight || { state: "idle" };
  const preflightCurrent = preflight.state === "ok"
    && preflight.workflowRevision === (draft.workflowRevision || 0)
    && preflight.bindingsRevision === (draft.bindingsRevision || 0);
  const reviewInvalid = preflight.state === "error" || preflight.state === "stale";
  const busy = Boolean(draft.busy) || draft.create?.state === "submitting";
  const frozen = draft.create?.state === "submitting"
    || draft.create?.state === "uncertain"
    || draft.create?.state === "verify";

  const completed = [];
  if (workflowValid) completed.push("workflow");
  if (sourcesComplete) completed.push("sources");
  if (paramsComplete) completed.push("params");

  const invalid = [];
  if (capturedWithoutBindings) invalid.push("workflow");
  if (counts.error > 0 || conflict) invalid.push("sources");
  if ((String(draft.countRaw ?? "").trim() !== "" && !count.valid) || planExceeded) invalid.push("params");
  if (reviewInvalid) invalid.push("review");

  let maxUnlocked = 0;
  if (workflowValid) maxUnlocked = 1;
  if (workflowValid && sourcesComplete) maxUnlocked = 2;
  if (workflowValid && sourcesComplete && paramsComplete) maxUnlocked = 3;

  const blockers = [];
  if (!workflowValid) blockers.push("先捕获工作流并绑定 LoadImage 与最终 SaveImage。");
  if (conflict) blockers.push("引用模式不能使用浏览器上传的图片。");
  if (counts.error > 0) blockers.push(`还有 ${counts.error} 张原图上传或校验失败。`);
  if (counts.total === 0) blockers.push("至少需要一张原图。");
  if (!count.valid) blockers.push(count.error);
  if (planExceeded) blockers.push(`计划 DRAW ${totalPlanned} 超过后端上限 ${planLimit}。`);
  if (planLimitUnknown && count.valid) blockers.push("尚未读取后端计划上限。");
  if (!preflightCurrent && preflight.state !== "running") blockers.push("最新预检尚未通过。");
  if (preflight.state === "running") blockers.push("预检仍在进行。");
  if (draft.create?.state === "uncertain") blockers.push("上一次创建请求结果未知，请先重试同一请求或到任务列表核实。");

  const canCreate = workflowValid
    && sourcesComplete
    && paramsComplete
    && !planLimitUnknown
    && preflightCurrent
    && !busy
    && draft.create?.state !== "uncertain"
    && draft.create?.state !== "verify";

  return {
    workflowValid,
    capturedWithoutBindings,
    sources: counts,
    conflict,
    sourcesComplete,
    count,
    planLimit,
    planLimitUnknown,
    totalPlanned,
    planExceeded,
    paramsComplete,
    preflightCurrent,
    reviewInvalid,
    completed,
    invalid,
    maxUnlocked,
    canCreate,
    frozen,
    blockers,
  };
}

export function buildCreateBody(draft) {
  const sources = (draft.sources || []).map((source) => {
    if (source.kind === "upload") return { upload_id: source.uploadId, sha256: source.sha256 };
    return { root: source.root, rel_path: source.rel_path, sha256: source.sha256 };
  });
  const body = {
    name: String(draft.name || "").trim() || undefined,
    protection_mode: draft.mode,
    count_per_source: draft.count,
    sources,
    workflow_ui: draft.workflow?.ui,
    workflow_api: draft.workflow?.api,
    bindings: draft.workflow?.bindings,
  };
  if (body.name === undefined) delete body.name;
  return body;
}

export function fingerprintBody(body) {
  return JSON.stringify(body);
}

export function isUncertainError(error) {
  if (!error) return false;
  if (error.code === "NETWORK_ERROR") return true;
  const status = Number(error.status);
  return status === 0 || status >= 500;
}
