"""Tests for tools/target_environment.py.

The target package is a precondition, not a product feature: the tool must say
exactly what is missing instead of letting an incomplete file look ready.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOLS = os.path.join(ROOT, "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import target_environment as te  # noqa: E402


class TargetEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.mkdtemp(prefix="gm-target-env-")
        self.addCleanup(lambda: shutil.rmtree(self.temp, ignore_errors=True))

    def test_template_is_all_missing(self):
        path = os.path.join(self.temp, "TARGET-ENVIRONMENT.json")
        payload = te.template(path)
        self.assertFalse(payload["filled"])
        self.assertEqual(len(payload["missing"]), sum(len(keys) for keys in te.REQUIRED.values()))
        result = te.validate(path)
        self.assertFalse(result["ready"])
        self.assertEqual(result["result"], "MISSING_INPUTS")
        self.assertIn("workflow.ui_json", result["missing"])

    def test_filled_file_with_real_paths_is_ready(self):
        instance_dir = os.path.join(self.temp, "ComfyUI")
        for name in ("custom_nodes", "input", "output"):
            os.makedirs(os.path.join(instance_dir, name))
        python = os.path.join(instance_dir, "python.exe")
        with open(python, "wb") as handle:
            handle.write(b"")
        workflow = os.path.join(self.temp, "workflow_api.json")
        with open(workflow, "w", encoding="utf-8") as handle:
            json.dump({"1": {"class_type": "LoadImage"}}, handle)
        model = os.path.join(self.temp, "model.safetensors")
        with open(model, "wb") as handle:
            handle.write(b"weights")
        digest = te._sha256_file(model)
        image = os.path.join(self.temp, "image.png")
        with open(image, "wb") as handle:
            handle.write(b"png")
        data_root = os.path.join(self.temp, "data")
        results_root = os.path.join(self.temp, "results")
        for path in (data_root, results_root):
            os.makedirs(path)
        backup = os.path.join(self.temp, "backup.sqlite3")
        with open(backup, "wb") as handle:
            handle.write(b"db")

        document = {
            "schema_version": 1,
            "instance": {
                "comfy_root": instance_dir, "python": python, "launch_command": "python main.py",
                "workdir": instance_dir, "base_url": "http://127.0.0.1:8188",
                "base_directory": instance_dir,
                "custom_nodes": os.path.join(instance_dir, "custom_nodes"),
                "input_dir": os.path.join(instance_dir, "input"),
                "output_dir": os.path.join(instance_dir, "output"),
            },
            "environment": {
                "os": "Windows 11", "comfy_version": "0.45.0", "comfy_commit": "abc",
                "frontend_version": "1.58.5", "python_version": "3.12.10",
                "torch_version": "2.9.1+cu126", "cuda_version": "12.6",
                "driver_version": "580.00", "gpu": "RTX 3070 8GB",
                "custom_nodes_versions": {"ComfyUI-Gacha-Master": "1.0.0"},
            },
            "workflow": {
                "ui_json": workflow, "api_json": workflow,
                "load_image_node_id": "1", "save_image_node_id": "9",
                "seed_node_ids": ["2"], "outputs_per_draw": 1,
            },
            "models_and_inputs": {
                "model_path": model, "model_sha256": digest,
                "input_root": os.path.join(instance_dir, "input"),
                "representative_images": [{"path": image, "sha256": "x"}],
            },
            "load": {
                "resolution": "512x512", "steps": 4, "batch": 1, "sources": 3,
                "count_per_source": 5, "outputs_per_draw": 1,
                "export_layouts": ["FLAT", "GROUPED"], "acceptable_seconds": 600,
                "disk_budget_bytes": 5 * 1024 ** 3,
            },
            "data_and_install": {
                "data_root": data_root, "results_root": results_root,
                "export_roots": [os.path.join(instance_dir, "output")],
                "schema_version": 2, "current_plugin_source": "candidate-delivery.zip",
                "install_mode": "copy", "backup_path": backup,
            },
            "execution_scope": {
                "isolated_dir": self.temp, "isolated_port": 8296,
                "existing_queue_policy": "stop new submissions",
                "maintenance_window": "2026-11-16 02:00-04:00",
                "production_operations_allowed": "install, start, one pilot cycle",
            },
        }
        path = os.path.join(self.temp, "TARGET-ENVIRONMENT.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        result = te.validate(path)
        self.assertTrue(result["ready"], result)
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["problems"], [])

    def test_wrong_model_hash_is_a_problem(self):
        path = os.path.join(self.temp, "TARGET-ENVIRONMENT.json")
        te.template(path)
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
        model = os.path.join(self.temp, "model.safetensors")
        with open(model, "wb") as handle:
            handle.write(b"weights")
        document["models_and_inputs"]["model_path"] = model
        document["models_and_inputs"]["model_sha256"] = "0" * 64
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        result = te.validate(path)
        self.assertFalse(result["ready"])
        self.assertTrue(any(item["problem"] == "sha256 mismatch" for item in result["problems"]))

    def test_invalid_workflow_json_is_a_problem(self):
        path = os.path.join(self.temp, "TARGET-ENVIRONMENT.json")
        te.template(path)
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
        broken = os.path.join(self.temp, "broken.json")
        with open(broken, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        document["workflow"]["api_json"] = broken
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        result = te.validate(path)
        self.assertTrue(any(item["problem"] == "invalid JSON" for item in result["problems"]))

    def test_volumes_capture_records_system_and_project(self):
        output = os.path.join(self.temp, "VOLUMES.json")
        payload = te.volumes(output)
        self.assertTrue(os.path.isfile(output))
        system = [drive for drive in payload["drives"] if drive.get("is_system")]
        self.assertTrue(system, "系统卷必须被识别")
        self.assertTrue(any(drive.get("is_project") for drive in payload["drives"]))
        # This host has no disposable volume; the tool must say so rather than
        # propose filling the system or project volume.
        for candidate in payload["controlled_volume_candidates"]:
            self.assertFalse(candidate.get("is_system"))
            self.assertFalse(candidate.get("is_project"))


if __name__ == "__main__":
    unittest.main()
