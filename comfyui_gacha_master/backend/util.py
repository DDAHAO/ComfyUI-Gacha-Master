"""Low-level helpers: time, hashing, durable file publishing and path safety.

The rules implemented here are load-bearing for the data invariants:

* existing files are never overwritten by ``publish_noreplace``;
* JSON checkpoints are written to a temporary file, fsynced, then published;
* a path is only accepted when its resolved real path is inside a registered
  root (no string-prefix checks, no symlink/junction escapes);
* source files are observed before and after a copy so a changing input is
  detected rather than silently snapshotted.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Optional

from .errors import GachaError, conflict


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def utc_now_ts() -> int:
    return int(time.time())


def parse_utc(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def uuid4() -> str:
    return str(uuid.uuid4())


def short_id(value: str, length: int = 8) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", value)[:length]


def display_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_text(value: Any, *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: str, chunk_size: int = 1024 * 1024) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def fsync_file(path: str) -> None:
    if os.name == "nt":
        # Windows: flush the file data; directory handles cannot be fsynced
        # portably from Python.  The platform capability is reported instead.
        with open(path, "rb+") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_dir(path: str) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def create_exclusive_dir(path: str) -> str:
    os.makedirs(path, exist_ok=False)
    return path


def disk_free(path: str) -> int:
    probe = path
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        return shutil.disk_usage(probe or path).free
    except OSError:
        return 0


def require_disk_space(path: str, required: int, reserve: int) -> None:
    free = disk_free(path)
    if free - required < reserve:
        raise GachaError(
            "DISK_LOW",
            "磁盘可用空间不足，已停止后续调度。",
            details={
                "path": path,
                "free_bytes": free,
                "required_bytes": required,
                "reserve_bytes": reserve,
            },
        )


def require_writable_dir(path: str, *, what: str = "目录") -> None:
    """Prove a directory can be written before a DRAW is submitted.

    A read-only filesystem can still report free space, so free-space checks
    alone are insufficient.  The probe is created with O_EXCL and removed
    immediately; a failure is a hard pre-submit block.
    """
    probe = os.path.join(path, f".gacha-write-probe-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, b"probe")
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise GachaError(
            "READ_ONLY",
            f"{what}不可写，已停止后续调度。",
            details={"path": path, "error": f"{type(exc).__name__}: {exc}"},
        ) from exc
    finally:
        try:
            if os.path.exists(probe):
                os.unlink(probe)
        except OSError:
            pass


def _normcase_realpath(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def path_is_within(root: str, path: str) -> bool:
    """True only when ``path`` is the root or a descendant of it.

    Uses realpath + commonpath so Windows case folding, junctions and symlinks
    cannot be used to escape the registered root.  A non-existent path is
    resolved through its existing parent.
    """
    if not root or not path:
        return False
    root_real = _normcase_realpath(root)
    path_real = _normcase_realpath(path)
    try:
        return os.path.commonpath([root_real, path_real]) == root_real
    except ValueError:
        # Different drives on Windows.
        return False


def require_within(root: str, path: str, *, what: str = "路径") -> str:
    absolute = os.path.abspath(path)
    if not path_is_within(root, absolute):
        raise GachaError(
            "PATH_NOT_ALLOWED",
            f"{what}不在允许的根目录内。",
            details={"path": absolute, "root": os.path.abspath(root)},
        )
    return absolute


def reject_unsafe_path(value: str, *, what: str = "路径") -> str:
    if not isinstance(value, str) or not value:
        raise GachaError("BAD_REQUEST", f"{what}无效。")
    if "\x00" in value:
        raise GachaError("PATH_NOT_ALLOWED", f"{what}包含非法字符。")
    if os.path.isabs(value):
        # Callers that accept absolute paths must validate containment first.
        return os.path.abspath(value)
    parts = value.replace("\\", "/").split("/")
    if any(part == ".." for part in parts):
        raise GachaError("PATH_NOT_ALLOWED", f"{what}包含上级目录跳转。")
    return value


def is_regular_file(path: str) -> bool:
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        return False
    return stat.S_ISREG(mode)


def is_link_like(path: str) -> bool:
    try:
        if os.path.islink(path):
            return True
        if hasattr(os.path, "isjunction") and os.path.isjunction(path):
            return True
    except OSError:
        return True
    return False


def iter_direct_files(root: str) -> Iterable[os.DirEntry]:
    """Yield direct regular file entries only, without following links."""
    try:
        entries = list(os.scandir(root))
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_symlink():
                continue
            if hasattr(entry, "is_junction") and entry.is_junction():
                continue
            if entry.is_file(follow_symlinks=False):
                yield entry
        except OSError:
            continue


def natural_sort_key(name: str) -> tuple[Any, bytes]:
    normalized = unicodedata.normalize("NFC", name).casefold()
    parts: list[tuple[int, Any]] = []
    for chunk in re.split(r"(\d+)", normalized):
        if not chunk:
            continue
        if chunk.isdigit():
            parts.append((0, int(chunk)))
        else:
            parts.append((1, chunk))
    return (tuple(parts), unicodedata.normalize("NFC", name).encode("utf-8"))


def _publish_windows(src: str, dst: str) -> None:
    """Atomic no-overwrite move on NTFS.

    ``MoveFileExW`` without ``MOVEFILE_REPLACE_EXISTING`` fails when the target
    exists; ``MOVEFILE_WRITE_THROUGH`` asks the filesystem to complete the move
    before returning.
    """
    import ctypes
    from ctypes import wintypes

    movefile_write_through = 0x8
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    kernel32.MoveFileExW.restype = wintypes.BOOL
    ok = kernel32.MoveFileExW(str(src), str(dst), movefile_write_through)
    if not ok:
        error = ctypes.get_last_error()
        if error in (80, 183):  # ERROR_FILE_EXISTS / ERROR_ALREADY_EXISTS
            raise FileExistsError(error, "target already exists", dst)
        raise OSError(error, "MoveFileExW failed", dst)


def publish_noreplace(src: str, dst: str) -> None:
    """Publish ``src`` at ``dst`` without ever replacing an existing file.

    Windows: MoveFileExW (no replace flag).
    POSIX: hard link + directory fsync + unlink of the temporary name.
    """
    if os.path.exists(dst):
        raise FileExistsError(errno.EEXIST, "target already exists", dst)
    parent = os.path.dirname(os.path.abspath(dst))
    if not os.path.isdir(parent):
        raise FileNotFoundError(errno.ENOENT, "target parent missing", parent)
    if os.name == "nt":
        _publish_windows(src, dst)
        return
    try:
        os.link(src, dst)
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno not in (errno.EPERM, errno.EOPNOTSUPP, errno.EMLINK, errno.EXDEV):
            raise
        # Exclusive-create copy fallback: no overwrite, but not a single atomic
        # publish.  The caller records the downgrade in capabilities.
        with open(src, "rb") as source, open(dst, "xb") as target:
            shutil.copyfileobj(source, target, 1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        fsync_dir(parent)
        os.unlink(src)
        return
    fsync_dir(parent)
    os.unlink(src)


def verify_publish_semantics(root: str) -> dict[str, Any]:
    """Self-test the local filesystem's no-overwrite publish operation."""
    os.makedirs(root, exist_ok=True)
    token = secrets.token_hex(8)
    src1 = os.path.join(root, f".publish-test-{token}-1")
    src2 = os.path.join(root, f".publish-test-{token}-2")
    dst = os.path.join(root, f".publish-test-{token}-target")
    result: dict[str, Any] = {"ok": False, "atomic": False, "error": None}
    try:
        with open(src1, "wb") as handle:
            handle.write(b"first")
            handle.flush()
            os.fsync(handle.fileno())
        publish_noreplace(src1, dst)
        with open(src2, "wb") as handle:
            handle.write(b"second")
            handle.flush()
            os.fsync(handle.fileno())
        blocked = False
        try:
            publish_noreplace(src2, dst)
        except FileExistsError:
            blocked = True
        with open(dst, "rb") as handle:
            content = handle.read()
        result["ok"] = blocked and content == b"first"
        result["atomic"] = result["ok"] and os.name == "nt" or result["ok"]
        if os.path.exists(src2):
            os.unlink(src2)
    except Exception as exc:  # pragma: no cover - platform dependent
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        for path in (src1, src2, dst):
            try:
                if os.path.exists(path):
                    os.unlink(path)
            except OSError:
                pass
    return result


