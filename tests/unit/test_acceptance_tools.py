"""Tests for the acceptance tooling added for NP01.

These tests are about evidence quality, not product behavior: deterministic
manifests, exact runtime-set verification, scenario validation, structured
failures and the controlled-volume guard.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOLS = os.path.join(ROOT, "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import candidate_manifest as cm  # noqa: E402
import production_acceptance as pa  # noqa: E402
import run_regression as rr  # noqa: E402
import storage_acceptance as sa  # noqa: E402

PYTHON = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
if not os.path.exists(PYTHON):
    PYTHON = sys.executable


class CandidateManifestTests(unittest.TestCase):
    def test_scopes_are_nested_and_repeatable(self):
        first = cm.build_manifest(ROOT)
        second = cm.build_manifest(ROOT)
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        runtime = first["scopes"]["runtime"]
        delivery = first["scopes"]["delivery"]
        browser = first["scopes"]["browser_assets"]
        self.assertEqual(runtime["file_count"], len(runtime["files"]))
        self.assertGreaterEqual(runtime["file_count"], 50)
        self.assertTrue(set(runtime["files"]) <= set(delivery["files"]))
        self.assertTrue(set(browser["files"]) <= set(runtime["files"]))
        self.assertIn("__init__.py", runtime["files"])
        self.assertIn("comfyui_gacha_master/web/views/detail.js", browser["files"])

    def test_extra_runtime_file_fails_exact_set_comparison(self):
        temp = tempfile.mkdtemp(prefix="gm-install-check-")
        self.addCleanup(lambda: shutil.rmtree(temp, ignore_errors=True))
        for rel in cm.runtime_manifest(ROOT):
            target = os.path.join(temp, *rel.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(os.path.join(ROOT, *rel.split("/")), target)
        clean = cm.installed_report(ROOT, temp)
        self.assertTrue(clean["ok"])
        rogue = os.path.join(temp, "comfyui_gacha_master", "backend", "rogue.py")
        with open(rogue, "w", encoding="utf-8") as handle:
            handle.write("VALUE = 1\n")
        dirty = cm.installed_report(ROOT, temp)
        self.assertFalse(dirty["ok"])
        self.assertIn("comfyui_gacha_master/backend/rogue.py", dirty["extra_runtime_files"])

    def test_installer_verify_reports_extra_runtime_file(self):
        comfy = tempfile.mkdtemp(prefix="gm-fake-comfy-")
        self.addCleanup(lambda: shutil.rmtree(comfy, ignore_errors=True))
        with open(os.path.join(comfy, "main.py"), "w", encoding="utf-8") as handle:
            handle.write("# fake\n")
        os.makedirs(os.path.join(comfy, "custom_nodes"))
        install = subprocess.run(
            [PYTHON, os.path.join(TOOLS, "install_plugin.py"), "--comfy-root", comfy, "--mode", "copy"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        self.assertEqual(install.returncode, 0, install.stderr)
        destination = json.loads(install.stdout)["destination"]
        verify = subprocess.run(
            [PYTHON, os.path.join(TOOLS, "install_plugin.py"), "--comfy-root", comfy, "--verify"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        self.assertEqual(verify.returncode, 0, verify.stderr)
        self.assertEqual(json.loads(verify.stdout)["result"], "MATCH")
        rogue = os.path.join(destination, "comfyui_gacha_master", "backend", "rogue.py")
        with open(rogue, "w", encoding="utf-8") as handle:
            handle.write("VALUE = 1\n")
        dirty = subprocess.run(
            [PYTHON, os.path.join(TOOLS, "install_plugin.py"), "--comfy-root", comfy, "--verify"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        payload = json.loads(dirty.stdout)
        self.assertEqual(payload["result"], "MISMATCH")
        self.assertIn("comfyui_gacha_master/backend/rogue.py", payload["extra_runtime_files"])


class ScenarioTests(unittest.TestCase):
    def test_aliases_and_explicit_tokens(self):
        self.assertEqual(pa.parse_scenario("555")["sources"], 111)
        self.assertEqual(pa.parse_scenario("555")["count_per_source"], 5)
        one = pa.parse_scenario("1x101")
        self.assertEqual((one["sources"], one["count_per_source"]), (1, 101))
        self.assertFalse(pa.parse_scenario("1x101")["label"].startswith("legacy"))

    def test_rejects_non_positive_unknown_and_overflow(self):
        for token in ("0x1", "1x0", "1x-1", "abc", "1x", "x5", "1.5x2"):
            with self.subTest(token=token):
                with self.assertRaises(pa.AcceptanceError):
                    pa.parse_scenario(token)

    def test_scenario_file_cannot_redefine_alias(self):
        temp = tempfile.mkdtemp(prefix="gm-scenario-")
        self.addCleanup(lambda: shutil.rmtree(temp, ignore_errors=True))
        path = os.path.join(temp, "scenarios.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"scenarios": {"555": {"sources": 1, "count_per_source": 1}}}, handle)
        with self.assertRaises(pa.AcceptanceError):
            pa.load_scenario_file(path)

    def test_argument_error_is_structured(self):
        result = pa.main(["--scenarios", "1x101"])
        self.assertEqual(result, 2)


class FailureReportTests(unittest.TestCase):
    def test_missing_workflow_writes_structured_failure(self):
        temp = tempfile.mkdtemp(prefix="gm-accept-fail-")
        self.addCleanup(lambda: shutil.rmtree(temp, ignore_errors=True))
        input_root = os.path.join(temp, "input")
        os.makedirs(input_root)
        from PIL import Image

        Image.new("RGB", (8, 8), (1, 2, 3)).save(os.path.join(input_root, "a.png"))
        output_dir = os.path.join(temp, "evidence")
        code = pa.main([
            "--data-root", os.path.join(temp, "data"),
            "--workflow-api", os.path.join(temp, "missing-api.json"),
            "--workflow-ui", os.path.join(temp, "missing-ui.json"),
            "--input-root", input_root,
            "--output-dir", output_dir,
            "--scenarios", "1x1",
            "--data-source", "fixture",
        ])
        self.assertEqual(code, 2)
        with open(os.path.join(output_dir, "SUMMARY.json"), encoding="utf-8") as handle:
            summary = json.load(handle)
        self.assertEqual(summary["result"], "FAIL")
        self.assertEqual(summary["error"]["phase"], "load_inputs")
        self.assertIn("candidate", summary)
        self.assertFalse(summary["side_effects"]["inference_started"])

    def test_transport_error_is_structured(self):
        with self.assertRaises(pa.AcceptanceError) as raised:
            pa.api("http://127.0.0.1:1", "/gacha_master/v1/health")
        self.assertEqual(raised.exception.phase, "http")


class RegressionParsingTests(unittest.TestCase):
    def test_zero_tests_is_visible(self):
        self.assertEqual(rr.parse_python("Ran 0 tests in 0.001s")["tests"], 0)
        self.assertEqual(rr.parse_js("# tests 0\n# pass 0\n# fail 0")["tests"], 0)
        self.assertEqual(rr.parse_js("# tests 3\n# pass 2\n# fail 1")["fail"], 1)


@unittest.skipUnless(os.name == "nt", "Windows volume guard")
class StorageGuardTests(unittest.TestCase):
    def test_system_and_project_volumes_are_refused(self):
        temp = tempfile.mkdtemp(prefix="gm-storage-")
        self.addCleanup(lambda: shutil.rmtree(temp, ignore_errors=True))
        report = os.path.join(temp, "preflight.json")
        result = sa.preflight(os.environ.get("TEMP", temp), report)
        self.assertEqual(result["result"], "BLOCKED")
        self.assertTrue(any("系统卷" in item for item in result["errors"]))
        self.assertTrue(os.path.isfile(report))

    def test_fill_requires_matching_serial(self):
        temp = tempfile.mkdtemp(prefix="gm-storage-fill-")
        self.addCleanup(lambda: shutil.rmtree(temp, ignore_errors=True))
        report = os.path.join(temp, "fill.json")
        result = sa.fill(temp, os.path.join(temp, "run"), "00000000", 1024, report, max_bytes=1, timeout=1)
        self.assertEqual(result["result"], "REFUSED")
        # The refusal comes from the write entry point itself: ``fill`` was
        # called without any prior ``preflight`` and still refused.
        self.assertIn(result["refusal"]["code"], ("E_SYSTEM_VOLUME", "E_REGISTRY"))
        self.assertEqual(result["filler_files"], [])


if __name__ == "__main__":
    unittest.main()
