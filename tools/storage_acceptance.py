#!/usr/bin/env python3
"""Controlled-volume ENOSPC tooling that refuses unsafe writes by itself.

The 2026-11-15 version checked the volume only in ``preflight()``; a direct
``fill()`` call could point at any directory, reuse an existing run directory
and overwrite a previous filler.  Every write entry point now authorises its
own target, so skipping preflight cannot bypass the guard.

Invariants
----------
* Only a volume registered in a controlled-volume registry can be filled, and
  its serial number must match the observed one.
* The system volume and the volume holding this project are always refused.
* The run directory is created exclusively (``os.mkdir``) and carries an
  ownership marker; an existing or non-empty directory is refused.
* Filler files are created with ``'xb'`` and registered in an off-volume
  manifest *before* the first byte is written, so a hard exit still leaves a
  recoverable record.
* Report, filler manifest, log and stop signal all live outside the controlled
  volume.
* Writes are bounded by ``--headroom-bytes``, ``--max-bytes`` and ``--timeout``,
  and can be stopped through a stop file.
* Cleanup re-checks the serial, the final (junction-resolved) paths and each
  filler's recorded size, then removes only this run's registered files.  It
  never deletes a directory tree by name.

Test seam
---------
``VOLUME_READER`` is the single place the OS is asked about a volume.  Unit
tests replace it to simulate a volume without writing to a real one.  The CLI
never exposes it, and while it is the production reader the literal drive
letter guard is also active, so a refusal cannot be avoided by editing a JSON
registry.

Actions
-------
  status          print volume information for one path
  preflight       inspect path/volume/admin state and write a JSON report
  register        record a controlled volume (serial confirmed by the operator)
  fill            fill to a headroom with bounded, registered writes
  cleanup         remove this run's fillers after re-verifying identity
  recover         cleanup driven by the filler manifest left by a hard exit
  reserve-set     record and set an isolated instance's disk reserve
  reserve-restore restore the recorded previous reserve value

Examples
--------
  python tools/storage_acceptance.py status --path E:\\gacha-enospc
  python tools/storage_acceptance.py register --path E:\\gacha-enospc \\
      --confirm-volume-serial 1234ABCD --capacity-limit-bytes 8589934592 \\
      --registry <evidence>/storage/controlled-volumes.json
  python tools/storage_acceptance.py fill --path E:\\gacha-enospc \\
      --run-dir E:\\gacha-enospc\\run1 --registry <evidence>/storage/controlled-volumes.json \\
      --confirm-volume-serial 1234ABCD --headroom-bytes 67108864 \\
      --report <evidence>/storage/FILL.json
  python tools/storage_acceptance.py cleanup --report <evidence>/storage/FILL.json
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
import shutil
import signal
import sys
import time
from ctypes import wintypes
from datetime import datetime, timezone
from typing import Any, Callable

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHUNK = 8 * 1024 * 1024
OWNER_MARKER = ".gm-run-owner.json"
FILLER_PREFIX = "filler-"
REGISTRY_SCHEMA = 1


class StorageRefusal(RuntimeError):
    """Raised when a write or cleanup cannot be proven safe."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.details}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: str, payload: Any) -> None:
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def is_admin() -> bool:
    if os.name != "nt":
        return os.geteuid() == 0  # type: ignore[attr-defined]
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _final_path(path: str) -> str:
    """Resolve junctions/symlinks to the real path when the OS supports it."""
    absolute = os.path.abspath(path)
    if os.name != "nt":
        return os.path.realpath(absolute)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    handle = kernel32.CreateFileW(
        absolute, 0, 0x1 | 0x2 | 0x4, None, 3, 0x02000000, None
    )
    if handle in (None, wintypes.HANDLE(-1).value):
        return os.path.realpath(absolute)
    try:
        size = kernel32.GetFinalPathNameByHandleW(handle, None, 0, 0)
        buffer = ctypes.create_unicode_buffer(size + 1)
        kernel32.GetFinalPathNameByHandleW(handle, buffer, size + 1, 0)
        value = buffer.value
    finally:
        kernel32.CloseHandle(handle)
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return os.path.abspath(value)


def final_path_for_missing(path: str) -> str:
    """Final path for a path that may not exist yet.

    The deepest existing ancestor is resolved through the OS (so a junction is
    detected) and the missing tail is appended unchanged.
    """
    absolute = os.path.abspath(path)
    tail: list[str] = []
    current = absolute
    while current and not os.path.exists(current):
        parent, name = os.path.split(current)
        if parent == current:
            break
        tail.append(name)
        current = parent
    resolved = _final_path(current) if os.path.exists(current) else _final_path(absolute)
    for name in reversed(tail):
        resolved = os.path.join(resolved, name)
    return os.path.abspath(resolved)


