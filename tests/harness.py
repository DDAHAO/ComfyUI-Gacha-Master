"""Shared L0 harness: real SQLite/files/scheduler with a fake engine.

This is not a ComfyUI replacement.  It exercises the same service, scheduler,
storage and recovery code with an injected engine so crash windows and ordering
can be tested deterministically.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from typing import Any, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PIL import Image

from comfyui_gacha_master.backend.adapters.fake import FakeEngine
from comfyui_gacha_master.backend.config import DEFAULT_SETTINGS
from comfyui_gacha_master.backend.runtime import Runtime


def make_png(path: str, size=(40, 30), color=(120, 40, 200)) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new("RGB", size, color).save(path, "PNG")


def workflow_fixture() -> dict[str, Any]:
    api = {
        "1": {"class_type": "LoadImage", "inputs": {"image": "example.png"}},
        "2": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["3", 0],
                "seed": 1234,
                "steps": 20,
                "cfg": 7.0,
                "sampler_name": "euler",
                "scheduler": "normal",
                "positive": ["7", 0],
                "negative": ["8", 0],
                "latent_image": ["4", 0],
                "denoise": 0.65,
            },
        },
        "3": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "test.safetensors"}},
        "4": {"class_type": "VAEEncode", "inputs": {"pixels": ["1", 0], "vae": ["3", 2]}},
        "5": {"class_type": "VAEDecode", "inputs": {"samples": ["2", 0], "vae": ["3", 2]}},
        "6": {"class_type": "SaveImage", "inputs": {"images": ["5", 0], "filename_prefix": "gacha"}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "test", "clip": ["3", 1]}},
        "8": {"class_type": "CLIPTextEncode", "inputs": {"text": "negative", "clip": ["3", 1]}},
    }
    ui = {
        "last_node_id": 8,
        "last_link_id": 8,
        "nodes": [
            {"id": 1, "type": "LoadImage"},
            {"id": 2, "type": "KSampler"},
            {"id": 3, "type": "CheckpointLoaderSimple"},
            {"id": 4, "type": "VAEEncode"},
            {"id": 5, "type": "VAEDecode"},
            {"id": 6, "type": "SaveImage"},
            {"id": 7, "type": "CLIPTextEncode"},
            {"id": 8, "type": "CLIPTextEncode"},
        ],
        "links": [],
        "version": 0.4,
    }
    return {"api": api, "ui": ui}


class Harness:
    def __init__(
        self,
        *,
        outputs_per_attempt: int = 1,
        auto_complete: bool = True,
        honor_client_id: bool = True,
        targeted_cancel: bool = False,
        settings_overrides: Optional[dict[str, Any]] = None,
        root: Optional[str] = None,
    ) -> None:
        self.root = root or tempfile.mkdtemp(prefix="gacha-test-")
        os.makedirs(self.root, exist_ok=True)
        self.data_root = os.path.join(self.root, "data")
        self.results_root = os.path.join(self.root, "results")
        self.input_root = os.path.join(self.root, "comfy_input")
        self.output_root = os.path.join(self.root, "comfy_output")
        self.export_root = os.path.join(self.root, "exports")
        for path in (self.data_root, self.results_root, self.input_root, self.output_root, self.export_root):
            os.makedirs(path, exist_ok=True)
        settings = dict(DEFAULT_SETTINGS)
        settings.update(
            {
                "status_poll": 0.01,
                "submit_timeout": 2.0,
                "backup_draws": 1000000,
                "backup_interval": 1000000,
                "disk_reserve": 0,
                "allowed_input_roots": [self.input_root],
                "allowed_export_roots": [self.export_root],
                "comfy_input_root": self.input_root,
                "results_root": self.results_root,
            }
        )
        if settings_overrides:
            settings.update(settings_overrides)
        self.settings = settings
        with open(os.path.join(self.data_root, "config.json"), "w", encoding="utf-8") as handle:
            json.dump(settings, handle)
        self.engine = FakeEngine(
            honor_client_id=honor_client_id,
            targeted_cancel=targeted_cancel,
            auto_complete=auto_complete,
            outputs_per_attempt=outputs_per_attempt,
        )
        self.runtime = Runtime(
            data_root=self.data_root,
            results_root=self.results_root,
            comfy_input_root=self.input_root,
            comfy_output_root=self.output_root,
            base_url="http://127.0.0.1:1",
            engine_factory=lambda: self.engine,
            api_engine_factory=lambda: self.engine,
        )

    async def start(self) -> "Harness":
        await self.runtime.ensure_started()
        return self

    def make_sources(self, names=("a.png", "b.png", "c.png"), colors=None) -> list[dict[str, Any]]:
        colors = colors or [(200, 10, 10), (10, 200, 10), (10, 10, 200)]
        items = []
        for index, name in enumerate(names):
            path = os.path.join(self.input_root, name)
            make_png(path, color=colors[index % len(colors)])
            from comfyui_gacha_master.backend.util import sha256_file

            digest, _ = sha256_file(path)
            items.append({"root": self.input_root, "rel_path": name, "sha256": digest})
        return items

    def create_request(
        self,
        *,
        count: int = 2,
        names=("a.png", "b.png", "c.png"),
        protection_mode: str = "snapshot",
        bindings: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        fixture = workflow_fixture()
        return {
            "name": "test job",
            "protection_mode": protection_mode,
            "count_per_source": count,
            "sources": self.make_sources(names),
            "workflow_ui": fixture["ui"],
            "workflow_api": fixture["api"],
            "bindings": bindings or {"load_image_node_id": "1", "save_image_node_id": "6"},
        }

    def wait_creation(self, job_id: str, timeout: float = 120.0) -> dict[str, Any]:
        """Wait until the persisted creation plan leaves CREATING."""
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = self.runtime.service.job_detail(job_id)
            if last["status"] != "CREATING":
                return last
            time.sleep(0.01)
        return last

    def create_job(
        self,
        *,
        count: int = 2,
        names=("a.png", "b.png", "c.png"),
        protection_mode: str = "snapshot",
        bindings: Optional[dict[str, Any]] = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        request = self.create_request(
            count=count, names=names, protection_mode=protection_mode, bindings=bindings
        )
        job = self.runtime.service.create_job(request)
        return self.wait_creation(job["id"]) if wait else job

    def wait_job(self, job_id: str, statuses=("COMPLETED", "BLOCKED", "RECOVERY_REQUIRED", "PAUSED"), timeout: float = 20.0):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = self.runtime.service.job_detail(job_id)
            if last["status"] in statuses:
                return last
            time.sleep(0.02)
        return last

    def cleanup(self) -> None:
        try:
            self.runtime.close()
        finally:
            # Windows may still hold the lock file for a moment after close.
            for _ in range(10):
                try:
                    shutil.rmtree(self.root)
                    break
                except FileNotFoundError:
                    break
                except PermissionError:
                    time.sleep(0.05)
                except OSError:
                    time.sleep(0.05)


def event_loop_run(coro):
    import asyncio

    return asyncio.run(coro)
