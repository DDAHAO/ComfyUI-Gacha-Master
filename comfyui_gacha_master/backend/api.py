"""aiohttp routes for the ComfyUI task center.

The handlers are thin: validation, idempotency, execution off the event loop
and error mapping.  Business state changes stay in the service/scheduler.
Registering these on ComfyUI's ``PromptServer.instance.routes`` is done by the
integration module.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import traceback
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlsplit

from aiohttp import web

from . import storage
from .errors import GachaError
from .states import ExportItemStatus, OutputHealth, ReviewDecision
from .util import (
    canonical_json,
    ensure_dir,
    path_is_within,
    require_within,
    sha256_text,
    utc_now,
)

LOGGER = logging.getLogger("gacha.api")


def _json_error(exc: GachaError) -> web.Response:
    return web.json_response(exc.to_dict(), status=exc.http_status)


def _check_origin(request: web.Request) -> None:
    origin = request.headers.get("Origin")
    if not origin:
        return
    try:
        parsed = urlsplit(origin)
    except Exception:
        raise GachaError("BAD_REQUEST", "Origin 无效。", http_status=403)
    if not parsed.netloc or parsed.netloc.lower() != request.host.lower():
        raise GachaError("BAD_REQUEST", "跨站请求被拒绝。", http_status=403)



async def _read_json(request: web.Request, *, max_bytes: int = 32 * 1024 * 1024) -> Any:
    """Read a JSON request without the host app's 1 MiB default limit.

    A 5000-source plan request is legitimate and larger than aiohttp's
    Application default.  Only this plugin's metadata endpoints use this
    helper; multipart uploads keep their own size limit.
    """
    total = 0
    chunks: list[bytes] = []
    async for chunk in request.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > max_bytes:
            raise GachaError(
                "PAYLOAD_TOO_LARGE",
                "请求体超过插件允许大小。",
                details={"limit": max_bytes},
            )
        chunks.append(chunk)
    if not chunks:
        return {}
    try:
        return json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GachaError("BAD_REQUEST", "请求不是有效 JSON。", details={"error": str(exc)})


def _request_hash(body: Any) -> str:
    return sha256_text(canonical_json(body))


class Api:
    ROUTE_SPECS = [
        ("get", "/capabilities", "capabilities"),
        ("get", "/health", "health"),
        ("get", "/settings", "get_settings"),
        ("put", "/settings", "put_settings"),
        ("post", "/uploads", "upload"),
        ("get", "/uploads/{upload_id}/image", "upload_image"),
        ("post", "/inputs/scan", "scan_inputs"),
        ("post", "/preflight", "preflight"),
        ("post", "/jobs", "create_job"),
        ("get", "/jobs", "list_jobs"),
        ("get", "/jobs/{job_id}", "job_detail"),
        ("post", "/jobs/{job_id}/start", "start_job"),
        ("post", "/jobs/{job_id}/pause", "pause_job"),
        ("post", "/jobs/{job_id}/abandon-creation", "abandon_creation"),
        ("post", "/jobs/{job_id}/resume", "start_job"),
        ("get", "/jobs/{job_id}/recovery", "job_recovery"),
        ("get", "/jobs/{job_id}/sources", "list_sources"),
        ("get", "/jobs/{job_id}/review-session", "review_session"),
        ("post", "/jobs/{job_id}/review-session", "open_review_session"),
        ("get", "/jobs/{job_id}/review-sources", "review_sources"),
        ("get", "/sources/{source_id}/review-state", "review_state"),
        ("patch", "/review-sessions/{session_id}/position", "review_position"),
        ("post", "/review-sessions/{session_id}/decisions", "review_decision"),
        ("post", "/review-sessions/{session_id}/back", "review_back"),
        ("post", "/jobs/{job_id}/exports/preview", "export_preview"),
        ("get", "/jobs/{job_id}/exports", "export_list"),
        ("post", "/attempts/{attempt_id}/reconcile", "reconcile_attempt"),
        ("post", "/attempts/{attempt_id}/resolve", "resolve_attempt"),
        ("post", "/sources/{source_id}/remove", "remove_source"),
        ("post", "/sources/{source_id}/relocate", "relocate_source"),
        ("post", "/sources/{source_id}/append", "append_source"),
        ("post", "/sources/{source_id}/review", "review_source"),
        ("post", "/sources/{source_id}/outputs/state", "output_states"),
        ("get", "/sources/{source_id}/outputs", "list_outputs"),
        ("get", "/sources/{source_id}/image", "source_image"),
        ("get", "/sources/{source_id}/thumbnail", "source_thumbnail"),
        ("get", "/outputs/{output_id}/image", "output_image"),
        ("get", "/outputs/{output_id}/thumbnail", "output_thumbnail"),
        ("put", "/outputs/{output_id}/selection", "selection"),
        ("post", "/outputs/{output_id}/viewed", "viewed"),
        ("put", "/export-drafts/{draft_id}/reviews/{source_id}", "export_review"),
        ("post", "/export-drafts/{draft_id}/confirm", "export_confirm"),
        ("get", "/exports/{export_id}", "export_detail"),
        ("post", "/exports/{export_id}/resume", "export_resume"),
        ("get", "/events", "events"),
        ("get", "/backups", "list_backups"),
        ("post", "/backups", "create_backup"),
        ("post", "/backups/{backup_id}/restore", "restore_backup"),
        ("get", "/storage", "storage"),
        ("post", "/cache/clear", "clear_cache"),
        ("post", "/jobs/{job_id}/cleanup/preview", "cleanup_preview"),
        ("get", "/jobs/{job_id}/cleanups", "job_cleanups"),
        ("get", "/storage-cleanups/{cleanup_id}", "cleanup_detail"),
        ("get", "/storage-cleanups/{cleanup_id}/items", "cleanup_items"),
        ("post", "/storage-cleanups/{cleanup_id}/confirm", "cleanup_confirm"),
    ]

    def __init__(
        self,
        *,
        service: Any,
        repo: Any,
        exports: Any,
        recovery: Any,
        engine_provider: Callable[[], Awaitable[Any]],
        cleanup: Any = None,
        prefix: str = "/gacha_master/v1",
        copy_executor: Any = None,
    ) -> None:
        self.service = service
        self.repo = repo
        self.exports = exports
        self.recovery = recovery
        self.cleanup = cleanup
        self.engine_provider = engine_provider
        self.prefix = prefix.rstrip("/")
        self.copy_executor = copy_executor
        self.handlers: dict[str, Any] = {}
        for method, path, name in self.ROUTE_SPECS:
            self.handlers[name] = self.route(path, method.upper())(getattr(self, name))

    # ---------------------------------------------------------- plumbing
    async def _run_sync(self, fn, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    def route(self, path: str, method: str = "GET"):
        full = self.prefix + path

        def decorator(fn):
            async def handler(request: web.Request) -> web.Response:
                trace_id = request.headers.get("X-Request-Id") or ""
                try:
                    _check_origin(request)
                    result = await fn(request)
                    if isinstance(result, web.StreamResponse):
                        return result
                    status = 200
                    if isinstance(result, tuple):
                        result, status = result
                    return web.json_response(
                        {
                            "schema_version": 1,
                            "data": result,
                            "event_seq": self.repo.last_event_seq(),
                        },
                        status=status,
                    )
                except GachaError as exc:
                    exc.with_trace(trace_id)
                    LOGGER.warning("api %s %s -> %s: %s", method, full, exc.code, exc.message)
                    return _json_error(exc)
                except Exception as exc:
                    LOGGER.error("api %s %s internal error: %s\n%s", method, full, exc, traceback.format_exc())
                    return _json_error(
                        GachaError(
                            "INTERNAL_ERROR",
                            "服务器内部错误。",
                            details={"error": f"{type(exc).__name__}: {exc}"},
                        )
                    )

            handler.__name__ = f"gacha_{method.lower()}_{abs(hash(full))}"
            return handler

        return decorator

    def register(self, routes) -> None:
        p = self.prefix
        for method, path, name in self.ROUTE_SPECS:
            getattr(routes, method)(p + path)(self.handlers[name])

    async def reconcile_attempt(self, request: web.Request):
        body = await _read_json(request) if request.can_read_body else {}
        engine = await self.engine_provider()
        return await self.recovery.reconcile(request.match_info["attempt_id"], engine)

    async def resolve_attempt(self, request: web.Request):
        body = await _read_json(request)
        if not isinstance(body, dict):
            raise GachaError("BAD_REQUEST", "请求必须是 JSON 对象。")
        engine = await self.engine_provider()
        return await self.recovery.resolve(
            request.match_info["attempt_id"],
            engine,
            decision=str(body.get("decision") or ""),
            reason=body.get("reason"),
            confirm_inactive=bool(body.get("confirm_inactive")),
        )

    # ---------------------------------------------------------- handlers
    async def capabilities(self, request: web.Request):
        engine = await self.engine_provider()
        try:
            caps = await engine.capabilities()
        except Exception as exc:
            caps = {
                "schema_version": 1,
                "comfy_version": None,
                "frontend_version": None,
                "client_supplied_prompt_id": False,
                "targeted_cancel": "unverified",
                "queue_lookup": False,
                "history_lookup": False,
                "execution_events": False,
                "load_image_binding": "unverified",
                "save_image_binding": "unverified",
                "filesystem_no_replace_publish": False,
                "warnings": [f"adapter unavailable: {type(exc).__name__}: {exc}"],
            }
        publish_ok = self.repo.get_runtime_meta("filesystem_no_replace_publish") == "true"
        caps.update(
            {
                "schema_version": 1,
                "plugin_version": "1.0.0",
                "api_prefix": self.prefix,
                "filesystem_no_replace_publish": bool(
                    caps.get("filesystem_no_replace_publish", publish_ok) or publish_ok
                ),
                "load_image_binding": caps.get("load_image_binding", "unverified"),
                "load_image_binding_mode": "native_staged",
                "async_creation": True,
                "creation_progress": True,
                "output_state_api": True,
                "output_backward_pagination": True,
                "output_offset_pagination": True,
                "resumable_review_sessions": True,
                "review_decision_transaction": True,
                "warnings": list(caps.get("warnings") or []),
                "limits": {
                    "max_inflight": 1,
                    "max_active_jobs": 1,
                    "plan_limit": self.service.settings.plan_limit,
                    "plan_insert_batch": self.service.settings.plan_insert_batch,
                    "creation_batch_max": self.service.settings.plan_insert_batch,
                    "upload_max_file": self.service.settings.upload_max_file,
                    "decode_max_pixels": self.service.settings.decode_max_pixels,
                    "page_size": self.service.settings.page_size,
                    "page_size_max": self.service.settings.page_size_max,
                    "output_state_max_ids": 200,
                },
            }
        )
        if caps.get("targeted_cancel") != "verified_atomic":
            caps["warnings"].append("立即暂停可能降级为等待当前执行结束（未验证定向取消）。")
        return caps

    async def health(self, request: web.Request):
        return self.service.health()

    async def get_settings(self, request: web.Request):
        return self.service.get_settings()

    async def put_settings(self, request: web.Request):
        body = await _read_json(request)
        if not isinstance(body, dict):
            raise GachaError("BAD_REQUEST", "设置必须是 JSON 对象。")
        return await self._run_sync(self.service.update_settings, body)

    async def upload(self, request: web.Request):
        post = await request.post()
        results = []
        incoming_dir = os.path.join(self.service.paths.inbox_root, ".incoming")
        ensure_dir(incoming_dir)
        for field in post.values():
            if not getattr(field, "file", None) or not getattr(field, "filename", None):
                continue
            fd, temp_path = tempfile.mkstemp(prefix=".upload-", dir=incoming_dir)
            size = 0
            try:
                with os.fdopen(fd, "wb") as handle:
                    while True:
                        chunk = field.file.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > self.service.settings.upload_max_file:
                            raise GachaError(
                                "PAYLOAD_TOO_LARGE",
                                "上传文件超过允许大小。",
                                details={"limit": self.service.settings.upload_max_file},
                            )
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                metadata = await self._run_sync(
                    self.service.inputs.create_upload,
                    temp_path=temp_path,
                    original_name=field.filename,
                    size=size,
                )
                results.append(metadata)
            finally:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
        if not results:
            raise GachaError("BAD_REQUEST", "没有收到图片文件。")
        return results

    async def upload_image(self, request: web.Request):
        upload = self.service.inputs.get_upload(request.match_info["upload_id"])
        mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}.get(
            str(upload.get("format") or "").upper(), "application/octet-stream"
        )
        return web.FileResponse(
            upload["path"],
            headers={"Content-Type": mime, "Cache-Control": "private, max-age=300"},
        )

    async def scan_inputs(self, request: web.Request):
        body = await _read_json(request)
        if not isinstance(body, dict):
            raise GachaError("BAD_REQUEST", "请求必须是 JSON 对象。")
        return await self._run_sync(
            self.service.inputs.scan_folder,
            str(body.get("root") or ""),
            str(body.get("subdir") or ""),
        )

    async def preflight(self, request: web.Request):
        body = await _read_json(request)
        if not isinstance(body, dict):
            raise GachaError("BAD_REQUEST", "请求必须是 JSON 对象。")
        workflow_api = body.get("workflow_api")
        classes = set()
        if isinstance(workflow_api, dict):
            for node in workflow_api.values():
                if isinstance(node, dict) and isinstance(node.get("class_type"), str):
                    classes.add(node["class_type"])
        provider = None
        engine = await self.engine_provider()
        info_map: dict[str, Any] = {}
        for class_type in classes:
            try:
                info_map[class_type] = await engine.object_info(class_type)
            except Exception:
                info_map[class_type] = None
        if info_map:
            provider = lambda class_type: info_map.get(class_type)
        result = await self._run_sync(
            self.service.preflight,
            workflow_ui=body.get("workflow_ui"),
            workflow_api=workflow_api,
            bindings=body.get("bindings") or {},
            node_info_provider=provider,
        )
        return result.to_dict()

    async def create_job(self, request: web.Request):
        body = await _read_json(request)
        if not isinstance(body, dict):
            raise GachaError("BAD_REQUEST", "请求必须是 JSON 对象。")
        key = request.headers.get("Idempotency-Key")
        if key:
            result = await self._idempotent(
                "create_job", key, body, lambda: self._run_sync(
                    self.service.create_job, body, idempotency_key=key
                )
            )
        else:
            result = await self._run_sync(self.service.create_job, body)
        return result, 201

    async def list_jobs(self, request: web.Request):
        limit = request.query.get("limit", "50")
        cursor = request.query.get("cursor")
        return await self._run_sync(self.service.list_jobs, limit=int(limit), cursor=cursor)

    async def job_detail(self, request: web.Request):
        return await self._run_sync(self.service.job_detail, request.match_info["job_id"])

    async def start_job(self, request: web.Request):
        body = await _read_json(request) if request.can_read_body else {}
        if not isinstance(body, dict):
            body = {}
        job_id = request.match_info["job_id"]
        key = request.headers.get("Idempotency-Key")

        async def run():
            return await self._run_sync(
                self.service.start_job,
                job_id,
                expected_version=body.get("expected_version"),
                recovery_version=body.get("recovery_version"),
                acknowledge_recovery=bool(body.get("acknowledge_recovery")),
            )

        if key:
            return await self._idempotent(f"start:{job_id}", key, body, run)
        return await run()

    async def abandon_creation(self, request: web.Request):
        body = await _read_json(request) if request.can_read_body else {}
        return await self._run_sync(
            self.service.abandon_creation, request.match_info["job_id"], reason=(body or {}).get("reason")
        )

    async def pause_job(self, request: web.Request):
        body = await _read_json(request) if request.can_read_body else {}
        if not isinstance(body, dict):
            body = {}
        job_id = request.match_info["job_id"]
        return await self._run_sync(self.service.pause_job, job_id, mode=str(body.get("mode") or "safe"))

    async def job_recovery(self, request: web.Request):
        job_id = request.match_info["job_id"]
        job = self.repo.get_job(job_id)
        if job is None:
            raise GachaError("NOT_FOUND", "任务不存在。")
        engine = await self.engine_provider()
        return await self.recovery.current_report(job, engine)

    async def list_sources(self, request: web.Request):
        job_id = request.match_info["job_id"]
        limit = int(request.query.get("limit", "200"))
        after = int(request.query.get("after_order", "0"))
        return await self._run_sync(self.service.list_sources, job_id, limit=limit, after_order=after)

    async def review_session(self, request: web.Request):
        return await self._run_sync(
            self.service.review.get_session, request.match_info["job_id"]
        )

    async def open_review_session(self, request: web.Request):
        body = await _read_json(request) if request.can_read_body else {}
        if not isinstance(body, dict):
            raise GachaError("BAD_REQUEST", "请求必须是 JSON 对象。")
        return await self._run_sync(
            self.service.review.open_session,
            request.match_info["job_id"],
            scope=str(body.get("scope") or "pending"),
            start_source_id=body.get("start_source_id"),
        )

    async def review_sources(self, request: web.Request):
        after = request.query.get("after_order")
        before = request.query.get("before_order")
        return await self._run_sync(
            self.service.review.list_sources,
            request.match_info["job_id"],
            scope=str(request.query.get("scope") or "pending"),
            after_order=None if after is None or after == "" else int(after),
            before_order=None if before is None or before == "" else int(before),
            limit=int(request.query.get("limit", "40")),
        )

    async def review_state(self, request: web.Request):
        return await self._run_sync(
            self.service.review.source_state, request.match_info["source_id"]
        )

    async def review_position(self, request: web.Request):
        body = await _read_json(request)
        if not isinstance(body, dict):
            raise GachaError("BAD_REQUEST", "请求必须是 JSON 对象。")
        return await self._run_sync(
            self.service.review.save_position,
            request.match_info["session_id"],
            body,
        )

    async def review_decision(self, request: web.Request):
        body = await _read_json(request)
        if not isinstance(body, dict):
            raise GachaError("BAD_REQUEST", "请求必须是 JSON 对象。")
        action_id = request.headers.get("Idempotency-Key") or str(
            body.get("action_id") or ""
        )
        return await self._run_sync(
            self.service.review.decide_from_body,
            request.match_info["session_id"],
            body,
            action_id,
        )

    async def review_back(self, request: web.Request):
        body = await _read_json(request)
        if not isinstance(body, dict):
            raise GachaError("BAD_REQUEST", "请求必须是 JSON 对象。")
        return await self._run_sync(
            self.service.review.back, request.match_info["session_id"], body
        )

    async def remove_source(self, request: web.Request):
        body = await _read_json(request) if request.can_read_body else {}
        source_id = request.match_info["source_id"]
        return await self._run_sync(
            self.service.remove_source, source_id, expected_version=(body or {}).get("expected_version")
        )

    async def relocate_source(self, request: web.Request):
        body = await _read_json(request)
        return await self._run_sync(
            self.service.relocate_source, request.match_info["source_id"], str(body.get("path") or "")
        )

    async def append_source(self, request: web.Request):
        body = await _read_json(request)
        if not isinstance(body, dict):
            raise GachaError("BAD_REQUEST", "请求必须是 JSON 对象。")
        source_id = request.match_info["source_id"]
        key = request.headers.get("Idempotency-Key")

        async def run():
            return await self._run_sync(
                self.service.append_block,
                source_id,
                count=int(body.get("count") or 0),
                priority=str(body.get("priority") or "tail"),
                expected_version=body.get("expected_version"),
                idempotency_key=key,
            )

        if key:
            return await self._idempotent(f"append:{source_id}", key, body, run)
        return await run()

    async def review_source(self, request: web.Request):
        body = await _read_json(request)
        return await self._run_sync(
            self.service.confirm_source_review,
            request.match_info["source_id"],
            decision=str(body.get("decision") or ""),
            reason=body.get("reason"),
        )

    async def list_outputs(self, request: web.Request):
        source_id = request.match_info["source_id"]
        cursor = request.query.get("cursor")
        draw_no = out_no = 0
        if cursor:
            try:
                payload = json.loads(cursor)
                draw_no = int(payload.get("draw_no", 0))
                out_no = int(payload.get("out_no", 0))
            except Exception:
                raise GachaError("BAD_REQUEST", "分页游标无效。")
        offset_raw = request.query.get("offset")
        offset = None
        if offset_raw is not None and offset_raw != "":
            try:
                offset = int(offset_raw)
            except ValueError:
                raise GachaError("BAD_REQUEST", "offset 必须是非负整数。")
        return await self._run_sync(
            self.service.list_outputs,
            source_id,
            include_unhealthy=request.query.get("include_unhealthy") in ("1", "true"),
            limit=int(request.query.get("limit", "50")),
            cursor_draw_no=draw_no,
            cursor_out_no=out_no,
            direction=str(request.query.get("direction") or "forward"),
            offset=offset,
        )

    async def output_states(self, request: web.Request):
        body = await _read_json(request)
        if not isinstance(body, dict):
            raise GachaError("BAD_REQUEST", "请求必须是 JSON 对象。")
        output_ids = body.get("output_ids")
        if output_ids is None:
            output_ids = []
        if not isinstance(output_ids, list):
            raise GachaError("BAD_REQUEST", "output_ids 必须是数组。")
        return await self._run_sync(
            self.service.output_states,
            request.match_info["source_id"],
            output_ids,
        )

    async def selection(self, request: web.Request):
        body = await _read_json(request)
        if not isinstance(body.get("selected"), bool):
            raise GachaError("BAD_REQUEST", "selected 必须是布尔值。")
        return await self._run_sync(
            self.service.set_selection,
            request.match_info["output_id"],
            bool(body["selected"]),
            expected_version=body.get("expected_version"),
        )

    async def viewed(self, request: web.Request):
        return await self._run_sync(self.service.mark_viewed, request.match_info["output_id"])

    # ------------------------------------------------------------ images
    def _source_path(self, source: dict[str, Any]) -> str:
        from .util import is_link_like

        path = source.get("snapshot_path") or source.get("original_path")
        if not path:
            raise GachaError("INPUT_MISSING", "原图文件不存在。", source_id=source["id"])
        if is_link_like(path):
            raise GachaError("PATH_NOT_ALLOWED", "原图路径是符号链接/junction，拒绝读取。", source_id=source["id"])
        if source.get("snapshot_path"):
            if not path_is_within(os.path.dirname(source.get("snapshot_path")), path):
                raise GachaError("PATH_NOT_ALLOWED", "快照路径越界。")
            if not path_is_within(self.service.paths.jobs_root, path):
                raise GachaError("PATH_NOT_ALLOWED", "快照路径不在任务目录内。")
        else:
            allowed = self.service.inputs.resolve_allowed_root(os.path.dirname(path))
        if not os.path.isfile(path):
            raise GachaError("INPUT_MISSING", "原图文件缺失。", source_id=source["id"])
        return path

    def _output_path(self, output: dict[str, Any]) -> str:
        rel = str(output["rel_path"]).replace("\\", "/")
        path = os.path.join(self.service.paths.results_root, *rel.split("/"))
        require_within(self.service.paths.results_root, path, what="结果文件")
        if not os.path.isfile(path):
            raise GachaError("OUTPUT_MISSING", "结果文件缺失。", output_id=output["id"])
        return path

    async def source_image(self, request: web.Request):
        source = self.repo.get_source(request.match_info["source_id"])
        if source is None:
            raise GachaError("NOT_FOUND", "原图不存在。")
        path = self._source_path(source)
        return web.FileResponse(
            path,
            headers={
                "Content-Type": self._mime_for(source.get("image_format")),
                "Cache-Control": "private, max-age=300",
            },
        )

    async def source_thumbnail(self, request: web.Request):
        source = self.repo.get_source(request.match_info["source_id"])
        if source is None:
            raise GachaError("NOT_FOUND", "原图不存在。")
        path = self._source_path(source)
        cache = os.path.join(self.service.paths.thumbnails_root, "sources", f"{source['id']}.jpg")
        return await self._serve_thumbnail(path, cache)

    async def output_image(self, request: web.Request):
        output = self.repo.get_output(request.match_info["output_id"])
        if output is None:
            raise GachaError("NOT_FOUND", "输出不存在。")
        self._assert_output_retained(output["id"])
        if output["health"] in (OutputHealth.MISSING, OutputHealth.CORRUPT):
            raise GachaError("OUTPUT_MISSING", "结果文件不可用。", output_id=output["id"])
        try:
            path = self._output_path(output)
        except GachaError as exc:
            if exc.code in ("OUTPUT_MISSING", "OUTPUT_CORRUPT"):
                self.repo.update_output_health(output["id"], OutputHealth.MISSING, "结果文件在保存后丢失")
            raise
        return web.FileResponse(
            path,
            headers={
                "Content-Type": "image/png",
                "Cache-Control": "private, max-age=31536000, immutable",
            },
        )

    async def output_thumbnail(self, request: web.Request):
        output = self.repo.get_output(request.match_info["output_id"])
        if output is None:
            raise GachaError("NOT_FOUND", "输出不存在。")
        self._assert_output_retained(output["id"])
        if output["health"] in (OutputHealth.MISSING, OutputHealth.CORRUPT):
            raise GachaError("OUTPUT_MISSING", "结果文件不可用。", output_id=output["id"])
        try:
            path = self._output_path(output)
        except GachaError as exc:
            if exc.code in ("OUTPUT_MISSING", "OUTPUT_CORRUPT"):
                self.repo.update_output_health(output["id"], OutputHealth.MISSING, "结果文件在保存后丢失")
            raise
        cache = os.path.join(
            self.service.paths.thumbnails_root, "outputs", f"{output['sha256'][:32]}.jpg"
        )
        return await self._serve_thumbnail(path, cache)

    def _assert_output_retained(self, output_id: str) -> None:
        retention = self.repo.get_retention(output_id)
        state = (retention or {}).get("state") or "RETAINED"
        if state == "PURGED":
            raise GachaError(
                "OUTPUT_PURGED",
                "结果图片已被永久清理，不能读取。",
                details={"output_id": output_id, "retention_state": state},
            )
        if state == "CLEANING":
            raise GachaError(
                "OUTPUT_CLEANING",
                "结果图片正在清理中，暂不可读取。",
                retryable=True,
                details={"output_id": output_id, "retention_state": state},
            )

    async def _serve_thumbnail(self, source: str, cache: str):
        from .images import make_thumbnail

        if not os.path.isfile(cache):
            await self._run_sync(
                make_thumbnail, source, cache, edge=int(self.service.settings.thumbnail_edge)
            )
        try:
            with open(cache, "rb") as handle:
                data = handle.read()
        except FileNotFoundError:
            # Cache clear raced this request: regenerate once and serve the
            # bytes from memory so a second unlink cannot turn it into a 500.
            await self._run_sync(
                make_thumbnail, source, cache, edge=int(self.service.settings.thumbnail_edge)
            )
            with open(cache, "rb") as handle:
                data = handle.read()
        return web.Response(
            body=data,
            headers={
                "Content-Type": "image/jpeg",
                "Cache-Control": "private, max-age=31536000, immutable",
            },
        )

    @staticmethod
    def _mime_for(fmt: Optional[str]) -> str:
        return {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}.get(
            (fmt or "").upper(), "application/octet-stream"
        )

    # ----------------------------------------------------------- exports
    async def export_preview(self, request: web.Request):
        body = await _read_json(request)
        job_id = request.match_info["job_id"]
        key = request.headers.get("Idempotency-Key")

        async def run():
            import uuid as _uuid

            draft_id = str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"gacha-master/export-preview/{job_id}/{key}")) if key else None
            return await self._run_sync(
                self.exports.preview,
                job_id,
                kind=str(body.get("kind") or "QUICK"),
                layout=body.get("layout"),
                target_root=body.get("target_root"),
                draft_id=draft_id,
            )

        if key:
            return await self._idempotent(f"export_preview:{job_id}", key, body, run)
        return await run()

    async def export_list(self, request: web.Request):
        return await self._run_sync(self.exports.list_exports, request.match_info["job_id"])

    async def export_review(self, request: web.Request):
        body = await _read_json(request)
        return await self._run_sync(
            self.exports.put_review,
            request.match_info["draft_id"],
            request.match_info["source_id"],
            decision=str(body.get("decision") or ""),
            expected_result_revision=body.get("expected_result_revision"),
            expected_selection_version=body.get("expected_selection_version"),
            selected_output_ids=body.get("selected_output_ids"),
            reason=body.get("reason"),
        )

    async def export_confirm(self, request: web.Request):
        body = await _read_json(request)
        draft_id = request.match_info["draft_id"]
        key = request.headers.get("Idempotency-Key")

        async def run():
            result = await self._run_sync(
                self.exports.confirm,
                draft_id,
                expected_draft_version=body.get("expected_draft_version"),
                expected_cutoff_event_id=body.get("expected_cutoff_event_id"),
                layout=body.get("layout"),
                target_root=body.get("target_root"),
                confirm_cutoff=bool(body.get("confirm_cutoff")),
                confirm_zero=bool(body.get("confirm_zero")),
            )
            if result["items"] and result["status"] in ("COPYING", "FROZEN"):
                self._start_copy(result["id"])
            return result

        if key:
            result = await self._idempotent(f"export_confirm:{draft_id}", key, body, run)
        else:
            result = await run()
        if result.get("items") and result.get("status") in ("COPYING", "FROZEN"):
            # Also covers a retry that returns the stored response after a
            # crash between the freeze transaction and starting the copy.
            self._start_copy(result["id"])
        return result, 202

    def _start_copy(self, export_id: str) -> None:
        if self.copy_executor is not None:
            self.copy_executor.submit(self.exports.copy_export, export_id)
            return
        import threading

        threading.Thread(
            target=self.exports.copy_export, args=(export_id,), name=f"gacha-export-{export_id[:8]}", daemon=True
        ).start()

    async def export_detail(self, request: web.Request):
        return await self._run_sync(self.exports.get_export, request.match_info["export_id"])

    async def export_resume(self, request: web.Request):
        export_id = request.match_info["export_id"]
        key = request.headers.get("Idempotency-Key")

        async def run():
            return await self._run_sync(self.exports.resume, export_id)

        if key:
            result = await self._idempotent(f"export_resume:{export_id}", key, {}, run)
        else:
            result = await run()
        self._start_copy(export_id)
        return result

    # ------------------------------------------------------------ events
    async def events(self, request: web.Request):
        after = int(request.query.get("after", "0"))
        job_id = request.query.get("job_id")
        limit = int(request.query.get("limit", "200"))
        items = await self._run_sync(self.repo.list_events, after=after, job_id=job_id, limit=limit)
        return {"items": items, "last_seq": await self._run_sync(self.repo.last_event_seq)}

    # ------------------------------------------------------------ backups
    async def list_backups(self, request: web.Request):
        return await self._run_sync(self.service.list_backups)

    async def create_backup(self, request: web.Request):
        return await self._run_sync(self.service.create_backup, "manual")

    async def restore_backup(self, request: web.Request):
        return await self._run_sync(self.service.restore_backup, request.match_info["backup_id"])

    async def storage(self, request: web.Request):
        return await self._run_sync(self.service.storage_summary)

    async def clear_cache(self, request: web.Request):
        return await self._run_sync(self.service.clear_thumbnail_cache)

    # ------------------------------------------------------------ cleanup
    def _require_cleanup(self):
        if self.cleanup is None:
            raise GachaError("SERVICE_NOT_READY", "清理服务尚未就绪。")
        return self.cleanup

    async def cleanup_preview(self, request: web.Request):
        cleanup = self._require_cleanup()
        job_id = request.match_info["job_id"]
        key = request.headers.get("Idempotency-Key")

        async def run():
            return await self._run_sync(cleanup.preview, job_id)

        if key:
            return await self._idempotent(f"cleanup_preview:{job_id}", key, {}, run)
        return await run()

    async def cleanup_detail(self, request: web.Request):
        cleanup = self._require_cleanup()
        return await self._run_sync(cleanup.get, request.match_info["cleanup_id"])

    async def cleanup_items(self, request: web.Request):
        cleanup = self._require_cleanup()
        raw_limit = request.query.get("limit", "50")
        try:
            limit = int(raw_limit)
        except ValueError:
            raise GachaError("BAD_REQUEST", "limit 必须是整数。")
        return await self._run_sync(
            cleanup.items,
            request.match_info["cleanup_id"],
            limit=limit,
            cursor=request.query.get("cursor"),
        )

    async def job_cleanups(self, request: web.Request):
        cleanup = self._require_cleanup()
        raw_limit = request.query.get("limit", "20")
        try:
            limit = int(raw_limit)
        except ValueError:
            raise GachaError("BAD_REQUEST", "limit 必须是整数。")
        return await self._run_sync(
            cleanup.list_job_cleanups,
            request.match_info["job_id"],
            limit=limit,
            cursor=request.query.get("cursor"),
        )

    async def cleanup_confirm(self, request: web.Request):
        cleanup = self._require_cleanup()
        body = await _read_json(request) if request.can_read_body else {}
        if not isinstance(body, dict):
            raise GachaError("BAD_REQUEST", "请求必须是 JSON 对象。")
        plan_token = body.get("plan_token")
        if not isinstance(plan_token, str) or not plan_token:
            raise GachaError("BAD_REQUEST", "plan_token 必须是非空字符串。")
        count = body.get("expected_candidate_count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise GachaError("BAD_REQUEST", "expected_candidate_count 必须是非负整数。")
        if body.get("confirm_permanent_delete") is not True:
            raise GachaError(
                "BAD_REQUEST",
                "必须显式提交 confirm_permanent_delete=true 才能永久删除。",
            )
        cleanup_id = request.match_info["cleanup_id"]
        key = request.headers.get("Idempotency-Key")

        async def run():
            return await self._run_sync(
                cleanup.confirm,
                cleanup_id,
                plan_token=plan_token,
                expected_candidate_count=count,
                confirm_permanent_delete=True,
            )

        if key:
            result = await self._idempotent(f"cleanup_confirm:{cleanup_id}", key, body, run)
        else:
            result = await run()
        # A retry must observe the real progress, not the initial RUNNING
        # snapshot stored on the first response.
        return await self._run_sync(cleanup.get, result["id"]), 202

    # -------------------------------------------------------- idempotency
    async def _idempotent(self, scope: str, key: str, body: Any, fn):
        request_hash = _request_hash(body)
        existing = self.repo.get_idempotency(scope, key)
        if existing is not None:
            if existing["request_hash"] != request_hash:
                raise GachaError(
                    "IDEMPOTENCY_CONFLICT",
                    "相同 Idempotency-Key 用于了不同的请求内容。",
                    details={"scope": scope},
                )
            if existing["status"] == "COMPLETED" and existing.get("response_json"):
                return json.loads(existing["response_json"])
            if existing["status"] == "IN_PROGRESS":
                # A previous attempt may have committed its transaction and
                # crashed before storing the response.  Deterministic IDs in
                # the service make re-running safe; do it now.
                pass
            elif existing["status"] == "FAILED":
                raise GachaError(
                    "IDEMPOTENCY_CONFLICT",
                    "该请求此前失败，请使用新的 Idempotency-Key 重试。",
                    details={"scope": scope},
                )
        else:
            try:
                self.repo.begin_idempotency(scope, key, request_hash)
            except GachaError as exc:
                raise exc
        try:
            result = await fn()
        except GachaError as exc:
            try:
                if 400 <= int(exc.http_status) < 500:
                    # Validation/idempotency failures must not consume a key.
                    self.repo.delete_idempotency(scope, key)
                else:
                    self.repo.fail_idempotency(scope, key)
            except Exception:
                pass
            raise
        except Exception:
            try:
                self.repo.fail_idempotency(scope, key)
            except Exception:
                pass
            raise
        entity_id = None
        if isinstance(result, dict):
            entity_id = result.get("id") or result.get("block_id") or result.get("export_id")
        self.repo.complete_idempotency(scope, key, response=result, http_status=200, entity_id=entity_id)
        return result