def _nearest_existing(path: str) -> str:
    current = os.path.abspath(path)
    while current and not os.path.exists(current):
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return current or PROJECT_ROOT


def volume_info(path: str) -> dict[str, Any]:
    """Ask the OS about the volume that really holds *path*."""
    final = final_path_for_missing(path)
    probe = _nearest_existing(final)
    info: dict[str, Any] = {"path": os.path.abspath(path), "final_path": final}
    if os.name != "nt":
        usage = shutil.disk_usage(probe)
        info.update({
            "volume_root": os.path.abspath(os.sep),
            "filesystem": "posix",
            "serial": None,
            "free_bytes": usage.free,
            "total_bytes": usage.total,
            "admin": is_admin(),
            "probe_path": probe,
        })
        return info
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    buffer = ctypes.create_unicode_buffer(261)
    if not kernel32.GetVolumePathNameW(probe, buffer, 261):
        raise OSError(ctypes.get_last_error(), "GetVolumePathNameW failed", probe)
    volume_root = buffer.value
    fs_name = ctypes.create_unicode_buffer(261)
    label = ctypes.create_unicode_buffer(261)
    serial = wintypes.DWORD()
    max_component = wintypes.DWORD()
    flags = wintypes.DWORD()
    if not kernel32.GetVolumeInformationW(
        volume_root, label, 261, ctypes.byref(serial), ctypes.byref(max_component),
        ctypes.byref(flags), fs_name, 261,
    ):
        raise OSError(ctypes.get_last_error(), "GetVolumeInformationW failed", volume_root)
    free = ctypes.c_ulonglong()
    total = ctypes.c_ulonglong()
    total_free = ctypes.c_ulonglong()
    if not kernel32.GetDiskFreeSpaceExW(
        volume_root, ctypes.byref(free), ctypes.byref(total), ctypes.byref(total_free)
    ):
        raise OSError(ctypes.get_last_error(), "GetDiskFreeSpaceExW failed", volume_root)
    info.update({
        "volume_root": volume_root,
        "volume_label": label.value,
        "filesystem": fs_name.value,
        "serial": f"{serial.value:08X}",
        "free_bytes": free.value,
        "total_bytes": total.value,
        "admin": is_admin(),
        "probe_path": probe,
    })
    return info


#: Single OS seam.  Tests replace it; the CLI never does.
VOLUME_READER: Callable[[str], dict[str, Any]] = volume_info


def read_volume(path: str) -> dict[str, Any]:
    return VOLUME_READER(path)


def is_within(path: str, root: str) -> bool:
    path = os.path.normcase(os.path.abspath(path))
    root = os.path.normcase(os.path.abspath(root))
    try:
        common = os.path.commonpath([path, root])
    except ValueError:
        return False
    return common == root


def same_path(left: str, right: str) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _volume_key(info: dict[str, Any]) -> str:
    root = info.get("volume_root") or ""
    return os.path.normcase(root.rstrip("\\/") + os.sep) if root else ""


def load_registry(path: str | None) -> dict[str, Any]:
    if not path:
        raise StorageRefusal("E_REGISTRY", "未提供受控卷登记文件；未登记的卷一律拒绝写入")
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        raise StorageRefusal("E_REGISTRY", f"受控卷登记文件不存在: {path}")
    except (OSError, ValueError) as exc:
        raise StorageRefusal("E_REGISTRY", f"受控卷登记文件不可读: {path}", detail=str(exc))
    if not isinstance(document, dict) or not isinstance(document.get("controlled_volumes"), list):
        raise StorageRefusal("E_REGISTRY", "受控卷登记文件结构无效")
    return document


def registry_entry(document: dict[str, Any], info: dict[str, Any]) -> dict[str, Any] | None:
    key = _volume_key(info)
    for entry in document.get("controlled_volumes") or []:
        if not isinstance(entry, dict):
            continue
        if _volume_key({"volume_root": entry.get("volume_root")}) == key:
            return entry
    return None


def _refuse_system_or_project(info: dict[str, Any]) -> None:
    """Literal drive/volume guard, independent of any registry."""
    volume_root = info.get("volume_root") or ""
    system_drive = os.environ.get("SystemDrive", "C:")
    if os.path.normcase(volume_root).startswith(os.path.normcase(system_drive + os.sep)):
        raise StorageRefusal(
            "E_SYSTEM_VOLUME", f"拒绝在系统卷上创建故障数据: {volume_root}"
        )
    project = read_volume(PROJECT_ROOT)
    if _volume_key(info) == _volume_key(project):
        raise StorageRefusal(
            "E_PROJECT_VOLUME", f"拒绝在项目卷上创建故障数据: {volume_root}"
        )


