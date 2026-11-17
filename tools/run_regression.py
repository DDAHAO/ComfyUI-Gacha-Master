#!/usr/bin/env python3
"""Run the formal Python and JS regression suites and refuse empty runs.

The repository keeps Python unittest modules under ``tests/unit``,
``tests/integration`` and ``tests/fault_injection`` and Node test files under
``tests/js``.  This entrypoint discovers all of them, runs each suite once,
records the raw logs and fails when discovery or execution finds zero tests.

Usage:
  python tools/run_regression.py --output-dir test-artifacts/.../regression
  python tools/run_regression.py --skip-js
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON_DIRS = (
    os.path.join("tests", "unit"),
    os.path.join("tests", "integration"),
    os.path.join("tests", "fault_injection"),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def discover_python_modules(root: str = ROOT) -> list[str]:
    modules = []
    for directory in PYTHON_DIRS:
        for path in sorted(glob.glob(os.path.join(root, directory, "test_*.py"))):
            rel = os.path.relpath(path, root).replace(os.sep, ".")
            modules.append(rel[:-3])
    return modules


def discover_js_files(root: str = ROOT) -> list[str]:
    return sorted(glob.glob(os.path.join(root, "tests", "js", "*.test.mjs")))


def parse_python(output: str) -> dict[str, Any]:
    ran = re.findall(r"^Ran (\d+) tests?", output, re.MULTILINE)
    skipped = re.findall(r"skipped=(\d+)", output)
    failures = re.findall(r"failures=(\d+)", output)
    errors = re.findall(r"errors=(\d+)", output)
    return {
        "tests": int(ran[-1]) if ran else 0,
        "skipped": int(skipped[-1]) if skipped else 0,
        "failures": int(failures[-1]) if failures else 0,
        "errors": int(errors[-1]) if errors else 0,
    }


def parse_js(output: str) -> dict[str, Any]:
    def last_int(pattern: str) -> int:
        matches = re.findall(pattern, output, re.MULTILINE)
        return int(matches[-1]) if matches else 0

    return {
        "tests": max(last_int(r"^# tests (\d+)"), last_int(r"ℹ tests (\d+)")),
        "pass": max(last_int(r"^# pass (\d+)"), last_int(r"ℹ pass (\d+)")),
        "fail": max(last_int(r"^# fail (\d+)"), last_int(r"ℹ fail (\d+)")),
        "skipped": max(last_int(r"^# skipped (\d+)"), last_int(r"ℹ skipped (\d+)")),
    }


def run(command: list[str], cwd: str, timeout: int) -> dict[str, Any]:
    started = utc_now()
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "started_at": started,
            "finished_at": utc_now(),
            "returncode": None,
            "timeout": timeout,
            "stdout": exc.stdout or "",
            "stderr": (exc.stderr or "") + f"\nTIMEOUT after {timeout}s",
        }
    return {
        "command": command,
        "started_at": started,
        "finished_at": utc_now(),
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Formal regression entrypoint")
    parser.add_argument("--root", default=ROOT, help="repository root to discover tests from")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--python", default=os.path.join(ROOT, ".venv", "Scripts", "python.exe"))
    parser.add_argument("--node", default="node")
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--skip-python", action="store_true")
    parser.add_argument("--skip-js", action="store_true")
    args = parser.parse_args(argv)

    output_dir = os.path.abspath(
        args.output_dir
        or os.path.join(ROOT, "test-artifacts", "regression-" + datetime.now().strftime("%Y%m%dT%H%M%S"))
    )
    os.makedirs(output_dir, exist_ok=True)
    root = os.path.abspath(args.root)
    python_exe = args.python if os.path.isfile(args.python) else sys.executable
    report: dict[str, Any] = {
        "schema_version": 1,
        "started_at": utc_now(),
        "root": root,
        "output_dir": output_dir,
        "python_executable": python_exe,
        "python": None,
        "js": None,
    }
    ok = True

    if not args.skip_python:
        modules = discover_python_modules(root)
        report["python_modules_discovered"] = modules
        if not modules:
            report["python"] = {"error": "no Python test modules discovered"}
            ok = False
        else:
            command = [python_exe, "-m", "unittest", "-v", *modules]
            raw = run(command, root, args.timeout)
            parsed = parse_python(raw["stdout"] + "\n" + raw["stderr"])
            python_log = os.path.join(output_dir, "python-tests.log")
            with open(python_log, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("$ " + " ".join(command) + "\n\n")
                handle.write(raw["stdout"])
                if raw["stderr"]:
                    handle.write("\n[stderr]\n" + raw["stderr"])
            report["python"] = {
                "module_count": len(modules),
                "log": python_log,
                "returncode": raw["returncode"],
                **parsed,
            }
            if parsed["tests"] <= 0:
                report["python"]["error"] = "zero tests executed"
                ok = False
            if raw["returncode"] != 0 or parsed["failures"] or parsed["errors"]:
                ok = False

    if not args.skip_js:
        files = discover_js_files(root)
        report["js_files_discovered"] = [os.path.relpath(path, root) for path in files]
        if not files:
            report["js"] = {"error": "no JS test files discovered"}
            ok = False
        else:
            command = [args.node, "--test", "--test-reporter=tap", *files]
            raw = run(command, root, args.timeout)
            parsed = parse_js(raw["stdout"] + "\n" + raw["stderr"])
            js_log = os.path.join(output_dir, "js-tests.log")
            with open(js_log, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("$ " + " ".join(command) + "\n\n")
                handle.write(raw["stdout"])
                if raw["stderr"]:
                    handle.write("\n[stderr]\n" + raw["stderr"])
            report["js"] = {
                "file_count": len(files),
                "log": js_log,
                "returncode": raw["returncode"],
                **parsed,
            }
            if parsed["tests"] <= 0:
                report["js"]["error"] = "zero tests executed"
                ok = False
            if raw["returncode"] != 0 or parsed["fail"]:
                ok = False

    report["finished_at"] = utc_now()
    report["result"] = "PASS" if ok else "FAIL"
    report_path = os.path.join(output_dir, "REGRESSION.json")
    with open(report_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
