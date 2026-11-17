"""Stage-synchronised business writer for the DISK01—05 scenarios.

This is a test process, not product code.  It performs the target write named
by the scenario file through the *product* primitive recorded there, and it
reports every stage to an event log outside the controlled volume so the
controller knows exactly when the write is about to happen.

Protocol
--------
1. ``prepare``   build the layout, record hashes/DB counts
2. ``ready``     ask the controller to create the ENOSPC window and wait for
                 ``release-1`` (watchdog: ``--barrier-timeout``)
3. ``write``     execute the scenario stages in order; stop at the first
                 failure and record the phase, exception and commit state
4. ``write-done`` report success/failure, then wait for ``release-2``
5. ``recover``   retry the failed stage on the freed volume and re-check the
                 invariants recorded in the scenario file
6. ``done``      exit 0 only when every stage and every assertion holds

Nothing here injects a *real* ENOSPC: in ``selftest`` mode a single stage raises
a synthetic "no space" error, and the report says so.  Real evidence requires a
controlled volume and the ``enospc`` controller mode.

Usage::

  python tests/host_acceptance/storage_writer.py --spec <work-dir>/spec.json
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import sqlite3
import sys
import time
import traceback
from datetime import datetime, timezone
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from comfyui_gacha_master.backend import storage  # noqa: E402
from comfyui_gacha_master.backend.db import Database  # noqa: E402
from comfyui_gacha_master.backend.errors import GachaError  # noqa: E402
from comfyui_gacha_master.backend.util import (  # noqa: E402
    copy_file_verified,
    publish_json_once,
    publish_noreplace,
    sha256_file,
    write_temp_file,
)

PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6360000002000154a24f9d00000000"
    "49454e44ae426082"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Ledger:
    """Append-only event log; every record is flushed and fsynced."""

    def __init__(self, path: str, run_id: str) -> None:
        self.path = path
        self.run_id = run_id
        self.seq = 0

    def write(self, stage_name: str, **fields: Any) -> None:
        self.seq += 1
        payload = {
            "seq": self.seq,
            "at": utc_now(),
            "at_epoch": time.time(),
            "run_id": self.run_id,
            "pid": os.getpid(),
            **fields,
            # The protocol stage always wins over a payload field with the
            # same name; failed-stage detail travels as "failed_stage".
            "stage": stage_name,
        }
        with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def write_json(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())


def hash_tree(root: str, keys: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for rel in keys:
        path = os.path.join(root, *rel.split("/"))
        if os.path.isfile(path):
            result[rel] = sha256_file(path)[0]
    return result


PUBLISHED = [
    "results/JOB-0001/SRC-0001/DRAW-0001/ATTEMPT-01/OUT-0001.png",
    "results/JOB-0001/SRC-0001/DRAW-0001/ATTEMPT-01/manifest.json",
    "results/JOB-0001/SRC-0002/DRAW-0001/ATTEMPT-01/OUT-0001.png",
    "results/JOB-0001/SRC-0002/DRAW-0001/ATTEMPT-01/manifest.json",
    "exports/EXPORT-existing.png",
    "backups/backup-existing.sqlite3",
]
ORIGINALS = ["source/original-a.png", "source/original-b.png"]


class Writer:
    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec
        self.target = os.path.abspath(spec["target_root"])
        self.work = os.path.abspath(spec["work_dir"])
        self.barrier = os.path.join(self.work, "barrier")
        self.run_id = spec["run_id"]
        self.scenario = spec["scenario"]
        self.mode = spec.get("mode", "normal")
        self.stages: list[dict[str, Any]] = spec["stages"]
        self.ledger = Ledger(spec["events"], self.run_id)
        self.before: dict[str, Any] = {}
        self.failed: dict[str, Any] | None = None
        self.seed = int(spec.get("seed", 1234567))

    # ------------------------------------------------------------- layout
    def prepare(self) -> None:
        os.makedirs(self.target, exist_ok=True)
        for rel in ORIGINALS:
            path = os.path.join(self.target, *rel.split("/"))
            if not os.path.exists(path):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                data = PNG_BYTES + rel.encode("ascii")
                with open(path, "xb") as handle:
                    handle.write(data)
        for rel in PUBLISHED:
            path = os.path.join(self.target, *rel.split("/"))
            if os.path.exists(path):
                continue
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if rel.endswith(".png"):
                with open(path, "xb") as handle:
                    handle.write(PNG_BYTES + rel.encode("ascii"))
            elif rel.endswith(".json"):
                write_json(path, {"kind": "attempt_outputs", "published": True, "rel": rel})
            else:
                with open(path, "xb") as handle:
                    handle.write(b"previous-backup")
        self._prepare_database()
        self.conflict_rel = ""
        if self.mode == "refused":
            # Pre-seed the first stage's target with different bytes: the
            # product publish must refuse instead of overwriting it.
            first = next((stage for stage in self.stages if "rel" in stage), None)
            self.conflict_rel = first["rel"] if first else "workspace/conflict.bin"
            conflict = os.path.join(self.target, *self.conflict_rel.split("/"))
            os.makedirs(os.path.dirname(conflict), exist_ok=True)
            if not os.path.exists(conflict):
                with open(conflict, "xb") as handle:
                    handle.write(b"conflicting-existing-bytes")
        os.makedirs(os.path.join(self.work, "barrier"), exist_ok=True)
        self.before = {
            "originals": hash_tree(self.target, ORIGINALS),
            "published": hash_tree(self.target, PUBLISHED),
            "conflicts": hash_tree(self.target, [self.conflict_rel] if self.conflict_rel else []),
            "db_rows": self._db_rows(),
            "seed": self.seed,
            "free_bytes": self._free(),
        }

    def _db_path(self) -> str:
        return os.path.join(self.target, "data", "gacha.sqlite3")

    def _open_db(self) -> Database:
        db = Database(self._db_path())
        db.open()
        return db

    def _prepare_database(self) -> None:
        if os.path.exists(self._db_path()):
            return
        os.makedirs(os.path.dirname(self._db_path()), exist_ok=True)
        db = self._open_db()
        try:
            with db.tx() as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS storage_probe ("
                    "id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, seed INTEGER NOT NULL,"
                    " created_at TEXT NOT NULL)"
                )
                conn.execute(
                    "INSERT OR IGNORE INTO storage_probe VALUES (?,?,?,?)",
                    ("probe-existing", "ATTEMPT-EXISTING", 42, utc_now()),
                )
        finally:
            db.close()

    def _db_rows(self) -> int:
        if not os.path.exists(self._db_path()):
            return 0
        db = self._open_db()
        try:
            with db.read() as conn:
                return int(conn.execute("SELECT COUNT(*) FROM storage_probe").fetchone()[0])
        finally:
            db.close()

    def _free(self) -> int:
        import shutil

        return shutil.disk_usage(self.target).free

    # -------------------------------------------------------------- stages
    def execute_stage(self, stage: dict[str, Any], *, retry: bool = False) -> None:
        op = stage["op"]
        label = stage.get("label") or stage["id"]
        self.ledger.write("stage-begin", stage_id=stage["id"], op=op, retry=retry, label=label)
        if self.mode == "selftest" and not retry and stage.get("selftest_fault"):
            # Self-test only: a synthetic no-space error exercises the failure
            # path.  It is never used for real DISK evidence.
            raise OSError(errno.ENOSPC, "simulated no space (protocol self-test only)")
        if op == "publish_json_once":
            target = os.path.join(self.target, *stage["rel"].split("/"))
            payload = {"kind": "stage", "stage": stage["id"], "seed": self.seed, "run_id": self.run_id}
            if retry and os.path.exists(target):
                from comfyui_gacha_master.backend.util import read_json

                if read_json(target) != payload:
                    raise GachaError("OUTPUT_CONFLICT", "既有清单内容不同，拒绝覆盖。")
            else:
                publish_json_once(target, payload)
        elif op == "copy_file_verified":
            source = os.path.join(self.target, *stage["src"].split("/"))
            target = os.path.join(self.target, *stage["rel"].split("/"))
            if retry and os.path.exists(target):
                if sha256_file(source)[0] != sha256_file(target)[0]:
                    raise GachaError("OUTPUT_CONFLICT", "既有目标内容不同，拒绝覆盖。")
            else:
                copy_file_verified(source, target)
        elif op == "temp_then_publish":
            target = os.path.join(self.target, *stage["rel"].split("/"))
            if retry and os.path.exists(target):
                if sha256_file(target)[0] != hashlib.sha256(PNG_BYTES).hexdigest():
                    raise GachaError("OUTPUT_CONFLICT", "既有 OUT 内容不同，拒绝覆盖。")
            else:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                temp = write_temp_file(os.path.dirname(target), ".out", PNG_BYTES)
                try:
                    publish_noreplace(temp, target)
                finally:
                    if os.path.exists(temp):
                        os.unlink(temp)
        elif op == "terminal_receipt":
            job_dir = os.path.join(self.target, "workspace", "job")
            storage.write_terminal_receipt(
                job_dir, attempt_id="ATTEMPT-01",
                evidence={"seed": self.seed, "run_id": self.run_id, "stage": stage["id"]},
            )
        elif op == "sqlite_commit":
            row_id = f"probe-{self.run_id}-{stage['id']}"
            db = self._open_db()
            try:
                with db.tx() as conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO storage_probe VALUES (?,?,?,?)",
                        (row_id, "ATTEMPT-01", self.seed, utc_now()),
                    )
            finally:
                db.close()
        else:
            raise RuntimeError(f"unknown op: {op}")
        self.ledger.write("stage-ok", stage_id=stage["id"], retry=retry)

    def _record_failure(self, stage: dict[str, Any], exc: BaseException) -> None:
        error: dict[str, Any] = {
            "failed_stage": stage["id"],
            "op": stage["op"],
            "type": type(exc).__name__,
            "message": str(exc),
            "errno": getattr(exc, "errno", None),
            "winerror": getattr(exc, "winerror", None),
        }
        if isinstance(exc, GachaError):
            error["gacha_code"] = exc.code
            error["details"] = getattr(exc, "details", None)
        if isinstance(exc, sqlite3.Error):
            error["sqlite_errorcode"] = getattr(exc, "sqlite_errorcode", None)
            error["sqlite_errorname"] = getattr(exc, "sqlite_errorname", None)
        committed = None
        try:
            if stage["op"] == "sqlite_commit":
                row_id = f"probe-{self.run_id}-{stage['id']}"
                db = self._open_db()
                try:
                    with db.read() as conn:
                        committed = bool(
                            conn.execute(
                                "SELECT 1 FROM storage_probe WHERE id=?", (row_id,)
                            ).fetchone()
                        )
                finally:
                    db.close()
        except Exception as exc:  # noqa: BLE001 - evidence must survive
            error["commit_probe_error"] = f"{type(exc).__name__}: {exc}"
        error["committed"] = committed
        error["traceback"] = traceback.format_exc(limit=6)
        self.failed = {"stage": stage["id"], **error}
        self.ledger.write("stage-failed", **error)

    # ------------------------------------------------------------- barrier
    def wait_release(self, name: str) -> bool:
        deadline = time.time() + float(self.spec.get("barrier_timeout", 120.0))
        marker = os.path.join(self.barrier, name)
        while time.time() < deadline:
            if os.path.exists(marker):
                return True
            time.sleep(0.02)
        self.ledger.write("barrier-timeout", waiting_for=name)
        return False

    # ----------------------------------------------------------- assertions
    def assertions(self) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []

        def check(name: str, ok: bool, **detail: Any) -> None:
            checks.append({"name": name, "ok": bool(ok), **detail})

        originals = hash_tree(self.target, ORIGINALS)
        check("originals_unchanged", originals == self.before["originals"],
              before=self.before["originals"], after=originals)
        published = hash_tree(self.target, PUBLISHED)
        check("published_unchanged", published == self.before["published"],
              before=self.before["published"], after=published)
        expected_seed = self.before["seed"]
        check("seed_reused", self.seed == expected_seed, seed=self.seed)
        # A retry must not create a second accepted manifest for the attempt.
        attempt_dir = os.path.join(
            self.target, "workspace", "results", "JOB-0001", "SRC-0001", "DRAW-0001", "ATTEMPT-01"
        )
        manifests = 0
        if os.path.isdir(attempt_dir):
            manifests = sum(1 for name in os.listdir(attempt_dir) if name == "manifest.json")
        check("single_accepted_manifest", manifests <= 1, manifests=manifests)
        if any(stage["op"] == "sqlite_commit" for stage in self.stages):
            rows = self._db_rows()
            # In ``refused`` mode the batch stopped before the commit, so the
            # row count must be unchanged; otherwise exactly one row for this
            # attempt is allowed.
            expected = self.before["db_rows"] if self.mode == "refused" else self.before["db_rows"] + 1
            check("db_single_commit", rows == expected,
                  before=self.before["db_rows"], after=rows, expected=expected)
        failed_index = None
        if self.mode == "refused" and self.failed:
            failed_index = next(
                (index for index, stage in enumerate(self.stages)
                 if stage["id"] == self.failed["stage"]), None
            )
        for index, stage in enumerate(self.stages):
            if "rel" not in stage:
                continue
            path = os.path.join(self.target, *stage["rel"].split("/"))
            if failed_index is not None and index > failed_index:
                # The product refused; nothing after the failed stage may be
                # published.  The failed stage's own target is the pre-existing
                # file, covered by ``conflict_preserved``.
                check(f"artifact_absent:{stage['id']}", not os.path.exists(path), path=path)
                continue
            if failed_index is not None and index == failed_index:
                continue
            check(f"artifact_present:{stage['id']}", os.path.isfile(path), path=path)
        conflicts = hash_tree(self.target, [self.conflict_rel] if self.conflict_rel else [])
        check("conflict_preserved", conflicts == self.before["conflicts"],
              before=self.before["conflicts"], after=conflicts)
        return checks

    # ---------------------------------------------------------------- main
    def run(self) -> int:
        self.ledger.write("start", scenario=self.scenario, mode=self.mode,
                          target=self.target, stages=[stage["id"] for stage in self.stages])
        self.prepare()
        if self.mode == "refused" and not os.path.isdir(self.target):
            self.ledger.write("prepare-refused", reason="target missing")
            return 3
        self.ledger.write("prepared", before=self.before)
        if self.mode in ("never-ready", "timeout"):
            self.ledger.write("ready-skipped", reason="timeout-mode")
            # Wait long enough for the controller to time out and kill us, but
            # never forever if the controller already died.
            self.wait_release("kill")
            return 4
        self.ledger.write("ready", before=self.before)
        if not self.wait_release("release-1"):
            return 4
        self.ledger.write("released", phase="write")
        for stage in self.stages:
            try:
                self.execute_stage(stage)
            except BaseException as exc:  # noqa: BLE001 - recorded, not swallowed
                self._record_failure(stage, exc)
                break
        self.ledger.write("write-done", failed=self.failed)
        if self.failed is not None:
            if not self.wait_release("release-2"):
                return 4
            self.ledger.write("released", phase="recover")
            recovered_failure = None
            if self.spec.get("recovery", "retry") == "assert_only":
                self.ledger.write("recover-skipped", reason="assert_only")
            else:
                retry_stage = next(
                    (stage for stage in self.stages if stage["id"] == self.failed["stage"]), None
                )
                if retry_stage is None:
                    self.ledger.write("done", result="FAIL", reason="failed stage not found")
                    return 5
                try:
                    self.execute_stage(retry_stage, retry=True)
                    remaining = self.stages[self.stages.index(retry_stage) + 1:]
                    for stage in remaining:
                        self.execute_stage(stage)
                except BaseException as exc:  # noqa: BLE001
                    recovered_failure = {"type": type(exc).__name__, "message": str(exc)}
                    self.ledger.write("recover-failed", **recovered_failure)
        else:
            recovered_failure = None
        try:
            checks = self.assertions()
        except BaseException as exc:  # noqa: BLE001 - report, do not lose the run
            checks = [{"name": "assertions", "ok": False,
                       "error": f"{type(exc).__name__}: {exc}"}]
            self.ledger.write("assertions-error", error=f"{type(exc).__name__}: {exc}",
                              traceback=traceback.format_exc(limit=6))
        ok = all(item["ok"] for item in checks) and recovered_failure is None
        self.ledger.write("assertions", checks=checks, recovery_failure=recovered_failure)
        self.ledger.write("done", result="PASS" if ok else "FAIL",
                          failure=self.failed, recovery_failure=recovered_failure)
        return 0 if ok else 5


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage-synchronised DISK writer")
    parser.add_argument("--spec", required=True)
    args = parser.parse_args(argv)
    with open(args.spec, encoding="utf-8") as handle:
        spec = json.load(handle)
    try:
        return Writer(spec).run()
    except Exception as exc:  # noqa: BLE001 - the controller needs a record
        Ledger(spec["events"], spec["run_id"]).write(
            "crashed", error=f"{type(exc).__name__}: {exc}",
            traceback=traceback.format_exc(limit=6),
        )
        return 6


if __name__ == "__main__":
    sys.exit(main())
