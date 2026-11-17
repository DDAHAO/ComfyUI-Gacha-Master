"""Controlled result storage, receipts and manifest verification.

A GachaSaveImage call saves one IMAGE tensor batch into an exclusive ATTEMPT
directory.  Each OUT file is encoded to a temporary file, fully decoded and
hashed, then published without overwrite.  The complete manifest is published
last as the file-side completion receipt.  The database is only updated after
the scheduler independently re-verifies that manifest and every file.
"""

from __future__ import annotations

import io
import json
import os
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

from .errors import GachaError, conflict
from .images import ImageInfo, validate_image_bytes
from .util import (
    canonical_json,
    create_exclusive_dir,
    ensure_dir,
    fault_point,
    json_text,
    publish_json_once,
    publish_noreplace,
    read_json,
    sha256_bytes,
    sha256_file,
    utc_now,
    write_temp_file,
)

MANIFEST_NAME = "outputs.complete.json"
ATTEMPT_OUTPUT_SCHEMA = 1
ATTEMPT_CONTEXT_LOCK = threading.RLock()
ATTEMPT_CONTEXTS: dict[str, "AttemptContext"] = {}


def job_result_dir(results_root: str, display_id: str) -> str:
    return os.path.join(results_root, display_id)


def source_result_dir(job_result: str, source_order: int) -> str:
    return os.path.join(job_result, f"SRC-{int(source_order):04d}")


def draw_result_dir(source_result: str, draw_no: int) -> str:
    return os.path.join(source_result, f"DRAW-{int(draw_no):06d}")


def attempt_result_dir(draw_result: str, attempt_no: int, attempt_id: str) -> str:
    short = "".join(ch for ch in attempt_id if ch.isalnum())[:8]
    return os.path.join(draw_result, f"ATTEMPT-{int(attempt_no):03d}-{short}")


def allocate_attempt_dir(path: str) -> str:
    ensure_dir(os.path.dirname(path))
    try:
        create_exclusive_dir(path)
    except FileExistsError:
        raise GachaError(
            "OUTPUT_CONFLICT",
            "尝试结果目录已存在，拒绝复用（可能是重复提交）。",
            details={"path": path},
        )
    return path


@dataclass
class AttemptContext:
    job_id: str
    job_display_id: str
    source_id: str
    source_order: int
    draw_id: str
    draw_no: int
    attempt_id: str
    attempt_no: int
    attempt_dir: str
    results_root: str
    source_sha256: str
    execution_hash: str
    include_metadata: bool = True
    max_pixels: int = 100_000_000
    comfy_output_root: Optional[str] = None
    saved: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock)

    def identity(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "job_display_id": self.job_display_id,
            "source_id": self.source_id,
            "source_order": self.source_order,
            "draw_id": self.draw_id,
            "draw_no": self.draw_no,
            "attempt_id": self.attempt_id,
            "attempt_no": self.attempt_no,
            "source_sha256": self.source_sha256,
            "execution_hash": self.execution_hash,
        }


def register_attempt_context(context: AttemptContext) -> None:
    with ATTEMPT_CONTEXT_LOCK:
        if context.attempt_id in ATTEMPT_CONTEXTS:
            raise GachaError("OUTPUT_CONFLICT", "尝试上下文重复注册。", attempt_id=context.attempt_id)
        ATTEMPT_CONTEXTS[context.attempt_id] = context


def get_attempt_context(token: str) -> AttemptContext:
    with ATTEMPT_CONTEXT_LOCK:
        context = ATTEMPT_CONTEXTS.get(token)
    if context is None:
        raise GachaError(
            "OUTPUT_CONFLICT",
            "输出节点收到的执行令牌无效或已过期；拒绝保存。",
            details={"token_present": bool(token)},
        )
    if context.saved:
        raise GachaError(
            "OUTPUT_CONFLICT",
            "同一执行令牌再次保存输出；V1.0 拒绝重复保存以防重复计数。",
            details={"attempt_id": context.attempt_id},
        )
    return context


def unregister_attempt_context(token: str) -> None:
    with ATTEMPT_CONTEXT_LOCK:
        ATTEMPT_CONTEXTS.pop(token, None)


