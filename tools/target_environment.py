#!/usr/bin/env python3
"""Capture the host environment and validate the target-information package.

PU03 needs a disposable volume and PU04 needs the user's real instance,
workflow, model and images.  Neither exists yet, so the useful development is
the part that makes the gap explicit and machine-checkable:

  volumes   probe drives/volumes, admin state and controlled-volume readiness
  template  write an empty TARGET-ENVIRONMENT.json with every required field
  validate  check a filled TARGET-ENVIRONMENT.json and list exactly what is
            still missing (no guessing, no silent defaults)

A validation failure is a *missing input* result, not a product failure.

Usage::

  python tools/target_environment.py volumes --output <run>/environment/VOLUMES.json
  python tools/target_environment.py template --output <run>/environment/TARGET-ENVIRONMENT.json
  python tools/target_environment.py validate --file <run>/environment/TARGET-ENVIRONMENT.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

TOOLS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TOOLS)
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import storage_acceptance as sa  # noqa: E402

REQUIRED = {
    "instance": [
        "comfy_root", "python", "launch_command", "workdir", "base_url",
        "base_directory", "custom_nodes", "input_dir", "output_dir",
    ],
    "environment": [
        "os", "comfy_version", "comfy_commit", "frontend_version", "python_version",
        "torch_version", "cuda_version", "driver_version", "gpu", "custom_nodes_versions",
    ],
    "workflow": [
        "ui_json", "api_json", "load_image_node_id", "save_image_node_id",
        "seed_node_ids", "outputs_per_draw",
    ],
    "models_and_inputs": [
        "model_path", "model_sha256", "input_root", "representative_images",
    ],
    "load": [
        "resolution", "steps", "batch", "sources", "count_per_source",
        "outputs_per_draw", "export_layouts", "acceptable_seconds", "disk_budget_bytes",
    ],
    "data_and_install": [
        "data_root", "results_root", "export_roots", "schema_version",
        "current_plugin_source", "install_mode", "backup_path",
    ],
    "execution_scope": [
        "isolated_dir", "isolated_port", "existing_queue_policy", "maintenance_window",
        "production_operations_allowed",
    ],
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def is_admin() -> bool:
    return sa.is_admin()


def _diskpart_probe() -> dict[str, Any]:
    """Non-destructive probe: can this account list disks (i.e. create a VHD)?"""
    try:
        result = subprocess.run(
            ["diskpart"], input="list disk\n", capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}
    output = (result.stdout or "") + (result.stderr or "")
    denied = "denied" in output.casefold() or "拒绝访问" in output or result.returncode != 0
    return {
        "available": not denied,
        "returncode": result.returncode,
        "output_tail": output.strip()[-600:],
    }


def volumes(output: str) -> dict[str, Any]:
    drives: list[dict[str, Any]] = []
    if os.name == "nt":
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            root = f"{letter}:\\"
            if not os.path.exists(root):
                continue
            try:
                info = sa.volume_info(root)
            except Exception as exc:  # noqa: BLE001
                drives.append({"drive": letter, "error": f"{type(exc).__name__}: {exc}"})
                continue
            drives.append({
                "drive": letter,
                "path": root,
                "volume_root": info.get("volume_root"),
                "filesystem": info.get("filesystem"),
                "serial": info.get("serial"),
                "label": info.get("volume_label"),
                "total_bytes": info.get("total_bytes"),
                "free_bytes": info.get("free_bytes"),
                "is_system": os.path.normcase(letter) == os.path.normcase(
                    os.environ.get("SystemDrive", "C:")[:1]
                ),
                "is_project": sa.is_within(ROOT, root),
            })
    project_volume = sa.volume_info(ROOT)
    system_drive = os.environ.get("SystemDrive", "C:")
    candidates = [
        drive for drive in drives
        if not drive.get("error") and not drive.get("is_system") and not drive.get("is_project")
    ]
    payload = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "host": os.environ.get("COMPUTERNAME"),
        "os": f"{os.name} {sys.getwindowsversion().major}.{sys.getwindowsversion().build}"
              if os.name == "nt" else os.uname().sysname,
        "admin": is_admin(),
        "project_root": ROOT,
        "project_volume": {
            "volume_root": project_volume.get("volume_root"),
            "serial": project_volume.get("serial"),
        },
        "system_drive": system_drive,
        "drives": drives,
        "controlled_volume_candidates": candidates,
        "controlled_volume_available": bool(candidates),
        "diskpart": _diskpart_probe(),
        "conclusion": (
            "存在非系统、非项目卷候选；仍需操作者确认该卷可丢弃并登记序列号"
            if candidates else
            "没有非系统、非项目卷可供写满测试；需要管理员权限创建容量受控的 VHD/分区，"
            "或由操作者提供明确归本轮使用的小容量 NTFS 卷"
        ),
    }
    write_json(output, payload)
    return payload


def template(output: str) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "filled": False,
        "note": "复制本文件到他处填写；不要在本模板上假装完成。空值会被 validate 判为缺失。",
        "path_base": "absolute",
        "instance": {key: "" for key in REQUIRED["instance"]},
        "environment": {key: "" for key in REQUIRED["environment"]},
        "workflow": {
            "ui_json": "", "api_json": "", "load_image_node_id": "",
            "save_image_node_id": "", "seed_node_ids": [], "outputs_per_draw": 0,
        },
        "models_and_inputs": {
            "model_path": "", "model_sha256": "", "input_root": "",
            "representative_images": [],
        },
        "load": {
            "resolution": "", "steps": 0, "batch": 0, "sources": 0,
            "count_per_source": 0, "outputs_per_draw": 0, "export_layouts": [],
            "acceptable_seconds": 0, "disk_budget_bytes": 0,
        },
        "data_and_install": {
            "data_root": "", "results_root": "", "export_roots": [], "schema_version": 0,
            "current_plugin_source": "", "install_mode": "", "backup_path": "",
        },
        "execution_scope": {
            "isolated_dir": "", "isolated_port": 0, "existing_queue_policy": "",
            "maintenance_window": "", "production_operations_allowed": "",
        },
        "missing": [
            f"{section}.{key}" for section, keys in REQUIRED.items() for key in keys
        ],
    }
    write_json(output, payload)
    return payload


def _empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return not value
    if isinstance(value, (int, float)):
        return value == 0
    return False


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
    missing: list[str] = []
    problems: list[dict[str, str]] = []
    for section, keys in REQUIRED.items():
        values = document.get(section)
        if not isinstance(values, dict):
            missing.extend(f"{section}.{key}" for key in keys)
            continue
        for key in keys:
            if _empty(values.get(key)):
                missing.append(f"{section}.{key}")
    # Path-bearing fields are only checked when present; a missing path is
    # already reported above.
    def check_file(label: str, value: Any) -> None:
        if _empty(value):
            return
        if not os.path.isfile(str(value)):
            problems.append({"field": label, "problem": "file not found", "value": str(value)})

    def check_dir(label: str, value: Any) -> None:
        if _empty(value):
            return
        if not os.path.isdir(str(value)):
            problems.append({"field": label, "problem": "directory not found", "value": str(value)})

    instance = document.get("instance") or {}
    for key in ("comfy_root", "input_dir", "output_dir", "custom_nodes"):
        check_dir(f"instance.{key}", instance.get(key))
    check_file("instance.python", instance.get("python"))
    workflow = document.get("workflow") or {}
    for key in ("ui_json", "api_json"):
        check_file(f"workflow.{key}", workflow.get(key))
        if not _empty(workflow.get(key)) and os.path.isfile(str(workflow[key])):
            try:
                with open(workflow[key], encoding="utf-8") as handle:
                    json.load(handle)
            except (OSError, ValueError) as exc:
                problems.append({"field": f"workflow.{key}", "problem": "invalid JSON",
                                 "value": str(exc)})
    model = document.get("models_and_inputs") or {}
    check_file("models_and_inputs.model_path", model.get("model_path"))
    if not _empty(model.get("model_path")) and os.path.isfile(str(model["model_path"])):
        actual = _sha256_file(str(model["model_path"]))
        if model.get("model_sha256") and model["model_sha256"].casefold() != actual:
            problems.append({"field": "models_and_inputs.model_sha256",
                             "problem": "sha256 mismatch", "value": actual})
    check_dir("models_and_inputs.input_root", model.get("input_root"))
    for index, item in enumerate(model.get("representative_images") or []):
        if isinstance(item, dict) and item.get("path"):
            check_file(f"models_and_inputs.representative_images[{index}]", item["path"])
    data = document.get("data_and_install") or {}
    check_dir("data_and_install.data_root", data.get("data_root"))
    check_dir("data_and_install.results_root", data.get("results_root"))
    for index, root in enumerate(data.get("export_roots") or []):
        check_dir(f"data_and_install.export_roots[{index}]", root)
    check_file("data_and_install.backup_path", data.get("backup_path"))
    ready = not missing and not problems
    payload = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "file": os.path.abspath(path),
        "ready": ready,
        "result": "READY" if ready else "MISSING_INPUTS",
        "missing": missing,
        "problems": problems,
        "note": (
            "ready=true 仅表示资料齐备，可开始 PU04 隔离预验；不等于目标实例已验收。"
        ),
    }
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Host/target environment capture")
    sub = parser.add_subparsers(dest="action", required=True)
    p_volumes = sub.add_parser("volumes")
    p_volumes.add_argument("--output", required=True)
    p_template = sub.add_parser("template")
    p_template.add_argument("--output", required=True)
    p_validate = sub.add_parser("validate")
    p_validate.add_argument("--file", required=True)
    p_validate.add_argument("--report", default=None)
    args = parser.parse_args(argv)
    if args.action == "volumes":
        payload = volumes(args.output)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.action == "template":
        payload = template(args.output)
        print(json.dumps({"written": os.path.abspath(args.output),
                          "missing_fields": len(payload["missing"])}, ensure_ascii=False, indent=2))
        return 0
    if args.action == "validate":
        payload = validate(args.file)
        if args.report:
            write_json(args.report, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["ready"] else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
