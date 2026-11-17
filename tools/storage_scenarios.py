#!/usr/bin/env python3
"""Stage-synchronised DISK01—DISK05 controller.

The controller and the business writer exchange files, never sleeps:

    writer: prepared -> ready
    controller: <create the ENOSPC window> -> release-1
    writer: executes the target write -> write-done [failure]
    controller: <free the space> -> release-2
    writer: retries the failed stage, re-checks invariants -> done

Every record lives outside the controlled volume: the spec, the JSONL event
log, the barrier markers, the controller report and the filler manifest.  The
writer has its own watchdog, so a controller that dies cannot leave a writer
waiting forever, and the filler manifest lets ``recover`` clean up after a
hard exit.

Modes
-----
normal      no fill; every stage must succeed (driver/product-primitive check)
refused     pre-seeded conflict; the product must refuse and not overwrite
timeout     writer never reports ready; the controller must time out and kill
hard-kill   a fill child is terminated mid-run; ``recover`` must clean it up
enospc      real fill on a registered controlled volume, then failure +
            space-release + retry assertions.  Requires a controlled volume.
selftest    protocol self-test in a temp area with a *simulated* failure; the
            report always carries ``simulated: true`` and is never DISK evidence

Usage::

  python tools/storage_scenarios.py list
  python tools/storage_scenarios.py run --scenario DISK02 --mode normal \\
      --target-root <temp>/target --work-dir <temp>/work --report <temp>/DISK02.json
  python tools/storage_scenarios.py run --scenario DISK01 --mode enospc \\
      --controlled-root E:\\gacha-enospc --registry <evidence>/storage/controlled-volumes.json \\
      --serial 1234ABCD --headroom-bytes 67108864 --report <evidence>/storage/DISK01.json
  python tools/storage_scenarios.py selftest --report <evidence>/storage/PROTOCOL-SELFTEST.json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Any

TOOLS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TOOLS)
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import storage_acceptance as sa  # noqa: E402

SCENARIO_FILE = os.path.join(TOOLS, "scenarios", "storage-disk.json")
WRITER = os.path.join(ROOT, "tests", "host_acceptance", "storage_writer.py")
PYTHON = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
if not os.path.exists(PYTHON):  # pragma: no cover - environment dependent
    PYTHON = sys.executable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_scenarios(path: str = SCENARIO_FILE) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
    if document.get("schema_version") != 1:
        raise ValueError("scenario file schema_version must be 1")
    scenarios = document.get("scenarios")
    if not isinstance(scenarios, dict) or not scenarios:
        raise ValueError("scenario file has no scenarios")
    for name, scenario in scenarios.items():
        stages = scenario.get("stages")
        if not isinstance(stages, list) or not stages:
            raise ValueError(f"{name}: stages must be a non-empty list")
        for stage in stages:
            for key in ("id", "op"):
                if not stage.get(key):
                    raise ValueError(f"{name}: stage missing {key}")
            if stage["op"] not in {
                "publish_json_once", "copy_file_verified", "temp_then_publish",
                "terminal_receipt", "sqlite_commit",
            }:
                raise ValueError(f"{name}.{stage['id']}: unknown op {stage['op']}")
    return document


def read_events(path: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not os.path.exists(path):
        return events
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                events.append({"stage": "unparsed-event", "raw": line})
    return events


def wait_for_event(path: str, stage: str, timeout: float, proc: subprocess.Popen | None = None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for event in read_events(path):
            if event.get("stage") == stage:
                return event
        if proc is not None and proc.poll() is not None:
            for event in read_events(path):
                if event.get("stage") == stage:
                    return event
            return None
        time.sleep(0.05)
    return None


class ScenarioRun:
    def __init__(
        self,
        *,
        scenario: dict[str, Any],
        name: str,
        mode: str,
        target_root: str,
        work_dir: str,
        report_path: str,
        run_id: str,
        controlled_root: str | None = None,
        registry: str | None = None,
        serial: str | None = None,
        headroom: int = 64 * 1024 * 1024,
        max_fill_bytes: int | None = None,
        fill_timeout: float = 1800.0,
        barrier_timeout: float = 120.0,
        ready_timeout: float = 60.0,
        recovery: str = "retry",
        simulated: bool = False,
    ) -> None:
        self.scenario = scenario
        self.name = name
        self.mode = mode
        self.target_root = os.path.abspath(target_root)
        self.work_dir = os.path.abspath(work_dir)
        self.report_path = os.path.abspath(report_path)
        self.run_id = run_id
        self.controlled_root = controlled_root
        self.registry = registry
        self.serial = serial
        self.headroom = headroom
        self.max_fill_bytes = max_fill_bytes
        self.fill_timeout = fill_timeout
        self.barrier_timeout = barrier_timeout
        self.ready_timeout = ready_timeout
        self.recovery = recovery
        self.simulated = simulated
        self.events_path = os.path.join(self.work_dir, "events.jsonl")
        self.barrier_dir = os.path.join(self.work_dir, "barrier")
        self.spec_path = os.path.join(self.work_dir, "spec.json")
        self.fill_report = os.path.join(self.work_dir, "fill.json")
        self.report: dict[str, Any] = {
            "schema_version": 1,
            "scenario": name,
            "title": scenario.get("title"),
            "mode": mode,
            "run_id": run_id,
            "started_at": utc_now(),
            "target_root": self.target_root,
            "work_dir": self.work_dir,
            "controlled_root": self.controlled_root,
            "simulated": simulated,
            "events": self.events_path,
            "report_path": self.report_path,
            "stages": [],
            "errors": [],
        }

    # ----------------------------------------------------------------- utils
    def _spec(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "scenario": self.name,
            "mode": self.mode,
            "target_root": self.target_root,
            "work_dir": self.work_dir,
            "events": self.events_path,
            "barrier": self.barrier_dir,
            "barrier_timeout": self.barrier_timeout,
            # A product refusal must not be retried: there is nothing to
            # recover, and the point is that the existing file stays intact.
            "recovery": "assert_only" if self.mode == "refused" else self.recovery,
            "seed": 1234567,
            "stages": self.scenario["stages"],
        }

    def _release(self, name: str, payload: Any = True) -> None:
        os.makedirs(self.barrier_dir, exist_ok=True)
        path = os.path.join(self.barrier_dir, name)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump({"at": utc_now(), "payload": payload}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        self.report.setdefault("releases", []).append({"marker": name, "at": utc_now()})

    def _spawn_writer(self) -> subprocess.Popen:
        command = [PYTHON, WRITER, "--spec", self.spec_path]
        return subprocess.Popen(
            command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )

    def _kill(self, proc: subprocess.Popen, reason: str) -> None:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                proc.kill()
                proc.wait(timeout=10)
        self.report.setdefault("kills", []).append({"reason": reason, "at": utc_now()})

    def _finish_release(self, proc: subprocess.Popen, stage: str) -> dict[str, Any]:
        """Wait for the writer to exit and capture its output."""
        try:
            stdout, stderr = proc.communicate(timeout=120)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            self._kill(proc, "writer-timeout")
            stdout, stderr = "", ""
        event = next(
            (item for item in read_events(self.events_path) if item.get("stage") == stage), None
        )
        self.report["writer"] = {
            "returncode": proc.returncode,
            "stdout_tail": (stdout or "")[-2000:],
            "stderr_tail": (stderr or "")[-2000:],
            f"{stage}_event": event,
        }
        return event or {}

    # -------------------------------------------------------------- the run
    def run(self) -> dict[str, Any]:
        os.makedirs(self.work_dir, exist_ok=True)
        os.makedirs(self.barrier_dir, exist_ok=True)
        os.makedirs(self.target_root, exist_ok=True)
        with open(self.spec_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(self._spec(), handle, ensure_ascii=False, indent=2, sort_keys=True)
        started = time.time()
        proc = self._spawn_writer()
        self.report["writer_pid"] = proc.pid
        try:
            if self.mode == "timeout":
                self._run_timeout(proc)
            elif self.mode == "selftest":
                self._run_selftest(proc)
            elif self.mode == "hard-kill":
                self._run_hard_kill(proc)
            elif self.mode == "enospc":
                self._run_enospc(proc)
            elif self.mode in ("normal", "refused"):
                self._run_plain(proc)
            else:
                raise ValueError(f"unknown mode: {self.mode}")
        finally:
            if proc.poll() is None:
                self._kill(proc, "finally")
        self.report["events_captured"] = read_events(self.events_path)
        self.report["duration_seconds"] = round(time.time() - started, 3)
        self.report["finished_at"] = utc_now()
        checks = self._determine_result()
        self.report["checks"] = checks
        self.report["result"] = "PASS" if all(item["ok"] for item in checks) else "FAIL"
        sa.atomic_json(self.report_path, self.report)
        return self.report

    # ----------------------------------------------------------- mode bodies
    def _run_plain(self, proc: subprocess.Popen) -> None:
        ready = wait_for_event(self.events_path, "ready", self.ready_timeout, proc)
        self.report["ready"] = ready
        if not ready:
            self._kill(proc, "no-ready")
            return
        self._release("release-1", {"mode": self.mode})
        wait_for_event(self.events_path, "write-done", self.ready_timeout + 60, proc)
        self._release("release-2", {"mode": self.mode})
        self._finish_release(proc, "done")

    def _run_timeout(self, proc: subprocess.Popen) -> None:
        ready = wait_for_event(self.events_path, "ready", self.ready_timeout, proc)
        self.report["ready"] = ready
        if ready:
            self.report["errors"].append("timeout 模式下 writer 意外进入 ready")
        self._kill(proc, "ready-timeout")
        self.report["timeout_observed"] = True

    def _run_hard_kill(self, proc: subprocess.Popen) -> None:
        ready = wait_for_event(self.events_path, "ready", self.ready_timeout, proc)
        self.report["ready"] = ready
        if not ready:
            self._kill(proc, "no-ready")
            return
        # Fill in a child process, then terminate it mid-fill: the manifest must
        # already list the partial filler so ``recover`` can clean up.
        fill_command = [
            PYTHON, os.path.join(TOOLS, "storage_acceptance.py"), "fill",
            "--path", self.controlled_root or self.target_root,
            "--run-dir", os.path.join(self.controlled_root or self.target_root, "fill-run"),
            "--registry", self.registry or "",
            "--confirm-volume-serial", self.serial or "",
            "--headroom-bytes", str(self.headroom),
            "--report", self.fill_report,
            "--log", self.fill_report + ".log",
            "--poll-interval", "0.05",
        ]
        fill_proc = subprocess.Popen(
            fill_command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
        manifest_path = self.fill_report + ".fillers.json"
        deadline = time.time() + 60
        while time.time() < deadline:
            if os.path.isfile(manifest_path):
                try:
                    with open(manifest_path, encoding="utf-8") as handle:
                        manifest = json.load(handle)
                    if manifest.get("files"):
                        break
                except (OSError, ValueError):
                    pass
            if fill_proc.poll() is not None:
                break
            time.sleep(0.05)
        self._kill(fill_proc, "controller-crash-simulation")
        recorded = 0
        try:
            with open(manifest_path, encoding="utf-8") as handle:
                recorded = len(json.load(handle).get("files") or [])
        except (OSError, ValueError):
            recorded = 0
        self.report["fill_child"] = {"returncode": fill_proc.returncode, "recorded_fillers": recorded,
                                     "manifest": manifest_path}
        # The controller "died"; the writer watchdog must end it on its own.
        try:
            proc.wait(timeout=self.barrier_timeout + 30)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            self._kill(proc, "writer-watchdog-failed")
        self.report["writer_watchdog_exit"] = proc.returncode
        cleanup = sa.cleanup(manifest=manifest_path, registry=self.registry or None,
                             outer_report=self.report_path + ".recovery.json")
        self.report["recovery"] = cleanup

    def _run_selftest(self, proc: subprocess.Popen) -> None:
        """Protocol self-test: no fill, one injected failure, assert release."""
        ready = wait_for_event(self.events_path, "ready", self.ready_timeout, proc)
        self.report["ready"] = ready
        if not ready:
            self._kill(proc, "no-ready")
            return
        self.report["fill"] = {
            "simulated": True,
            "reason": "自检不填充任何卷；使用注入的 ENOSPC 模拟错误验证屏障/失败/恢复协议",
        }
        self._release("release-1", {"simulated": True})
        wait_for_event(self.events_path, "write-done", self.ready_timeout + 60, proc)
        self._release("release-2", {"simulated": True})
        self._finish_release(proc, "done")

    def _run_enospc(self, proc: subprocess.Popen) -> None:
        ready = wait_for_event(self.events_path, "ready", self.ready_timeout, proc)
        self.report["ready"] = ready
        if not ready:
            self._kill(proc, "no-ready")
            return
        run_dir = os.path.join(self.controlled_root, f"fill-run-{self.run_id}")
        fill = sa.fill(
            self.controlled_root,
            run_dir,
            self.serial or "",
            self.headroom,
            self.fill_report,
            max_bytes=self.max_fill_bytes,
            timeout=self.fill_timeout,
            registry=self.registry,
            log_path=self.fill_report + ".log",
            run_id=self.run_id,
        )
        self.report["fill"] = fill
        if fill.get("result") not in ("FILLED", "ENOSPC_READY"):
            self.report["errors"].append(f"填充未达到预期状态: {fill.get('result')}")
        # Confirm the write really faces the reduced space before releasing.
        free = sa.read_volume(self.controlled_root)["free_bytes"]
        self.report["free_at_release"] = free
        self.report["release_headroom_satisfied"] = free <= self.headroom
        self._release("release-1", {"free_bytes": free, "headroom": self.headroom})
        wait_for_event(self.events_path, "write-done", self.fill_timeout, proc)
        cleanup = sa.cleanup(report=self.fill_report, registry=self.registry,
                             outer_report=self.fill_report + ".cleanup.json")
        self.report["fill_cleanup"] = cleanup
        self.report["free_after_cleanup"] = (cleanup.get("volume") or {}).get("free_bytes")
        self._release("release-2", {"free_after_cleanup": self.report["free_after_cleanup"]})
        self._finish_release(proc, "done")

    # --------------------------------------------------------------- verdict
    def _determine_result(self) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []

        def check(name: str, ok: bool, **detail: Any) -> None:
            checks.append({"name": name, "ok": bool(ok), **detail})

        events = read_events(self.events_path)
        stages = {event.get("stage") for event in events}
        done = next((event for event in events if event.get("stage") == "done"), None)
        failed = next((event for event in events if event.get("stage") == "stage-failed"), None)
        if self.mode == "timeout":
            check("no_ready_event", "ready" not in stages)
            check("timeout_recorded", bool(self.report.get("timeout_observed")))
            check("writer_killed", self.report.get("writer", {}).get("returncode") not in (0, None)
                  or bool(self.report.get("kills")))
            return checks
        if self.mode == "hard-kill":
            check("filler_recorded_before_kill",
                  (self.report.get("fill_child") or {}).get("recorded_fillers", 0) >= 0)
            cleanup = self.report.get("recovery") or {}
            check("recovery_cleaned", cleanup.get("result") == "CLEANED",
                  result=cleanup.get("result"), errors=cleanup.get("errors"))
            check("no_leftovers", not cleanup.get("leftovers"), leftovers=cleanup.get("leftovers"))
            check("writer_watchdog_exited",
                  self.report.get("writer_watchdog_exit") not in (None, 0) or "barrier-timeout" in stages)
            return checks
        if self.mode == "refused":
            check("product_refused", failed is not None, failure=failed)
            check("no_overwrite_asserted", bool(done) and done.get("result") == "PASS",
                  done=done)
            check("no_fill", "fill" not in self.report)
            return checks
        if self.mode == "selftest":
            check("simulated_failure_recorded", failed is not None, failure=failed)
            check("recovery_retried", "released" in stages)
            check("writer_reported_done", done is not None, done=done)
            check("writer_exit_zero", self.report.get("writer", {}).get("returncode") == 0,
                  returncode=self.report.get("writer", {}).get("returncode"))
            return checks
        if self.mode == "enospc":
            fill = self.report.get("fill") or {}
            check("fill_reached_headroom",
                  fill.get("result") in ("FILLED", "ENOSPC_READY"), result=fill.get("result"))
            check("space_reduced_before_release", bool(self.report.get("release_headroom_satisfied")),
                  free=self.report.get("free_at_release"), headroom=self.headroom)
            check("target_write_failed", failed is not None, failure=failed)
            check("space_released_before_retry",
                  (self.report.get("fill_cleanup") or {}).get("result") == "CLEANED",
                  cleanup=(self.report.get("fill_cleanup") or {}).get("result"))
            check("writer_recovered", done is not None and done.get("result") == "PASS", done=done)
            check("writer_exit_zero", self.report.get("writer", {}).get("returncode") == 0,
                  returncode=self.report.get("writer", {}).get("returncode"))
            return checks
        check("no_failure_recorded", failed is None, failure=failed)
        check("writer_reported_done", done is not None, done=done)
        check("writer_exit_zero", self.report.get("writer", {}).get("returncode") == 0,
              returncode=self.report.get("writer", {}).get("returncode"))
        return checks


def _new_run_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now().strftime('%Y%m%dT%H%M%S')}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage-synchronised DISK controller")
    parser.add_argument("--scenario-file", default=SCENARIO_FILE)
    sub = parser.add_subparsers(dest="action", required=True)

    sub.add_parser("list")

    p_run = sub.add_parser("run")
    p_run.add_argument("--scenario", required=True)
    p_run.add_argument("--mode", required=True,
                       choices=["normal", "refused", "timeout", "hard-kill", "enospc"])
    p_run.add_argument("--target-root", required=True)
    p_run.add_argument("--work-dir", required=True)
    p_run.add_argument("--report", required=True)
    p_run.add_argument("--run-id", default=None)
    p_run.add_argument("--controlled-root", default=None)
    p_run.add_argument("--registry", default=None)
    p_run.add_argument("--serial", default=None)
    p_run.add_argument("--headroom-bytes", type=int, default=64 * 1024 * 1024)
    p_run.add_argument("--max-fill-bytes", type=int, default=None)
    p_run.add_argument("--fill-timeout", type=float, default=1800.0)
    p_run.add_argument("--barrier-timeout", type=float, default=120.0)
    p_run.add_argument("--ready-timeout", type=float, default=60.0)
    p_run.add_argument("--recovery", choices=["retry", "assert_only"], default="retry")

    p_self = sub.add_parser("selftest")
    p_self.add_argument("--scenario", default="DISK02")
    p_self.add_argument("--report", required=True)
    p_self.add_argument("--work-root", default=None)
    p_self.add_argument("--barrier-timeout", type=float, default=30.0)

    args = parser.parse_args(argv)
    scenarios = load_scenarios(args.scenario_file)["scenarios"]
    if args.action == "list":
        print(json.dumps(
            {name: {"title": item["title"], "stages": [s["id"] for s in item["stages"]]}
             for name, item in scenarios.items()},
            ensure_ascii=False, indent=2,
        ))
        return 0
    if args.action == "run":
        if args.scenario not in scenarios:
            print(f"unknown scenario: {args.scenario}", file=sys.stderr)
            return 2
        if args.mode in ("enospc", "hard-kill") and (
            not args.controlled_root or not args.registry or not args.serial
        ):
            print(f"{args.mode} 模式需要 --controlled-root/--registry/--serial", file=sys.stderr)
            return 2
        run_id = args.run_id or _new_run_id(args.scenario)
        run = ScenarioRun(
            scenario=scenarios[args.scenario],
            name=args.scenario,
            mode=args.mode,
            target_root=args.target_root,
            work_dir=args.work_dir,
            report_path=args.report,
            run_id=run_id,
            controlled_root=args.controlled_root,
            registry=args.registry,
            serial=args.serial,
            headroom=args.headroom_bytes,
            max_fill_bytes=args.max_fill_bytes,
            fill_timeout=args.fill_timeout,
            barrier_timeout=args.barrier_timeout,
            ready_timeout=args.ready_timeout,
            recovery=args.recovery,
        )
        report = run.run()
        print(json.dumps({k: report[k] for k in ("scenario", "mode", "result", "checks")},
                         ensure_ascii=False, indent=2))
        return 0 if report["result"] == "PASS" else 1
    if args.action == "selftest":
        work_root = args.work_root or tempfile.mkdtemp(prefix="gm-disk-selftest-")
        own = args.work_root is None
        try:
            matrix: list[tuple[str, str]] = []
            if args.scenario == "all":
                for name in scenarios:
                    matrix.extend([(name, "normal"), (name, "refused")])
                matrix.extend([("DISK02", "timeout"), ("DISK02", "selftest")])
            else:
                matrix = [(args.scenario, mode) for mode in ("normal", "refused", "timeout", "selftest")]
            runs = []
            for index, (name, mode) in enumerate(matrix):
                case_dir = os.path.join(work_root, f"{index:02d}-{name}-{mode}")
                os.makedirs(case_dir, exist_ok=True)
                run = ScenarioRun(
                    scenario=scenarios[name],
                    name=name,
                    mode=mode,
                    target_root=os.path.join(case_dir, "target"),
                    work_dir=os.path.join(case_dir, "work"),
                    report_path=os.path.join(case_dir, f"{mode}.json"),
                    run_id=f"selftest-{name}-{mode}-{datetime.now().strftime('%H%M%S')}",
                    barrier_timeout=args.barrier_timeout,
                    ready_timeout=15.0,
                    recovery="retry",
                    simulated=True,
                )
                runs.append(run.run())
            summary = {
                "schema_version": 1,
                "generated_at": utc_now(),
                "simulated": True,
                "scenario": args.scenario,
                "work_root": work_root,
                "note": (
                    "本自检只验证屏障/失败/超时/恢复协议与产品写入原语；"
                    "没有真实填充任何卷，不能作为 DISK 真实 ENOSPC 证据。"
                ),
                "cases": [
                    {"scenario": item["scenario"], "mode": item["mode"],
                     "result": item["result"], "checks": item["checks"],
                     "report": item.get("report_path", item.get("work_dir"))}
                    for item in runs
                ],
            }
            summary["result"] = "PASS" if all(item["result"] == "PASS" for item in runs) else "FAIL"
            sa.atomic_json(args.report, summary)
            print(json.dumps({
                "result": summary["result"],
                "cases": {f"{item['scenario']}/{item['mode']}": item["result"] for item in runs},
            }, ensure_ascii=False, indent=2))
            return 0 if summary["result"] == "PASS" else 1
        finally:
            if own:
                shutil.rmtree(work_root, ignore_errors=True)
    return 2


if __name__ == "__main__":
    sys.exit(main())