def authorise_target(
    path: str,
    run_dir: str | None,
    registry_path: str | None,
    *,
    serial: str | None = None,
    require_registry: bool = True,
) -> dict[str, Any]:
    """Prove that *path* (and *run_dir*) is this round's controlled volume."""
    if not os.path.isdir(path):
        raise StorageRefusal("E_PATH", f"目标路径不存在或不是目录: {path}")
    final_path = _final_path(path)
    info = read_volume(final_path)
    _refuse_system_or_project(info)
    if str(info.get("filesystem", "")).upper() != "NTFS":
        raise StorageRefusal(
            "E_FILESYSTEM", f"受控卷不是 NTFS: {info.get('filesystem')}"
        )
    if serial is not None and serial.casefold() != str(info.get("serial", "")).casefold():
        raise StorageRefusal(
            "E_SERIAL", "卷序列号不匹配",
            expected=serial, actual=info.get("serial"),
        )
    entry = None
    if require_registry:
        document = load_registry(registry_path)
        entry = registry_entry(document, info)
        if entry is None:
            raise StorageRefusal(
                "E_UNREGISTERED_VOLUME",
                f"卷未登记为受控卷: {info.get('volume_root')}",
                serial=info.get("serial"),
            )
        if str(entry.get("serial", "")).casefold() != str(info.get("serial", "")).casefold():
            raise StorageRefusal(
                "E_SERIAL",
                "登记文件中的卷序列号与实测不一致",
                registry=entry.get("serial"), actual=info.get("serial"),
            )
        limit = entry.get("capacity_limit_bytes")
        if isinstance(limit, int) and limit > 0:
            total = int(info.get("total_bytes") or 0)
            if total > int(limit * 1.05):
                raise StorageRefusal(
                    "E_CAPACITY",
                    "卷容量超过登记的受控上限，拒绝在其上填充",
                    registered_limit=limit, actual_total=total,
                )
        prefix = entry.get("path_prefix")
        if prefix and not is_within(_final_path(prefix), final_path):
            raise StorageRefusal(
                "E_PATH_PREFIX", "目标不在登记路径前缀内",
                prefix=prefix, path=final_path,
            )
    result: dict[str, Any] = {
        "path": os.path.abspath(path),
        "final_path": final_path,
        "volume": info,
        "registry_entry": entry,
    }
    if run_dir is not None:
        final_run = final_path_for_missing(run_dir)
        if not is_within(final_run, final_path):
            raise StorageRefusal(
                "E_RUN_DIR", "run-dir 的真实路径不在受控卷目录内",
                run_dir=run_dir, final_run_dir=final_run, final_path=final_path,
            )
        result["run_dir"] = os.path.abspath(run_dir)
        result["final_run_dir"] = final_run
    return result


def ensure_off_volume(paths: list[tuple[str, str]], volume: dict[str, Any]) -> None:
    """Every durable record must resolve to a different volume."""
    key = _volume_key(volume)
    for label, value in paths:
        if not value:
            continue
        resolved = final_path_for_missing(value)
        info = read_volume(_nearest_existing(resolved))
        if _volume_key(info) == key:
            raise StorageRefusal(
                "E_RECORD_ON_VOLUME",
                f"{label} 位于受控卷内，卷外持久记录要求不满足",
                label=label, path=value, volume=info.get("volume_root"),
            )


