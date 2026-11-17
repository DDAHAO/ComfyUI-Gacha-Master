#!/usr/bin/env python3
"""Production acceptance runner for ComfyUI Gacha Master.

The tool connects to an already running ComfyUI instance, creates jobs through
the plugin API and independently verifies the persisted DRAW/ATTEMPT/OUT/seed
evidence against SQLite, the attempt manifests and the files on disk.  It never
installs ComfyUI, PyTorch, models or the plugin.

Examples:
  # Validate paths, capabilities and workflow without creating a job.
  python tools/production_acceptance.py --dry-run \
      --base-url http://127.0.0.1:8188 \
      --data-root D:\\ComfyUI\\user\\gacha_master \
      --workflow-api examples\\workflow_api.json \
      --workflow-ui examples\\workflow_ui.json \
      --input-root D:\\production-sources

  # Run the legacy aliases plus a single-source 101-OUT pagination scenario.
  python tools/production_acceptance.py \
      --base-url http://127.0.0.1:8296 \
      --data-root ... --workflow-api ... --workflow-ui ... --input-root ... \
      --scenarios 1x1,3x5,555,1x101 --data-source real_gpu

Scenario tokens are ``NxM`` (positive source count x draws per source).  The
aliases ``1x1``, ``3x5`` and ``555`` (111 sources x 5 draws) are preserved.  A
JSON scenario file can carry reusable definitions, including the expected
number of OUT files per DRAW for workflows that save more than one image.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Mapping

try:
    from PIL import Image
except ImportError:  # reported by the dry-run environment check
    Image = None

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
TERMINAL = {"COMPLETED", "BLOCKED", "RECOVERY_REQUIRED", "PAUSED", "FAILED"}
ALIASES = {"1x1": (1, 1), "3x5": (3, 5), "555": (111, 5)}
SCENARIO_RE = re.compile(r"^(\d+)x(\d+)$")
EXIT_FAIL = 1
EXIT_USAGE = 2


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def natural_key(name: str):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


def atomic_json(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class AcceptanceError(RuntimeError):
    def __init__(
        self,
        phase: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
        exit_code: int = EXIT_USAGE,
    ):
        super().__init__(message)
        self.phase = phase
        self.message = message
        self.details = dict(details or {})
        self.exit_code = exit_code


def api(base: str, path: str, method: str = "GET", body: Any = None, key: str | None = None) -> tuple[int, Any]:
    """Call a JSON endpoint and surface transport/protocol failures.

    HTTP error statuses are returned to the caller (the plugin uses them for
    business errors).  Empty bodies and non-JSON bodies are protocol failures
    and become structured acceptance errors.
    """
    url = base.rstrip("/") + path
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if key:
        headers["Idempotency-Key"] = key
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            status = response.status
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AcceptanceError(
            "http",
            "HTTP 请求失败",
            details={"url": url, "method": method, "error": f"{type(exc).__name__}: {exc}"},
        ) from exc
    text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        raise AcceptanceError(
            "http",
            "HTTP 响应为空",
            details={"url": url, "method": method, "status": status},
        )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AcceptanceError(
            "http",
            "HTTP 响应不是 JSON",
            details={
                "url": url,
                "method": method,
                "status": status,
                "body_prefix": text[:300],
                "error": str(exc),
            },
        ) from exc
    return status, payload


def parse_scenario(token: str) -> dict[str, Any]:
    token = token.strip()
    if not token:
        raise AcceptanceError("arguments", "场景名不能为空")
    if token in ALIASES:
        sources, count = ALIASES[token]
        return {
            "label": token,
            "sources": sources,
            "count_per_source": count,
            "outputs_per_draw": 1,
            "description": "legacy alias",
        }
    match = SCENARIO_RE.fullmatch(token)
    if not match:
        raise AcceptanceError(
            "arguments",
            "场景必须是 NxM（源图数 x 每源 DRAW 数）或别名 1x1/3x5/555",
            details={"scenario": token},
        )
    sources, count = int(match.group(1)), int(match.group(2))
    if sources < 1 or count < 1:
        raise AcceptanceError(
            "arguments",
            "场景的源图数和每源 DRAW 数必须是正整数",
            details={"scenario": token, "sources": sources, "count_per_source": count},
        )
    return {
        "label": token,
        "sources": sources,
        "count_per_source": count,
        "outputs_per_draw": 1,
        "description": "explicit NxM token",
    }


def load_scenario_file(path: str) -> list[dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise AcceptanceError(
            "arguments", "无法读取场景文件", details={"path": path, "error": str(exc)}
        ) from exc
    except json.JSONDecodeError as exc:
        raise AcceptanceError(
            "arguments", "场景文件不是合法 JSON", details={"path": path, "error": str(exc)}
        ) from exc
    definitions = payload.get("scenarios") if isinstance(payload, dict) and "scenarios" in payload else payload
    if isinstance(definitions, list):
        definitions = {str(item): {} for item in definitions}
    if not isinstance(definitions, dict) or not definitions:
        raise AcceptanceError("arguments", "场景文件必须包含非空的 scenarios 对象或名称列表", details={"path": path})
    scenarios = []
    for label, spec in definitions.items():
        if isinstance(spec, str):
            parsed = parse_scenario(spec)
            parsed["label"] = str(label)
            scenarios.append(parsed)
            continue
        if not isinstance(spec, dict):
            raise AcceptanceError(
                "arguments", "场景定义必须是对象", details={"scenario": label, "definition": spec}
            )
        sources = spec.get("sources")
        count = spec.get("count_per_source")
        if not isinstance(sources, int) or not isinstance(count, int) or sources < 1 or count < 1:
            raise AcceptanceError(
                "arguments",
                "场景定义需要正整数 sources 和 count_per_source",
                details={"scenario": label, "definition": spec},
            )
        outputs_per_draw = spec.get("outputs_per_draw", 1)
        if not isinstance(outputs_per_draw, int) or outputs_per_draw < 1:
            raise AcceptanceError(
                "arguments",
                "outputs_per_draw 必须是正整数",
                details={"scenario": label, "outputs_per_draw": outputs_per_draw},
            )
        if str(label) in ALIASES and ALIASES[str(label)] != (sources, count):
            raise AcceptanceError(
                "arguments",
                "场景文件不得改变保留别名的含义",
                details={"scenario": label, "alias": ALIASES[str(label)], "definition": spec},
            )
        scenarios.append(
            {
                "label": str(label),
                "sources": sources,
                "count_per_source": count,
                "outputs_per_draw": outputs_per_draw,
                "description": spec.get("description") or "scenario file",
            }
        )
    return scenarios


class Runner:
    def __init__(self, args):
        self.args = args
        # The plugin resolves input roots relative to the ComfyUI process, so
        # every path this tool sends or reads must be absolute.
        for attribute in (
            "data_root", "results_root", "workflow_api", "workflow_ui",
            "input_root", "comfy_root", "source_root", "installed_plugin_root",
            "scenario_file",
        ):
            value = getattr(args, attribute, None)
            if value:
                setattr(args, attribute, os.path.abspath(value))
        self.base = args.base_url.rstrip("/")
        self.output_dir = os.path.abspath(
            args.output_dir
            or os.path.join("test-artifacts", "production-acceptance-" + datetime.now().strftime("%Y%m%dT%H%M%S"))
        )
        os.makedirs(self.output_dir, exist_ok=True)
        self.run_id = args.run_id or datetime.now().strftime("%Y%m%dT%H%M%S")
        self.progress_path = os.path.abspath(
            args.progress_file or os.path.join(self.output_dir, "PROGRESS.json")
        )
        self.summary_path = os.path.join(self.output_dir, "SUMMARY.json")
        self.current_phase = "init"
        self.summary: dict[str, Any] = {
            "schema_version": 2,
            "run_id": self.run_id,
            "started_at": utc_now(),
            "base_url": self.base,
            "output_dir": self.output_dir,
            "dry_run": args.dry_run,
            "provenance": {
                "data_source": args.data_source,
                "environment": args.environment,
                "host": args.host,
            },
            "phase": self.current_phase,
            "scenarios": {},
            "side_effects": {"jobs_created": 0, "inference_started": False},
        }
        self.sources: list[dict[str, Any]] = []
        self.workflow_api: dict[str, Any] = {}
        self.workflow_ui: dict[str, Any] = {}
        self.scenarios: list[dict[str, Any]] = []

    # ------------------------------------------------------------ evidence
    def save(self):
        self.summary["phase"] = self.current_phase
        self.summary["updated_at"] = utc_now()
        atomic_json(self.summary_path, self.summary)

    def progress(self, phase: str, **details: Any):
        self.current_phase = phase
        self.summary["phase"] = phase
        progress = {
            "run_id": self.run_id,
            "pid": os.getpid(),
            "phase": phase,
            "updated_at": utc_now(),
            **details,
        }
        atomic_json(self.progress_path, progress)
        self.save()

    def require(self, condition: bool, message: str, **details: Any):
        if not condition:
            raise AcceptanceError(self.current_phase, message, details=details)

    def record_failure(self, exc: BaseException) -> int:
        if isinstance(exc, AcceptanceError):
            error = {
                "phase": exc.phase,
                "type": type(exc).__name__,
                "message": exc.message,
                "details": exc.details,
            }
            self.current_phase = exc.phase
            exit_code = exc.exit_code
        else:
            error = {
                "phase": self.current_phase,
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            }
            exit_code = EXIT_USAGE
        self.summary["result"] = "FAIL"
        self.summary["error"] = error
        self.summary["finished_at"] = utc_now()
        self.save()
        atomic_json(
            self.progress_path,
            {
                "run_id": self.run_id,
                "pid": os.getpid(),
                "phase": self.current_phase,
                "failed": True,
                "error": error,
                "updated_at": utc_now(),
            },
        )
        return exit_code

    # --------------------------------------------------------------- setup
    def load_inputs(self):
        a = self.args
        self.progress("load_inputs")
        try:
            with open(a.workflow_api, encoding="utf-8") as handle:
                self.workflow_api = json.load(handle)
            with open(a.workflow_ui, encoding="utf-8") as handle:
                self.workflow_ui = json.load(handle)
        except OSError as exc:
            raise AcceptanceError(
                "load_inputs", "无法读取工作流文件", details={"error": str(exc)}
            ) from exc
        except json.JSONDecodeError as exc:
            raise AcceptanceError(
                "load_inputs", "工作流文件不是合法 JSON", details={"error": str(exc)}
            ) from exc
        self.require(isinstance(self.workflow_api, dict), "API 工作流必须是 JSON 对象")
        self.require(isinstance(self.workflow_ui, dict), "UI 工作流必须是 JSON 对象")
        self.require(os.path.isdir(a.input_root), "输入目录不存在", path=a.input_root)
        try:
            names = [
                name for name in os.listdir(a.input_root)
                if os.path.isfile(os.path.join(a.input_root, name))
                and os.path.splitext(name)[1].lower() in IMAGE_EXTENSIONS
            ]
        except OSError as exc:
            raise AcceptanceError(
                "load_inputs", "无法读取输入目录", details={"path": a.input_root, "error": str(exc)}
            ) from exc
        names.sort(key=natural_key)
        self.require(bool(names), "输入目录没有支持的图片", path=a.input_root)
        for name in names:
            path = os.path.join(a.input_root, name)
            self.sources.append({"root": a.input_root, "rel_path": name, "sha256": sha256_file(path)})
        self.summary["inputs"] = {
            "root": a.input_root,
            "image_count": len(self.sources),
            "first": [item["rel_path"] for item in self.sources[:5]],
            "workflow_api": a.workflow_api,
            "workflow_ui": a.workflow_ui,
            "workflow_ui_sha256": sha256_file(a.workflow_ui),
            "workflow_api_sha256": sha256_file(a.workflow_api),
            "input_bytes": sum(
                os.path.getsize(os.path.join(item["root"], item["rel_path"])) for item in self.sources
            ),
        }
        self.summary["environment_type"] = a.environment
        self.progress("load_inputs", image_count=len(self.sources))

    def code_fingerprint(self) -> dict[str, Any]:
        a = self.args
        source_root = os.path.abspath(a.source_root or os.path.dirname(os.path.dirname(__file__)))
        tools_dir = os.path.join(source_root, "tools")
        if tools_dir not in sys.path:
            sys.path.insert(0, tools_dir)
        try:
            import candidate_manifest as cm
        except ImportError as exc:  # pragma: no cover - packaging error
            return {"error": f"candidate_manifest unavailable: {exc}"}
        manifest = cm.build_manifest(source_root)
        info: dict[str, Any] = {
            "source_root": source_root,
            "manifest_fingerprint": manifest["fingerprint"],
            "scopes": {
                name: {
                    "file_count": scope["file_count"],
                    "tree_sha256": scope["tree_sha256"],
                }
                for name, scope in manifest["scopes"].items()
            },
        }
        try:
            info["git_commit"] = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=source_root,
                capture_output=True, text=True, timeout=20
            ).stdout.strip() or None
            status = subprocess.run(
                ["git", "status", "--porcelain"], cwd=source_root,
                capture_output=True, text=True, timeout=20
            ).stdout.splitlines()
            info["git_dirty_entries"] = len(status)
        except Exception as exc:
            info["git_error"] = str(exc)
        installed = a.installed_plugin_root
        if installed:
            report = cm.installed_report(source_root, installed)
            info["installed"] = report
            runtime = report["runtime"]
            info["installed_plugin_root"] = os.path.abspath(installed)
            info["installed_checked"] = runtime["expected_count"]
            info["installed_mismatches"] = sorted(
                set(runtime["missing"]) | set(runtime["extra"]) | set(runtime["changed"])
            )[:50]
            info["installed_tree_matches"] = report["ok"]
            info["installed_extra_runtime_files"] = report["extra_runtime_files"]
            info["installed_runtime_tree_sha256"] = report["runtime_tree_sha256"]
        return info

    def check_environment(self):
        a = self.args
        self.progress("health")
        status, health = api(self.base, "/gacha_master/v1/health")
        self.require(
            status == 200 and health.get("data", {}).get("db", {}).get("ok") is True,
            "ComfyUI Gacha Master health 不可用",
            status=status,
            body=health,
        )
        status, caps = api(self.base, "/gacha_master/v1/capabilities")
        self.require(status == 200, "capabilities 不可用", status=status, body=caps)
        self.summary["health"] = health["data"]
        self.summary["capabilities"] = caps.get("data", caps)
        self.summary["limits"] = (caps.get("data", caps) or {}).get("limits") or {}
        self.summary["paths"] = {
            "data_root": a.data_root,
            "results_root": a.results_root,
            "comfy_root": a.comfy_root,
        }

        self.progress("preflight")
        status, preflight = api(self.base, "/gacha_master/v1/preflight", "POST", {
            "workflow_ui": self.workflow_ui,
            "workflow_api": self.workflow_api,
            "bindings": {"load_image_node_id": a.load_node, "save_image_node_id": a.save_node},
        })
        self.require(
            status == 200 and preflight.get("data", {}).get("ok") is True,
            "工作流预检失败",
            status=status,
            body=preflight,
        )
        self.summary["preflight"] = preflight["data"]
        seed_bindings = (preflight.get("data") or {}).get("seed_bindings") or []
        self.summary["seed_binding_count"] = len(seed_bindings) or 1

        if a.comfy_root:
            checkpoints = []
            for node in self.workflow_api.values():
                if isinstance(node, dict) and node.get("class_type") == "CheckpointLoaderSimple":
                    name = (node.get("inputs") or {}).get("ckpt_name")
                    if isinstance(name, str):
                        path = os.path.join(a.comfy_root, "models", "checkpoints", name.replace("/", os.sep))
                        checkpoints.append({"name": name, "path": path, "exists": os.path.isfile(path),
                                            "size": os.path.getsize(path) if os.path.isfile(path) else None})
            self.require(
                all(item["exists"] for item in checkpoints),
                "工作流引用的 checkpoint 不存在",
                checkpoints=checkpoints,
            )
            self.summary["checkpoints"] = checkpoints
        if Image is None:
            raise AcceptanceError("environment", "Pillow 未安装，无法校验 PNG；请使用项目 venv 运行。")
        self.progress("environment_ready")

    def load_scenarios(self):
        a = self.args
        scenarios: list[dict[str, Any]] = []
        if a.scenario_file:
            scenarios.extend(load_scenario_file(a.scenario_file))
        tokens = a.scenarios
        if tokens is None:
            tokens = None if a.scenario_file else "1x1,3x5,555"
        if tokens:
            for token in [item.strip() for item in tokens.split(",") if item.strip()]:
                scenarios.append(parse_scenario(token))
        if not scenarios:
            raise AcceptanceError("arguments", "没有可执行的场景")
        seen = set()
        unique = []
        for scenario in scenarios:
            if scenario["label"] in seen:
                # A duplicate label would overwrite evidence; reject instead of
                # silently keeping the last definition.
                raise AcceptanceError(
                    "arguments", "场景标签重复", details={"scenario": scenario["label"]}
                )
            seen.add(scenario["label"])
            unique.append(scenario)
        plan_limit = int((self.summary.get("limits") or {}).get("plan_limit") or 100000)
        for scenario in unique:
            total = scenario["sources"] * scenario["count_per_source"]
            if scenario["sources"] > 5000:
                raise AcceptanceError(
                    "arguments", "单次任务原图数量超过产品上限",
                    details={"scenario": scenario["label"], "sources": scenario["sources"], "limit": 5000},
                )
            if total > plan_limit:
                raise AcceptanceError(
                    "arguments", "场景超过计划的 DRAW 上限",
                    details={"scenario": scenario["label"], "draws": total, "limit": plan_limit},
                )
            if scenario["sources"] > len(self.sources):
                raise AcceptanceError(
                    "arguments", "输入图片不足",
                    details={
                        "scenario": scenario["label"],
                        "required": scenario["sources"],
                        "available": len(self.sources),
                    },
                )
            scenario["expected_draws"] = total
            scenario["expected_outputs"] = total * scenario["outputs_per_draw"]
            scenario["seed_bindings"] = int(self.summary.get("seed_binding_count") or 1)
        self.scenarios = unique
        self.summary["requested_scenarios"] = unique
        self.progress("scenarios_ready", scenarios=[item["label"] for item in unique])

    # --------------------------------------------------------------- jobs
    def create_and_run(self, scenario: dict[str, Any]):
        a = self.args
        label = scenario["label"]
        source_count = scenario["sources"]
        count_per_source = scenario["count_per_source"]
        expected_draws = scenario["expected_draws"]
        expected_outputs = scenario["expected_outputs"]
        self.progress("create_job", scenario=label)
        self.require(len(self.sources) >= source_count,
                     "输入图片不足", scenario=label, required=source_count, available=len(self.sources))
        sources = self.sources[:source_count]
        body = {
            "name": f"PRODUCTION {label} {self.run_id}",
            "protection_mode": "snapshot",
            "count_per_source": count_per_source,
            "sources": sources,
            "workflow_ui": self.workflow_ui,
            "workflow_api": self.workflow_api,
            "bindings": {"load_image_node_id": a.load_node, "save_image_node_id": a.save_node},
        }
        key = f"production-{self.run_id}-{label}"
        status, created = api(self.base, "/gacha_master/v1/jobs", "POST", body, key)
        self.require(status in (200, 201), "创建任务失败", scenario=label, status=status, body=created)
        self.summary["side_effects"]["jobs_created"] += 1
        job = created["data"]
        job_id = job["id"]
        self.require(
            job.get("status") in ("CREATING", "READY"),
            "创建响应状态无效",
            scenario=label,
            status=job.get("status"),
            creation=job.get("creation"),
        )
        if job.get("status") == "CREATING":
            # POST /jobs only proves the request checkpoint is recorded.  The
            # plan is produced by the bounded worker and must reach READY
            # before this tool is allowed to request inference.
            self.progress("creating", scenario=label, job_id=job_id)
            creation_deadline = time.time() + a.creation_timeout
            while time.time() < creation_deadline:
                status, payload = api(self.base, f"/gacha_master/v1/jobs/{job_id}")
                self.require(status == 200, "创建进度读取失败", scenario=label, status=status, body=payload)
                job = payload["data"]
                creation = job.get("creation") or {}
                if job["status"] != "CREATING":
                    break
                if creation.get("stage") in ("FAILED", "ABANDONED", "PAUSED", "PAUSING"):
                    self.require(
                        False,
                        "创建计划未就绪",
                        scenario=label,
                        stage=creation.get("stage"),
                        error=creation.get("error_message"),
                        progress=creation,
                    )
                time.sleep(a.poll_interval)
            self.require(
                job.get("status") == "READY",
                "等待创建计划超时",
                scenario=label,
                timeout=a.creation_timeout,
                creation=job.get("creation"),
                summary=job.get("summary"),
            )
        self.summary["scenarios"].setdefault(label, {})["creation"] = {
            "final_status": job.get("status"),
            "stage": (job.get("creation") or {}).get("stage"),
            "draws_committed": (job.get("creation") or {}).get("draws_committed"),
            "draws_total": (job.get("creation") or {}).get("draws_total"),
        }
        # Only now is inference explicitly authorized.
        self.progress("start_job", scenario=label, job_id=job_id)
        status, started = api(self.base, f"/gacha_master/v1/jobs/{job_id}/start", "POST", {})
        self.require(status in (200, 202), "启动任务失败", scenario=label, status=status, body=started)
        self.summary["side_effects"]["inference_started"] = True

        self.progress("running", scenario=label, job_id=job_id)
        deadline = time.time() + a.timeout
        detail = None
        while time.time() < deadline:
            status, payload = api(self.base, f"/gacha_master/v1/jobs/{job_id}")
            if status == 200:
                detail = payload["data"]
                if detail["status"] in TERMINAL:
                    break
            time.sleep(a.poll_interval)
        self.require(detail is not None, "任务轮询失败", scenario=label)
        if detail["status"] != "COMPLETED":
            self.require(
                False,
                "任务未完成",
                scenario=label,
                status=detail["status"],
                error_code=detail.get("error_code"),
                error_message=detail.get("error_message"),
                summary=detail.get("summary"),
            )
        self.require(
            detail["summary"]["succeeded"] == expected_draws,
            "成功 DRAW 数不符",
            scenario=label,
            expected=expected_draws,
            summary=detail["summary"],
        )

        self.progress("verify", scenario=label, job_id=job_id)
        verification = self.verify_job(
            job_id,
            expected_draws,
            scenario["seed_bindings"],
            source_count=source_count,
            count_per_source=count_per_source,
            expected_outputs_per_draw=scenario["outputs_per_draw"],
        )
        result = {
            "job_id": job_id,
            "display_id": job["display_id"],
            "expected_draws": expected_draws,
            "expected_outputs": expected_outputs,
            "source_count": source_count,
            "count_per_source": count_per_source,
            "outputs_per_draw": scenario["outputs_per_draw"],
            "final_status": detail["status"],
            "creation": job.get("creation") or detail.get("creation"),
            "summary": detail["summary"],
            "verification": verification,
            "result": "PASS" if verification["ok"] else "FAIL",
        }
        self.summary["scenarios"][label] = result
        self.save()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        self.require(verification["ok"], "数据核验失败", scenario=label, verification=verification)
        self.progress("scenario_passed", scenario=label)
        return result

    # ------------------------------------------------------------- verify
    def verify_job(
        self,
        job_id: str,
        expected_draws: int,
        seed_bindings: int,
        *,
        source_count: int | None = None,
        count_per_source: int | None = None,
        expected_outputs_per_draw: int = 1,
    ) -> dict[str, Any]:
        db_path = os.path.join(self.args.data_root, "db", "gacha.sqlite3")
        self.require(os.path.isfile(db_path), "数据库不存在", path=db_path)
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error as exc:
            raise AcceptanceError(
                "verify", "无法打开数据库", details={"path": db_path, "error": str(exc)}
            ) from exc
        try:
            draws = [dict(row) for row in conn.execute(
                """
                SELECT d.id, d.draw_no, d.status AS draw_status,
                       d.accepted_attempt_id, d.row_version,
                       s.source_order, s.id AS source_id, s.sha256 AS source_sha256
                FROM draws d JOIN sources s ON s.id=d.source_id
                WHERE d.job_id=? ORDER BY s.source_order, d.draw_no
                """, (job_id,))]
            attempts = [dict(row) for row in conn.execute(
                """
                SELECT a.*, d.draw_no, s.source_order
                FROM attempts a JOIN draws d ON d.id=a.draw_id JOIN sources s ON s.id=d.source_id
                WHERE d.job_id=? ORDER BY a.rowid
                """, (job_id,))]
            seeds = [dict(row) for row in conn.execute(
                "SELECT draw_id, node_id, input_name, seed FROM seed_allocations WHERE job_id=?",
                (job_id,))]
            outputs = [dict(row) for row in conn.execute(
                """
                SELECT o.*, d.draw_no, s.source_order, a.attempt_no, a.id AS attempt_id,
                       a.attempt_dir, a.actual_prompt_id
                FROM outputs o JOIN attempts a ON a.id=o.attempt_id
                JOIN draws d ON d.id=a.draw_id JOIN sources s ON s.id=d.source_id
                WHERE d.job_id=? ORDER BY s.source_order, d.draw_no, o.out_no
                """, (job_id,))]
        finally:
            conn.close()

        problems: list[str] = []
        sources_expected = source_count or (max((int(row["source_order"]) for row in draws), default=1))
        draws_expected = count_per_source or 1
        expected_sequence = [
            (source, draw)
            for source in range(1, sources_expected + 1)
            for draw in range(1, draws_expected + 1)
        ]
        sequence = [(int(row["source_order"]), int(row["draw_no"])) for row in draws]
        expected_output_count = expected_draws * max(0, expected_outputs_per_draw)
        if len(draws) != expected_draws:
            problems.append(f"draws={len(draws)} expected={expected_draws}")
        if sequence != expected_sequence:
            problems.append("draw/source sequence mismatch")
        if len(attempts) != expected_draws:
            problems.append(f"attempts={len(attempts)} expected={expected_draws}")
        if len(seeds) != expected_draws * seed_bindings:
            problems.append(f"seeds={len(seeds)} expected={expected_draws * seed_bindings}")
        if len({int(row["seed"]) for row in seeds}) != len(seeds):
            problems.append("duplicate seeds")
        if len({row["actual_prompt_id"] for row in attempts}) != len(attempts):
            problems.append("duplicate prompt ids")
        if any(row["status"] != "SUCCEEDED" for row in attempts):
            problems.append("non-succeeded attempt")
        if expected_outputs_per_draw and len(outputs) != expected_output_count:
            problems.append(f"outputs={len(outputs)} expected={expected_output_count}")

        attempts_by_draw: dict[str, list[dict[str, Any]]] = {}
        for attempt in attempts:
            attempts_by_draw.setdefault(attempt["draw_id"], []).append(attempt)
        seeds_by_draw: dict[str, int] = {}
        for seed in seeds:
            seeds_by_draw[seed["draw_id"]] = seeds_by_draw.get(seed["draw_id"], 0) + 1
        outputs_by_attempt: dict[str, list[dict[str, Any]]] = {}
        for output in outputs:
            outputs_by_attempt.setdefault(output["attempt_id"], []).append(output)

        accepted_checks = []
        for draw in draws:
            draw_attempts = attempts_by_draw.get(draw["id"], [])
            accepted = draw.get("accepted_attempt_id")
            accepted_ok = (
                len(draw_attempts) == 1
                and accepted == draw_attempts[0]["id"]
                and draw["draw_status"] == "SUCCEEDED"
            )
            if not accepted_ok:
                problems.append(f"accepted relation invalid for draw {draw['draw_no']}")
            if seeds_by_draw.get(draw["id"], 0) != seed_bindings:
                problems.append(f"seed count invalid for draw {draw['draw_no']}")
            accepted_count = len(outputs_by_attempt.get(accepted or "", []))
            if expected_outputs_per_draw and accepted_count != expected_outputs_per_draw:
                problems.append(
                    f"accepted outputs={accepted_count} expected={expected_outputs_per_draw} for draw {draw['draw_no']}"
                )
            accepted_checks.append(
                {
                    "draw_id": draw["id"],
                    "source_order": int(draw["source_order"]),
                    "draw_no": int(draw["draw_no"]),
                    "attempts": len(draw_attempts),
                    "accepted_attempt_id": accepted,
                    "accepted_ok": accepted_ok,
                    "seed_count": seeds_by_draw.get(draw["id"], 0),
                    "accepted_outputs": accepted_count,
                }
            )

        root = self.args.results_root
        if not root:
            root = os.path.join(os.path.dirname(self.args.data_root), "..", "output", "gacha_master", "Jobs")
        root = os.path.abspath(root)
        output_checks = []
        for output in outputs:
            path = os.path.join(root, *str(output["rel_path"]).split("/"))
            exists = os.path.isfile(path)
            hash_ok = exists and sha256_file(path) == output["sha256"]
            size_ok = exists and os.path.getsize(path) == int(output["size"])
            decode_ok = False
            width = height = None
            if exists:
                try:
                    with Image.open(path) as image:
                        image.load()
                        width, height = image.size
                    decode_ok = True
                except Exception:
                    decode_ok = False
            if output["health"] != "VERIFIED" or not (exists and hash_ok and size_ok and decode_ok):
                problems.append(f"bad output {output['rel_path']}")
            output_checks.append({"rel_path": output["rel_path"], "health": output["health"],
                                  "exists": exists, "hash_ok": hash_ok, "size_ok": size_ok,
                                  "decode_ok": decode_ok, "width": width, "height": height})

        manifests = []
        for attempt in attempts:
            path = os.path.join(attempt["attempt_dir"], "outputs.complete.json")
            exists = os.path.isfile(path)
            manifest_ok = False
            count = None
            if exists:
                try:
                    with open(path, encoding="utf-8") as handle:
                        manifest = json.load(handle)
                except (OSError, json.JSONDecodeError):
                    manifest = None
                if isinstance(manifest, dict):
                    count = int(manifest.get("n") or 0)
                    entries = manifest.get("outputs") or []
                    actual = outputs_by_attempt.get(attempt["id"], [])
                    identity = manifest.get("identity") or {}
                    manifest_ok = (
                        manifest.get("attempt_id") == attempt["id"]
                        and count == len(entries)
                        and count == len(actual)
                        and identity.get("job_id") == job_id
                        and identity.get("source_order") == int(attempt["source_order"])
                        and identity.get("draw_no") == int(attempt["draw_no"])
                        and identity.get("attempt_no") == int(attempt["attempt_no"])
                        and {
                            (int(entry.get("out_no", -1)), entry.get("sha256"), int(entry.get("size", -1)))
                            for entry in entries
                        }
                        == {
                            (int(item["out_no"]), item["sha256"], int(item["size"]))
                            for item in actual
                        }
                    )
            if not exists or not manifest_ok:
                problems.append(f"bad manifest {attempt['attempt_dir']}")
            manifests.append({"attempt_id": attempt["id"], "exists": exists, "n": count, "ok": manifest_ok})

        for source in self.sources[:sources_expected]:
            path = os.path.join(source["root"], source["rel_path"])
            if not os.path.isfile(path) or sha256_file(path) != source["sha256"]:
                problems.append(f"source changed {source['rel_path']}")

        return {
            "ok": not problems,
            "problems": problems[:100],
            "independent": True,
            "draws": len(draws),
            "attempts": len(attempts),
            "unique_seeds": len({int(row["seed"]) for row in seeds}),
            "unique_prompt_ids": len({row["actual_prompt_id"] for row in attempts}),
            "outputs": len(outputs),
            "expected_outputs": expected_output_count,
            "all_outputs_verified": not any(not item["health"] == "VERIFIED" for item in output_checks),
            "all_hashes_ok": all(item["hash_ok"] for item in output_checks),
            "all_decode_ok": all(item["decode_ok"] for item in output_checks),
            "all_manifests_ok": all(item["ok"] for item in manifests),
            "accepted_relations": accepted_checks,
            "sequence": sequence,
            "expected_sequence": expected_sequence,
            "output_checks": output_checks,
            "manifests": manifests,
        }


class Parser(argparse.ArgumentParser):
    """ArgumentParser that reports usage errors as structured failures."""

    def error(self, message):  # type: ignore[override]
        raise AcceptanceError("arguments", message, exit_code=EXIT_USAGE)


def build_parser() -> Parser:
    parser = Parser(description="ComfyUI Gacha Master production acceptance runner")
    parser.add_argument("--base-url", default="http://127.0.0.1:8188")
    parser.add_argument("--data-root", required=True, help="Gacha Master data root containing db/gacha.sqlite3")
    parser.add_argument("--workflow-api", required=True)
    parser.add_argument("--workflow-ui", required=True)
    parser.add_argument("--input-root", required=True)
    parser.add_argument("--load-node", default="1")
    parser.add_argument("--save-node", default="8")
    parser.add_argument(
        "--scenarios",
        default=None,
        help="comma-separated NxM tokens; aliases 1x1/3x5/555 are preserved "
             "(default: 1x1,3x5,555 when no --scenario-file is given)",
    )
    parser.add_argument("--scenario-file", default=None, help="JSON file with reusable scenario definitions")
    parser.add_argument("--timeout", type=int, default=7200, help="seconds per scenario")
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--results-root", default=None, help="Gacha results root; default derived from --data-root")
    parser.add_argument("--comfy-root", default=None, help="optional, for checkpoint existence check")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--progress-file", default=None,
                        help="default: <output-dir>/PROGRESS.json; a supervisor reads this after a hard exit")
    parser.add_argument("--creation-timeout", type=int, default=900,
                        help="seconds to wait for the asynchronous plan to become READY")
    parser.add_argument("--environment", default="isolated",
                        choices=["isolated", "target"],
                        help="record whether this is an isolated acceptance or the user's target instance")
    parser.add_argument("--data-source", default="unknown",
                        choices=["real_gpu", "real_cpu", "fixture", "unknown"],
                        help="where the images come from; fixture must never be reported as GPU output")
    parser.add_argument("--host", default="real", choices=["real", "harness", "unknown"],
                        help="real ComfyUI host vs. a test harness")
    parser.add_argument("--source-root", default=None,
                        help="candidate source tree; defaults to this repository")
    parser.add_argument("--installed-plugin-root", default=None,
                        help="optional installed plugin copy to compare against --source-root")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def count_jobs(base: str) -> int | None:
    try:
        status, payload = api(base, "/gacha_master/v1/jobs?limit=1")
    except AcceptanceError:
        return None
    if status != 200:
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        items = data.get("items") or data.get("jobs") or []
        total = data.get("total")
        return int(total) if isinstance(total, int) else len(items)
    if isinstance(data, list):
        return len(data)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except AcceptanceError as exc:
        print(json.dumps({"result": "FAIL", "error": {"phase": exc.phase, "message": exc.message,
                                                      "details": exc.details}}, ensure_ascii=False, indent=2))
        return exc.exit_code

    runner: Runner | None = None
    try:
        runner = Runner(args)
        runner.summary["candidate"] = runner.code_fingerprint()
        if args.data_source == "unknown":
            runner.summary["provenance_warning"] = (
                "data_source=unknown: this evidence must not be reported as real GPU output"
            )
        runner.load_inputs()
        runner.check_environment()
        runner.load_scenarios()
        if args.dry_run:
            before = count_jobs(runner.base)
            after = count_jobs(runner.base)
            runner.summary["dry_run_checks"] = {
                "jobs_created": runner.summary["side_effects"]["jobs_created"],
                "inference_started": runner.summary["side_effects"]["inference_started"],
                "job_count_before": before,
                "job_count_after": after,
                "job_count_unchanged": before is None or after is None or before == after,
            }
            runner.require(
                runner.summary["dry_run_checks"]["jobs_created"] == 0
                and not runner.summary["side_effects"]["inference_started"],
                "dry-run 期间发生了创建或推理副作用",
            )
            runner.summary["result"] = "DRY_RUN_OK"
            runner.progress("dry_run_ok")
            runner.summary["finished_at"] = utc_now()
            runner.save()
            print(json.dumps(runner.summary, ensure_ascii=False, indent=2))
            return 0

        for scenario in runner.scenarios:
            runner.create_and_run(scenario)
        runner.summary["result"] = "PASS" if all(
            item.get("result") == "PASS" for item in runner.summary["scenarios"].values()
        ) else "FAIL"
        runner.summary["finished_at"] = utc_now()
        runner.progress("completed")
        runner.save()
        print(json.dumps(runner.summary, ensure_ascii=False, indent=2))
        return 0 if runner.summary["result"] == "PASS" else EXIT_FAIL
    except BaseException as exc:  # noqa: BLE001 - evidence must survive every exit path
        if runner is None:
            print(json.dumps({"result": "FAIL", "error": {"phase": "init", "type": type(exc).__name__,
                                                          "message": str(exc)}}, ensure_ascii=False, indent=2))
            return EXIT_USAGE
        code = runner.record_failure(exc)
        print(json.dumps(runner.summary, ensure_ascii=False, indent=2))
        return code


if __name__ == "__main__":
    sys.exit(main())
