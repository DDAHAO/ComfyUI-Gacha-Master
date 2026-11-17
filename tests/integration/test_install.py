import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INSTALLER = os.path.join(ROOT, "tools", "install_plugin.py")
PYTHON = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
if not os.path.exists(PYTHON):
    PYTHON = sys.executable


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.comfy = tempfile.mkdtemp(prefix="fake-comfy-")
        open(os.path.join(self.comfy, "main.py"), "w", encoding="utf-8").write("# fake ComfyUI\n")
        os.makedirs(os.path.join(self.comfy, "custom_nodes"))
        # Pretend this is the user's persistent data outside the install.
        self.data = os.path.join(self.comfy, "user", "gacha_master")
        os.makedirs(self.data)
        with open(os.path.join(self.data, "keep.txt"), "w", encoding="utf-8") as handle:
            handle.write("retain me")

    def tearDown(self):
        shutil.rmtree(self.comfy, ignore_errors=True)

    def run_installer(self, *args):
        return subprocess.run(
            [PYTHON, INSTALLER, "--comfy-root", self.comfy, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )

    def test_copy_install_idempotent_import_and_uninstall_retains_data(self):
        result = self.run_installer("--mode", "copy")
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["action"], "copied")
        destination = summary["destination"]
        self.assertTrue(os.path.isfile(os.path.join(destination, "__init__.py")))
        self.assertTrue(os.path.isfile(os.path.join(destination, "comfyui_gacha_master", "web", "extension.js")))
        # The copied package can be loaded by ComfyUI's file-location loader
        # without a real ComfyUI present (route registration is optional).
        name = "gacha_install_test"
        spec = importlib.util.spec_from_file_location(
            name, os.path.join(destination, "__init__.py"), submodule_search_locations=[destination]
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        self.assertIn("GachaSaveImage", module.NODE_CLASS_MAPPINGS)
        again = self.run_installer("--mode", "copy")
        self.assertEqual(again.returncode, 0)
        self.assertEqual(json.loads(again.stdout)["action"], "already_installed_copy")
        removed = self.run_installer("--uninstall")
        self.assertEqual(removed.returncode, 0)
        self.assertEqual(json.loads(removed.stdout)["action"], "removed_copy")
        self.assertFalse(os.path.exists(destination))
        self.assertTrue(os.path.isfile(os.path.join(self.data, "keep.txt")))

    def test_link_install_and_unknown_directory_is_not_deleted(self):
        result = self.run_installer("--mode", "link")
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertIn(summary["action"], ("linked", "copied"))
        destination = summary["destination"]
        self.assertTrue(os.path.exists(destination))
        again = self.run_installer("--mode", "link")
        self.assertEqual(again.returncode, 0)
        self.assertIn(json.loads(again.stdout)["action"], ("already_installed_link", "already_installed_copy"))
        removed = self.run_installer("--uninstall")
        self.assertEqual(removed.returncode, 0)
        self.assertTrue(os.path.isfile(os.path.join(self.data, "keep.txt")))
        # An unknown directory with the same name must never be removed.
        unknown = os.path.join(self.comfy, "custom_nodes", "ComfyUI-Gacha-Master")
        os.makedirs(unknown)
        with open(os.path.join(unknown, "important.txt"), "w", encoding="utf-8") as handle:
            handle.write("user file")
        refused = self.run_installer("--uninstall")
        self.assertNotEqual(refused.returncode, 0)
        self.assertTrue(os.path.isfile(os.path.join(unknown, "important.txt")))

    def test_invalid_comfy_root_refused(self):
        bad = tempfile.mkdtemp(prefix="not-comfy-")
        self.addCleanup(lambda: shutil.rmtree(bad, ignore_errors=True))
        result = subprocess.run(
            [PYTHON, INSTALLER, "--comfy-root", bad, "--dry-run"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("main.py", result.stderr + result.stdout)


if __name__ == "__main__":
    unittest.main()
