#!/usr/bin/env python3
"""Finalize a partially executed fake-engine fixture without inference.

The large-list host case needs 5000 sources / 10000 OUT as a *fixture*.  Running
the single-inflight fake scheduler for every draw takes too long; this tool
takes a plan already produced by the product creation worker and writes the
remaining successful attempts, OUT files, manifests, output rows and accepted
relations directly, using the product schema and result layout.

The evidence produced by this tool is always labelled data_source=fixture.
It must never be used as real GPU evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PIL import Image  # noqa: E402

from comfyui_gacha_master.backend.db import Database  # noqa: E402
from comfyui_gacha_master.backend.util import sha256_bytes, utc_now  # noqa: E402


def write_json(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Finalize a fake-engine large fixture")
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args(argv)
    runtime_root = os.path.abspath(args.runtime_root)
    data_root = os.path.join(runtime_root, "user", "gacha_master")
    db_path = os.path.join(data_root, "db", "gacha.sqlite3")
    started = time.time()
    db = Database(db_path, busy_timeout=30000)
    db.open()
    conn = db._require_conn()
    try:
        job = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if job is None:
            raise SystemExit("no job in fixture database")
        job = dict(job)
        results_root = job["results_root"]
        draws = conn.execute(
            """
            SELECT d.id AS draw_id, d.draw_no, d.accepted_attempt_id, d.status,
                   s.id AS source_id, s.source_order, s.sha256 AS source_sha256,
                   s.snapshot_path, s.original_path
            FROM draws d JOIN sources s ON s.id=d.source_id
            WHERE d.job_id=? ORDER BY s.source_order, d.draw_no
            """, (job["id"],)
        ).fetchall()
        buffer = io.BytesIO()
        Image.new("RGB", (40, 30), (30, 30, 30)).save(buffer, "PNG")
        png = buffer.getvalue()
        output_sha = sha256_bytes(png)
        now = utc_now()
        created_outputs = 0
        created_attempts = 0
        already = 0
        steps = 0
        with db.tx() as tx:
            for draw in draws:
                existing = tx.execute(
                    """
                    SELECT a.id AS attempt_id, o.id AS output_id, o.rel_path
                    FROM attempts a JOIN outputs o ON o.attempt_id=a.id
                    WHERE a.draw_id=? AND a.status='SUCCEEDED' AND o.health='VERIFIED'
                    LIMIT 1
                    """, (draw["draw_id"],)
                ).fetchone()
                if existing is not None:
                    tx.execute(
                        "UPDATE draws SET status='SUCCEEDED', accepted_attempt_id=?, "
                        "succeeded_at=COALESCE(succeeded_at,?), updated_at=?, row_version=row_version+1 "
                        "WHERE id=?",
                        (existing["attempt_id"], now, now, draw["draw_id"]),
                    )
                    already += 1
                    continue
                source_order = int(draw["source_order"])
                draw_no = int(draw["draw_no"])
                attempt_id = str(uuid.uuid4())
                output_id = str(uuid.uuid4())
                token = attempt_id.replace("-", "")[:8]
                attempt_dir = os.path.join(
                    results_root,
                    f"JOB-{job['display_id'].split('-', 1)[1] if '-' in job['display_id'] else job['display_id']}",
                    f"SRC-{source_order:04d}",
                    f"DRAW-{draw_no:06d}",
                    f"ATTEMPT-001-{token}",
                )
                # job["result_dir"] is the authoritative root for this job and
                # may differ from a simple display-id layout.
                attempt_dir = os.path.join(
                    job["result_dir"], f"SRC-{source_order:04d}", f"DRAW-{draw_no:06d}",
                    f"ATTEMPT-001-{token}",
                )
                os.makedirs(attempt_dir, exist_ok=True)
                output_path = os.path.join(attempt_dir, "OUT-0001.png")
                with open(output_path, "wb") as handle:
                    handle.write(png)
                    handle.flush()
                    os.fsync(handle.fileno())
                rel_path = os.path.relpath(output_path, results_root).replace(os.sep, "/")
                manifest_outputs = [{
                    "out_no": 1,
                    "filename": "OUT-0001.png",
                    "rel_path": rel_path,
                    "sha256": output_sha,
                    "size": len(png),
                    "width": 40,
                    "height": 30,
                }]
                manifest = {
                    "schema_version": 1,
                    "kind": "attempt_outputs",
                    "generated_at": now,
                    "n": 1,
                    "attempt_id": attempt_id,
                    "identity": {
                        "job_id": job["id"],
                        "source_id": draw["source_id"],
                        "source_order": source_order,
                        "draw_id": draw["draw_id"],
                        "draw_no": draw_no,
                        "attempt_no": 1,
                        "attempt_id": attempt_id,
                    },
                    "outputs": manifest_outputs,
                    "manifest_sha256": sha256_bytes(
                        json.dumps(manifest_outputs, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")).encode("utf-8")
                    ),
                }
                with open(os.path.join(attempt_dir, "outputs.complete.json"), "w", encoding="utf-8") as handle:
                    json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                expected_prompt = f"fixture-{attempt_id}"
                tx.execute(
                    """
                    INSERT INTO attempts (
                      id, draw_id, attempt_no, expected_prompt_id, actual_prompt_id,
                      boot_id, status, submission_intent_at, submitted_at, started_at,
                      finished_at, execution_hash, source_hash, input_stage_name,
                      attempt_dir, remote_state, reconcile_json, error_code,
                      error_message, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        attempt_id, draw["draw_id"], 1, expected_prompt, expected_prompt,
                        "fixture", "SUCCEEDED", now, now, now, now,
                        sha256_bytes(b"fixture-execution"), draw["source_sha256"], None,
                        attempt_dir, "completed", None, None, None, now, now,
                    ),
                )
                tx.execute(
                    """
                    INSERT INTO outputs (
                      id, attempt_id, out_no, rel_path, sha256, size, width, height,
                      health, created_at, verified_at, note
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        output_id, attempt_id, 1, rel_path, output_sha, len(png), 40, 30,
                        "VERIFIED", now, now, "fixture",
                    ),
                )
                tx.execute(
                    "UPDATE draws SET status='SUCCEEDED', accepted_attempt_id=?, "
                    "succeeded_at=?, updated_at=?, row_version=row_version+1 WHERE id=?",
                    (attempt_id, now, now, draw["draw_id"]),
                )
                created_attempts += 1
                created_outputs += 1
                steps += 1
                if steps % 1000 == 0:
                    print(f"finalized {steps} draws", flush=True)
            tx.execute(
                "UPDATE blocks SET status='DONE', completed_at=COALESCE(completed_at,?) WHERE job_id=?",
                (now, job["id"]),
            )
            tx.execute(
                "UPDATE jobs SET status='COMPLETED', run_intent='STOPPED', "
                "completed_at=COALESCE(completed_at,?), updated_at=?, row_version=row_version+1 "
                "WHERE id=?",
                (now, now, job["id"]),
            )
        total_draws = len(draws)
        outputs = conn.execute(
            "SELECT COUNT(*) FROM outputs o JOIN attempts a ON a.id=o.attempt_id "
            "JOIN draws d ON d.id=a.draw_id WHERE d.job_id=?", (job["id"],)
        ).fetchone()[0]
        accepted = conn.execute(
            "SELECT COUNT(*) FROM draws WHERE job_id=? AND status='SUCCEEDED' "
            "AND accepted_attempt_id IS NOT NULL", (job["id"],)
        ).fetchone()[0]
        report = {
            "schema_version": 1,
            "result": "PASS" if accepted == total_draws and outputs >= total_draws else "FAIL",
            "provenance": {"data_source": "fixture", "environment": "isolated", "host": "real"},
            "job_id": job["id"],
            "display_id": job["display_id"],
            "sources": conn.execute(
                "SELECT COUNT(*) FROM sources WHERE job_id=?", (job["id"],)
            ).fetchone()[0],
            "draws": total_draws,
            "outputs": outputs,
            "accepted_draws": accepted,
            "created_attempts": created_attempts,
            "created_outputs": created_outputs,
            "already_succeeded": already,
            "runtime_root": runtime_root,
            "data_root": data_root,
            "results_root": results_root,
            "elapsed_seconds": round(time.time() - started, 2),
            "generated_at": utc_now(),
        }
        write_json(args.report, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["result"] == "PASS" else 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
