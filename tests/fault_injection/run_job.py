"""Subprocess runner for crash-window tests.

Usage: python run_job.py <persistent-root> <mode>

The selected fault is supplied through GACHA_FAULT/GACHA_FAULT_LOG.  The
process deliberately exits with os._exit at the injected point; the parent
test then reopens the same root in a new process/runtime.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.join(ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tests"))

from harness import Harness


def write_state(root, payload):
    with open(os.path.join(root, "state.json"), "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def two_sampler_workflow(fixture):
    api = json.loads(json.dumps(fixture["api"]))
    ui = json.loads(json.dumps(fixture["ui"]))
    api["20"] = {
        "class_type": "KSampler",
        "inputs": {
            "model": ["3", 0], "seed": 99, "steps": 10, "cfg": 7.0,
            "sampler_name": "euler", "scheduler": "normal",
            "positive": ["7", 0], "negative": ["8", 0],
            "latent_image": ["2", 0], "denoise": 0.3,
        },
    }
    api["5"]["inputs"]["samples"] = ["20", 0]
    ui["nodes"].append({"id": 20, "type": "KSampler"})
    return api, ui


async def main() -> None:
    root = os.path.abspath(sys.argv[1])
    mode = sys.argv[2] if len(sys.argv) > 2 else "job"
    saved_fault = os.environ.get("GACHA_FAULT")
    if mode in {"create", "create2"}:
        # Deterministic creation crash fixture: register the JOB while the
        # worker is stopped, record its identity, then let the worker run into
        # the injected fault.
        h = Harness(
            root=root,
            settings_overrides={
                "plan_insert_batch": int(os.environ.get("GACHA_CREATE_BATCH", "1")),
                "backup_draws": 1000000,
                "backup_interval": 1000000,
            },
        )
        await h.start()
        h.runtime.creation.stop()
        count = int(os.environ.get("GACHA_CREATE_COUNT", "5"))
        if mode == "create2":
            from harness import workflow_fixture

            fixture = workflow_fixture()
            api, ui = two_sampler_workflow(fixture)
            request = {
                "name": "two sampler creation",
                "protection_mode": "snapshot",
                "count_per_source": count,
                "sources": h.make_sources(("a.png",)),
                "workflow_ui": ui,
                "workflow_api": api,
                "bindings": {"load_image_node_id": "1", "save_image_node_id": "6"},
            }
            job = h.runtime.service.create_job(request)
        else:
            job = h.create_job(count=count, names=("a.png",), wait=False)
        write_state(root, {"job_id": job["id"], "display_id": job["display_id"], "phase": "registered"})
        if saved_fault:
            os.environ["GACHA_FAULT"] = saved_fault
        h.runtime.creation.start()
        deadline = time.time() + 60
        while time.time() < deadline:
            await asyncio.sleep(0.1)
        raise SystemExit("creation fault was not triggered")
    if mode == "backup":
        # Do not let the job-created checkpoint (which is also a backup) fire
        # F12 before the test has recorded its job id; F12 targets the manual
        # backup below.
        os.environ.pop("GACHA_FAULT", None)
    h = Harness(
        root=root,
        settings_overrides={
            "backup_draws": 1,
            "backup_interval": 0,
        },
    )
    await h.start()
    job = h.create_job(count=1, names=("a.png", "b.png"))
    write_state(root, {"job_id": job["id"], "display_id": job["display_id"], "phase": "created"})
    if mode == "backup":
        if saved_fault:
            os.environ["GACHA_FAULT"] = saved_fault
        h.runtime.service.create_backup("manual")
        write_state(root, {"job_id": job["id"], "phase": "backup_done"})
        return
    if mode == "append":
        source = h.runtime.service.list_sources(job["id"])["items"][0]
        detail = h.runtime.service.job_detail(job["id"])
        h.runtime.service.append_block(
            source["id"], count=2, priority="tail", expected_version=detail["row_version"], idempotency_key="fault-append"
        )
        write_state(root, {"job_id": job["id"], "phase": "append_done"})
        return
    if mode == "export":
        h.runtime.service.start_job(job["id"])
        final = h.wait_job(job["id"], statuses=("COMPLETED",), timeout=20)
        if final["status"] != "COMPLETED":
            raise SystemExit(f"job did not complete: {final['status']} {final.get('error_message')}")
        source = h.runtime.service.list_sources(job["id"])["items"][0]
        output = h.runtime.service.list_outputs(source["id"])["items"][0]
        h.runtime.service.set_selection(output["id"], True)
        draft = h.runtime.exports.preview(
            job["id"], kind="QUICK", layout="FLAT", target_root=h.export_root
        )
        confirmed = h.runtime.exports.confirm(
            draft["id"], expected_draft_version=draft["draft_version"], confirm_cutoff=True
        )
        write_state(
            root,
            {
                "job_id": job["id"],
                "export_id": confirmed["id"],
                "batch_dir": confirmed["batch_dir"],
                "phase": "frozen",
            },
        )
        h.runtime.exports.copy_export(confirmed["id"])
        write_state(root, {"job_id": job["id"], "export_id": confirmed["id"], "phase": "copy_done"})
        return
    h.runtime.service.start_job(job["id"])
    # Let the scheduler run until the injected fault exits the process.
    deadline = time.time() + 30
    while time.time() < deadline:
        await asyncio.sleep(0.2)
    raise SystemExit("fault was not triggered")


if __name__ == "__main__":
    asyncio.run(main())
