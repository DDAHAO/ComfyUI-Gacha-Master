"""Browser test harness: real plugin backend + real frontend modules + stub
ComfyUI extension host.

This is explicitly an L0 UI harness, not a real ComfyUI installation.  It
serves the same web modules ComfyUI would load and registers them through the
same sidebar-tab API shape, while the backend uses the fake engine.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.join(ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tests"))

from aiohttp import web

from harness import Harness, make_png, workflow_fixture
from comfyui_gacha_master.backend.util import path_is_within

WEB_ROOT = os.path.join(ROOT, "comfyui_gacha_master", "web")

APP_STUB = """
export const app = {
  extensionManager: {
    registerSidebarTab(tab) {
      window.__gachaTab = tab;
      const mount = () => {
        const host = document.getElementById("panel");
        if (!host) { setTimeout(mount, 30); return; }
        if (host.dataset.mounted) return;
        host.dataset.mounted = "1";
        tab.render(host);
      };
      setTimeout(mount, 10);
    },
  },
  registerExtension(extension) {
    window.__gachaExtension = extension;
    setTimeout(() => extension.setup(), 0);
  },
  async graphToPrompt() {
    return { workflow: window.__gachaWorkflow.ui, output: window.__gachaWorkflow.api };
  },
};
"""

API_STUB = """
export const api = {
  async fetchApi(path, init) { return fetch(path, init); },
  apiURL(path) { return path; },
};
"""


def test_page(fixture):
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Gacha Master UI Harness</title>
<style>
  html,body {{ margin:0; height:100%; background:#181818; color:#ddd; font-family: system-ui, sans-serif; }}
  #panel {{ width: 980px; height: 92vh; margin: 10px auto; background:#202020; border:1px solid #444; }}
</style></head>
<body>
<div id="panel"></div>
<script>
window.__gachaWorkflow = {json.dumps(fixture)};
</script>
<script type="module" src="/extensions/gacha/extension.js"></script>
</body></html>"""


def safe_static(root, relative):
    target = os.path.abspath(os.path.join(root, relative))
    if not path_is_within(root, target):
        raise web.HTTPForbidden()
    if not os.path.isfile(target):
        raise web.HTTPNotFound()
    return target


async def main():
    port = int(os.environ.get("GACHA_UI_PORT", "8765"))
    # Optional fixed root lets a reviewer restart the harness without losing
    # the jobs created for a screenshot session.  Still an L0 harness only.
    harness = Harness(
        outputs_per_attempt=int(os.environ.get("GACHA_UI_OUTPUTS", "2")),
        settings_overrides={"backup_draws": 1000000, "backup_interval": 1000000},
        root=os.environ.get("GACHA_UI_ROOT") or None,
    )
    await harness.start()
    make_png(os.path.join(harness.input_root, "harness_a.png"), color=(180, 70, 70))
    make_png(os.path.join(harness.input_root, "harness_b.png"), color=(70, 180, 90))

    app = web.Application()
    routes = web.RouteTableDef()
    harness.runtime.api.register(routes)

    # Deterministic L0 fault injection for the UI pagination/retry tests.
    # Only this harness registers these controls; production code never sees
    # them.
    faults = {"rules": []}

    @web.middleware
    async def fault_middleware(request, handler):
        for rule in list(faults["rules"]):
            if rule["remaining"] <= 0:
                continue
            if rule.get("cookie") and rule["cookie"] not in request.headers.get("Cookie", ""):
                continue
            if request.method == rule.get("method", "GET") and rule["contains"] in request.path:
                rule["remaining"] -= 1
                return web.Response(status=int(rule.get("status", 500)), text="injected UI harness fault")
        return await handler(request)

    app.middlewares.append(fault_middleware)

    async def harness_fail(request):
        body = await request.json()
        if body.get("clear"):
            faults["rules"] = []
            return web.json_response({"ok": True, "rules": []})
        rule = {
            "contains": str(body.get("contains") or ""),
            "method": str(body.get("method") or "GET").upper(),
            "remaining": int(body.get("count") or 1),
            "status": int(body.get("status") or 500),
            "cookie": str(body.get("cookie") or ""),
        }
        faults["rules"].append(rule)
        return web.json_response({"ok": True, "rules": faults["rules"]})

    async def static_extension(request):
        relative = request.match_info["tail"]
        target = safe_static(WEB_ROOT, relative)
        content_type = "text/javascript" if target.endswith(".js") else ("text/css" if target.endswith(".css") else None)
        headers = {"Content-Type": content_type, "Cache-Control": "no-store"} if content_type else {"Cache-Control": "no-store"}
        return web.FileResponse(target, headers=headers)

    async def app_stub(request):
        return web.Response(text=APP_STUB, content_type="text/javascript", headers={"Cache-Control": "no-store"})

    async def api_stub(request):
        return web.Response(text=API_STUB, content_type="text/javascript", headers={"Cache-Control": "no-store"})

    async def harness_engine(request):
        """Test-only controls for the fake engine. Never part of production."""
        body = await request.json()
        engine = harness.engine
        applied = {}
        for key in (
            "auto_run", "auto_complete", "drop_response_next", "not_sent_next",
            "fail_next", "interrupt_next", "return_wrong_id", "lookup_delay",
            "cancel_removes_pending",
        ):
            if key in body:
                setattr(engine, key, body[key])
                applied[key] = getattr(engine, key)
        if "targeted_cancel" in body:
            engine._targeted_cancel = bool(body["targeted_cancel"])
            applied["targeted_cancel"] = engine._targeted_cancel
        return web.json_response({
            "ok": True,
            "applied": applied,
            "capabilities": await engine.capabilities(),
        })

    async def harness_create_fixture(request):
        """Create a ready job from the bundled fixture for UI scenarios."""
        body = await request.json()
        count = int(body.get("count") or 3)
        names = tuple(body.get("names") or ("harness_a.png", "harness_b.png", "harness_c.png"))
        for index, name in enumerate(names):
            path = os.path.join(harness.input_root, name)
            if not os.path.isfile(path):
                make_png(path, color=((170 + index * 25) % 255, (60 + index * 60) % 255, 90))
        job = harness.create_job(count=count, names=names, protection_mode=str(body.get("mode") or "snapshot"))
        return web.json_response({"ok": True, "id": job["id"], "display_id": job["display_id"], "status": job["status"]})

    routes.get("/extensions/gacha/{tail:.*}")(static_extension)
    routes.post("/__harness/engine")(harness_engine)
    routes.post("/__harness/create")(harness_create_fixture)
    routes.post("/__harness/fail")(harness_fail)
    routes.get("/scripts/app.js")(app_stub)
    routes.get("/scripts/api.js")(api_stub)
    routes.get("/")(lambda request: web.Response(text=test_page(workflow_fixture()), content_type="text/html", headers={"Cache-Control": "no-store"}))
    routes.get("/test")(lambda request: web.Response(text=test_page(workflow_fixture()), content_type="text/html", headers={"Cache-Control": "no-store"}))
    app.add_routes(routes)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    info = {
        "url": f"http://127.0.0.1:{port}/test",
        "input_root": harness.input_root,
        "export_root": harness.export_root,
        "data_root": harness.data_root,
        "results_root": harness.results_root,
    }
    with open(os.path.join(ROOT, "test-artifacts", "ui-harness.json"), "w", encoding="utf-8") as handle:
        json.dump(info, handle, indent=2)
    print(json.dumps(info), flush=True)
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()
        harness.cleanup()


if __name__ == "__main__":
    os.makedirs(os.path.join(ROOT, "test-artifacts"), exist_ok=True)
    asyncio.run(main())
