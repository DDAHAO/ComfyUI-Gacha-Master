#!/usr/bin/env python3
"""Seed a deterministic READY/COMPLETED job for real-host large-list tests.

The data is produced by the product service and scheduler with the in-memory
fake engine, then left in an isolated ComfyUI base directory.  The real
ComfyUI host later loads that database and serves the same job; this is a
fixture, not evidence of real GPU inference.  The fixture summary records that
provenance explicitly.

Usage:
  python tools/host_acceptance/seed_fixture.py \
      --runtime-root test-artifacts/next-20261115/<run>/runtime-large \
      --sources 5000 --draws-per-source 2 \
      --report test-artifacts/next-20261115/<run>/host/H06-C-fixture.json
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
TESTS = os.path.join(ROOT, "tests")
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)

from PIL import Image  # noqa: E402

from comfyui_gacha_master.backend.adapters.fake import FakeEngine  # noqa: E402
from comfyui_gacha_master.backend.config import DEFAULT_SETTINGS  # noqa: E402
from comfyui_gacha_master.backend.runtime import Runtime  # noqa: E402
from comfyui_gacha_master.backend.util import sha256_file  # noqa: E402
from harness import workflow_fixture  # noqa: E402


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def make_source(path: str, index: int) -> dict[str, Any]:
    color = ((index * 37) % 256, (index * 67) % 256, (index * 97) % 256)
    Image.new("RGB", (40, 30), color).save(path, "PNG")
    digest, _ = sha256_file(path)
    return {"root": os.path.dirname(path), "rel_path": os.path.basename(path), "sha256": digest}


async def seed(args) -> dict[str, Any]:
    runtime_root = os.path.abspath(args.runtime_root)
    data_root = os.path.join(runtime_root, "user", "gacha_master")
    results_root = os.path.join(runtime_root, "output", "gacha_master", "Jobs")
    input_root = os.path.join(runtime_root, "input")
    output_root = os.path.join(runtime_root, "output")
    for path in (data_root, results_root, input_root, output_root):
        os.makedirs(path, exist_ok=True)
    settings = dict(DEFAULT_SETTINGS)
    settings.update({
        "status_poll": 0.01,
        "submit_timeout": 5.0,
        "backup_draws": 1000000,
        "backup_interval": 1000000,
        "disk_reserve": 0,
        "allowed_input_roots": [input_root],
        "allowed_export_roots": [os.path.join(output_root, "gacha_master", "Exports")],
        "comfy_input_root": input_root,
        "results_root": results_root,
    })
    os.makedirs(os.path.join(output_root, "gacha_master", "Exports"), exist_ok=True)
    with open(os.path.join(data_root, "config.json"), "w", encoding="utf-8") as handle:
        json.dump(settings, handle)
    started = time.time()
    sources = []
    for index in range(1, args.sources + 1):
        path = os.path.join(input_root, f"fixture-{index:04d}.png")
        sources.append(make_source(path, index))
    print(f"sources prepared: {len(sources)} in {time.time() - started:.1f}s", flush=True)

    engine = FakeEngine(outputs_per_attempt=args.outputs_per_attempt)
    runtime = Runtime(
        data_root=data_root,
        results_root=results_root,
        comfy_input_root=input_root,
        comfy_output_root=output_root,
        base_url="http://127.0.0.1:1",
        engine_factory=lambda: engine,
        api_engine_factory=lambda: engine,
    )
    await runtime.ensure_started()
    fixture = workflow_fixture()
    request = {
        "name": f"LARGE FIXTURE {args.sources}x{args.draws_per_source}",
        "protection_mode": "snapshot",
        "count_per_source": args.draws_per_source,
        "sources": sources,
        "workflow_ui": fixture["ui"],
        "workflow_api": fixture["api"],
        "bindings": {"load_image_node_id": "1", "save_image_node_id": "6"},
    }
    try:
        job = runtime.service.create_job(request)
        job_id = job["id"]
        print(f"job created: {job_id} status={job['status']}", flush=True)
        creation = None
        deadline = time.time() + args.creation_timeout
        last_print = 0
        while time.time() < deadline:
            detail = runtime.service.job_detail(job_id)
            creation = detail.get("creation") or {}
            if detail["status"] != "CREATING":
                break
            if time.time() - last_print >= 10:
                print(
                    f"creating: {creation.get('draws_committed')}/{creation.get('draws_total')}",
                    flush=True,
                )
                last_print = time.time()
            await asyncio.sleep(0.1)
        detail = runtime.service.job_detail(job_id)
        if detail["status"] not in ("READY", "COMPLETED"):
            return {
                "result": "FAIL",
                "phase": "creation",
                "job_id": job_id,
                "status": detail["status"],
                "creation": creation,
                "elapsed_seconds": round(time.time() - started, 1),
            }
        runtime.service.start_job(job_id)
        print("inference started (fake engine)", flush=True)
        deadline = time.time() + args.run_timeout
        last_print = 0
        while time.time() < deadline:
            detail = runtime.service.job_detail(job_id)
            if detail["status"] in ("COMPLETED", "BLOCKED", "RECOVERY_REQUIRED", "PAUSED"):
                break
            if time.time() - last_print >= 10:
                summary = detail.get("summary") or {}
                print(
                    f"running: {detail['status']} succeeded={summary.get('succeeded')} "
                    f"out={summary.get('out_total')}",
                    flush=True,
                )
                last_print = time.time()
            await asyncio.sleep(0.1)
        detail = runtime.service.job_detail(job_id)
        result = {
            "schema_version": 1,
            "result": "PASS" if detail["status"] == "COMPLETED" else "FAIL",
            "provenance": {"data_source": "fixture", "environment": "isolated", "host": "real"},
            "job_id": job_id,
            "display_id": detail.get("display_id"),
            "status": detail["status"],
            "sources": args.sources,
            "draws_per_source": args.draws_per_source,
            "outputs_per_attempt": args.outputs_per_attempt,
            "summary": detail.get("summary"),
            "creation": detail.get("creation"),
            "runtime_root": runtime_root,
            "data_root": data_root,
            "results_root": results_root,
            "input_root": input_root,
            "elapsed_seconds": round(time.time() - started, 1),
            "generated_at": utc_now(),
        }
        return result
    finally:
        runtime.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed a large fake-engine fixture for real-host UI tests")
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--sources", type=int, default=5000)
    parser.add_argument("--draws-per-source", type=int, default=2)
    parser.add_argument("--outputs-per-attempt", type=int, default=1)
    parser.add_argument("--creation-timeout", type=float, default=1800)
    parser.add_argument("--run-timeout", type=float, default=3600)
    parser.add_argument("--report", required=True)
    args = parser.parse_args(argv)
    if args.sources < 1 or args.draws_per_source < 1:
        print(json.dumps({"result": "FAIL", "error": "sources and draws must be positive"}))
        return 2
    result = asyncio.run(seed(args))
    write_json(args.report, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("result") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