def write_temp_file(directory: str, prefix: str, data: bytes) -> str:
    ensure_dir(directory)
    fd, temp_path = tempfile.mkstemp(prefix=f".{prefix}-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
    return temp_path


def atomic_write_json(path: str, value: Any, *, fsync: bool = True) -> None:
    """Write/replace a small JSON control file atomically (config, checkpoints)."""
    directory = os.path.dirname(os.path.abspath(path))
    ensure_dir(directory)
    temp_path = write_temp_file(directory, os.path.basename(path), json_text(value, pretty=True).encode("utf-8"))
    try:
        os.replace(temp_path, path)
        if fsync:
            fsync_dir(directory)
    finally:
        if os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def publish_json_once(path: str, value: Any) -> bool:
    """Publish a JSON receipt without overwriting an existing one.

    Returns True when published, False when an identical file already exists.
    A differing existing file raises OUTPUT_CONFLICT.
    """
    data = json_text(value, pretty=True).encode("utf-8")
    if os.path.exists(path):
        try:
            with open(path, "rb") as handle:
                existing = handle.read()
        except OSError as exc:
            raise GachaError("OUTPUT_CONFLICT", "既有清单无法读取。", details={"path": path, "error": str(exc)})
        if existing == data:
            return False
        raise GachaError(
            "OUTPUT_CONFLICT",
            "目标清单已存在但内容不同，已停止并保留原文件。",
            details={"path": path},
        )
    temp_path = write_temp_file(os.path.dirname(path), os.path.basename(path), data)
    try:
        publish_noreplace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass
    return True


def read_json(path: str, *, limit: int = 64 * 1024 * 1024) -> Any:
    with open(path, "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise GachaError("PAYLOAD_TOO_LARGE", "JSON 文件超过允许大小。", details={"path": path, "limit": limit})
    return json.loads(data.decode("utf-8"))


@dataclass
class CopyResult:
    sha256: str
    size: int
    source_changed: bool
    target: str


def copy_file_verified(
    src: str,
    dst: str,
    *,
    chunk_size: int = 1024 * 1024,
    fault_name: Optional[str] = None,
    expected_sha256: Optional[str] = None,
) -> CopyResult:
    """Copy a source file with hashing, durability and no-overwrite publish.

    The source is observed before and after reading.  A change in size or
    mtime_ns blocks the copy and leaves no target file.
    """
    before = os.stat(src)
    if not stat.S_ISREG(before.st_mode):
        raise GachaError("INPUT_CHANGED", "输入不是普通文件。", details={"path": src})
    directory = os.path.dirname(os.path.abspath(dst))
    ensure_dir(directory)
    prefix = ".copy-" + hashlib.sha256(dst.encode("utf-8")).hexdigest()[:12]
    fd, temp_path = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=directory)
    digest = hashlib.sha256()
    size = 0
    try:
        with open(src, "rb") as source, os.fdopen(fd, "wb") as target:
            while True:
                chunk = source.read(chunk_size)
                if not chunk:
                    break
                digest.update(chunk)
                target.write(chunk)
                size += len(chunk)
            target.flush()
            os.fsync(target.fileno())
        after = os.stat(src)
        changed = (
            before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or size != after.st_size
        )
        if changed:
            raise GachaError(
                "INPUT_CHANGED",
                "复制过程中源文件发生变化，已阻断。",
                details={
                    "path": src,
                    "size_before": before.st_size,
                    "size_after": after.st_size,
                    "mtime_before_ns": before.st_mtime_ns,
                    "mtime_after_ns": after.st_mtime_ns,
                },
            )
        digest_value = digest.hexdigest()
        if expected_sha256 is not None and digest_value != expected_sha256:
            raise GachaError(
                "OUTPUT_CORRUPT",
                "源文件哈希与冻结值不一致，拒绝发布目标文件。",
                details={"path": src, "expected": expected_sha256, "actual": digest_value},
            )
        if os.path.exists(dst):
            raise FileExistsError(errno.EEXIST, "target exists", dst)
        if fault_name:
            fault_point(fault_name)
        publish_noreplace(temp_path, dst)
        return CopyResult(digest_value, size, False, dst)
    finally:
        if os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def copy_bytes_verified(data: bytes, dst: str) -> CopyResult:
    directory = os.path.dirname(os.path.abspath(dst))
    ensure_dir(directory)
    temp_path = write_temp_file(directory, os.path.basename(dst), data)
    digest = sha256_bytes(data)
    try:
        if os.path.exists(dst):
            raise FileExistsError(errno.EEXIST, "target exists", dst)
        publish_noreplace(temp_path, dst)
        return CopyResult(digest, len(data), False, dst)
    finally:
        if os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    except OSError:
        return


def read_file_limited(path: str, limit: int) -> bytes:
    with open(path, "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise GachaError("PAYLOAD_TOO_LARGE", "文件超过允许大小。", details={"path": path, "limit": limit})
    return data


def parse_int(value: Any, name: str, *, minimum: Optional[int] = None, maximum: Optional[int] = None) -> int:
    if isinstance(value, bool):
        raise GachaError("BAD_REQUEST", f"{name} 必须是整数。")
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise GachaError("BAD_REQUEST", f"{name} 必须是整数。")
    if isinstance(value, float) and not value.is_integer():
        raise GachaError("BAD_REQUEST", f"{name} 必须是整数。")
    if minimum is not None and result < minimum:
        raise GachaError("BAD_REQUEST", f"{name} 不能小于 {minimum}。")
    if maximum is not None and result > maximum:
        raise GachaError("BAD_REQUEST", f"{name} 不能大于 {maximum}。")
    return result


def parse_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise GachaError("BAD_REQUEST", f"{name} 必须是布尔值。")


class FileLock:
    """Cross-process exclusive lock for a data root.

    The lock is held by an open file handle; the PID file is diagnostic only
    and must never be used to steal a lock on heartbeat timeout.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._fd: Optional[int] = None
        self._handle = None

    @property
    def acquired(self) -> bool:
        return self._fd is not None

    def acquire(self) -> bool:
        if self._fd is not None:
            return True
        ensure_dir(os.path.dirname(os.path.abspath(self.path)))
        self._handle = open(self.path, "a+b")
        self._handle.seek(0, os.SEEK_END)
        if self._handle.tell() == 0:
            self._handle.write(b"L")
            self._handle.flush()
        self._handle.seek(0)
        self._fd = self._handle.fileno()
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fd = None
            self._handle.close()
            self._handle = None
            return False
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            self._fd = None
            try:
                self._handle.close()
            finally:
                self._handle = None

    def __enter__(self) -> "FileLock":
        if not self.acquire():
            raise GachaError("INSTANCE_LOCKED", "另一个插件实例正在使用同一数据目录。", details={"path": self.path})
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


def fault_point(name: str) -> None:
    """Deliberate hard exit for crash-window tests.

    Only active when ``GACHA_FAULT`` names this point.  Uses ``os._exit`` so
    ``finally`` blocks and normal shutdown cannot mask the simulated crash.
    """
    if os.environ.get("GACHA_FAULT") != name:
        return
    log_path = os.environ.get("GACHA_FAULT_LOG")
    if log_path:
        try:
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(f"{utc_now()} fault-point {name} pid={os.getpid()}\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            pass
    os._exit(91)


def fault_if_raise(name: str) -> None:
    if os.environ.get("GACHA_FAULT") == name:
        raise RuntimeError(f"injected fault {name}")
