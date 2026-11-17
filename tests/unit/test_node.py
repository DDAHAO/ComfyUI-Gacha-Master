import json
import os
import sys
import tempfile
import unittest

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from comfyui_gacha_master.backend import storage
from comfyui_gacha_master.nodes.save_image import GachaSaveImage


class FakeSlice:
    def __init__(self, array):
        self.array = array

    def detach(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return self.array


class FakeTensor:
    """Minimal IMAGE-tensor surface used by GachaSaveImage, without torch."""

    def __init__(self, array):
        self.array = np.asarray(array, dtype=np.float32)
        self.shape = self.array.shape

    def __getitem__(self, index):
        return FakeSlice(self.array[index])


class GachaSaveImageTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="gacha-node-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))
        self.output_root = os.path.join(self.root, "output")
        self.results_root = os.path.join(self.output_root, "gacha_master", "Jobs")
        self.attempt_dir = os.path.join(self.results_root, "JOB-TEST", "SRC-0001", "DRAW-000001", "ATTEMPT-001-abcdef12")
        os.makedirs(self.attempt_dir)
        self.token = "attempt-token-1"
        self.context = storage.AttemptContext(
            job_id="job-1",
            job_display_id="JOB-TEST",
            source_id="source-1",
            source_order=1,
            draw_id="draw-1",
            draw_no=1,
            attempt_id=self.token,
            attempt_no=1,
            attempt_dir=self.attempt_dir,
            results_root=self.results_root,
            source_sha256="a" * 64,
            execution_hash="b" * 64,
            include_metadata=True,
            max_pixels=100_000_000,
            comfy_output_root=self.output_root,
        )
        storage.register_attempt_context(self.context)
        self.addCleanup(lambda: storage.unregister_attempt_context(self.token))

    def tensor(self, count=2, size=(8, 6)):
        rgb = np.zeros((count, size[1], size[0], 3), dtype=np.float32)
        rgb[..., 0] = 0.8
        rgb[..., 1] = 0.2
        rgb[..., 2] = 0.1
        return FakeTensor(rgb)

    def test_validate_and_is_changed(self):
        self.assertTrue(GachaSaveImage.VALIDATE_INPUTS(self.token))
        self.assertIn("执行令牌无效", GachaSaveImage.VALIDATE_INPUTS("missing"))
        changed = GachaSaveImage.IS_CHANGED(self.token)
        self.assertIn(self.token, changed)
        self.assertIn(self.context.execution_hash, changed)

    def test_saves_batch_manifest_ui_and_metadata(self):
        node = GachaSaveImage()
        result = node.save_images(self.tensor(2), self.token)
        self.assertIn("result", result)
        self.assertEqual(len(result["ui"]["images"]), 2)
        self.assertEqual(len(result["ui"]["gacha_outputs"]), 2)
        self.assertEqual(result["ui"]["images"][0]["type"], "output")
        manifest = json.load(open(os.path.join(self.attempt_dir, "outputs.complete.json"), encoding="utf-8"))
        self.assertEqual(manifest["n"], 2)
        self.assertEqual(manifest["identity"]["attempt_id"], self.token)
        for item in manifest["outputs"]:
            path = os.path.join(self.attempt_dir, item["filename"])
            self.assertTrue(os.path.isfile(path))
            with Image.open(path) as image:
                image.load()
                self.assertEqual(image.size, (8, 6))
                self.assertIn("gacha_master", image.text)
        # A second save for the same attempt is refused rather than duplicating OUT counts.
        with self.assertRaises(RuntimeError):
            node.save_images(self.tensor(1), self.token)

    def test_zero_images_fail(self):
        node = GachaSaveImage()
        with self.assertRaises(RuntimeError):
            node.save_images(self.tensor(0), self.token)
        self.assertFalse(os.path.exists(os.path.join(self.attempt_dir, "outputs.complete.json")))

    def test_unknown_token_is_rejected(self):
        node = GachaSaveImage()
        with self.assertRaises(RuntimeError):
            node.save_images(self.tensor(1), "unknown-token")


if __name__ == "__main__":
    unittest.main()
