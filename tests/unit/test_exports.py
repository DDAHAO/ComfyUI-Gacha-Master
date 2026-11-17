import asyncio
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.join(ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tests"))

from comfyui_gacha_master.backend.errors import GachaError
from comfyui_gacha_master.backend.util import sha256_file
from harness import Harness


class ExportTests(unittest.IsolatedAsyncioTestCase):
    async def harness(self, outputs=2):
        h = Harness(outputs_per_attempt=outputs)
        await h.start()
        self.addAsyncCleanup(h.cleanup)
        return h

    async def completed_job(self, h, sources=("a.png", "b.png"), count=1):
        job = h.create_job(count=count, names=sources)
        h.runtime.service.start_job(job["id"])
        final = h.wait_job(job["id"], timeout=30)
        self.assertEqual(final["status"], "COMPLETED", final)
        return job, h.runtime.service.list_sources(job["id"])["items"]

    def select_first(self, h, source, index=0):
        outputs = h.runtime.service.list_outputs(source["id"], limit=20)["items"]
        return h.runtime.service.set_selection(outputs[index]["id"], True), outputs[index]

    async def test_quick_flat_and_grouped_layouts_do_not_modify_sources(self):
        h = await self.harness()
        job, sources = await self.completed_job(h)
        self.select_first(h, sources[0])
        second, output2 = self.select_first(h, sources[1])
        quick = h.runtime.exports.preview(job["id"], kind="QUICK", layout="FLAT", target_root=h.export_root)
        frozen = h.runtime.exports.confirm(quick["id"], expected_draft_version=quick["draft_version"], confirm_cutoff=True)
        self.assertEqual(frozen["status"], "COPYING")
        done = h.runtime.exports.copy_export(frozen["id"])
        self.assertEqual(done["status"], "COMPLETED")
        names = sorted(item["target_rel_path"] for item in done["items"])
        self.assertEqual(len(names), len(done["items"]))
        for item in done["items"]:
            target = os.path.join(done["batch_dir"], *item["target_rel_path"].split("/"))
            self.assertTrue(os.path.isfile(target))
            actual, _ = sha256_file(target)
            self.assertEqual(actual, item["source_sha256"])
            source = h.runtime.repo.get_output(item["output_id"])
            self.assertEqual(actual, source["sha256"])
        manifest = json.load(open(os.path.join(done["batch_dir"], "manifest.json"), encoding="utf-8"))
        self.assertEqual(manifest["kind"], "gacha_export")
        self.assertEqual(len(manifest["items"]), done["item_count"])
        self.assertTrue(all(item.get("draw_no") for item in manifest["items"]))
        # A second export creates a new batch and leaves the first intact.
        quick2 = h.runtime.exports.preview(job["id"], kind="QUICK", layout="GROUPED", target_root=h.export_root)
        frozen2 = h.runtime.exports.confirm(quick2["id"], expected_draft_version=quick2["draft_version"], confirm_cutoff=True)
        self.assertNotEqual(frozen2["batch_dir"], done["batch_dir"])
        self.assertTrue(frozen2["items"])
        self.assertTrue("/" in frozen2["items"][0]["target_rel_path"])

    async def test_frozen_export_ignores_later_selection(self):
        h = await self.harness()
        job, sources = await self.completed_job(h, sources=("a.png",))
        _, output = self.select_first(h, sources[0], 0)
        quick = h.runtime.exports.preview(job["id"], kind="QUICK", layout="FLAT", target_root=h.export_root)
        frozen = h.runtime.exports.confirm(quick["id"], expected_draft_version=quick["draft_version"], confirm_cutoff=True)
        before = [(item["output_id"], item["target_rel_path"]) for item in frozen["items"]]
        # Select another output after freezing; the frozen batch must not change.
        h.runtime.service.set_selection(h.runtime.service.list_outputs(sources[0]["id"])["items"][1]["id"], True)
        after = h.runtime.exports.get_export(frozen["id"])
        self.assertEqual([(item["output_id"], item["target_rel_path"]) for item in after["items"]], before)
        self.assertEqual(after["item_count"], len(before))

    async def test_final_review_required_and_skip(self):
        h = await self.harness()
        job, sources = await self.completed_job(h)
        self.select_first(h, sources[0])
        draft = h.runtime.exports.preview(job["id"], kind="FINAL", layout="FLAT", target_root=h.export_root)
        with self.assertRaises(GachaError) as ctx:
            h.runtime.exports.confirm(draft["id"], expected_draft_version=draft["draft_version"], confirm_cutoff=True)
        self.assertEqual(ctx.exception.code, "EXPORT_REVIEW_REQUIRED")
        reviewed = h.runtime.exports.put_review(draft["id"], sources[0]["id"], decision="SELECTED")
        reviewed = h.runtime.exports.put_review(draft["id"], sources[1]["id"], decision="SKIPPED", reason="not selected")
        frozen = h.runtime.exports.confirm(draft["id"], expected_draft_version=reviewed["draft_version"], confirm_cutoff=True)
        self.assertEqual(frozen["status"], "COPYING")
        self.assertTrue(all(item["source_id"] == sources[0]["id"] for item in frozen["items"]))
        done = h.runtime.exports.copy_export(frozen["id"])
        self.assertEqual(done["status"], "COMPLETED")
        self.assertEqual(done["item_count"], 1)

    async def test_all_skipped_zero_export_requires_explicit_confirmation(self):
        h = await self.harness()
        job, sources = await self.completed_job(h, sources=("a.png",))
        draft = h.runtime.exports.preview(job["id"], kind="FINAL", layout="FLAT", target_root=h.export_root)
        reviewed = h.runtime.exports.put_review(draft["id"], sources[0]["id"], decision="SKIPPED", reason="test")
        with self.assertRaises(GachaError) as ctx:
            h.runtime.exports.confirm(draft["id"], expected_draft_version=reviewed["draft_version"], confirm_cutoff=True)
        self.assertEqual(ctx.exception.code, "EXPORT_REVIEW_REQUIRED")
        confirmed = h.runtime.exports.confirm(
            draft["id"], expected_draft_version=reviewed["draft_version"], confirm_cutoff=True, confirm_zero=True
        )
        self.assertEqual(confirmed["status"], "COMPLETED")
        self.assertEqual(confirmed["item_count"], 0)
        self.assertTrue(os.path.isfile(os.path.join(confirmed["batch_dir"], "manifest.json")))

    async def test_preview_cutoff_and_version_conflict(self):
        h = await self.harness()
        job, sources = await self.completed_job(h, sources=("a.png",))
        self.select_first(h, sources[0])
        draft = h.runtime.exports.preview(job["id"], kind="QUICK", layout="FLAT", target_root=h.export_root)
        # A new selection after preview invalidates freezing.
        h.runtime.service.set_selection(h.runtime.service.list_outputs(sources[0]["id"])["items"][1]["id"], True)
        with self.assertRaises(GachaError) as ctx:
            h.runtime.exports.confirm(draft["id"], expected_draft_version=draft["draft_version"], confirm_cutoff=True)
        self.assertEqual(ctx.exception.code, "VERSION_CONFLICT")

    async def test_background_pending_requires_cutoff_confirmation(self):
        h = await self.harness()
        job, sources = await self.completed_job(h, sources=("a.png",), count=1)
        self.select_first(h, sources[0])
        detail = h.runtime.service.job_detail(job["id"])
        h.runtime.service.append_block(sources[0]["id"], count=2, priority="tail", expected_version=detail["row_version"])
        self.assertEqual(h.runtime.service.job_detail(job["id"])["status"], "PAUSED")
        draft = h.runtime.exports.preview(job["id"], kind="QUICK", layout="FLAT", target_root=h.export_root)
        with self.assertRaises(GachaError) as ctx:
            h.runtime.exports.confirm(draft["id"], expected_draft_version=draft["draft_version"])
        self.assertEqual(ctx.exception.code, "EXPORT_REVIEW_REQUIRED")
        self.assertTrue(ctx.exception.details.get("confirm_cutoff_required"))
        confirmed = h.runtime.exports.confirm(
            draft["id"], expected_draft_version=draft["draft_version"], confirm_cutoff=True
        )
        self.assertEqual(confirmed["status"], "COPYING")
        self.assertIn("pending_draws", confirmed["cutoff_summary"])

    async def test_target_conflict_never_overwrites(self):
        h = await self.harness()
        job, sources = await self.completed_job(h, sources=("a.png",))
        self.select_first(h, sources[0])
        draft = h.runtime.exports.preview(job["id"], kind="QUICK", layout="FLAT", target_root=h.export_root)
        frozen = h.runtime.exports.confirm(draft["id"], expected_draft_version=draft["draft_version"], confirm_cutoff=True)
        item = frozen["items"][0]
        target = os.path.join(frozen["batch_dir"], *item["target_rel_path"].split("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(b"preexisting-conflict")
        before, _ = sha256_file(target)
        done = h.runtime.exports.copy_export(frozen["id"])
        self.assertEqual(done["status"], "PARTIAL_FAILED")
        self.assertEqual(done["items"][0]["status"], "FAILED")
        self.assertEqual(done["items"][0]["error_code"], "EXPORT_FILE_CONFLICT")
        after, _ = sha256_file(target)
        self.assertEqual(before, after)
        self.assertEqual(open(target, "rb").read(), b"preexisting-conflict")

    async def test_changed_source_is_not_published(self):
        h = await self.harness()
        job, sources = await self.completed_job(h, sources=("a.png",))
        _, output = self.select_first(h, sources[0])
        draft = h.runtime.exports.preview(job["id"], kind="QUICK", layout="FLAT", target_root=h.export_root)
        frozen = h.runtime.exports.confirm(draft["id"], expected_draft_version=draft["draft_version"], confirm_cutoff=True)
        row = h.runtime.repo.get_output(output["id"])
        source_path = os.path.join(h.runtime.paths.results_root, *str(row["rel_path"]).split("/"))
        with open(source_path, "ab") as handle:
            handle.write(b"tampered-after-freeze")
        done = h.runtime.exports.copy_export(frozen["id"])
        self.assertEqual(done["status"], "PARTIAL_FAILED")
        self.assertEqual(done["items"][0]["status"], "FAILED")
        self.assertEqual(done["items"][0]["error_code"], "OUTPUT_CORRUPT")
        target = os.path.join(done["batch_dir"], *done["items"][0]["target_rel_path"].split("/"))
        self.assertFalse(os.path.exists(target))

    async def test_missing_source_marks_partial_failure(self):
        h = await self.harness()
        job, sources = await self.completed_job(h, sources=("a.png",))
        selected, output = self.select_first(h, sources[0])
        draft = h.runtime.exports.preview(job["id"], kind="QUICK", layout="FLAT", target_root=h.export_root)
        frozen = h.runtime.exports.confirm(draft["id"], expected_draft_version=draft["draft_version"], confirm_cutoff=True)
        source_path = os.path.join(h.runtime.paths.results_root, *output["image_url"].split("/")[-3:]) if False else None
        row = h.runtime.repo.get_output(output["id"])
        os.unlink(os.path.join(h.runtime.paths.results_root, *str(row["rel_path"]).split("/")))
        done = h.runtime.exports.copy_export(frozen["id"])
        self.assertEqual(done["status"], "PARTIAL_FAILED")
        self.assertEqual(done["items"][0]["status"], "FAILED")
        self.assertEqual(done["items"][0]["error_code"], "OUTPUT_MISSING")


if __name__ == "__main__":
    unittest.main()
