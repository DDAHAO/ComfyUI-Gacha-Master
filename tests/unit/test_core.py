import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.join(ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tests"))

from PIL import Image

from comfyui_gacha_master.backend.errors import GachaError
from comfyui_gacha_master.backend.images import header_dimensions, validate_image_bytes
from comfyui_gacha_master.backend.util import (
    natural_sort_key,
    path_is_within,
    publish_noreplace,
    sha256_file,
    verify_publish_semantics,
)
from comfyui_gacha_master.backend.workflow import apply_output_token, build_execution_prompt, preflight
from harness import Harness, workflow_fixture


class UtilImageTests(unittest.TestCase):
    def test_natural_sort_is_numeric_and_stable(self):
        names = ["图10.png", "图2.png", "B.png", "a.png", "图1.png", "A.png"]
        ordered = sorted(names, key=natural_sort_key)
        self.assertEqual(ordered, ["A.png", "a.png", "B.png", "图1.png", "图2.png", "图10.png"])

    def test_path_containment(self):
        with tempfile.TemporaryDirectory() as root:
            inside = os.path.join(root, "a", "b.png")
            os.makedirs(os.path.dirname(inside))
            open(inside, "wb").close()
            self.assertTrue(path_is_within(root, inside))
            self.assertFalse(path_is_within(os.path.join(root, "a"), root))
            self.assertFalse(path_is_within(root, os.path.join(root, "..", "escape.png")))

    def test_publish_never_overwrites(self):
        with tempfile.TemporaryDirectory() as root:
            first = os.path.join(root, "first.tmp")
            second = os.path.join(root, "second.tmp")
            target = os.path.join(root, "published.bin")
            with open(first, "wb") as handle:
                handle.write(b"first")
            with open(second, "wb") as handle:
                handle.write(b"second")
            publish_noreplace(first, target)
            with self.assertRaises(FileExistsError):
                publish_noreplace(second, target)
            with open(target, "rb") as handle:
                self.assertEqual(handle.read(), b"first")
            self.assertTrue(os.path.exists(second))
            self.assertFalse(os.path.exists(first))

    def test_filesystem_publish_self_test(self):
        with tempfile.TemporaryDirectory() as root:
            result = verify_publish_semantics(root)
            self.assertTrue(result["ok"], result)

    def test_png_jpeg_webp_and_header(self):
        for fmt, ext, mime in (("PNG", ".png", "image/png"), ("JPEG", ".jpg", "image/jpeg"), ("WEBP", ".webp", "image/webp")):
            buffer = io.BytesIO()
            Image.new("RGB", (24, 16), (10, 20, 30)).save(buffer, fmt)
            data = buffer.getvalue()
            info = validate_image_bytes(data, max_pixels=1000000)
            self.assertEqual(info.format, fmt)
            self.assertEqual((info.width, info.height), (24, 16))
            self.assertEqual(info.extension, ext)
            self.assertEqual(info.mime, mime)
            self.assertEqual(header_dimensions(data), (24, 16))

    def test_corrupt_and_oversized_rejected(self):
        with self.assertRaises(GachaError) as ctx:
            validate_image_bytes(b"\x89PNG\r\n\x1a\nnot-a-real-png", max_pixels=1000000)
        self.assertEqual(ctx.exception.code, "UNSUPPORTED_MEDIA")
        buffer = io.BytesIO()
        Image.new("RGB", (200, 200), (0, 0, 0)).save(buffer, "PNG")
        with self.assertRaises(GachaError) as ctx:
            validate_image_bytes(buffer.getvalue(), max_pixels=1000)
        self.assertEqual(ctx.exception.code, "PAYLOAD_TOO_LARGE")

    def test_animated_webp_rejected(self):
        frames = [Image.new("RGB", (20, 20), (index * 40, 0, 0)) for index in range(3)]
        buffer = io.BytesIO()
        frames[0].save(buffer, "WEBP", save_all=True, append_images=frames[1:], duration=100, loop=0)
        with self.assertRaises(GachaError) as ctx:
            validate_image_bytes(buffer.getvalue(), max_pixels=1000000)
        self.assertEqual(ctx.exception.code, "UNSUPPORTED_MEDIA")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.fixture = workflow_fixture()
        self.bindings = {"load_image_node_id": "1", "save_image_node_id": "6"}

    def test_valid_preflight_and_seed(self):
        result = preflight(self.fixture["ui"], self.fixture["api"], self.bindings)
        self.assertTrue(result.ok, result.errors)
        self.assertTrue(result.load_image_upstream)
        self.assertEqual(len(result.seed_bindings), 1)
        self.assertEqual(result.seed_bindings[0].node_id, "2")
        self.assertEqual(result.seed_bindings[0].input_name, "seed")
        self.assertTrue(result.warnings == [])

    def test_missing_node(self):
        result = preflight(self.fixture["ui"], self.fixture["api"], {"load_image_node_id": "1", "save_image_node_id": "99"})
        self.assertFalse(result.ok)
        self.assertEqual(result.errors[0]["code"], "NODE_MISSING")

    def test_load_image_must_be_upstream(self):
        api = json.loads(json.dumps(self.fixture["api"]))
        api["4"]["inputs"]["pixels"] = ["9", 0]
        api["9"] = {"class_type": "EmptyImage", "inputs": {"width": 64, "height": 64}}
        result = preflight(self.fixture["ui"], api, self.bindings)
        self.assertFalse(result.ok)
        self.assertIn("上游", result.errors[0]["message"])

    def test_linked_or_custom_seed_blocked(self):
        api = json.loads(json.dumps(self.fixture["api"]))
        api["2"]["inputs"]["seed"] = ["10", 0]
        api["10"] = {"class_type": "SeedNode", "inputs": {}}
        result = preflight(self.fixture["ui"], api, self.bindings)
        self.assertFalse(result.ok)
        self.assertEqual(result.errors[0]["code"], "SEED_BINDING_INVALID")
        self.assertTrue(any("不解除种子连线" in error["message"] for error in result.errors))

    def test_unknown_upstream_random_input_blocked(self):
        api = json.loads(json.dumps(self.fixture["api"]))
        api["11"] = {"class_type": "CustomRandomThing", "inputs": {"seed": 5, "model": ["3", 0]}}
        api["2"]["inputs"]["model"] = ["11", 0]
        result = preflight(self.fixture["ui"], api, self.bindings)
        self.assertFalse(result.ok)
        self.assertEqual(result.errors[0]["code"], "SEED_BINDING_INVALID")

    def test_extra_save_image_warns(self):
        api = json.loads(json.dumps(self.fixture["api"]))
        ui = json.loads(json.dumps(self.fixture["ui"]))
        api["12"] = {"class_type": "SaveImage", "inputs": {"images": ["5", 0], "filename_prefix": "extra"}}
        ui["nodes"].append({"id": 12, "type": "SaveImage"})
        result = preflight(ui, api, self.bindings)
        self.assertTrue(result.ok, result.errors)
        self.assertTrue(any(error["code"] == "EXTRA_SAVE_IMAGE" for error in result.warnings))

    def test_transform_keeps_original_and_downstream(self):
        api = json.loads(json.dumps(self.fixture["api"]))
        api["13"] = {"class_type": "PreviewImage", "inputs": {"images": ["6", 0]}}
        original = json.dumps(api, sort_keys=True)
        prompt, _ = build_execution_prompt(
            api,
            load_image_node_id="1",
            save_image_node_id="6",
            staged_name="gacha_master_test.png",
            seeds={"2::seed": 777},
        )
        prompt = apply_output_token(prompt, "6", "attempt-uuid")
        self.assertEqual(json.dumps(api, sort_keys=True), original)
        self.assertEqual(prompt["1"]["inputs"]["image"], "gacha_master_test.png")
        self.assertEqual(prompt["2"]["inputs"]["seed"], 777)
        self.assertEqual(prompt["6"]["class_type"], "GachaSaveImage")
        self.assertEqual(prompt["6"]["inputs"]["images"], ["5", 0])
        # Downstream node still points at the same node ID and slot.
        self.assertEqual(prompt["13"]["inputs"]["images"], ["6", 0])

    def test_ui_mapping_required(self):
        ui = json.loads(json.dumps(self.fixture["ui"]))
        ui["nodes"] = [node for node in ui["nodes"] if node["id"] != 6]
        result = preflight(ui, self.fixture["api"], self.bindings)
        self.assertFalse(result.ok)
        self.assertEqual(result.errors[0]["code"], "WORKFLOW_UNSUPPORTED")


if __name__ == "__main__":
    unittest.main()
