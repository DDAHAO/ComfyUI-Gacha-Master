#!/usr/bin/env python3
"""Run an acceptance child and record hard exits.

The child is expected to maintain a PROGRESS.json with its last phase.  When
the child is killed (or crashes before writing a final report), this wrapper
still leaves an evidence file containing the exit code, the last readable
progress record and the captured output paths.

Usage:
  python tools/acceptance_supervisor.py --report evidence/CHILD.json \
      --progress evidence/PROGRESS.json --timeout 7200 \
      -- .venv/Scripts/python.exe tools/production_acceptance.py --dry-run ...

Exit codes: 0 child succeeded, otherwise the child/wrapper failure code.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Supervise an acceptance child process")
    parser.add_argument("--report", required=True)
    parser.add_argument("--progress", required=True)
    parser.add_argument("--timeout", type=float, default=None, help="seconds before terminating the child")
    parser.add_argument("--stdout", default=None)
    parser.add_argument("--stderr", default=None)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print(json.dumps({"result": "FAIL", "error": "missing child command"}, ensure_ascii=False))
        return 2

    report_path = os.path.abspath(args.report)
    os.makedirs(os.path.dirname(report_path) or ".", exist_ok=True)
    progress_path = os.path.abspath(args.progress)
    stdout_path = os.path.abspath(args.stdout or report_path + ".stdout.log")
    stderr_path = os.path.abspath(args.stderr or report_path + ".stderr.log")
    started = utc_now()
    started_ts = time.time()
    timed_out = False
    with open(stdout_path, "wb") as stdout, open(stderr_path, "wb") as stderr:
        try:
            process = subprocess.Popen(command, stdout=stdout, stderr=stderr, cwd=os.getcwd())
            try:
                returncode = process.wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                returncode = process.wait()
        except OSError as exc:
            report = {
                "schema_version": 1,
                "result": "FAIL",
                "started_at": started,
                "finished_at": utc_now(),
                "command": command,
                "spawn_error": f"{type(exc).__name__}: {exc}",
            }
            with open(report_path, "w", encoding="utf-8") as handle:
                json.dump(report, handle, ensure_ascii=False, indent=2)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 2

    report = {
        "schema_version": 1,
        "started_at": started,
        "finished_at": utc_now(),
        "duration_seconds": round(time.time() - started_ts, 3),
        "command": command,
        "returncode": returncode,
        "timed_out": timed_out,
        "stdout": stdout_path,
        "stderr": stderr_path,
        "progress_path": progress_path,
        "last_progress": read_json(progress_path),
    }
    # A child that exits non-zero still gets a readable report; the last
    # progress record is exactly what a hard kill leaves behind.
    report["result"] = "PASS" if returncode == 0 and not timed_out else "FAIL"
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