def _png_metadata(identity: Mapping[str, Any], include_metadata: bool) -> Any:
    if not include_metadata:
        return None
    try:
        from PIL.PngImagePlugin import PngInfo

        info = PngInfo()
        info.add_text(
            "gacha_master",
            json.dumps(
                {
                    "schema_version": 1,
                    "job_id": identity["job_id"],
                    "source_id": identity["source_id"],
                    "draw_id": identity["draw_id"],
                    "draw_no": identity["draw_no"],
                    "attempt_id": identity["attempt_id"],
                    "attempt_no": identity["attempt_no"],
                    "source_sha256": identity["source_sha256"],
                    "execution_hash": identity["execution_hash"],
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        return info
    except Exception:
        return None


def save_batch(
    token: str,
    images: Sequence[Any],
    *,
    max_pixels: int,
) -> dict[str, Any]:
    """Save one tensor batch (as PIL images) for an attempt.

    Returns the manifest dictionary.  Raises on any failure; already-published
    OUT files remain on disk as PARTIAL evidence and are never overwritten.
    """
    context = get_attempt_context(token)
    with context.lock:
        if context.saved:
            raise GachaError("OUTPUT_CONFLICT", "同一尝试的输出已经保存。", attempt_id=context.attempt_id)
        if not images:
            raise GachaError("OUTPUT_INCOMPLETE", "输出节点收到零张图片，不能记为成功。")
        os.makedirs(context.attempt_dir, exist_ok=True)
        identity = context.identity()
        pnginfo = _png_metadata(identity, context.include_metadata)
        outputs: list[dict[str, Any]] = []
        for index, image in enumerate(images, start=1):
            if image is None:
                raise GachaError("OUTPUT_INCOMPLETE", f"第 {index} 张输出为空。")
            try:
                buffer = io.BytesIO()
                image.save(buffer, format="PNG", pnginfo=pnginfo, compress_level=4)
                data = buffer.getvalue()
            except Exception as exc:
                raise GachaError(
                    "OUTPUT_INCOMPLETE",
                    f"第 {index} 张输出编码 PNG 失败。",
                    details={"error": f"{type(exc).__name__}: {exc}"},
                )
            info = validate_image_bytes(data, max_pixels=max_pixels)
            if info.width <= 0 or info.height <= 0:
                raise GachaError("OUTPUT_INCOMPLETE", f"第 {index} 张输出尺寸无效。")
            name = f"OUT-{index:04d}.png"
            target = os.path.join(context.attempt_dir, name)
            temp_path = write_temp_file(context.attempt_dir, f".{name}", data)
            fault_point("F04")
            try:
                if os.path.exists(target):
                    raise GachaError(
                        "OUTPUT_CONFLICT",
                        f"输出文件已存在，拒绝覆盖：{name}",
                        details={"path": target},
                    )
                publish_noreplace(temp_path, target)
            except FileExistsError:
                raise GachaError(
                    "OUTPUT_CONFLICT",
                    f"输出文件已存在，拒绝覆盖：{name}",
                    details={"path": target},
                )
            finally:
                if os.path.exists(temp_path):
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
            outputs.append(
                {
                    "out_no": index,
                    "filename": name,
                    "rel_path": os.path.relpath(target, context.results_root).replace(os.sep, "/"),
                    "sha256": info.sha256,
                    "size": info.size,
                    "width": info.width,
                    "height": info.height,
                }
            )
        fault_point("F05")
        manifest = {
            "schema_version": ATTEMPT_OUTPUT_SCHEMA,
            "kind": "attempt_outputs",
            "generated_at": utc_now(),
            "n": len(outputs),
            "attempt_id": context.attempt_id,
            "identity": identity,
            "outputs": outputs,
        }
        manifest["manifest_sha256"] = sha256_bytes(canonical_json(outputs).encode("utf-8"))
        manifest_path = os.path.join(context.attempt_dir, MANIFEST_NAME)
        publish_json_once(manifest_path, manifest)
        context.saved = True
        return manifest


def read_attempt_manifest(attempt_dir: str) -> Optional[dict[str, Any]]:
    path = os.path.join(attempt_dir, MANIFEST_NAME)
    if not os.path.exists(path):
        return None
    try:
        manifest = read_json(path)
    except Exception as exc:
        raise GachaError(
            "OUTPUT_CORRUPT",
            "输出清单无法读取。",
            details={"path": path, "error": str(exc)},
        )
    if not isinstance(manifest, dict):
        raise GachaError("OUTPUT_CORRUPT", "输出清单格式错误。", details={"path": path})
    return manifest


def verify_attempt_outputs(
    *,
    attempt_dir: str,
    results_root: str,
    expected: Mapping[str, Any],
    max_pixels: int,
) -> list[dict[str, Any]]:
    """Independently verify a completed attempt manifest and every OUT file."""
    manifest = read_attempt_manifest(attempt_dir)
    if manifest is None:
        raise GachaError("OUTPUT_INCOMPLETE", "缺少 outputs.complete.json，不能认定成功。", details={"attempt_dir": attempt_dir})
    if manifest.get("schema_version") != ATTEMPT_OUTPUT_SCHEMA or manifest.get("kind") != "attempt_outputs":
        raise GachaError("OUTPUT_CORRUPT", "输出清单版本或类型不匹配。", details={"attempt_dir": attempt_dir})
    identity = manifest.get("identity") or {}
    for key in ("job_id", "source_id", "draw_id", "attempt_id", "source_sha256", "execution_hash"):
        if str(identity.get(key)) != str(expected.get(key)):
            raise GachaError(
                "OUTPUT_CONFLICT",
                "输出清单身份与数据库记录不一致。",
                details={"field": key, "manifest": identity.get(key), "expected": expected.get(key)},
            )
    outputs = manifest.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise GachaError("OUTPUT_INCOMPLETE", "输出清单为空。", details={"attempt_dir": attempt_dir})
    if int(manifest.get("n", -1)) != len(outputs):
        raise GachaError("OUTPUT_CORRUPT", "输出清单数量与 n 不一致。", details={"attempt_dir": attempt_dir})
    manifest_hash = sha256_bytes(canonical_json(outputs).encode("utf-8"))
    if manifest.get("manifest_sha256") != manifest_hash:
        raise GachaError("OUTPUT_CORRUPT", "输出清单内容哈希不匹配。", details={"attempt_dir": attempt_dir})
    verified: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for index, output in enumerate(outputs, start=1):
        if not isinstance(output, dict):
            raise GachaError("OUTPUT_CORRUPT", "输出清单条目格式错误。")
        if int(output.get("out_no", 0)) != index:
            raise GachaError("OUTPUT_CORRUPT", "OUT 编号不连续或乱序。", details={"expected": index, "got": output.get("out_no")})
        filename = str(output.get("filename") or "")
        if not filename or filename in seen_names or "/" in filename or "\\" in filename:
            raise GachaError("OUTPUT_CORRUPT", "输出文件名非法或重复。", details={"filename": filename})
        seen_names.add(filename)
        target = os.path.join(attempt_dir, filename)
        if not os.path.isfile(target):
            raise GachaError("OUTPUT_INCOMPLETE", "输出文件缺失。", details={"path": target})
        actual_hash, actual_size = sha256_file(target)
        if actual_hash != output.get("sha256") or actual_size != int(output.get("size", -1)):
            raise GachaError("OUTPUT_CORRUPT", "输出文件哈希或大小不匹配。", details={"path": target})
        with open(target, "rb") as handle:
            info = validate_image_bytes(handle.read(), max_pixels=max_pixels)
        if info.width != int(output.get("width", -1)) or info.height != int(output.get("height", -1)):
            raise GachaError("OUTPUT_CORRUPT", "输出文件尺寸不匹配。", details={"path": target})
        rel_path = os.path.relpath(target, results_root).replace(os.sep, "/")
        if output.get("rel_path") != rel_path:
            raise GachaError("OUTPUT_CONFLICT", "输出清单相对路径与结果根不一致。", details={"path": target})
        verified.append(
            {
                "id": _output_id(identity["attempt_id"], index),
                "out_no": index,
                "rel_path": rel_path,
                "sha256": actual_hash,
                "size": actual_size,
                "width": info.width,
                "height": info.height,
            }
        )
    return verified


def _output_id(attempt_id: str, out_no: int) -> str:
    import uuid

    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"gacha-master/output/{attempt_id}/{out_no}"))


def scan_attempt_files(attempt_dir: str, results_root: str, *, max_pixels: int) -> list[dict[str, Any]]:
    """Collect partial evidence from an attempt directory (no success claim)."""
    if not os.path.isdir(attempt_dir):
        return []
    found: list[dict[str, Any]] = []
    for name in sorted(os.listdir(attempt_dir)):
        if not (name.startswith("OUT-") and name.lower().endswith(".png")):
            continue
        path = os.path.join(attempt_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as handle:
                data = handle.read()
            info = validate_image_bytes(data, max_pixels=max_pixels)
            found.append(
                {
                    "id": _output_id(os.path.basename(attempt_dir), len(found) + 1),
                    "out_no": len(found) + 1,
                    "rel_path": os.path.relpath(path, results_root).replace(os.sep, "/"),
                    "sha256": info.sha256,
                    "size": info.size,
                    "width": info.width,
                    "height": info.height,
                    "health": "PARTIAL",
                    "note": "saved before terminal verification; retained as evidence",
                }
            )
        except Exception:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            found.append(
                {
                    "id": _output_id(os.path.basename(attempt_dir), len(found) + 1),
                    "out_no": len(found) + 1,
                    "rel_path": os.path.relpath(path, results_root).replace(os.sep, "/"),
                    "sha256": "",
                    "size": size,
                    "width": 0,
                    "height": 0,
                    "health": "CORRUPT",
                    "note": "partial or corrupt output file retained as evidence",
                }
            )
    return found


def write_terminal_receipt(
    job_dir: str,
    *,
    attempt_id: str,
    evidence: Mapping[str, Any],
) -> str:
    """Persist a controlled terminal evidence receipt before DB bookkeeping."""
    receipt_dir = os.path.join(job_dir, "receipts", attempt_id)
    ensure_dir(receipt_dir)
    path = os.path.join(receipt_dir, "terminal.json")
    payload = {
        "schema_version": 1,
        "kind": "terminal_evidence",
        "recorded_at": utc_now(),
        "attempt_id": attempt_id,
        "evidence": dict(evidence),
    }
    publish_json_once(path, payload)
    fault_point("F07")
    return path


def read_terminal_receipt(job_dir: str, attempt_id: str) -> Optional[dict[str, Any]]:
    path = os.path.join(job_dir, "receipts", attempt_id, "terminal.json")
    if not os.path.exists(path):
        return None
    try:
        return read_json(path)
    except Exception as exc:
        raise GachaError("OUTPUT_CORRUPT", "终态收据无法读取。", details={"path": path, "error": str(exc)})


def write_job_manifest(
    job_result_dir: str,
    *,
    job: Mapping[str, Any],
    summary: Mapping[str, Any],
) -> str:
    path = os.path.join(job_result_dir, "manifest.json")
    payload = {
        "schema_version": 1,
        "kind": "job_manifest",
        "generated_at": utc_now(),
        "job_id": job["id"],
        "display_id": job["display_id"],
        "status": job["status"],
        "summary": dict(summary),
    }
    # This auxiliary manifest is refreshed; businesses truth remains SQLite.
    from .util import atomic_write_json

    atomic_write_json(path, payload)
    return path


def staging_filename(attempt_id: str, extension: str) -> str:
    ext = extension if extension.startswith(".") else f".{extension}"
    safe = "".join(ch for ch in attempt_id if ch.isalnum())
    return f"gacha_master_{safe}{ext}"


def stage_input_file(
    *,
    source_bytes: bytes,
    comfy_input_root: str,
    attempt_id: str,
    extension: str,
    max_pixels: int,
) -> tuple[str, str]:
    """Write a verified, attempt-unique input file under ComfyUI's input root.

    The original file is never modified.  The staged name is a direct child of
    the input directory so the native LoadImage combo can list it.  It is
    removed after the attempt reaches a terminal state.
    """
    info = validate_image_bytes(source_bytes, max_pixels=max_pixels)
    name = staging_filename(attempt_id, extension or info.extension)
    target = os.path.join(comfy_input_root, name)
    ensure_dir(comfy_input_root)
    if os.path.exists(target):
        existing_hash, _ = sha256_file(target)
        if existing_hash == info.sha256:
            return name, target
        raise GachaError(
            "OUTPUT_CONFLICT",
            "输入暂存文件名冲突且内容不同，拒绝覆盖。",
            details={"path": target},
        )
    temp_path = write_temp_file(comfy_input_root, "gacha-stage", source_bytes)
    try:
        publish_noreplace(temp_path, target)
    finally:
        if os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass
    return name, target


def remove_staged_input(comfy_input_root: Optional[str], name: Optional[str]) -> None:
    if not comfy_input_root or not name:
        return
    if os.path.basename(name) != name:
        return
    path = os.path.join(comfy_input_root, name)
    try:
        if os.path.isfile(path):
            os.unlink(path)
    except OSError:
        pass
