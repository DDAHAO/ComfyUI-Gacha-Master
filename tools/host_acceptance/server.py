#!/usr/bin/env python3
"""Start/stop the isolated real ComfyUI host used by host acceptance.

This is test infrastructure.  It does not install ComfyUI or the plugin; it
launches an existing ComfyUI checkout with an isolated base directory, records
the PID and logs, waits for the Gacha Master health endpoint, and stops the
server either through the maintained Ctrl+C wrapper or a hard kill for crash
cases.

Examples:
  python tools/host_acceptance/server.py start \
      --comfy-root D:\\Projects\\ComfyUI-gpu-acceptance\\ComfyUI \
      --python D:\\Projects\\ComfyUI-gpu-acceptance\\venv\\Scripts\\python.exe \
      --base-dir test-artifacts/next-20261115/<run>/runtime \
      --port 8296 --evidence-dir test-artifacts/next-20261115/<run>/host

  python tools/host_acceptance/server.py status --port 8296
  python tools/host_acceptance/server.py stop --evidence-dir ... --grace 60
  python tools/host_acceptance/server.py crash --evidence-dir ...
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUNNER = os.path.join(ROOT, "tools", "host_acceptance", "comfy_runner.py")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def read_json(path: str) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def http_json(url: str, timeout: float = 5.0) -> tuple[int, Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else None
        except Exception:
            return exc.code, {"raw": raw.decode("utf-8", errors="replace")[:500]}
    except Exception as exc:
        return 0, {"error": f"{type(exc).__name__}: {exc}"}


def process_alive(pid: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        return str(pid) in result.stdout and "No tasks" not in result.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def health(port: int) -> dict[str, Any]:
    status, payload = http_json(f"http://127.0.0.1:{int(port)}/gacha_master/v1/health")
    return {"port": int(port), "http_status": status, "body": payload}


def wait_ready(port: int, timeout: float) -> dict[str, Any]:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = health(port)
        body = last.get("body") or {}
        if last["http_status"] == 200 and (body.get("data") or {}).get("db", {}).get("ok"):
            return last
        time.sleep(0.5)
    return last or {"port": port, "http_status": 0, "body": None}


def command_start(args) -> int:
    evidence = os.path.abspath(args.evidence_dir)
    os.makedirs(evidence, exist_ok=True)
    base_dir = os.path.abspath(args.base_dir)
    os.makedirs(base_dir, exist_ok=True)
    pid_path = os.path.join(evidence, "server.pid.json")
    stop_file = os.path.join(evidence, f"stop-{int(time.time())}.flag")
    stdout_path = os.path.join(evidence, "server.stdout.log")
    stderr_path = os.path.join(evidence, "server.stderr.log")
    comfy_args = [
        "--base-directory", base_dir,
        "--port", str(args.port),
        "--listen", "127.0.0.1",
        "--disable-auto-launch",
    ]
    if args.models_dir:
        comfy_args.extend(["--models-directory", os.path.abspath(args.models_dir)])
    if args.extra_args:
        comfy_args.extend(args.extra_args)
    env = dict(os.environ)
    env.update({
        "GACHA_COMFY_ROOT": os.path.abspath(args.comfy_root),
        "GACHA_STOP_FILE": stop_file,
        "COMFY_ARGS": json.dumps(comfy_args),
        "PYTHONUNBUFFERED": "1",
    })
    with open(stdout_path, "wb") as stdout, open(stderr_path, "wb") as stderr:
        process = subprocess.Popen(
            [os.path.abspath(args.python), RUNNER],
            cwd=os.path.abspath(args.comfy_root),
            env=env,
            stdout=stdout,
            stderr=stderr,
        )
    record = {
        "schema_version": 1,
        "started_at": utc_now(),
        "pid": process.pid,
        "comfy_root": os.path.abspath(args.comfy_root),
        "python": os.path.abspath(args.python),
        "base_dir": base_dir,
        "port": int(args.port),
        "comfy_args": comfy_args,
        "stdout": stdout_path,
        "stderr": stderr_path,
        "stop_file": stop_file,
    }
    write_json(pid_path, record)
    ready = wait_ready(args.port, args.timeout)
    record["ready"] = bool(
        ready.get("http_status") == 200
        and (ready.get("body") or {}).get("data", {}).get("db", {}).get("ok")
    )
    record["ready_probe"] = ready
    record["result"] = "READY" if record["ready"] else "FAIL"
    write_json(pid_path, record)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    if not record["ready"]:
        if not process_alive(process.pid):
            record["error"] = "server exited before health became ready"
            write_json(pid_path, record)
        return 1
    return 0


def find_pid_file(evidence_dir: str) -> str:
    return os.path.join(os.path.abspath(evidence_dir), "server.pid.json")


def command_status(args) -> int:
    if args.evidence_dir:
        try:
            record = read_json(find_pid_file(args.evidence_dir))
        except OSError:
            record = {}
        pid = record.get("pid")
        port = record.get("port") or args.port
    else:
        pid = None
        port = args.port
    result = {
        "pid": pid,
        "alive": bool(pid and process_alive(int(pid))),
        "health": health(port) if port else None,
        "record": record if args.evidence_dir else None,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_stop(args) -> int:
    pid_file = find_pid_file(args.evidence_dir)
    record = read_json(pid_file)
    pid = int(record["pid"])
    stop_file = record.get("stop_file")
    report = {
        "schema_version": 1,
        "action": "stop",
        "started_at": utc_now(),
        "pid": pid,
        "mode": "ctrl_c",
    }
    if not process_alive(pid):
        report.update({"result": "ALREADY_EXITED", "exit_observed": False})
        write_json(os.path.join(args.evidence_dir, "server-stop.json"), report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if stop_file:
        open(stop_file, "a", encoding="utf-8").close()
    deadline = time.time() + args.grace
    while time.time() < deadline:
        if not process_alive(pid):
            report["exit_observed"] = True
            break
        time.sleep(0.25)
    else:
        report["exit_observed"] = False
        report["mode"] = "hard_kill_after_grace"
        subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True)
        for _ in range(40):
            if not process_alive(pid):
                break
            time.sleep(0.25)
    report["finished_at"] = utc_now()
    report["health_after"] = health(record.get("port") or args.port)
    report["result"] = "STOPPED"
    write_json(os.path.join(args.evidence_dir, "server-stop.json"), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def command_crash(args) -> int:
    record = read_json(find_pid_file(args.evidence_dir))
    pid = int(record["pid"])
    report = {
        "schema_version": 1,
        "action": "crash",
        "started_at": utc_now(),
        "pid": pid,
        "method": "taskkill /F /T" if os.name == "nt" else "SIGKILL",
    }
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True)
    else:
        os.kill(pid, signal.SIGKILL)
    for _ in range(80):
        if not process_alive(pid):
            break
        time.sleep(0.25)
    report["alive_after"] = process_alive(pid)
    report["health_after"] = health(record.get("port") or args.port)
    report["finished_at"] = utc_now()
    report["result"] = "CRASHED" if not report["alive_after"] else "FAIL"
    write_json(os.path.join(args.evidence_dir, "server-crash.json"), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["result"] == "CRASHED" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Isolated ComfyUI host controller")
    sub = parser.add_subparsers(dest="action", required=True)
    start = sub.add_parser("start")
    start.add_argument("--comfy-root", required=True)
    start.add_argument("--python", required=True)
    start.add_argument("--base-dir", required=True)
    start.add_argument("--models-dir", default=None,
                       help="models directory; --base-directory would otherwise look below the isolated runtime")
    start.add_argument("--port", type=int, default=8296)
    start.add_argument("--evidence-dir", required=True)
    start.add_argument("--timeout", type=float, default=240)
    start.add_argument("--extra-args", nargs=argparse.REMAINDER, default=None)
    start.set_defaults(func=command_start)

    status = sub.add_parser("status")
    status.add_argument("--port", type=int, default=8296)
    status.add_argument("--evidence-dir", default=None)
    status.set_defaults(func=command_status)

    stop = sub.add_parser("stop")
    stop.add_argument("--evidence-dir", required=True)
    stop.add_argument("--grace", type=float, default=60)
    stop.set_defaults(func=command_stop)

    crash = sub.add_parser("crash")
    crash.add_argument("--evidence-dir", required=True)
    crash.set_defaults(func=command_crash)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
