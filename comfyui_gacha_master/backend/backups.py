"""Consistent SQLite backups, verification and rotation.

Backups are made with the SQLite online backup API to a temporary file, fully
verified (`quick_check`, `foreign_key_check`, schema version), hashed, then
published without overwrite.  A failed or half-written backup is never counted
and never replaces the last valid rolling backup.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from typing import Any, Mapping, Optional

from .db import Database
from .errors import GachaError, not_found
from .schema import SCHEMA_VERSION, current_schema_version
from .util import (
    ensure_dir,
    fault_point,
    fsync_file,
    publish_noreplace,
    sha256_file,
    utc_now,
    write_temp_file,
)


class BackupManager:
    def __init__(self, *, db: Database, paths: Any, settings: Any, repository: Any) -> None:
        self.db = db
        self.paths = paths
        self.settings = settings
        self.repository = repository
        self._lock = threading.RLock()
        self.last_error: Optional[str] = None

    def list_backups(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.query_all(
            "SELECT * FROM backups ORDER BY created_at DESC LIMIT ?", (max(1, min(int(limit), 200)),)
        )
        return [dict(row) for row in rows]

    def verify_backup_file(self, path: str) -> dict[str, Any]:
        if not os.path.isfile(path):
            raise GachaError("NOT_FOUND", "备份文件不存在。", path=path)
        checksum, size = sha256_file(path)
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                quick = conn.execute("PRAGMA quick_check").fetchone()
                if not quick or str(quick[0]).lower() != "ok":
                    raise GachaError("DB_CORRUPT", "备份完整性检查失败。", details={"result": quick})
                integrity_rows = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
                if integrity_rows != ["ok"]:
                    raise GachaError("DB_CORRUPT", "备份 integrity_check 未通过。", details={"result": integrity_rows})
                foreign = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
                if foreign:
                    raise GachaError("DB_CORRUPT", "备份外键检查未通过。", details={"issues": foreign[:20]})
                try:
                    schema = current_schema_version(conn)
                except Exception as exc:
                    raise GachaError("DB_CORRUPT", "备份缺少 schema 版本。", details={"error": str(exc)})
                if schema < 1 or schema > SCHEMA_VERSION:
                    raise GachaError(
                        "DB_READ_ONLY",
                        "备份 schema 与当前代码不兼容。",
                        details={"backup_schema": schema, "code_schema": SCHEMA_VERSION},
                    )
                # Older schema backups stay restorable: db.open() applies the
                # additive migrations after the file is copied back.  A newer
                # schema is never opened or overwritten by this code.
                db_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            finally:
                conn.close()
        except GachaError:
            raise
        except sqlite3.Error as exc:
            raise GachaError("DB_CORRUPT", "备份无法作为 SQLite 数据库打开。", details={"error": str(exc)})
        return {"checksum": checksum, "size": size, "schema_version": schema, "db_version": db_version}

    def create_backup(self, *, reason: str = "scheduled", force: bool = False) -> Optional[dict[str, Any]]:
        with self._lock:
            ensure_dir(self.paths.backups_root)
            backup_id = str(uuid.uuid4())
            stamp = utc_now().replace(":", "").replace("-", "").replace(".", "")[:18]
            filename = f"gacha_{stamp}_{reason}_{backup_id[:8]}.sqlite3"
            final_path = os.path.join(self.paths.backups_root, filename)
            temp_path = os.path.join(self.paths.backups_root, f".{filename}.tmp")
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            try:
                self.db.backup_to(temp_path)
                fault_point("F12")
                verified = self.verify_backup_file(temp_path)
                publish_noreplace(temp_path, final_path)
                fsync_file(final_path)
                record = {
                    "id": backup_id,
                    "path": final_path,
                    "db_version": verified["db_version"],
                    "schema_version": verified["schema_version"],
                    "checksum": verified["checksum"],
                    "status": "VERIFIED",
                    "size": verified["size"],
                    "created_at": utc_now(),
                }
                with self.db.tx() as conn:
                    conn.execute(
                        """
                        INSERT INTO backups(id, path, db_version, schema_version, checksum, status, size, created_at, verified_at)
                        VALUES (?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            record["id"],
                            record["path"],
                            record["db_version"],
                            record["schema_version"],
                            record["checksum"],
                            "VERIFIED",
                            record["size"],
                            record["created_at"],
                            record["created_at"],
                        ),
                    )
                self.last_error = None
                self.rotate()
                return self.get_backup(backup_id)
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                try:
                    if os.path.exists(temp_path):
                        os.unlink(temp_path)
                except OSError:
                    pass
                if isinstance(exc, GachaError):
                    raise
                raise GachaError("INTERNAL_ERROR", "备份失败，已保留现有有效备份。", details={"error": self.last_error})


    def _rotate_candidates(self) -> list[dict[str, Any]]:
        rows = self.db.query_all(
            "SELECT * FROM backups WHERE status='VERIFIED' ORDER BY created_at DESC"
        )
        return [dict(row) for row in rows]

    def rotate(self) -> list[str]:
        with self._lock:
            rows = self._rotate_candidates()
            keep = int(self.settings.backup_retention)
            removed: list[str] = []
            if len(rows) <= keep:
                return removed
            for row in rows[keep:]:
                path = row["path"]
                try:
                    if os.path.isfile(path):
                        os.unlink(path)
                    with self.db.tx() as conn:
                        conn.execute("UPDATE backups SET status='FAILED', error='rotated out' WHERE id=?", (row["id"],))
                    removed.append(path)
                except OSError:
                    # Never pretend a file was removed; leave the DB row valid.
                    continue
            return removed

    def get_backup(self, backup_id: str) -> Optional[dict[str, Any]]:
        row = self.db.query_one("SELECT * FROM backups WHERE id=?", (backup_id,))
        return dict(row) if row else None

    def maybe_backup(self, *, draws_since: int, last_backup_at: Optional[str]) -> Optional[dict[str, Any]]:
        from .util import parse_utc
        from datetime import datetime, timezone

        due_draws = draws_since >= int(self.settings.backup_draws)
        due_time = False
        if last_backup_at:
            then = parse_utc(last_backup_at)
            if then is not None:
                due_time = (datetime.now(timezone.utc) - then).total_seconds() >= int(self.settings.backup_interval)
        else:
            # No successful backup yet: wait for the DRAW threshold rather than
            # creating a backup on every first completion.
            due_time = False
        if due_draws or due_time:
            return self.create_backup(reason="scheduled")
        return None

    def restore(self, backup_id: str, *, before_restore=None) -> dict[str, Any]:
        """Restore a verified backup after preserving the current database.

        The caller must stop scheduling and prove there is no active attempt.
        The current DB, WAL and SHM are copied/moved aside, never deleted.
        """
        with self._lock:
            backup = self.get_backup(backup_id)
            if backup is None:
                raise not_found("备份不存在。", backup_id=backup_id)
            if backup["status"] != "VERIFIED":
                raise GachaError("DB_READ_ONLY", "只能恢复校验通过的备份。", backup_id=backup_id)
            self.verify_backup_file(backup["path"])
            if before_restore is not None:
                before_restore()
            # Consistent copy of the current database for rollback.
            stamp = utc_now().replace(":", "").replace("-", "").replace(".", "")[:18]
            pre_path = os.path.join(self.paths.backups_root, f"pre-restore_{stamp}.sqlite3")
            self.db.backup_to(pre_path)
            pre_verified = self.verify_backup_file(pre_path)
            self.db.checkpoint()
            self.db.close()
            db_path = self.paths.db_path
            wal = db_path + "-wal"
            shm = db_path + "-shm"
            displaced = db_path + f".replaced-{stamp}"
            try:
                if os.path.exists(db_path):
                    os.replace(db_path, displaced)
                for side in (wal, shm):
                    if os.path.exists(side):
                        os.unlink(side)
                import shutil

                shutil.copy2(backup["path"], db_path)
                fsync_file(db_path)
            except Exception as exc:
                # Try to put the original database back.
                try:
                    if os.path.exists(db_path):
                        os.unlink(db_path)
                    if os.path.exists(displaced):
                        os.replace(displaced, db_path)
                except OSError:
                    pass
                self.db.open()
                raise GachaError("INTERNAL_ERROR", "恢复备份失败，已尝试还原原数据库。", details={"error": str(exc)})
            self.db.open()
            self.repository.append_event(
                job_id=None,
                entity_type="BACKUP",
                entity_id=backup_id,
                action="BACKUP_RESTORED",
                reason=f"pre_restore={pre_path}",
                actor="user",
                payload={"pre_restore_sha256": pre_verified["checksum"]},
            )
            return {
                "backup_id": backup_id,
                "restored_at": utc_now(),
                "pre_restore_path": pre_path,
                "pre_restore_checksum": pre_verified["checksum"],
                "schema_version": backup["schema_version"],
            }
