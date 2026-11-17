// API client for the Gacha Master backend. All URLs are relative to the
// ComfyUI origin; api.fetchApi is used when available so base paths/proxies
// work exactly like the rest of the frontend.

export const API_ROOT = "/gacha_master/v1";

export class ApiError extends Error {
  constructor(payload, status) {
    const error = payload?.error || {};
    super(error.message || `HTTP ${status}`);
    this.name = "ApiError";
    this.code = error.code || "HTTP_ERROR";
    this.status = status;
    this.traceId = error.trace_id || "";
    this.details = error.details || {};
    this.retryable = Boolean(error.retryable);
  }
}

export function idempotencyKey() {
  if (globalThis.crypto?.randomUUID) return crypto.randomUUID();
  return `gacha-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export class ApiClient {
  constructor(comfyApi) {
    this.comfyApi = comfyApi || null;
  }

  url(path) {
    if (/^https?:\/\//i.test(path)) return path;
    if (this.comfyApi?.apiURL) return this.comfyApi.apiURL(API_ROOT + path);
    return API_ROOT + path;
  }

  async request(path, { method = "GET", body, headers = {}, raw = false, signal } = {}) {
    const init = { method, headers: { ...headers }, signal };
    if (body !== undefined) {
      if (body instanceof FormData) {
        init.body = body;
      } else {
        init.headers["Content-Type"] = "application/json";
        init.body = JSON.stringify(body);
      }
    }
    let response;
    try {
      response = this.comfyApi?.fetchApi
        ? await this.comfyApi.fetchApi(API_ROOT + path, init)
        : await fetch(API_ROOT + path, init);
    } catch (error) {
      if (error?.name === "AbortError") throw error;
      throw new ApiError({ error: { code: "NETWORK_ERROR", message: "无法连接抽卡大师后端。" } }, 0);
    }
    if (raw) return response;
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    if (!response.ok) {
      throw new ApiError(payload, response.status);
    }
    return payload;
  }

  async get(path, params) {
    const query = params ? "?" + new URLSearchParams(params).toString() : "";
    return (await this.request(path + query)).data;
  }

  async post(path, body, { key, signal } = {}) {
    const headers = key ? { "Idempotency-Key": key } : {};
    return (await this.request(path, { method: "POST", body: body ?? {}, headers, signal })).data;
  }

  async put(path, body, { signal } = {}) {
    return (await this.request(path, { method: "PUT", body, signal })).data;
  }

  async upload(file, { signal } = {}) {
    const form = new FormData();
    form.append("image", file, file.name || "image");
    const payload = await this.request("/uploads", { method: "POST", body: form, signal });
    const items = payload.data || [];
    if (!items.length) throw new ApiError({ error: { code: "UPLOAD_FAILED", message: "上传未被接受。" } }, 200);
    return items[0];
  }

  imageUrl(path) {
    return this.url(path);
  }

  capabilities() { return this.get("/capabilities"); }
  health() { return this.get("/health"); }
  settings() { return this.get("/settings"); }
  updateSettings(body) { return this.put("/settings", body); }
  scanInputs(root, subdir = "", { signal } = {}) {
    return this.post("/inputs/scan", { root, subdir }, { signal });
  }
  preflight(body, { signal } = {}) { return this.post("/preflight", body, { signal }); }
  createJob(body, key, { signal } = {}) { return this.post("/jobs", body, { key, signal }); }
  listJobs(cursor, { signal } = {}) {
    return this.request("/jobs" + (cursor ? "?" + new URLSearchParams({ cursor }).toString() : ""), { signal })
      .then((payload) => payload.data);
  }
  job(jobId, { signal } = {}) { return this.request(`/jobs/${jobId}`, { signal }).then((payload) => payload.data); }
  startJob(jobId, body, key) { return this.post(`/jobs/${jobId}/start`, body, { key }); }
  pauseJob(jobId, mode) { return this.post(`/jobs/${jobId}/pause`, { mode }); }
  abandonCreation(jobId, reason) { return this.post(`/jobs/${jobId}/abandon-creation`, { reason }); }
  recovery(jobId, { signal } = {}) {
    return this.request(`/jobs/${jobId}/recovery`, { signal }).then((payload) => payload.data);
  }
  sources(jobId, afterOrder = 0, limit = 200, { signal } = {}) {
    return this.request(`/jobs/${jobId}/sources?` + new URLSearchParams({ after_order: afterOrder, limit }).toString(), { signal })
      .then((payload) => payload.data);
  }
  outputs(sourceId, { cursor, includeUnhealthy = false, limit = 50, direction = "forward", offset, signal } = {}) {
    const params = { limit, include_unhealthy: includeUnhealthy ? "1" : "0" };
    if (offset !== undefined && offset !== null) params.offset = String(offset);
    else if (cursor) params.cursor = JSON.stringify(cursor);
    if (direction && direction !== "forward") params.direction = direction;
    return this.request(`/sources/${sourceId}/outputs?` + new URLSearchParams(params).toString(), { signal })
      .then((payload) => payload.data);
  }
  reviewSession(jobId, { signal } = {}) {
    return this.request(`/jobs/${jobId}/review-session`, { signal }).then((payload) => payload.data);
  }
  openReviewSession(jobId, body = {}, { signal } = {}) {
    return this.post(`/jobs/${jobId}/review-session`, body, { signal });
  }
  reviewSources(jobId, { scope = "pending", afterOrder, beforeOrder, limit = 40, signal } = {}) {
    const params = { scope, limit };
    if (afterOrder !== undefined && afterOrder !== null) params.after_order = String(afterOrder);
    if (beforeOrder !== undefined && beforeOrder !== null) params.before_order = String(beforeOrder);
    return this.get(`/jobs/${jobId}/review-sources`, params);
  }
  reviewSourceState(sourceId, { signal } = {}) {
    return this.request(`/sources/${sourceId}/review-state`, { signal }).then((payload) => payload.data);
  }
  saveReviewPosition(sessionId, body, { signal } = {}) {
    return this.request(`/review-sessions/${sessionId}/position`, {
      method: "PATCH", body, signal,
    }).then((payload) => payload.data);
  }
  reviewDecision(sessionId, body, key, { signal } = {}) {
    return this.post(`/review-sessions/${sessionId}/decisions`, body, { key, signal });
  }
  reviewBack(sessionId, body, { signal } = {}) {
    return this.post(`/review-sessions/${sessionId}/back`, body, { signal });
  }
  outputStates(sourceId, outputIds, { signal } = {}) {
    return this.post(`/sources/${sourceId}/outputs/state`, { output_ids: outputIds }, { signal });
  }
  selection(outputId, selected, expectedVersion) {
    const body = { selected };
    if (expectedVersion !== undefined && expectedVersion !== null) body.expected_version = expectedVersion;
    return this.put(`/outputs/${outputId}/selection`, body);
  }
  viewed(outputId, { signal } = {}) { return this.post(`/outputs/${outputId}/viewed`, {}, { signal }); }
  append(sourceId, body, key) { return this.post(`/sources/${sourceId}/append`, body, { key }); }
  removeSource(sourceId, expectedVersion) {
    return this.post(`/sources/${sourceId}/remove`, { expected_version: expectedVersion });
  }
  relocateSource(sourceId, path) { return this.post(`/sources/${sourceId}/relocate`, { path }); }
  reviewSource(sourceId, body) { return this.post(`/sources/${sourceId}/review`, body); }
  recoveryReport(jobId) { return this.get(`/jobs/${jobId}/recovery`); }
  reconcile(attemptId) { return this.post(`/attempts/${attemptId}/reconcile`, {}); }
  resolve(attemptId, body, key) { return this.post(`/attempts/${attemptId}/resolve`, body, { key }); }
  exportPreview(jobId, body, key) { return this.post(`/jobs/${jobId}/exports/preview`, body, { key }); }
  exportReview(draftId, sourceId, body) {
    return this.put(`/export-drafts/${draftId}/reviews/${sourceId}`, body);
  }
  exportConfirm(draftId, body, key) {
    return this.post(`/export-drafts/${draftId}/confirm`, body, { key });
  }
  exportDetail(exportId) { return this.get(`/exports/${exportId}`); }
  exportResume(exportId, key) { return this.post(`/exports/${exportId}/resume`, {}, { key }); }
  exportsList(jobId) { return this.get(`/jobs/${jobId}/exports`); }
  events(after, jobId) {
    const params = { after };
    if (jobId) params.job_id = jobId;
    return this.get("/events", params);
  }
  backups() { return this.get("/backups"); }
  createBackup() { return this.post("/backups", {}); }
  restoreBackup(id) { return this.post(`/backups/${id}/restore`, {}); }
  storage() { return this.get("/storage"); }
  clearCache() { return this.post("/cache/clear", {}); }
  cleanupPreview(jobId, key) { return this.post(`/jobs/${jobId}/cleanup/preview`, {}, { key }); }
  cleanupDetail(cleanupId, { signal } = {}) {
    return this.request(`/storage-cleanups/${cleanupId}`, { signal }).then((payload) => payload.data);
  }
  cleanupItems(cleanupId, { limit = 50, cursor, signal } = {}) {
    const params = { limit };
    if (cursor) params.cursor = cursor;
    return this.get(`/storage-cleanups/${cleanupId}/items`, params);
  }
  cleanupConfirm(cleanupId, body, key) {
    return this.post(`/storage-cleanups/${cleanupId}/confirm`, body, { key });
  }
  jobCleanups(jobId) { return this.get("/jobs/" + jobId + "/cleanups", { limit: 20 }); }
}
