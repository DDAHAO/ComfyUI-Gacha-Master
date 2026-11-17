import asyncio
import importlib.util
import os
import sys
import tempfile
import textwrap
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class IntegrationRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.mkdtemp(prefix="fake-comfy-modules-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.temp, ignore_errors=True))
        for name in ("user", "output", "input"):
            os.makedirs(os.path.join(self.temp, name), exist_ok=True)
        with open(os.path.join(self.temp, "server.py"), "w", encoding="utf-8") as handle:
            handle.write(textwrap.dedent(f"""
                import asyncio
                from aiohttp import web

                class PromptServer:
                    instance = None
                    def __init__(self):
                        self.routes = web.RouteTableDef()
                        self.loop = asyncio.new_event_loop()
                        PromptServer.instance = self
            """))
        with open(os.path.join(self.temp, "folder_paths.py"), "w", encoding="utf-8") as handle:
            handle.write(textwrap.dedent(f"""
                def get_user_directory(): return r"{os.path.join(self.temp, 'user')}"
                def get_output_directory(): return r"{os.path.join(self.temp, 'output')}"
                def get_input_directory(): return r"{os.path.join(self.temp, 'input')}"
            """))
        os.makedirs(os.path.join(self.temp, "comfy"), exist_ok=True)
        with open(os.path.join(self.temp, "comfy", "__init__.py"), "w", encoding="utf-8") as handle:
            handle.write("")
        with open(os.path.join(self.temp, "comfy", "cli_args.py"), "w", encoding="utf-8") as handle:
            handle.write(textwrap.dedent("""
                class _Args:
                    port = 8188
                    listen = "127.0.0.1"
                    disable_metadata = False
                args = _Args()
            """))
        sys.path.insert(0, self.temp)
        self.addCleanup(lambda: sys.path.remove(self.temp))
        import server as fake_server

        fake_server.PromptServer()
        # Fresh copies of the modules under a unique package name.
        self.name = "gacha_integration_test_" + __import__("uuid").uuid4().hex
        spec = importlib.util.spec_from_file_location(
            self.name, os.path.join(ROOT, "__init__.py"), submodule_search_locations=[ROOT]
        )
        self.module = importlib.util.module_from_spec(spec)
        sys.modules[self.name] = self.module
        spec.loader.exec_module(self.module)

    def test_routes_nodes_and_web_directory_register(self):
        server = sys.modules["server"].PromptServer.instance
        paths = set()
        for route in server.routes:
            paths.add(route.path)
        self.assertIn("/gacha_master/v1/capabilities", paths)
        self.assertIn("/gacha_master/v1/jobs", paths)
        self.assertIn("/gacha_master/v1/outputs/{output_id}/selection", paths)
        self.assertIn("/gacha_master/v1/export-drafts/{draft_id}/confirm", paths)
        self.assertIn("/gacha_master/v1/jobs/{job_id}/abandon-creation", paths)
        self.assertIn("GachaSaveImage", self.module.NODE_CLASS_MAPPINGS)
        self.assertTrue(os.path.isdir(os.path.join(ROOT, self.module.WEB_DIRECTORY)))
        runtime = getattr(server, "_gacha_master_runtime", None)
        self.assertIsNotNone(runtime)
        self.assertFalse(runtime.started)  # import must not start scheduling

    def test_duplicate_register_does_not_duplicate_runtime(self):
        server = sys.modules["server"].PromptServer.instance
        first = server._gacha_master_runtime
        integration = sys.modules[self.name + ".comfyui_gacha_master.integration"]
        self.assertIsNone(integration.register())
        self.assertIs(server._gacha_master_runtime, first)


if __name__ == "__main__":
    unittest.main()