class EventLog:
    """Append-only JSONL log outside the controlled volume."""

    def __init__(self, path: str | None) -> None:
        self.path = os.path.abspath(path) if path else None
        if self.path:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            try:
                with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
                    handle.write(
                        json.dumps({"at": utc_now(), "event": "log-open"}, ensure_ascii=False) + "\n"
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                raise StorageRefusal(
                    "E_LOG", f"卷外日志不可写: {self.path}", detail=str(exc)
                )

    def write(self, event: str, **fields: Any) -> None:
        if not self.path:
            return
        payload = {"at": utc_now(), "event": event, **fields}
        try:
            with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            # A record we cannot append after the first successful open is a
            # reason to stop filling, not to continue blind.
            raise StorageRefusal("E_LOG", f"卷外日志写入失败: {self.path}", detail=str(exc))


def preflight(path: str, report: str, registry_path: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": 1,
        "action": "preflight",
        "generated_at": utc_now(),
        "admin": is_admin(),
        "platform": sys.platform,
        "project_root": PROJECT_ROOT,
        "errors": [],
        "warnings": [],
    }
    try:
        info = volume_info(path)
        result["volume"] = info
    except Exception as exc:
        result["errors"].append(f"volume_info: {type(exc).__name__}: {exc}")
        result["result"] = "BLOCKED"
        atomic_json(report, result)
        return result
    project_volume = None
    try:
        project_volume = volume_info(PROJECT_ROOT)
        result["project_volume"] = project_volume
    except Exception as exc:
        result["warnings"].append(f"project_volume: {type(exc).__name__}: {exc}")
    system_drive = os.environ.get("SystemDrive", "C:")
    if os.path.normcase(info["volume_root"]).startswith(os.path.normcase(system_drive + os.sep)):
        result["errors"].append(f"拒绝在系统卷上创建故障数据: {info['volume_root']}")
    if project_volume and info["volume_root"].casefold() == project_volume["volume_root"].casefold():
        result["errors"].append(f"拒绝在项目卷上创建故障数据: {info['volume_root']}")
    if not os.path.isdir(path):
        result["errors"].append(f"路径不存在或不是目录: {path}")
    else:
        probe = os.path.join(path, f".gm-storage-probe-{os.getpid()}")
        try:
            os.makedirs(probe)
            with open(os.path.join(probe, "probe.bin"), "wb") as handle:
                handle.write(b"gacha-master-storage-probe")
                handle.flush()
                os.fsync(handle.fileno())
            with open(os.path.join(probe, "probe.bin"), "rb") as handle:
                result["probe_readback"] = handle.read().decode("ascii")
        except OSError as exc:
            result["errors"].append(f"写入探针失败: {type(exc).__name__}: {exc}")
        finally:
            shutil.rmtree(probe, ignore_errors=True)
    if info["filesystem"].upper() != "NTFS":
        result["warnings"].append(f"文件系统不是 NTFS: {info['filesystem']}")
    if not result["admin"]:
        result["warnings"].append(
            "非管理员：通常无法创建/挂载容量受控的 VHD 或调整卷；需要预置受控卷"
        )
    if registry_path:
        try:
            document = load_registry(registry_path)
            entry = registry_entry(document, info)
            result["registry"] = {"path": registry_path, "entry": entry}
            if entry is None:
                result["warnings"].append(f"卷未登记为受控卷: {info['volume_root']}")
        except StorageRefusal as exc:
            result["warnings"].append(exc.message)
    result["result"] = "READY" if not result["errors"] else "BLOCKED"
    atomic_json(report, result)
    return result


def register(
    path: str,
    serial: str,
    registry: str,
    capacity_limit_bytes: int,
    note: str | None = None,
) -> dict[str, Any]:
    if capacity_limit_bytes <= 0:
        raise StorageRefusal("E_ARGUMENT", "capacity-limit-bytes 必须为正")
    if not os.path.isdir(path):
        raise StorageRefusal("E_PATH", f"路径不存在或不是目录: {path}")
    final = _final_path(path)
    info = read_volume(final)
    _refuse_system_or_project(info)
    if str(info.get("filesystem", "")).upper() != "NTFS":
        raise StorageRefusal("E_FILESYSTEM", f"受控卷不是 NTFS: {info.get('filesystem')}")
    if serial.casefold() != str(info.get("serial", "")).casefold():
        raise StorageRefusal(
            "E_SERIAL", "卷序列号不匹配", expected=serial, actual=info.get("serial")
        )
    if int(info.get("total_bytes") or 0) > int(capacity_limit_bytes * 1.05):
        raise StorageRefusal(
            "E_CAPACITY",
            "卷容量超过声明的受控上限，拒绝登记",
            limit=capacity_limit_bytes, actual_total=info.get("total_bytes"),
        )
    ensure_off_volume([("registry", registry)], info)
    try:
        document = load_registry(registry)
    except StorageRefusal:
        document = {"schema_version": REGISTRY_SCHEMA, "controlled_volumes": []}
    entry = {
        "volume_root": info.get("volume_root"),
        "final_root": final,
        "serial": info.get("serial"),
        "label": info.get("volume_label"),
        "filesystem": info.get("filesystem"),
        "total_bytes": info.get("total_bytes"),
        "capacity_limit_bytes": capacity_limit_bytes,
        "path_prefix": final,
        "registered_at": utc_now(),
        "note": note or "",
    }
    kept = [
        item for item in document.get("controlled_volumes") or []
        if _volume_key({"volume_root": item.get("volume_root")}) != _volume_key(info)
    ]
    kept.append(entry)
    document = {
        "schema_version": REGISTRY_SCHEMA,
        "generated_at": utc_now(),
        "controlled_volumes": kept,
    }
    atomic_json(registry, document)
    return {"schema_version": 1, "action": "register", "result": "REGISTERED", "entry": entry,
            "registry": os.path.abspath(registry)}


def fill(
    path: str,
    run_dir: str,
    serial: str,
    headroom: int,
    report: str,
    max_bytes: int | None = None,
    timeout: float | None = None,
    registry: str | None = None,
    stop_file: str | None = None,
    log_path: str | None = None,
    run_id: str | None = None,
    poll_interval: float = 0.0,
) -> dict[str, Any]:
    """Fill the controlled volume, refusing anything not proven safe."""
    result: dict[str, Any] = {
        "schema_version": 1,
        "action": "fill",
        "generated_at": utc_now(),
        "candidate_run_id": run_id or os.path.basename(os.path.dirname(os.path.abspath(report))),
        "path": os.path.abspath(path),
        "run_dir": os.path.abspath(run_dir),
        "filler_files": [],
        "errors": [],
        "reached_enospc": False,
        "stop_signal": os.path.abspath(stop_file) if stop_file else None,
    }
    manifest_path = report + ".fillers.json"
    log = None
    try:
        if headroom < 0:
            raise StorageRefusal("E_ARGUMENT", "headroom-bytes 不能为负", value=headroom)
        if max_bytes is not None and max_bytes <= 0:
            raise StorageRefusal("E_ARGUMENT", "max-bytes 必须为正", value=max_bytes)
        if timeout is not None and timeout <= 0:
            raise StorageRefusal("E_ARGUMENT", "timeout 必须为正", value=timeout)
        if poll_interval < 0:
            raise StorageRefusal("E_ARGUMENT", "poll-interval 不能为负")
        auth = authorise_target(
            path, run_dir, registry, serial=serial, require_registry=True
        )
        result["volume"] = auth["volume"]
        result["final_path"] = auth["final_path"]
        result["final_run_dir"] = auth["final_run_dir"]
        result["registry_entry"] = auth["registry_entry"]
        ensure_off_volume(
            [
                ("report", report),
                ("filler_manifest", manifest_path),
                ("log", log_path or ""),
                ("stop_file", stop_file or ""),
            ],
            auth["volume"],
        )
        log = EventLog(log_path or manifest_path + ".log")
        log.write("fill-start", run_dir=run_dir, headroom=headroom, max_bytes=max_bytes,
                  timeout=timeout, serial=auth["volume"].get("serial"))
        # Exclusive ownership: an existing run directory belongs to someone
        # else until this run created it.
        try:
            os.mkdir(run_dir)
        except FileExistsError:
            raise StorageRefusal(
                "E_RUN_DIR_EXISTS", "run-dir 已存在；每轮必须使用新建独占目录", run_dir=run_dir
            )
        nonce = os.urandom(16).hex()
        marker = {
            "schema_version": 1,
            "run_id": result["candidate_run_id"],
            "pid": os.getpid(),
            "nonce": nonce,
            "created_at": utc_now(),
            "final_run_dir": auth["final_run_dir"],
            "volume_serial": auth["volume"].get("serial"),
        }
        marker_path = os.path.join(run_dir, OWNER_MARKER)
        with open(marker_path, "x", encoding="utf-8", newline="\n") as handle:
            json.dump(marker, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        result["ownership"] = marker
        atomic_json(
            manifest_path,
            {
                "schema_version": 1,
                "run_id": result["candidate_run_id"],
                "path": os.path.abspath(path),
                "final_path": auth["final_path"],
                "run_dir": os.path.abspath(run_dir),
                "final_run_dir": auth["final_run_dir"],
                "volume_serial": auth["volume"].get("serial"),
                "ownership": marker,
                "started_at": utc_now(),
                "files": [],
            },
        )
    except StorageRefusal as exc:
        result["errors"].append(exc.message)
        result["refusal"] = exc.as_dict()
        result["result"] = "REFUSED"
        result["filler_manifest"] = manifest_path
        try:
            atomic_json(report, result)
        except OSError:
            pass
        return result

    result["free_before"] = auth["volume"]["free_bytes"]
    result["headroom_bytes"] = headroom
    started = time.time()
    written = 0
    index = 0
    stop_reason = None
    try:
        while True:
            if stop_file and os.path.exists(stop_file):
                stop_reason = "stop_signal"
                break
            if max_bytes is not None and written >= max_bytes:
                stop_reason = "max_bytes"
                break
            if timeout is not None and time.time() - started >= timeout:
                stop_reason = "timeout"
                break
            if poll_interval:
                time.sleep(poll_interval)
            free = read_volume(auth["final_path"])["free_bytes"]
            available = free - headroom
            if available <= 0:
                stop_reason = "headroom"
                break
            budget = CHUNK
            if max_bytes is not None:
                budget = min(budget, max_bytes - written)
            chunk = min(budget, available)
            if chunk <= 0:
                stop_reason = "budget"
                break
            filler = os.path.join(run_dir, f"{FILLER_PREFIX}{index:05d}-{nonce[:8]}.bin")
            index += 1
            # Register identity before the first byte so a hard exit leaves a
            # record of the half-written file.
            placeholder = {
                "path": filler,
                "planned_bytes": chunk,
                "registered_at": utc_now(),
                "written_bytes": 0,
                "complete": False,
            }
            result["filler_files"].append(placeholder)
            _write_manifest(manifest_path, auth, marker, result)
            log.write("filler-begin", path=filler, planned_bytes=chunk, free=free)
            try:
                with open(filler, "xb") as handle:
                    remaining = chunk
                    while remaining > 0:
                        block = b"\0" * min(1024 * 1024, remaining)
                        handle.write(block)
                        remaining -= len(block)
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError:
                result["errors"].append(f"filler 已存在，拒绝覆盖: {filler}")
                result["filler_files"].pop()
                stop_reason = "conflict"
                break
            except OSError as exc:
                if exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) == 112:
                    result["reached_enospc"] = True
                    result["enospc_error"] = {
                        "errno": exc.errno,
                        "winerror": getattr(exc, "winerror", None),
                        "message": str(exc),
                        "path": filler,
                    }
                    stop_reason = "enospc"
                else:
                    result["errors"].append(f"{type(exc).__name__}: {exc}")
                    stop_reason = "io_error"
                try:
                    os.unlink(filler)
                except OSError:
                    pass
                result["filler_files"].pop()
                log.write("filler-failed", path=filler, stop_reason=stop_reason)
                break
            size = os.path.getsize(filler)
            written += size
            placeholder["written_bytes"] = size
            placeholder["complete"] = size == chunk
            _write_manifest(manifest_path, auth, marker, result)
            log.write("filler-complete", path=filler, written_bytes=size, total=written)
    except KeyboardInterrupt:
        stop_reason = "interrupted"
        result["interrupted"] = True
    except StorageRefusal as exc:
        result["errors"].append(exc.message)
        stop_reason = "record_failure"
        result["refusal"] = exc.as_dict()
    finally:
        result["stop_reason"] = stop_reason
        result["free_after"] = read_volume(auth["final_path"])["free_bytes"]
        result["written_bytes"] = written
        result["duration_seconds"] = round(time.time() - started, 3)
        result["result"] = (
            "REFUSED" if result["errors"] else
            "ENOSPC_READY" if result["reached_enospc"] else
            "FILLED" if stop_reason == "headroom" else
            "PARTIAL"
        )
        result["filler_manifest"] = manifest_path
        _write_manifest(manifest_path, auth, marker, result, final=True)
        try:
            if log:
                log.write("fill-end", result=result["result"], stop_reason=stop_reason,
                          written_bytes=written)
        except StorageRefusal as exc:
            result["errors"].append(exc.message)
        atomic_json(report, result)
    return result


def _write_manifest(
    path: str,
    auth: dict[str, Any],
    marker: dict[str, Any],
    result: dict[str, Any],
    final: bool = False,
) -> None:
    atomic_json(
        path,
        {
            "schema_version": 1,
            "run_id": result.get("candidate_run_id"),
            "path": auth["path"],
            "final_path": auth["final_path"],
            "run_dir": auth["run_dir"],
            "final_run_dir": auth["final_run_dir"],
            "volume_serial": auth["volume"].get("serial"),
            "ownership": marker,
            "files": result.get("filler_files") or [],
            "stop_reason": result.get("stop_reason"),
            "complete": final,
            "updated_at": utc_now(),
        },
    )


def _read_document(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def cleanup(
    report: str | None = None,
    outer_report: str | None = None,
    manifest: str | None = None,
    registry: str | None = None,
) -> dict[str, Any]:
    """Remove this run's fillers after re-verifying volume and identity."""
    result: dict[str, Any] = {
        "schema_version": 1,
        "action": "cleanup",
        "generated_at": utc_now(),
        "removed": [],
        "skipped": [],
        "errors": [],
    }
    try:
        if manifest:
            source = _read_document(manifest)
            result["source_manifest"] = os.path.abspath(manifest)
        elif report:
            source = _read_document(report)
            result["source_report"] = os.path.abspath(report)
            manifest = source.get("filler_manifest") or report + ".fillers.json"
            if os.path.isfile(manifest) and os.path.abspath(manifest) != os.path.abspath(report):
                try:
                    manifest_document = _read_document(manifest)
                    if manifest_document.get("files"):
                        source = manifest_document
                    result["source_manifest"] = os.path.abspath(manifest)
                except (OSError, ValueError) as exc:
                    result["warnings"] = [f"filler manifest 不可读: {exc}"]
        else:
            raise StorageRefusal("E_ARGUMENT", "必须提供 --report 或 --manifest")
        path = source.get("path")
        run_dir = source.get("run_dir")
        if not path or not run_dir:
            raise StorageRefusal("E_MANIFEST", "记录缺少 path/run_dir", document=source)
        expected_serial = source.get("volume_serial")
        if not expected_serial:
            raise StorageRefusal("E_MANIFEST", "记录缺少卷序列号，无法证明卷身份")
        auth = authorise_target(
            path, run_dir, registry, serial=expected_serial, require_registry=registry is not None
        )
        result["volume"] = auth["volume"]
        result["final_run_dir"] = auth["final_run_dir"]
        if os.path.normcase(auth["final_run_dir"]) != os.path.normcase(
            source.get("final_run_dir") or auth["final_run_dir"]
        ):
            raise StorageRefusal(
                "E_PATH_CHANGED",
                "run-dir 的最终路径与登记时不同（可能被 junction/symlink 重定向）",
                expected=source.get("final_run_dir"),
                actual=auth["final_run_dir"],
            )
        marker_path = os.path.join(run_dir, OWNER_MARKER)
        ownership = source.get("ownership") or {}
        marker_ok = False
        if os.path.isfile(marker_path):
            try:
                current = _read_document(marker_path)
                marker_ok = bool(ownership.get("nonce")) and current.get("nonce") == ownership.get("nonce")
            except (OSError, ValueError):
                marker_ok = False
        if not marker_ok:
            raise StorageRefusal(
                "E_OWNERSHIP", "缺少匹配的目录所有权标记，拒绝清理该目录内容"
            )
        for item in source.get("files") or []:
            filler = item.get("path")
            if not filler:
                continue
            final_filler = final_path_for_missing(filler)
            if not is_within(final_filler, auth["final_run_dir"]):
                result["skipped"].append({"path": filler, "reason": "outside run dir"})
                continue
            if not os.path.basename(final_filler).startswith(FILLER_PREFIX):
                result["skipped"].append({"path": filler, "reason": "not a filler name"})
                continue
            if not os.path.exists(final_filler):
                result["skipped"].append({"path": filler, "reason": "not found"})
                continue
            recorded = item.get("written_bytes")
            actual = os.path.getsize(final_filler)
            if recorded is not None and actual != recorded:
                result["skipped"].append(
                    {"path": filler, "reason": "size differs from registered filler",
                     "recorded": recorded, "actual": actual}
                )
                continue
            try:
                os.unlink(final_filler)
                result["removed"].append(filler)
            except OSError as exc:
                result["errors"].append(f"{filler}: {type(exc).__name__}: {exc}")
    except StorageRefusal as exc:
        result["errors"].append(exc.message)
        result["refusal"] = exc.as_dict()
    except (OSError, ValueError) as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
    finally:
        leftovers = []
        refused = bool(result.get("refusal"))
        if "final_run_dir" in result and os.path.isdir(result["final_run_dir"]):
            try:
                leftovers = sorted(os.listdir(result["final_run_dir"]))
            except OSError:
                leftovers = []
            # A refusal means the directory may belong to someone else: report
            # its contents but never delete anything.
            if not refused and leftovers == [OWNER_MARKER]:
                try:
                    os.unlink(os.path.join(result["final_run_dir"], OWNER_MARKER))
                    leftovers = []
                except OSError:
                    pass
            if not refused and not leftovers:
                try:
                    os.rmdir(result["final_run_dir"])
                except OSError:
                    pass
        result["leftovers"] = leftovers
        result["result"] = (
            "REFUSED" if refused else
            "PARTIAL" if result["errors"] or leftovers else
            "CLEANED"
        )
        result["free_after"] = (result.get("volume") or {}).get("free_bytes")
        atomic_json(outer_report or (report + ".cleanup.json" if report else
                                    (manifest + ".cleanup.json")), result)
    return result


def reserve_set(config: str, value: int, report: str, key: str = "disk_reserve") -> dict[str, Any]:
    """Record the previous reserve, then set the test value."""
    document = _read_document(config)
    before = document.get(key)
    record = {
        "schema_version": 1,
        "action": "reserve-set",
        "generated_at": utc_now(),
        "config": os.path.abspath(config),
        "key": key,
        "before_value": before,
        "test_value": value,
        "restored": False,
    }
    if value < 0:
        raise StorageRefusal("E_ARGUMENT", "reserve 不能为负", value=value)
    document[key] = value
    atomic_json(config, document)
    record["after_set"] = _read_document(config).get(key)
    atomic_json(report, record)
    return record


def reserve_restore(record_path: str, report: str | None = None) -> dict[str, Any]:
    record = _read_document(record_path)
    config = record["config"]
    document = _read_document(config)
    document[record["key"]] = record["before_value"]
    atomic_json(config, document)
    after = _read_document(config).get(record["key"])
    record["restored"] = after == record["before_value"]
    record["restored_at"] = utc_now()
    record["after_restore"] = after
    atomic_json(report or record_path, record)
    if not record["restored"]:
        raise StorageRefusal(
            "E_CONFIG_RESTORE",
            "配置未恢复到测试前的值",
            expected=record["before_value"], actual=after,
        )
    return record


def _report_refusal(report: str, action: str, exc: StorageRefusal) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "action": action,
        "generated_at": utc_now(),
        "result": "REFUSED",
        "errors": [exc.message],
        "refusal": exc.as_dict(),
    }
    try:
        atomic_json(report, payload)
    except OSError:
        pass
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Controlled-volume ENOSPC tooling")
    sub = parser.add_subparsers(dest="action", required=True)

    p_status = sub.add_parser("status")
    p_status.add_argument("--path", required=True)

    p_pre = sub.add_parser("preflight")
    p_pre.add_argument("--path", required=True)
    p_pre.add_argument("--report", required=True)
    p_pre.add_argument("--registry", default=None)

    p_reg = sub.add_parser("register")
    p_reg.add_argument("--path", required=True)
    p_reg.add_argument("--confirm-volume-serial", required=True)
    p_reg.add_argument("--capacity-limit-bytes", type=int, required=True)
    p_reg.add_argument("--registry", required=True)
    p_reg.add_argument("--note", default=None)

    p_fill = sub.add_parser("fill")
    p_fill.add_argument("--path", required=True)
    p_fill.add_argument("--run-dir", required=True)
    p_fill.add_argument("--registry", required=True)
    p_fill.add_argument("--confirm-volume-serial", required=True)
    p_fill.add_argument("--headroom-bytes", type=int, default=64 * 1024 * 1024)
    p_fill.add_argument("--max-bytes", type=int, default=None)
    p_fill.add_argument("--timeout", type=float, default=1800)
    p_fill.add_argument("--stop-file", default=None)
    p_fill.add_argument("--log", default=None)
    p_fill.add_argument("--run-id", default=None)
    p_fill.add_argument("--poll-interval", type=float, default=0.0)
    p_fill.add_argument("--report", required=True)

    p_clean = sub.add_parser("cleanup")
    p_clean.add_argument("--report", default=None)
    p_clean.add_argument("--manifest", default=None)
    p_clean.add_argument("--registry", default=None)
    p_clean.add_argument("--outer-report", default=None)

    p_rec = sub.add_parser("recover")
    p_rec.add_argument("--manifest", required=True)
    p_rec.add_argument("--registry", default=None)
    p_rec.add_argument("--outer-report", default=None)

    p_reset = sub.add_parser("reserve-set")
    p_reset.add_argument("--config", required=True)
    p_reset.add_argument("--value", type=int, required=True)
    p_reset.add_argument("--key", default="disk_reserve")
    p_reset.add_argument("--report", required=True)

    p_restore = sub.add_parser("reserve-restore")
    p_restore.add_argument("--record", required=True)
    p_restore.add_argument("--report", default=None)

    args = parser.parse_args(argv)
    try:
        if args.action == "status":
            print(json.dumps(volume_info(args.path), ensure_ascii=False, indent=2))
            return 0
        if args.action == "preflight":
            result = preflight(args.path, args.report, args.registry)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["result"] == "READY" else 1
        if args.action == "register":
            result = register(
                args.path, args.confirm_volume_serial, args.registry,
                args.capacity_limit_bytes, args.note,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.action == "fill":
            result = fill(
                args.path, args.run_dir, args.confirm_volume_serial,
                args.headroom_bytes, args.report, max_bytes=args.max_bytes,
                timeout=args.timeout, registry=args.registry,
                stop_file=args.stop_file, log_path=args.log, run_id=args.run_id,
                poll_interval=args.poll_interval,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["result"] in ("FILLED", "ENOSPC_READY") else 1
        if args.action == "cleanup":
            if not args.report and not args.manifest:
                parser.error("cleanup 需要 --report 或 --manifest")
            result = cleanup(
                report=args.report, outer_report=args.outer_report,
                manifest=args.manifest, registry=args.registry,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["result"] == "CLEANED" else 1
        if args.action == "recover":
            result = cleanup(
                report=None, outer_report=args.outer_report,
                manifest=args.manifest, registry=args.registry,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["result"] == "CLEANED" else 1
        if args.action == "reserve-set":
            result = reserve_set(args.config, args.value, args.report, args.key)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.action == "reserve-restore":
            result = reserve_restore(args.record, args.report)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
    except StorageRefusal as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
