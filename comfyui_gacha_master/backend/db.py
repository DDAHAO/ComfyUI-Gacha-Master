"""Serialized SQLite access.

All reads and writes go through one connection guarded by a process-wide lock;
short ``BEGIN IMMEDIATE`` transactions provide cross-process serialization.
File copying, hashing and network waits are never performed while holding a
write transaction.  Fatal database errors latch a flag that stops scheduling.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Optional, Sequence

from .errors import GachaError
from .schema import SCHEMA_VERSION, apply_migrations, current_schema_version
from .util import utc_now


class Database:
    def __init__(self, path: str, *, busy_timeout: int = 5000) -> None:
        self.path = os.path.abspath(path)
        self.busy_timeout = busy_timeout
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._tx_depth = 0
        self._fatal_code: Optional[str] = None
        self._fatal_message: Optional[str] = None
        self.schema_version = 0
        self.pragmas: dict[str, Any] = {}

    # ---------------------------------------------------------------- open
    def open(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        conn = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout / 1000.0,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        self._conn = conn
        try:
            conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout)}")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA wal_autocheckpoint=1000")
            if int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
                raise GachaError("DB_CORRUPT", "无法启用 SQLite 外键约束。")
            apply_migrations(conn, utc_now())
            self.schema_version = current_schema_version(conn)
            conn.execute(f"PRAGMA user_version={int(self.schema_version)}")
            self.pragmas = {
                "journal_mode": str(conn.execute("PRAGMA journal_mode").fetchone()[0]),
                "synchronous": int(conn.execute("PRAGMA synchronous").fetchone()[0]),
                "foreign_keys": int(conn.execute("PRAGMA foreign_keys").fetchone()[0]),
                "busy_timeout": int(conn.execute("PRAGMA busy_timeout").fetchone()[0]),
                "user_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
            }
            check = conn.execute("PRAGMA quick_check").fetchone()
            if not check or str(check[0]).lower() != "ok":
                raise GachaError(
                    "DB_CORRUPT",
                    "数据库完整性检查失败，已停止写入。",
                    details={"result": str(check[0]) if check else "empty"},
                )
            if self.schema_version > SCHEMA_VERSION:
                raise GachaError(
                    "DB_READ_ONLY",
                    "数据库 schema 比当前插件代码更新，拒绝写入。",
                    details={"db_schema": self.schema_version, "code_schema": SCHEMA_VERSION},
                )
        except RuntimeError as exc:
            # Newer schema or a migration guard.  Never write to a database we
            # do not understand.
            self._fatal_code = "DB_READ_ONLY"
            self._fatal_message = str(exc)
            try:
                conn.close()
            finally:
                self._conn = None
            raise GachaError(
                "DB_READ_ONLY",
                "数据库 schema 与当前代码不兼容，拒绝写入。",
                details={"path": self.path, "error": str(exc)},
            )
        except sqlite3.DatabaseError as exc:
            text = str(exc).lower()
            if "readonly" in text or "read-only" in text:
                self._fatal_code = "DB_READ_ONLY"
                self._fatal_message = str(exc)
                try:
                    conn.close()
                finally:
                    self._conn = None
                raise GachaError(
                    "DB_READ_ONLY",
                    "数据库处于只读状态，已停止调度并保留原文件。",
                    details={"path": self.path, "error": str(exc)},
                )
            self._fatal_code = "DB_CORRUPT"
            self._fatal_message = str(exc)
            try:
                conn.close()
            finally:
                self._conn = None
            raise GachaError(
                "DB_CORRUPT",
                "数据库无法打开或已损坏，已保留原文件并停止调度。",
                details={"path": self.path, "error": str(exc)},
            )

    # ------------------------------------------------------------- helpers
    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise GachaError("SERVICE_NOT_READY", "数据库尚未打开。")
        if self._fatal_code is not None:
            raise GachaError(self._fatal_code, self._fatal_message or "数据库不可用。")
        return self._conn

    def _classify(self, exc: Exception) -> GachaError:
        text = str(exc).lower()
        if isinstance(exc, sqlite3.OperationalError):
            if "locked" in text or "busy" in text:
                return GachaError("DB_BUSY", "数据库正忙，请稍后重试。", retryable=True, details={"error": str(exc)})
            if "readonly" in text or "read-only" in text:
                self.mark_fatal("DB_READ_ONLY", str(exc))
                return GachaError("DB_READ_ONLY", "数据库处于只读状态，已停止调度。", details={"error": str(exc)})
            if "disk i/o" in text or "disk is full" in text or "malformed" in text:
                code = "DISK_LOW" if "full" in text else "DB_CORRUPT"
                self.mark_fatal(code, str(exc))
                return GachaError(code, "数据库写入失败，已停止调度并保留证据。", details={"error": str(exc)})
        if isinstance(exc, sqlite3.DatabaseError) and "malformed" in text:
            self.mark_fatal("DB_CORRUPT", str(exc))
            return GachaError("DB_CORRUPT", "数据库损坏，已停止调度。", details={"error": str(exc)})
        return GachaError("INTERNAL_ERROR", "数据库操作失败。", details={"error": f"{type(exc).__name__}: {exc}"})

    def mark_fatal(self, code: str, message: str) -> None:
        if self._fatal_code is None:
            self._fatal_code = code
            self._fatal_message = message

    @property
    def fatal(self) -> bool:
        return self._fatal_code is not None

    @property
    def fatal_code(self) -> Optional[str]:
        return self._fatal_code

    @property
    def fatal_message(self) -> Optional[str]:
        return self._fatal_message

    def clear_fatal(self) -> None:
        self._fatal_code = None
        self._fatal_message = None

    # --------------------------------------------------------- transactions
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Short write transaction.  Nested calls join the outer transaction."""
        conn = self._require_conn()
        with self._lock:
            outer = self._tx_depth == 0
            if outer:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                except sqlite3.Error as exc:
                    raise self._classify(exc)
            self._tx_depth += 1
            try:
                yield conn
            except BaseException as exc:
                self._tx_depth -= 1
                if outer:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                if isinstance(exc, sqlite3.Error):
                    raise self._classify(exc)
                raise
            else:
                self._tx_depth -= 1
                if outer:
                    try:
                        conn.execute("COMMIT")
                    except sqlite3.Error as exc:
                        try:
                            conn.execute("ROLLBACK")
                        except sqlite3.Error:
                            pass
                        raise self._classify(exc)

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self._require_conn()
        with self._lock:
            yield conn

    def query_all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        conn = self._require_conn()
        with self._lock:
            try:
                return list(conn.execute(sql, params))
            except sqlite3.Error as exc:
                raise self._classify(exc)

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        conn = self._require_conn()
        with self._lock:
            try:
                return conn.execute(sql, params).fetchone()
            except sqlite3.Error as exc:
                raise self._classify(exc)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        conn = self._require_conn()
        with self._lock:
            try:
                return conn.execute(sql, params)
            except sqlite3.Error as exc:
                raise self._classify(exc)

    def executescript(self, script: str) -> None:
        conn = self._require_conn()
        with self._lock:
            try:
                conn.executescript(script)
            except sqlite3.Error as exc:
                raise self._classify(exc)

    def integrity_check(self) -> dict[str, Any]:
        conn = self._require_conn()
        with self._lock:
            try:
                integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
                foreign = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
            except sqlite3.Error as exc:
                raise self._classify(exc)
        return {"integrity": integrity, "foreign_key_issues": foreign}

    def checkpoint(self) -> None:
        conn = self._require_conn()
        with self._lock:
            try:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.Error:
                pass

    # -------------------------------------------------------------- backup
    def backup_to(self, destination: str) -> None:
        """Consistent online backup using sqlite3.Connection.backup()."""
        conn = self._require_conn()
        os.makedirs(os.path.dirname(os.path.abspath(destination)), exist_ok=True)
        with self._lock:
            try:
                dest = sqlite3.connect(destination)
                try:
                    conn.backup(dest)
                    dest.execute("PRAGMA journal_mode=DELETE")
                finally:
                    dest.close()
            except sqlite3.Error as exc:
                raise self._classify(exc)

    # --------------------------------------------------------------- close
    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error:
                    pass
                self._conn.close()
                self._conn = None
