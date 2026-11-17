#!/usr/bin/env python3
"""Independent SQL/file verification for H06-B selection and export.

Reads the browser result (selected output ids + export id), then checks the
SQLite rows, the copied files and the export manifest directly.  It does not
call the product's own verification functions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Independent H06-B export verifier")
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--browser-result", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    with open(args.browser_result, encoding="utf-8") as handle:
        browser = json.load(handle)
    selected = browser.get("selected") or {}
    selected_ids = sorted({value for value in selected.values() if value})
    export_id = (browser.get("latest_export") or {}).get("id")
    problems: list[str] = []
    if not export_id:
        problems.append("browser result has no export id")

    db_path = os.path.join(args.data_root, "db", "gacha.sqlite3")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        export = dict(conn.execute(
            "SELECT * FROM exports WHERE id=? AND job_id=?", (export_id, args.job_id)
        ).fetchone() or {})
        if not export:
            problems.append("export row not found")
        items = [dict(row) for row in conn.execute(
            """
            SELECT ei.*, o.sha256 AS output_sha256, o.size AS output_size, o.health,
                   o.rel_path AS output_rel_path, d.draw_no, s.source_order
            FROM export_items ei
            JOIN outputs o ON o.id=ei.output_id
            JOIN attempts a ON a.id=o.attempt_id
            JOIN draws d ON d.id=a.draw_id
            JOIN sources s ON s.id=d.source_id
            WHERE ei.export_id=? ORDER BY ei.target_rel_path
            """, (export_id,)
        )]
        selections = {
            row["output_id"]: row["selected"]
            for row in conn.execute(
                "SELECT output_id, selected FROM selections WHERE output_id IN (%s)"
                % ",".join("?" for _ in selected_ids),
                selected_ids,
            )
        } if selected_ids else {}
        reviews = [dict(row) for row in conn.execute(
            "SELECT * FROM export_reviews WHERE export_id=?", (export_id,)
        )]
    finally:
        conn.close()

    if export.get("status") != "COMPLETED":
        problems.append(f"export status {export.get('status')}")
    if export.get("kind") != "FINAL":
        problems.append(f"export kind {export.get('kind')}")
    if int(export.get("item_count") or 0) != len(selected_ids):
        problems.append(f"item_count {export.get('item_count')} != selected {len(selected_ids)}")
    if sorted(item["output_id"] for item in items) != selected_ids:
        problems.append("export item ids differ from browser selection")
    for output_id in selected_ids:
        if selections.get(output_id) != 1:
            problems.append(f"DB selection not set for {output_id}")

    item_checks = []
    for item in items:
        target = os.path.join(export.get("batch_dir") or "", *str(item["target_rel_path"]).split("/"))
        exists = os.path.isfile(target)
        digest = sha256_file(target) if exists else None
        size = os.path.getsize(target) if exists else None
        ok = (
            item["status"] == "VERIFIED"
            and exists
            and digest == item["actual_sha256"] == item["output_sha256"]
            and size == item["output_size"] == item["size"]
            and item["health"] == "VERIFIED"
        )
        if not ok:
            problems.append(f"file check failed {item['target_rel_path']}")
        item_checks.append({
            "output_id": item["output_id"],
            "source_order": int(item["source_order"]),
            "draw_no": int(item["draw_no"]),
            "target_rel_path": item["target_rel_path"],
            "status": item["status"],
            "exists": exists,
            "db_actual_sha256": item["actual_sha256"],
            "output_sha256": item["output_sha256"],
            "file_sha256": digest,
            "ok": ok,
        })

    manifest_path = os.path.join(export.get("batch_dir") or "", "manifest.json")
    manifest_ok = False
    manifest = None
    if os.path.isfile(manifest_path):
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
        manifest_ok = (
            manifest.get("kind") == "gacha_export"
            and manifest.get("export_id") == export_id
            and manifest.get("job_id") == args.job_id
            and sorted(item.get("output_id") for item in manifest.get("items", [])) == selected_ids
            and sha256_file(manifest_path) == export.get("manifest_hash")
        )
    if not manifest_ok:
        problems.append("export manifest mismatch")

    result = {
        "schema_version": 1,
        "case": "H06-B-verify",
        "generated_at": utc_now(),
        "provenance": {"data_source": "real_gpu", "environment": "isolated", "host": "real"},
        "data_root": os.path.abspath(args.data_root),
        "job_id": args.job_id,
        "export_id": export_id,
        "selected_ids": selected_ids,
        "export_status": export.get("status"),
        "export_kind": export.get("kind"),
        "batch_dir": export.get("batch_dir"),
        "manifest_path": manifest_path,
        "manifest_hash": export.get("manifest_hash"),
        "item_count": len(items),
        "review_count": len(reviews),
        "items": item_checks,
        "manifest_ok": manifest_ok,
        "problems": problems,
        "result": "PASS" if not problems else "FAIL",
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
