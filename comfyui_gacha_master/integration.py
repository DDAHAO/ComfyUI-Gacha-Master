"""ComfyUI integration entry point.

Routes are registered on ``PromptServer.instance.routes`` before the aiohttp
app starts.  Handlers lazily start the runtime on first use; a delayed startup
task also initializes it after the server loop is alive so restart recovery is
not dependent on a browser being open.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any, Optional

from .backend.api import Api
from .backend.adapters.comfy import ComfyAdapter
from .backend.errors import GachaError
from .backend.runtime import Runtime

LOGGER = logging.getLogger("gacha.integration")

_registered = False
_register_lock = threading.Lock()


def _comfy_paths() -> dict[str, Any]:
    import folder_paths

    user_dir = folder_paths.get_user_directory()
    output_dir = folder_paths.get_output_directory()
    input_dir = folder_paths.get_input_directory()
    return {
        "data_root": os.path.join(user_dir, "gacha_master"),
        "results_root": os.path.join(output_dir, "gacha_master", "Jobs"),
        "comfy_input_root": input_dir,
        "comfy_output_root": output_dir,
    }


def _base_url() -> str:
    try:
        from comfy.cli_args import args

        port = int(getattr(args, "port", 8188))
        listen = str(getattr(args, "listen", "127.0.0.1") or "127.0.0.1")
        host = "127.0.0.1"
        if listen in ("::", "::1", "::0"):
            host = "[::1]"
        return f"http://{host}:{port}"
    except Exception:
        return "http://127.0.0.1:8188"


def _include_metadata() -> bool:
    try:
        from comfy.cli_args import args

        return not bool(getattr(args, "disable_metadata", False))
    except Exception:
        return True


class _LazyRouter:
    def __init__(self, runtime: Runtime, prefix: str) -> None:
        self.runtime = runtime
        self.prefix = prefix.rstrip("/")

    def register(self, routes) -> None:
        for method, path, name in Api.ROUTE_SPECS:
            handler = self._proxy(name)
            handler.__name__ = f"gacha_{method}_{name}_{abs(hash(path))}"
            getattr(routes, method)(self.prefix + path)(handler)

    def _proxy(self, name: str):
        async def handler(request):
            try:
                await self.runtime.ensure_started()
            except GachaError as exc:
                from aiohttp import web

                return web.json_response(exc.to_dict(), status=exc.http_status)
            except Exception as exc:
                from aiohttp import web

                LOGGER.exception("runtime startup failed")
                return web.json_response(
                    {
                        "schema_version": 1,
                        "error": {
                            "code": "SERVICE_NOT_READY",
                            "message": "抽卡大师后端启动失败。",
                            "trace_id": "",
                            "retryable": True,
                            "details": {"error": f"{type(exc).__name__}: {exc}"},
                        },
                    },
                    status=503,
                )
            api = self.runtime.api
            if api is None:
                from aiohttp import web

                return web.json_response(
                    {"schema_version": 1, "error": {"code": "SERVICE_NOT_READY", "message": "服务尚未就绪。"}},
                    status=503,
                )
            return await api.handlers[name](request)

        return handler


async def _safe_start(runtime: Runtime) -> None:
    try:
        await runtime.ensure_started()
    except Exception:
        LOGGER.exception("delayed Gacha Master startup failed; API calls will retry")


def register() -> Optional[Runtime]:
    global _registered
    with _register_lock:
        if _registered:
            return None
        try:
            from server import PromptServer
        except Exception:
            LOGGER.warning("ComfyUI PromptServer not importable; Gacha Master UI routes are disabled.")
            return None
        server = getattr(PromptServer, "instance", None)
        if server is None:
            LOGGER.warning("PromptServer instance not available; Gacha Master routes are disabled.")
            return None
        existing = getattr(server, "_gacha_master_runtime", None)
        if existing is not None:
            _registered = True
            return existing
        paths = _comfy_paths()
        base_url = _base_url()
        include_metadata = _include_metadata()

        def engine_factory():
            timeout = runtime.settings.submit_timeout if runtime.settings else 30.0
            verified = bool(runtime.settings.targeted_cancel_verified if runtime.settings else False)
            return ComfyAdapter(base_url=base_url, submit_timeout=timeout, targeted_cancel_verified=verified)

        def api_engine_factory():
            timeout = runtime.settings.submit_timeout if runtime.settings else 30.0
            verified = bool(runtime.settings.targeted_cancel_verified if runtime.settings else False)
            return ComfyAdapter(base_url=base_url, submit_timeout=timeout, targeted_cancel_verified=verified)

        runtime = Runtime(
            data_root=paths["data_root"],
            results_root=paths["results_root"],
            comfy_input_root=paths["comfy_input_root"],
            comfy_output_root=paths["comfy_output_root"],
            base_url=base_url,
            include_metadata=include_metadata,
            engine_factory=engine_factory,
            api_engine_factory=api_engine_factory,
        )
        server._gacha_master_runtime = runtime
        router = _LazyRouter(runtime, "/gacha_master/v1")
        router.register(server.routes)
        try:
            import atexit

            atexit.register(runtime.close)
        except Exception:
            pass
        try:
            loop = server.loop
            loop.call_later(5.0, lambda: asyncio.ensure_future(_safe_start(runtime), loop=loop))
        except Exception:
            LOGGER.debug("could not schedule delayed runtime startup", exc_info=True)
        _registered = True
        LOGGER.info("Gacha Master routes registered")
        return runtime
