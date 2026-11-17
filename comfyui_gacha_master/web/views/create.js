import { idempotencyKey } from "../api.js";
import {
  badge, button, clear, confirmDialog, errorBox, fmtBytes, fmtTime, h, labeled, spinner, frag,
} from "../dom.js";
import { createStepper, computeStepStates } from "../components/stepper.js";
import { createDropzone } from "../components/dropzone.js";
import {
  CREATE_STEPS, buildCreateBody, evaluateDraft, fingerprintBody, isUncertainError,
  parseCount,
} from "../create_draft.js";

const UPLOAD_CONCURRENCY = 3;
const ACCEPTED = /\.(png|jpe?g|webp)$/i;
const ACCEPTED_MIME = /^image\/(png|jpeg|webp)$/i;

export function mountCreate(container, ctx) {
  let destroyed = false;
  let seq = 0;
  let preflightToken = 0;
  let activeUploads = 0;
  let uploadPumpScheduled = false;
  const uploadQueue = [];
  const uploadControllers = new Set();
  const objectUrls = new Set();
  let preflightController = null;
  let scanController = null;
  let createController = null;
  let captureBusy = false;

  const draft = {
    step: 0,
    name: "",
    countRaw: "5",
    count: 5,
    mode: "snapshot",
    sources: [],
    rejected: [],
    workflow: { ui: null, api: null, nodes: [], bindings: { load_image_node_id: "", save_image_node_id: "" }, capturedAt: null },
    workflowRevision: 0,
    bindingsRevision: 0,
    preflight: { state: "idle", result: null, workflowRevision: -1, bindingsRevision: -1, error: null },
    busy: false,
    create: { state: "idle", key: null, body: null, fingerprint: "", error: null },
  };

  // ------------------------------------------------------------- controls
  const nameInput = h("input", { class: "gm-input", placeholder: "例如：角色 A 五连抽", maxlength: 120 });
  const countInput = h("input", {
    class: "gm-input", type: "number", min: 1, max: 100000, step: 1, value: "5", inputmode: "numeric",
  });
  const modeSelect = h("select", { class: "gm-input" },
    h("option", { value: "snapshot", selected: true }, "快照模式（默认，推荐）"),
    h("option", { value: "reference" }, "原路径引用模式"),
  );
  const sourcesGrid = h("div", { class: "gm-source-grid" });
  const rejectedArea = h("div", { class: "gm-rejected-area" });
  const conflictArea = h("div", { class: "gm-mode-conflict" });
  const folderResult = h("div", { class: "gm-folder-result" });
  const planStats = h("div", { class: "gm-plan-summary" });
  const planError = h("div", { class: "gm-plan-error" });
  const workflowArea = h("div", { class: "gm-workflow-area" });
  const preflightArea = h("div", { class: "gm-preflight-area" });
  const reviewSummary = h("div", { class: "gm-review-summary" });
  const createStatus = h("div", { class: "gm-create-status" });
  const stepStatus = h("div", { class: "gm-wizard-status", role: "status", "aria-live": "polite" });

  const stepperHost = h("div", { class: "gm-stepper-host" });
  const stepper = createStepper({
    steps: CREATE_STEPS,
    currentStep: 0,
    onRequestStep: (index) => goStep(index),
  });
  stepperHost.appendChild(stepper.element);

  const dropzone = createDropzone({
    accept: "image/png,image/jpeg,image/webp",
    onFiles: (files, meta) => handleDroppedFiles(files, meta),
    onFolderDropped: () => appendRejected("文件夹", "浏览器无法获得拖入文件夹的服务器绝对路径；请使用下方“从后端文件夹导入”。"),
    onOpenReference: () => openFolderImport(),
    onActivate: () => {},
  });

  const headers = h("div", { class: "gm-section-head" },
    h("div", null,
      h("h2", null, "新建抽卡任务"),
      h("p", { class: "gm-help" }, "四个步骤共享同一份草稿。创建成功后只进入任务详情，不会自动开始执行。"),
    ),
    button("← 返回任务", () => ctx.navigate("jobs"), { kind: "muted" }),
  );

  // ---------------------------------------------------------------- panes
  const workflowPane = h("section", { class: "gm-card gm-step-pane", "aria-labelledby": "gm-step-workflow" },
    h("h3", { id: "gm-step-workflow" }, "1 · 捕获工作流"),
    h("p", { class: "gm-help" }, "从当前画布捕获执行副本（不会修改编辑器）。必须绑定一个 LoadImage 和一个最终 SaveImage；额外 SaveImage 会保留原行为并给出警告。"),
    workflowArea,
  );

  let folderImport = null;
  const sourcesPane = h("section", { class: "gm-card gm-step-pane", "aria-labelledby": "gm-step-sources" },
    h("h3", { id: "gm-step-sources" }, "2 · 原图"),
    h("div", { class: "gm-form-row" },
      labeled("原图保护模式", modeSelect, "创建后固定，不能切换。快照模式保存受控副本；引用模式只记录后端可访问路径。"),
    ),
    conflictArea,
    dropzone.element,
    rejectedArea,
    h("p", { class: "gm-help" }, "添加顺序即执行顺序；尾部保留一个空白格。上传并发最多 3 个，其余排队等待。"),
    sourcesGrid,
    buildFolderImport(),
  );

  const paramsPane = h("section", { class: "gm-card gm-step-pane", "aria-labelledby": "gm-step-params" },
    h("h3", { id: "gm-step-params" }, "3 · 参数"),
    h("div", { class: "gm-form-row" }, labeled("任务名称（可留空）", nameInput)),
    h("div", { class: "gm-form-row" }, labeled("每张原图抽卡次数", countInput, "正整数；无需设置 ComfyUI 原生队列次数。")),
    planError,
    planStats,
  );

  const reviewPane = h("section", { class: "gm-card gm-step-pane", "aria-labelledby": "gm-step-review" },
    h("h3", { id: "gm-step-review" }, "4 · 检查并创建"),
    preflightArea,
    h("h3", null, "创建摘要"),
    reviewSummary,
    createStatus,
  );

  const backBtn = button("上一步", () => goStep(draft.step - 1), { kind: "muted" });
  const nextBtn = button("下一步", () => nextStep(), { kind: "primary" });
  const createBtn = button("创建任务", () => submitCreate(), { kind: "primary" });
  const footerActions = h("div", { class: "gm-inline-actions" }, backBtn, nextBtn, createBtn);

  container.append(
    headers,
    stepperHost,
    h("div", { class: "gm-step-panes" }, workflowPane, sourcesPane, paramsPane, reviewPane),
    h("div", { class: "gm-wizard-footer" }, footerActions, stepStatus),
  );

  nameInput.addEventListener("input", () => {
    if (isFrozen()) return;
    draft.name = nameInput.value;
    renderReview();
    updateStepper();
  });
  countInput.addEventListener("input", () => {
    if (isFrozen()) return;
    draft.countRaw = countInput.value;
    draft.count = parseCount(countInput.value).value;
    renderPlan();
    renderReview();
    updateStepper();
  });
  modeSelect.addEventListener("change", () => {
    if (isFrozen()) return;
    draft.mode = modeSelect.value;
    renderSources();
    renderConflict();
    renderPlan();
    renderReview();
    updateStepper();
    if (draft.mode === "reference") {
      ctx.notify("引用模式只使用后端可访问路径；浏览器上传需要先移除或改回快照模式。", "info");
    }
  });

  const beforeUnload = (event) => {
    if (!hasDraftContent()) return;
    event.preventDefault();
    event.returnValue = "";
  };
  window.addEventListener("beforeunload", beforeUnload);
  ctx.setLeaveGuard?.(leaveGuard);

  // ------------------------------------------------------------- workflow
  function captureWorkflow() {
    if (captureBusy || isFrozen()) return;
    const comfyApp = ctx.comfyApp || globalThis.app;
    if (!comfyApp?.graphToPrompt) {
      ctx.notify("无法访问 ComfyUI 画布 API，请确认插件由 ComfyUI 正常加载。", "error");
      return;
    }
    captureBusy = true;
    Promise.resolve(comfyApp.graphToPrompt()).then((captured) => {
      if (destroyed) return;
      const nodes = Object.entries(captured.output || {}).map(([id, node]) => ({
        id,
        class_type: node.class_type,
        title: node?._meta?.title || node.class_type,
      }));
      const loads = nodes.filter((node) => node.class_type === "LoadImage");
      const saves = nodes.filter((node) => node.class_type === "SaveImage");
      draft.workflow = {
        ui: captured.workflow,
        api: captured.output,
        nodes,
        bindings: {
          load_image_node_id: loads.length === 1 ? loads[0].id : "",
          save_image_node_id: saves.length === 1 ? saves[0].id : "",
        },
        capturedAt: Date.now(),
      };
      draft.workflowRevision += 1;
      draft.bindingsRevision += 1;
      invalidatePreflight("工作流已重新捕获");
      renderWorkflow();
      renderReview();
      updateStepper();
      ctx.notify(`已捕获 ${nodes.length} 个节点。`, "ok");
      requestAnimationFrame(() => {
        workflowArea.querySelector("select")?.focus();
      });
    }).catch((error) => {
      ctx.notify(`捕获工作流失败：${error?.message || error}`, "error");
    }).finally(() => {
      captureBusy = false;
    });
  }

  function renderWorkflow() {
    clear(workflowArea);
    const workflow = draft.workflow;
    if (!workflow.api) {
      workflowArea.append(
        h("div", { class: "gm-workflow-empty" },
          h("p", null, "尚未捕获工作流。请先打开要抽卡的图生图工作流。"),
          button("捕获当前画布工作流", captureWorkflow, { kind: "primary", disabled: isFrozen() }),
        ),
      );
      return;
    }
    const loads = workflow.nodes.filter((node) => node.class_type === "LoadImage");
    const saves = workflow.nodes.filter((node) => node.class_type === "SaveImage");
    const otherOutputs = workflow.nodes.filter((node) => ["SaveImage", "PreviewImage"].includes(node.class_type)
      && node.id !== workflow.bindings.save_image_node_id);
    const loadSelect = h("select", { class: "gm-input", disabled: isFrozen() },
      h("option", { value: "" }, "请选择 LoadImage"),
      ...loads.map((node) => h("option", { value: node.id, selected: workflow.bindings.load_image_node_id === node.id },
        `#${node.id} ${node.title} (${node.class_type})`)),
    );
    const saveSelect = h("select", { class: "gm-input", disabled: isFrozen() },
      h("option", { value: "" }, "请选择最终 SaveImage"),
      ...saves.map((node) => h("option", { value: node.id, selected: workflow.bindings.save_image_node_id === node.id },
        `#${node.id} ${node.title} (${node.class_type})`)),
    );
    loadSelect.addEventListener("change", () => {
      workflow.bindings.load_image_node_id = loadSelect.value;
      draft.bindingsRevision += 1;
      invalidatePreflight("LoadImage 绑定已变化");
      updateStepper();
      renderReview();
    });
    saveSelect.addEventListener("change", () => {
      workflow.bindings.save_image_node_id = saveSelect.value;
      draft.bindingsRevision += 1;
      invalidatePreflight("SaveImage 绑定已变化");
      updateStepper();
      renderReview();
    });
    workflowArea.append(frag(
      h("div", { class: "gm-workflow-summary" },
        h("span", null, `已捕获 ${workflow.nodes.length} 个节点`),
        h("span", { class: "gm-mono" }, `API 节点 ${Object.keys(workflow.api).length}`),
        h("span", { class: "gm-help" }, `捕获于 ${fmtTime(new Date(workflow.capturedAt).toISOString())}`),
        button("重新捕获", captureWorkflow, { kind: "muted", disabled: isFrozen() }),
      ),
      h("div", { class: "gm-form-row two" },
        labeled("输入节点 LoadImage", loadSelect),
        labeled("最终输出节点 SaveImage", saveSelect),
      ),
      loads.length > 1 ? h("p", { class: "gm-help" }, `发现 ${loads.length} 个 LoadImage；V1.0 只绑定其中一个，其他保持原行为。`) : null,
      saves.length === 0 ? h("p", { class: "gm-inline-error" }, "当前工作流没有标准 SaveImage，不能创建抽卡任务。") : null,
      loads.length === 0 ? h("p", { class: "gm-inline-error" }, "当前工作流没有标准 LoadImage，不能创建抽卡任务。") : null,
      otherOutputs.length ? h("p", { class: "gm-warning" }, "工作流包含额外输出节点；这些文件会按原工作流行为额外保存，不纳入抽卡大师管理。") : null,
      h("p", { class: "gm-help" }, "草稿保存的是捕获副本；外部画布改变不会替换它，需要时请重新捕获。"),
    ));
  }

  // -------------------------------------------------------------- sources
  function buildFolderImport() {
    const rootInput = h("input", { class: "gm-input", placeholder: "后端已登记的输入根目录" });
    const subInput = h("input", { class: "gm-input", placeholder: "子目录（可留空）" });
    const scanBtn = button("扫描根目录", async () => {
      clear(folderResult);
      folderResult.appendChild(spinner("正在扫描并校验图片…"));
      scanController?.abort();
      scanController = new AbortController();
      try {
        const payload = await ctx.api.scanInputs(rootInput.value.trim(), subInput.value.trim(), { signal: scanController.signal });
        if (destroyed) return;
        renderScanResult(payload, rootInput.value.trim());
      } catch (error) {
        if (error?.name === "AbortError" || destroyed) return;
        clear(folderResult);
        folderResult.appendChild(errorBox(error));
      }
    }, { kind: "muted", disabled: isFrozen() });
    const details = h("details", { class: "gm-folder-details" },
      h("summary", null, "从后端文件夹根目录导入（不递归）"),
      h("div", { class: "gm-form-row two" }, labeled("输入根目录（服务器路径）", rootInput), labeled("子目录", subInput)),
      h("div", { class: "gm-inline-actions" }, scanBtn, h("span", { class: "gm-help" }, "只枚举该目录直接包含的图片，不跟随子目录或链接。顺序按自然排序冻结。")),
      folderResult,
    );
    details._scanButton = scanBtn;
    details._rootInput = rootInput;
    folderImport = details;
    return details;
  }

  function openFolderImport() {
    if (!folderImport) return;
    folderImport.open = true;
    folderImport.scrollIntoView({ block: "nearest", behavior: "smooth" });
    folderImport._rootInput?.focus();
  }

  function renderScanResult(payload, root) {
    clear(folderResult);
    const valid = (payload.items || []).filter((item) => item.valid);
    const invalid = (payload.items || []).filter((item) => !item.valid);
    folderResult.append(
      h("div", { class: "gm-scan-summary" },
        h("span", null, `有效 ${valid.length} 张`),
        invalid.length ? badge(`${invalid.length} 个无效文件`, "bad") : null,
        h("span", { class: "gm-help" }, "顺序已按自然排序冻结"),
      ),
    );
    if (invalid.length) {
      folderResult.appendChild(h("div", { class: "gm-invalid-list" },
        h("p", null, "以下文件不能静默跳过；你可以修正后重新扫描，或明确排除这些文件再创建。"),
        ...invalid.map((item) => h("div", { class: "gm-invalid-row" },
          h("span", { class: "gm-ellipsis", title: item.name }, item.name),
          h("span", { class: "gm-inline-error" }, item.error?.message || "无效图片"),
        )),
      ));
    }
    if (!valid.length) {
      folderResult.appendChild(h("p", { class: "gm-help" }, "没有可导入的有效图片。"));
      return;
    }
    const list = h("div", { class: "gm-scan-list" });
    const checks = new Map();
    valid.forEach((item, index) => {
      const checkbox = h("input", { type: "checkbox", checked: true });
      checks.set(item, checkbox);
      list.appendChild(h("label", { class: "gm-scan-row" },
        checkbox,
        h("span", { class: "gm-mono" }, `#${String(index + 1).padStart(4, "0")}`),
        h("span", { class: "gm-ellipsis", title: item.name }, item.name),
        h("span", { class: "gm-help" }, `${item.width}×${item.height} · ${fmtBytes(item.size)}`),
      ));
    });
    folderResult.append(list, h("div", { class: "gm-inline-actions" },
      button(`添加选中的 ${valid.length} 张`, () => {
        let added = 0;
        let duplicates = 0;
        for (const item of valid) {
          if (!checks.get(item)?.checked) continue;
          if (draft.sources.some((source) => source.kind === "path" && source.root === root && source.rel_path === item.rel_path)) {
            duplicates += 1;
            continue;
          }
          draft.sources.push({
            key: `path-${++seq}`,
            kind: "path",
            root,
            rel_path: item.rel_path,
            name: item.name,
            sha256: item.sha256,
            size: item.size,
            width: item.width,
            height: item.height,
            status: "ready",
            error: null,
          });
          added += 1;
        }
        renderSources();
        renderPlan();
        renderReview();
        updateStepper();
        ctx.notify(`已添加 ${added} 张原图${duplicates ? `，跳过 ${duplicates} 张重复项` : ""}。`, "ok");
      }, { kind: "primary", disabled: isFrozen() }),
      h("span", { class: "gm-help" }, "创建时会重新校验路径和哈希，并冻结排序。"),
    ));
  }

  function handleDroppedFiles(files, { hasFolder } = {}) {
    if (hasFolder) {
      ctx.notify("浏览器无法获得拖入文件夹的服务器绝对路径，请使用“从后端文件夹导入”。", "warn");
    }
    addFiles(files);
  }

  function validateFile(file) {
    if (!file) return "无法读取文件。";
    if (!ACCEPTED.test(file.name) && !ACCEPTED_MIME.test(file.type)) return "不支持的文件类型（仅 PNG/JPEG/WebP）。";
    if (file.size === 0) return "空文件。";
    const limit = Number(ctx.store.get().capabilities?.limits?.upload_max_file);
    if (Number.isFinite(limit) && limit > 0 && file.size > limit) {
      return `文件超过上传上限 ${fmtBytes(limit)}。`;
    }
    return "";
  }

  function addFiles(files) {
    if (isFrozen()) return;
    if (draft.mode === "reference") {
      draft.rejected = files.map((file) => ({ name: file.name, reason: "引用模式不能使用浏览器上传；请改用快照模式或后端目录导入。" }));
      renderRejected();
      ctx.notify("引用模式不能使用浏览器上传。可改回快照模式，或使用后端文件夹导入。", "warn");
      return;
    }
    const accepted = [];
    const rejected = [];
    for (const file of files) {
      const reason = validateFile(file);
      if (reason) rejected.push({ name: file.name, reason });
      else accepted.push(file);
    }
    draft.rejected = rejected;
    for (const file of accepted) {
      const entry = {
        key: `upload-${++seq}`,
        kind: "upload",
        name: file.name,
        file,
        status: "queued",
        uploadId: null,
        sha256: null,
        size: file.size,
        width: null,
        height: null,
        error: null,
        objectUrl: URL.createObjectURL(file),
        attempt: 0,
      };
      objectUrls.add(entry.objectUrl);
      draft.sources.push(entry);
      uploadQueue.push(entry);
    }
    renderRejected();
    renderSources();
    renderPlan();
    renderReview();
    updateStepper();
    if (rejected.length) {
      ctx.notify(`已添加 ${accepted.length} 张；${rejected.length} 个文件被拒绝，原因见列表。`, accepted.length ? "warn" : "error");
    }
    pumpUploads();
  }

  function appendRejected(name, reason) {
    draft.rejected = [...draft.rejected, { name, reason }];
    renderRejected();
  }

  function renderRejected() {
    clear(rejectedArea);
    if (!draft.rejected.length) {
      rejectedArea.hidden = true;
      return;
    }
    rejectedArea.hidden = false;
    const list = h("div", { class: "gm-rejected-list" },
      h("div", { class: "gm-rejected-head" },
        h("strong", null, `${draft.rejected.length} 个文件未加入草稿`),
        button("清除说明", () => { draft.rejected = []; renderRejected(); }, { kind: "muted" }),
      ),
      ...draft.rejected.map((item) => h("div", { class: "gm-rejected-row" },
        h("span", { class: "gm-ellipsis", title: item.name }, item.name),
        h("span", { class: "gm-inline-error" }, item.reason),
      )),
    );
    rejectedArea.appendChild(list);
  }

  function pumpUploads() {
    if (destroyed || uploadPumpScheduled) return;
    uploadPumpScheduled = true;
    queueMicrotask(() => {
      uploadPumpScheduled = false;
      while (!destroyed && activeUploads < UPLOAD_CONCURRENCY && uploadQueue.length) {
        const entry = uploadQueue.shift();
        if (!draft.sources.includes(entry) || entry.status !== "queued") continue;
        startUpload(entry);
      }
    });
  }

  function startUpload(entry) {
    entry.status = "uploading";
    entry.attempt += 1;
    entry.error = null;
    activeUploads += 1;
    const controller = new AbortController();
    entry.controller = controller;
    uploadControllers.add(controller);
    renderSources();
    renderDropzone();
    updateStepper();
    ctx.api.upload(entry.file, { signal: controller.signal }).then((metadata) => {
      if (destroyed || !draft.sources.includes(entry)) return;
      entry.uploadId = metadata.upload_id;
      entry.sha256 = metadata.sha256;
      entry.size = metadata.size;
      entry.width = metadata.width;
      entry.height = metadata.height;
      entry.status = "ready";
    }).catch((error) => {
      if (destroyed || error?.name === "AbortError" || !draft.sources.includes(entry)) return;
      entry.status = "error";
      entry.error = error?.message || String(error);
    }).finally(() => {
      uploadControllers.delete(controller);
      if (entry.controller === controller) entry.controller = null;
      activeUploads = Math.max(0, activeUploads - 1);
      if (destroyed) return;
      renderSources();
      renderDropzone();
      renderPlan();
      renderReview();
      updateStepper();
      pumpUploads();
    });
  }

  function retryUpload(entry) {
    if (isFrozen() || entry.status !== "error") return;
    entry.status = "queued";
    entry.error = null;
    uploadQueue.push(entry);
    renderSources();
    pumpUploads();
  }

  function renderDropzone() {
    const counts = { ready: 0, queued: 0, uploading: 0, error: 0 };
    for (const source of draft.sources) {
      if (source.status === "ready") counts.ready += 1;
      else if (source.status === "queued") counts.queued += 1;
      else if (source.status === "error") counts.error += 1;
      else counts.uploading += 1;
    }
    dropzone.update({
      mode: draft.mode,
      disabled: isFrozen(),
      title: draft.mode === "reference" ? "引用模式：请从后端受控目录导入" : "点击选择，或把图片拖到这里",
      status: "",
      tone: counts.error ? "error" : "",
      counts,
    });
  }

  const sourceCards = new Map();
  function createSourceCard(entry) {
    const order = h("span", { class: "gm-draft-order" });
    const thumbHost = h("div", { class: "gm-draft-thumb-host" });
    const name = h("span", { class: "gm-ellipsis gm-draft-name" });
    const meta = h("div", { class: "gm-draft-meta" });
    const state = h("div", { class: "gm-draft-state" });
    const up = h("button", { class: "gm-icon-btn", type: "button", title: "向前移动", onclick: () => moveSource(entry, -1) }, "←");
    const down = h("button", { class: "gm-icon-btn", type: "button", title: "向后移动", onclick: () => moveSource(entry, 1) }, "→");
    const retry = button("重试", () => retryUpload(entry), { kind: "muted" });
    const remove = h("button", { class: "gm-icon-btn gm-draft-remove", type: "button", title: "从草稿移除", "aria-label": `从草稿移除 ${entry.name}`, onclick: () => removeSource(entry) }, "×");
    const node = h("div", { class: "gm-draft-card", dataset: { key: entry.key } },
      order, thumbHost,
      h("div", { class: "gm-draft-body" }, name, meta, state),
      h("div", { class: "gm-draft-move" }, up, down),
      retry,
      remove,
    );
    return { node, order, thumbHost, name, meta, state, up, down, retry, remove };
  }

  function updateSourceCard(card, entry, index) {
    card.node.className = `gm-draft-card ${entry.status}`;
    card.order.textContent = `#${String(index + 1).padStart(4, "0")}`;
    card.name.textContent = entry.name;
    card.name.title = entry.name;
    clear(card.thumbHost);
    if (entry.objectUrl) {
      // loading=lazy keeps a large drop from decoding every source at once;
      // only visible cards fetch/decode their preview.
      card.thumbHost.appendChild(h("img", {
        class: "gm-draft-thumb",
        src: entry.objectUrl,
        alt: entry.name,
        draggable: false,
        loading: "lazy",
        decoding: "async",
      }));
    } else {
      card.thumbHost.appendChild(h("div", { class: "gm-draft-thumb placeholder" }, entry.kind === "path" ? "SRC" : "…"));
    }
    clear(card.meta);
    if (entry.status === "ready") {
      card.meta.append(
        h("span", { class: "gm-help" }, `${entry.width || "?"}×${entry.height || "?"} · ${fmtBytes(entry.size)} · ${entry.kind === "upload" ? "浏览器上传" : "后端路径"}`),
      );
      if (entry.sha256) {
        card.meta.appendChild(h("details", { class: "gm-hash-details" },
          h("summary", null, "哈希"),
          h("code", { class: "gm-mono" }, entry.sha256),
        ));
      }
    } else if (entry.kind === "path") {
      card.meta.appendChild(h("span", { class: "gm-help" }, entry.rel_path));
    } else {
      card.meta.appendChild(h("span", { class: "gm-help" }, fmtBytes(entry.size)));
    }
    clear(card.state);
    card.state.className = `gm-draft-state ${entry.status}`;
    if (entry.status === "queued") card.state.appendChild(h("span", { class: "gm-help" }, "排队中…"));
    else if (entry.status === "uploading") card.state.appendChild(spinner("上传与校验中…"));
    else if (entry.status === "error") card.state.appendChild(h("span", { class: "gm-inline-error" }, entry.error || "上传失败"));
    else if (entry.status === "ready") card.state.appendChild(badge(entry.kind === "upload" ? "已上传并校验" : "路径已登记", "ok"));
    card.up.disabled = index === 0 || isFrozen();
    card.down.disabled = index === draft.sources.length - 1 || isFrozen();
    card.remove.disabled = isFrozen();
    card.retry.hidden = entry.status !== "error";
    card.retry.disabled = isFrozen();
  }

  function renderSources() {
    clear(sourcesGrid);
    const liveKeys = new Set(draft.sources.map((source) => source.key));
    for (const [key, card] of sourceCards) {
      if (!liveKeys.has(key)) {
        card.node.remove();
        sourceCards.delete(key);
      }
    }
    draft.sources.forEach((entry, index) => {
      let card = sourceCards.get(entry.key);
      if (!card) {
        card = createSourceCard(entry);
        sourceCards.set(entry.key, card);
      }
      updateSourceCard(card, entry, index);
      sourcesGrid.appendChild(card.node);
    });
    if (!draft.sources.length) {
      sourcesGrid.appendChild(h("div", { class: "gm-empty-draft" }, "还没有原图。"));
    }
    const blankTile = h("button", {
      class: "gm-source-add",
      type: "button",
      title: draft.mode === "reference" ? "打开后端目录导入" : "点击添加图片，或拖拽到此处",
      disabled: isFrozen(),
      onclick: () => {
        if (draft.mode === "reference") openFolderImport();
        else dropzone.openPicker();
      },
    },
      h("span", { class: "gm-plus", "aria-hidden": "true" }, "+"),
      h("span", null, draft.mode === "reference" ? "从后端目录导入" : "点击或拖拽添加原图"),
    );
    sourcesGrid.appendChild(blankTile);
    renderDropzone();
  }

  function renderConflict() {
    clear(conflictArea);
    const uploads = draft.sources.filter((source) => source.kind === "upload");
    if (draft.mode !== "reference" || !uploads.length) {
      conflictArea.hidden = true;
      return;
    }
    conflictArea.hidden = false;
    conflictArea.appendChild(h("div", { class: "gm-notice warn" },
      h("strong", null, "引用模式与浏览器上传冲突"),
      h("p", null, `草稿中有 ${uploads.length} 张浏览器上传图片。引用模式不能使用它们；请改回快照模式，或移除这些上传后再使用后端路径。`),
      h("div", { class: "gm-inline-actions" },
        button("改回快照模式", () => {
          draft.mode = "snapshot";
          modeSelect.value = "snapshot";
          renderSources(); renderConflict(); renderPlan(); renderReview(); updateStepper();
        }, { kind: "primary", disabled: isFrozen() }),
        button("移除全部浏览器上传", () => {
          for (const entry of uploads) removeSource(entry, { silent: true });
          renderSources(); renderConflict(); renderPlan(); renderReview(); updateStepper();
          ctx.notify("已移除草稿中的浏览器上传图片。", "ok");
        }, { kind: "danger", disabled: isFrozen() }),
      ),
    ));
  }

  function moveSource(entry, delta) {
    if (isFrozen()) return;
    const index = draft.sources.indexOf(entry);
    const target = index + delta;
    if (index < 0 || target < 0 || target >= draft.sources.length) return;
    [draft.sources[index], draft.sources[target]] = [draft.sources[target], draft.sources[index]];
    renderSources();
    renderPlan();
    renderReview();
    updateStepper();
  }

  function removeSource(entry, { silent = false } = {}) {
    if (isFrozen()) return;
    const index = draft.sources.indexOf(entry);
    if (index < 0) return;
    draft.sources.splice(index, 1);
    if (entry.status === "uploading" && entry.controller) {
      // Stops the browser from waiting; the server may still finish the inbox
      // write, which is intentionally not described as a rollback.
      entry.controller.abort();
      uploadControllers.delete(entry.controller);
      entry.controller = null;
    }
    if (entry.objectUrl) {
      URL.revokeObjectURL(entry.objectUrl);
      objectUrls.delete(entry.objectUrl);
    }
    const queuedIndex = uploadQueue.indexOf(entry);
    if (queuedIndex >= 0) uploadQueue.splice(queuedIndex, 1);
    if (!silent) {
      renderSources();
      renderConflict();
      renderPlan();
      renderReview();
      updateStepper();
    }
  }

  // ---------------------------------------------------------------- plan
  function renderPlan() {
    clear(planStats);
    clear(planError);
    const evaluation = evaluateDraft(draft, ctx.store.get().capabilities);
    const planLimitText = evaluation.planLimit === null ? "未读取" : String(evaluation.planLimit);
    planStats.append(
      h("div", { class: "gm-stat" }, h("span", null, "原图"), h("strong", null, String(evaluation.sources.total))),
      h("div", { class: "gm-stat" }, h("span", null, "每张次数"), h("strong", null, evaluation.count.valid ? String(evaluation.count.value) : "—")),
      h("div", { class: "gm-stat" }, h("span", null, "计划 DRAW"), h("strong", null, evaluation.totalPlanned === null ? "—" : String(evaluation.totalPlanned))),
      h("div", { class: "gm-stat" }, h("span", null, "后端上限"), h("strong", null, planLimitText)),
      h("div", { class: "gm-stat" }, h("span", null, "保护模式"), h("strong", null, draft.mode === "snapshot" ? "快照" : "引用")),
    );
    if (!evaluation.count.valid) {
      planError.appendChild(h("p", { class: "gm-inline-error", role: "alert" }, evaluation.count.error));
    } else if (evaluation.planExceeded) {
      planError.appendChild(h("p", { class: "gm-inline-error", role: "alert" }, `计划 DRAW ${evaluation.totalPlanned} 超过后端上限 ${evaluation.planLimit}。`));
    } else if (evaluation.planLimitUnknown) {
      planError.appendChild(h("p", { class: "gm-inline-error", role: "alert" }, "尚未读取后端计划上限，不能创建任务。"));
    }
    if (draft.mode === "reference") {
      planStats.appendChild(h("p", { class: "gm-help" }, "引用模式创建后每次执行前都会重新校验原图哈希；后端路径必须可访问。"));
    }
  }

  // ------------------------------------------------------------ preflight
  function invalidatePreflight(reason) {
    preflightToken += 1;
    preflightController?.abort();
    preflightController = null;
    const hadResult = ["ok", "error", "running", "stale"].includes(draft.preflight.state);
    draft.preflight = {
      state: hadResult ? "stale" : "idle",
      result: null,
      workflowRevision: -1,
      bindingsRevision: -1,
      error: null,
      reason,
    };
    renderPreflight();
    updateStepper();
    renderReview();
  }

  function renderPreflight() {
    clear(preflightArea);
    const state = draft.preflight.state;
    const result = draft.preflight.result;
    const statusRow = h("div", { class: `gm-preflight-status ${state === "ok" ? "ok" : state === "error" ? "bad" : state === "stale" ? "warn" : ""}` });
    if (state === "idle") {
      statusRow.append(
        h("div", null, h("strong", null, "尚未预检"), h("p", { class: "gm-help" }, "预检只验证工作流、绑定、上游关系、受控种子和安装节点；原图就绪、次数与计划条件另行检查。")),
        button("运行预检", () => runPreflight(), { kind: "primary", disabled: !draft.workflow.api || isFrozen() }),
      );
      preflightArea.appendChild(statusRow);
      return;
    }
    if (state === "running") {
      statusRow.appendChild(h("div", null, h("strong", null, "正在预检…"), spinner("验证工作流与绑定")));
      preflightArea.appendChild(statusRow);
      return;
    }
    if (state === "stale") {
      statusRow.append(
        h("div", null, h("strong", null, "预检结果已失效"), h("p", { class: "gm-help" }, draft.preflight.reason || "工作流或绑定已变化，需要重新运行预检。")),
        button("重新预检", () => runPreflight(), { kind: "primary", disabled: isFrozen() }),
      );
      preflightArea.appendChild(statusRow);
      return;
    }
    statusRow.append(
      h("div", null,
        h("strong", null, result?.ok ? "预检通过" : "预检未通过"),
        h("span", { class: "gm-help" }, result?.workflow_hash ? ` 工作流哈希 ${String(result.workflow_hash).slice(0, 12)}` : ""),
      ),
      button("重新预检", () => runPreflight(), { kind: "muted", disabled: isFrozen() }),
    );
    preflightArea.appendChild(statusRow);
    if (result?.errors?.length) {
      preflightArea.appendChild(h("div", { class: "gm-preflight-errors" },
        ...result.errors.map((error) => h("div", { class: "gm-preflight-row bad" },
          h("strong", null, error.code), h("span", null, error.message),
          error.details ? h("code", null, JSON.stringify(error.details).slice(0, 800)) : null,
        )),
      ));
    }
    if (result?.warnings?.length) {
      preflightArea.appendChild(h("div", { class: "gm-preflight-warnings" },
        ...result.warnings.map((warning) => h("div", { class: "gm-preflight-row warn" },
          h("strong", null, warning.code), h("span", null, warning.message),
        )),
      ));
    }
    if (result?.seed_bindings?.length) {
      preflightArea.appendChild(h("div", { class: "gm-seed-list" },
        h("h4", null, "受控种子绑定"),
        ...result.seed_bindings.map((seed) => h("div", { class: "gm-seed-row" },
          h("span", { class: "gm-mono" }, `#${seed.node_id}.${seed.input_name}`),
          h("span", null, `${seed.min} … ${seed.max}`),
          seed.active === false ? badge("未启用", "muted") : badge("受控", "ok"),
        )),
      ));
    }
  }

  async function runPreflight() {
    if (!draft.workflow.api || isFrozen()) return;
    preflightController?.abort();
    preflightController = new AbortController();
    const token = ++preflightToken;
    const workflowRevision = draft.workflowRevision;
    const bindingsRevision = draft.bindingsRevision;
    draft.preflight = { state: "running", result: null, workflowRevision, bindingsRevision, error: null };
    renderPreflight();
    updateStepper();
    renderReview();
    let result;
    let failure = null;
    try {
      result = await ctx.api.preflight({
        workflow_ui: draft.workflow.ui,
        workflow_api: draft.workflow.api,
        bindings: { ...draft.workflow.bindings },
      }, { signal: preflightController.signal });
    } catch (error) {
      if (error?.name === "AbortError" || destroyed) return;
      failure = error;
    }
    if (destroyed || token !== preflightToken) return;
    if (workflowRevision !== draft.workflowRevision || bindingsRevision !== draft.bindingsRevision) {
      draft.preflight = { state: "stale", result: null, workflowRevision: -1, bindingsRevision: -1, error: null, reason: "工作流或绑定在预检期间变化" };
      renderPreflight();
      updateStepper();
      renderReview();
      return;
    }
    if (failure) {
      result = { ok: false, errors: [{ code: failure.code || "ERROR", message: failure.message || String(failure) }], warnings: [] };
    }
    draft.preflight = {
      state: result?.ok ? "ok" : "error",
      result,
      workflowRevision,
      bindingsRevision,
      error: failure,
    };
    renderPreflight();
    renderReview();
    updateStepper();
  }

  // -------------------------------------------------------------- review
  function renderReview() {
    clear(reviewSummary);
    clear(createStatus);
    const evaluation = evaluateDraft(draft, ctx.store.get().capabilities);
    const activeSeeds = (draft.preflight.result?.seed_bindings || []).filter((seed) => seed.active !== false).length;
    const sourceNames = draft.sources.slice(0, 6).map((source, index) => `#${index + 1} ${source.name}`).join("；");
    reviewSummary.append(frag(
      h("div", { class: "gm-plan-summary" },
        h("div", { class: "gm-stat" }, h("span", null, "任务名称"), h("strong", null, draft.name.trim() || "未命名")),
        h("div", { class: "gm-stat" }, h("span", null, "原图"), h("strong", null, String(evaluation.sources.total))),
        h("div", { class: "gm-stat" }, h("span", null, "每张次数"), h("strong", null, evaluation.count.valid ? String(evaluation.count.value) : "—")),
        h("div", { class: "gm-stat" }, h("span", null, "计划 DRAW"), h("strong", null, evaluation.totalPlanned === null ? "—" : String(evaluation.totalPlanned))),
        h("div", { class: "gm-stat" }, h("span", null, "保护模式"), h("strong", null, draft.mode === "snapshot" ? "快照" : "引用")),
        h("div", { class: "gm-stat" }, h("span", null, "种子映射"), h("strong", null, String(activeSeeds))),
      ),
      h("dl", { class: "gm-kv" },
        h("dt", null, "LoadImage"), h("dd", { class: "gm-mono" }, draft.workflow.bindings.load_image_node_id ? `#${draft.workflow.bindings.load_image_node_id}` : "未绑定"),
        h("dt", null, "SaveImage"), h("dd", { class: "gm-mono" }, draft.workflow.bindings.save_image_node_id ? `#${draft.workflow.bindings.save_image_node_id}` : "未绑定"),
        h("dt", null, "工作流捕获"), h("dd", null, draft.workflow.capturedAt ? fmtTime(new Date(draft.workflow.capturedAt).toISOString()) : "未捕获"),
        h("dt", null, "原图顺序"), h("dd", null, sourceNames ? `${sourceNames}${draft.sources.length > 6 ? `；…等 ${draft.sources.length} 张` : ""}` : "尚无原图"),
      ),
    ));
    if (!draft.sources.length) reviewSummary.appendChild(h("p", { class: "gm-help" }, "返回“原图”步骤添加图片。"));
    if (evaluation.blockers.length) {
      const list = h("ul", { class: "gm-blocker-list" });
      for (const blocker of evaluation.blockers) list.appendChild(h("li", null, blocker));
      reviewSummary.appendChild(list);
    }
    renderCreateState();
  }

  function renderCreateState() {
    clear(createStatus);
    const state = draft.create.state;
    if (state === "submitting") {
      createStatus.appendChild(h("div", { class: "gm-notice" }, h("strong", null, "正在创建任务"), spinner("冻结工作流、保存原图快照并生成计划…")));
      return;
    }
    if (state === "uncertain") {
      createStatus.appendChild(h("div", { class: "gm-notice warn" },
        h("strong", null, "创建请求结果未知"),
        h("p", null, "网络响应丢失，任务可能已经创建。请重试同一请求（相同载荷与幂等键，服务器只会产生一个任务），或先到任务列表核实。"),
        draft.create.error ? h("p", { class: "gm-inline-error" }, draft.create.error.message || String(draft.create.error)) : null,
        h("div", { class: "gm-inline-actions" },
          button("重试同一请求", () => submitCreate({ retry: true }), { kind: "primary" }),
          button("去任务列表核实", () => verifyThenLeave(), { kind: "muted" }),
        ),
      ));
      return;
    }
    if (state === "verify") {
      createStatus.appendChild(h("div", { class: "gm-notice warn" },
        h("strong", null, "请先核实上次创建结果"),
        h("p", null, "后端已记录该幂等请求失败，不能安全地重复提交。请到任务列表确认是否已产生任务，再决定是否新建。"),
        draft.create.error ? h("p", { class: "gm-inline-error" }, draft.create.error.message || String(draft.create.error)) : null,
        h("div", { class: "gm-inline-actions" }, button("去任务列表核实", () => verifyThenLeave(), { kind: "primary" })),
      ));
      return;
    }
    if (state === "error") {
      createStatus.appendChild(errorBox(draft.create.error || { code: "CREATE_FAILED", message: "创建任务失败。" }));
      createStatus.appendChild(h("p", { class: "gm-help" }, "修正后可以重新创建；本次未确认产生任务。"));
      return;
    }
    createStatus.appendChild(h("p", { class: "gm-help" }, "创建后进入任务详情；需要手动点击“开始 / 继续”，不会自动调用 start/resume。"));
  }

  async function submitCreate({ retry = false } = {}) {
    if (destroyed || draft.create.state === "submitting") return;
    let body;
    let key;
    if (retry && draft.create.state === "uncertain" && draft.create.body && draft.create.key) {
      body = draft.create.body;
      key = draft.create.key;
    } else {
      const evaluation = evaluateDraft(draft, ctx.store.get().capabilities);
      if (!evaluation.canCreate) {
        ctx.notify(evaluation.blockers[0] || "创建条件尚未满足。", "warn");
        dashFocus();
        return;
      }
      body = buildCreateBody({ ...draft, count: evaluation.count.value });
      key = idempotencyKey();
      draft.create = { state: "submitting", key, body, fingerprint: fingerprintBody(body), error: null };
    }
    draft.busy = true;
    draft.create = { ...draft.create, state: "submitting", error: null };
    createController?.abort();
    createController = new AbortController();
    renderCreateState();
    updateStepper();
    ctx.notify("正在创建任务…", "info", { id: "create-job", sticky: true });
    try {
      const job = await ctx.api.createJob(body, key, { signal: createController.signal });
      if (destroyed) return;
      draft.create = { state: "done", key, body, fingerprint: fingerprintBody(body), error: null, job };
      ctx.dismissNotice?.("create-job");
      ctx.setLeaveGuard && ctx.clearLeaveGuard?.();
      window.removeEventListener("beforeunload", beforeUnload);
      for (const url of objectUrls) URL.revokeObjectURL(url);
      objectUrls.clear();
      const planned = job.creation?.draws_total ?? job.summary?.total_planned ?? 0;
      if (job.status === "CREATING") {
        ctx.notify(`任务 ${job.display_id} 已登记，正在生成计划（${planned} DRAW）；完成后仍需显式开始推理。`, "ok");
      } else {
        ctx.notify(`任务 ${job.display_id} 已创建（${planned} DRAW）。`, "ok");
      }
      ctx.navigate("detail", { jobId: job.id });
    } catch (error) {
      if (error?.name === "AbortError" || destroyed) {
        ctx.dismissNotice?.("create-job");
        return;
      }
      ctx.dismissNotice?.("create-job");
      if (error?.code === "IDEMPOTENCY_CONFLICT") {
        draft.create = { ...draft.create, state: "verify", error };
      } else if (isUncertainError(error)) {
        draft.create = { ...draft.create, state: "uncertain", error };
      } else {
        draft.create = { state: "error", key: null, body: null, fingerprint: "", error };
      }
      ctx.notify(
        draft.create.state === "error" ? `创建任务失败：${error?.message || error}` : "创建请求响应不确定，请重试同一请求或核实任务列表。",
        draft.create.state === "error" ? "error" : "warn",
      );
    } finally {
      draft.busy = false;
      if (!destroyed) {
        renderCreateState();
        updateStepper();
        renderPlan();
      }
    }
  }

  function verifyThenLeave() {
    ctx.clearLeaveGuard?.();
    window.removeEventListener("beforeunload", beforeUnload);
    ctx.navigate("jobs");
  }

  // --------------------------------------------------------------- state
  function isFrozen() {
    return draft.create.state === "submitting" || draft.create.state === "uncertain" || draft.create.state === "verify";
  }

  function hasDraftContent() {
    return Boolean(draft.workflow.api || draft.sources.length || draft.name.trim()
      || String(draft.countRaw).trim() !== "5" || draft.create.state === "submitting");
  }

  async function leaveGuard() {
    if (draft.create.state === "submitting") {
      ctx.notify("创建请求正在提交，请等待结果后再离开。", "warn");
      return false;
    }
    if (draft.create.state === "uncertain" || draft.create.state === "verify") {
      return confirmDialog("上一次创建请求结果尚未确认；离开会丢弃当前草稿。请先在任务列表核实是否已产生任务。仍要离开？", {
        title: "创建结果未确认",
        confirmLabel: "仍然离开",
        danger: true,
      });
    }
    if (!hasDraftContent()) return true;
    return confirmDialog("离开会丢弃当前草稿（工作流捕获、原图清单与参数），已上传的临时文件不会回滚到服务器。继续离开？", {
      title: "丢弃草稿？",
      confirmLabel: "丢弃并离开",
      danger: true,
    });
  }

  function updateStepper() {
    const evaluation = evaluateDraft(draft, ctx.store.get().capabilities);
    draft.count = evaluation.count.value;
    stepper.update({
      currentStep: draft.step,
      disabled: evaluation.frozen,
      states: computeStepStates(CREATE_STEPS, draft.step, {
        completed: evaluation.completed,
        invalid: evaluation.invalid,
        maxUnlocked: evaluation.maxUnlocked,
      }),
    });
    for (const [index, pane] of [workflowPane, sourcesPane, paramsPane, reviewPane].entries()) {
      pane.hidden = index !== draft.step;
    }
    backBtn.disabled = draft.step === 0 || evaluation.frozen;
    nextBtn.hidden = draft.step === 3;
    createBtn.hidden = draft.step !== 3;
    nextBtn.textContent = draft.step === 0 ? "下一步：原图" : draft.step === 1 ? "下一步：参数" : "下一步：检查创建";
    nextBtn.disabled = evaluation.frozen;
    createBtn.disabled = !evaluation.canCreate;
    createBtn.textContent = draft.create.state === "submitting" ? "正在创建…" : "创建任务";
    modeSelect.disabled = evaluation.frozen;
    nameInput.disabled = evaluation.frozen;
    countInput.disabled = evaluation.frozen;
    if (folderImport?._scanButton) folderImport._scanButton.disabled = evaluation.frozen;
    renderFooterStatus(evaluation);
  }

  function renderFooterStatus(evaluation) {
    const step = draft.step;
    let message = "";
    let kind = "";
    if (draft.create.state === "submitting") message = "正在提交创建请求；请勿关闭或切换页面。";
    else if (draft.create.state === "uncertain") message = "创建结果未知：请重试同一请求或核实任务列表。";
    else if (step === 0) message = evaluation.workflowValid ? "工作流与绑定已就绪。" : "捕获工作流并选择 LoadImage / 最终 SaveImage 后继续。";
    else if (step === 1) {
      if (evaluation.conflict) message = "引用模式与浏览器上传冲突，需要先解决。";
      else if (evaluation.sources.error) message = `有 ${evaluation.sources.error} 张图片上传失败；请重试或移除。`;
      else if (evaluation.sources.uploading || evaluation.sources.queued) message = "仍有图片在上传或排队；完成后才能继续。";
      else if (!evaluation.sources.total) message = "至少添加一张原图。";
      else message = `已就绪 ${evaluation.sources.ready} 张原图；顺序即执行顺序。`;
    } else if (step === 2) {
      if (!evaluation.count.valid) { message = evaluation.count.error; kind = "bad"; }
      else if (evaluation.planExceeded) { message = `计划 DRAW 超过后端上限 ${evaluation.planLimit}。`; kind = "bad"; }
      else message = `计划 ${evaluation.sources.total} × ${evaluation.count.value} = ${evaluation.totalPlanned} DRAW。`;
    } else if (step === 3) {
      if (evaluation.canCreate) message = "全部创建条件已满足；创建后不会自动开始任务。";
      else message = evaluation.blockers[0] || "请完成检查后再创建。";
      if (!evaluation.canCreate) kind = "bad";
    }
    stepStatus.textContent = message;
    stepStatus.className = `gm-wizard-status ${kind}`;
  }

  function nextStep() {
    const evaluation = evaluateDraft(draft, ctx.store.get().capabilities);
    if (draft.step === 0 && !evaluation.workflowValid) {
      ctx.notify("请先捕获工作流并完成 LoadImage / SaveImage 绑定。", "warn");
      return;
    }
    if (draft.step === 1 && !evaluation.sourcesComplete) {
      ctx.notify(evaluation.blockers[0] || "原图尚未全部就绪。", "warn");
      return;
    }
    if (draft.step === 2 && !evaluation.paramsComplete) {
      ctx.notify(evaluation.blockers[0] || "参数尚不合法。", "warn");
      return;
    }
    goStep(Math.min(3, draft.step + 1));
  }

  function goStep(index) {
    if (isFrozen()) return;
    if (index < 0 || index > 3 || index === draft.step) return;
    const evaluation = evaluateDraft(draft, ctx.store.get().capabilities);
    if (index > evaluation.maxUnlocked) {
      ctx.notify("完成前面步骤后才能进入该步骤。", "warn");
      return;
    }
    draft.step = index;
    updateStepper();
    if (index === 3 && draft.workflow.api && !evaluation.preflightCurrent
      && ["idle", "stale"].includes(draft.preflight.state)) {
      runPreflight();
    }
  }

  function dashFocus() {
    createBtn.focus?.();
  }

  // -------------------------------------------------------------- startup
  stepper.update({ currentStep: 0, states: computeStepStates(CREATE_STEPS, 0, { completed: [], invalid: [], maxUnlocked: 0 }) });
  renderWorkflow();
  renderSources();
  renderConflict();
  renderRejected();
  renderPlan();
  renderPreflight();
  renderReview();
  updateStepper();

  return {
    destroy() {
      destroyed = true;
      window.removeEventListener("beforeunload", beforeUnload);
      ctx.clearLeaveGuard?.();
      for (const controller of uploadControllers) controller.abort();
      uploadControllers.clear();
      preflightController?.abort();
      scanController?.abort();
      createController?.abort();
      for (const url of objectUrls) URL.revokeObjectURL(url);
      objectUrls.clear();
      uploadQueue.length = 0;
      dropzone.destroy();
      stepper.destroy();
    },
    refresh() {
      renderReview();
      updateStepper();
    },
    isDirty: hasDraftContent,
  };
}
