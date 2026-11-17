"""Upload inbox, folder scanning, snapshot/reference source verification.

Original images are read-only.  Snapshot mode writes a verified byte copy into
the job's private source directory.  Reference mode stores absolute path plus
hash/size/mtime and re-verifies the same bytes at every execution; it never
creates a permanent copy.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence

from .errors import GachaError, conflict, not_found
from .images import ImageInfo, validate_image_bytes
from .util import (
    copy_bytes_verified,
    ensure_dir,
    iter_direct_files,
    json_text,
    natural_sort_key,
    path_is_within,
    publish_json_once,
    read_json,
    read_file_limited,
    require_within,
    sha256_bytes,
    sha256_file,
    utc_now,
)

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


@dataclass
class FolderItem:
    name: str
    rel_path: str
    abs_path: str
    info: Optional[ImageInfo] = None
    error: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "name": self.name,
            "rel_path": self.rel_path,
            "valid": self.info is not None,
            "error": self.error,
        }
        if self.info is not None:
            result.update(
                {
                    "size": self.info.size,
                    "sha256": self.info.sha256,
                    "width": self.info.width,
                    "height": self.info.height,
                    "format": self.info.format,
                }
            )
        return result


@dataclass
class ReadImage:
    data: bytes
    info: ImageInfo
    path: str
    mtime_ns: int


class InputManager:
    def __init__(self, *, paths: Any, settings: Any) -> None:
        self.paths = paths
        self.settings = settings

    # ------------------------------------------------------------ uploads
    def create_upload(self, *, temp_path: str, original_name: str, size: int) -> dict[str, Any]:
        if size <= 0:
            raise GachaError("UNSUPPORTED_MEDIA", "上传文件为空。")
        if size > self.settings.upload_max_file:
            raise GachaError(
                "PAYLOAD_TOO_LARGE",
                "上传文件超过允许大小。",
                details={"size": size, "limit": self.settings.upload_max_file},
            )
        with open(temp_path, "rb") as handle:
            data = handle.read(self.settings.upload_max_file + 1)
        if len(data) > self.settings.upload_max_file:
            raise GachaError("PAYLOAD_TOO_LARGE", "上传文件超过允许大小。")
        info = validate_image_bytes(data, max_pixels=self.settings.decode_max_pixels)
        upload_id = str(uuid.uuid4())
        upload_dir = os.path.join(self.paths.inbox_root, upload_id)
        ensure_dir(upload_dir)
        name = os.path.basename(original_name or "upload").replace("\x00", "")
        target_name = "original" + info.extension
        target = os.path.join(upload_dir, target_name)
        copy_bytes_verified(data, target)
        metadata = {
            "schema_version": 1,
            "upload_id": upload_id,
            "original_name": name,
            "stored_name": target_name,
            "size": info.size,
            "sha256": info.sha256,
            "width": info.width,
            "height": info.height,
            "format": info.format,
            "created_at": utc_now(),
        }
        publish_json_once(os.path.join(upload_dir, "upload.json"), metadata)
        return metadata

    def get_upload(self, upload_id: str) -> dict[str, Any]:
        if not upload_id or "/" in upload_id or "\\" in upload_id or ".." in upload_id:
            raise GachaError("BAD_REQUEST", "上传令牌无效。")
        upload_dir = os.path.join(self.paths.inbox_root, upload_id)
        if not path_is_within(self.paths.inbox_root, upload_dir):
            raise GachaError("PATH_NOT_ALLOWED", "上传目录不在受控范围内。")
        metadata_path = os.path.join(upload_dir, "upload.json")
        if not os.path.isfile(metadata_path):
            raise not_found("上传不存在或已被认领。", upload_id=upload_id)
        metadata = read_json(metadata_path)
        metadata["path"] = os.path.join(upload_dir, metadata["stored_name"])
        if not os.path.isfile(metadata["path"]):
            raise GachaError("INPUT_MISSING", "上传文件缺失。", upload_id=upload_id)
        return metadata

    def remove_upload(self, upload_id: str) -> None:
        try:
            upload_dir = os.path.join(self.paths.inbox_root, upload_id)
            if path_is_within(self.paths.inbox_root, upload_dir):
                shutil.rmtree(upload_dir, ignore_errors=True)
        except OSError:
            pass

    # ------------------------------------------------------------ folders
    def allowed_input_roots(self) -> list[str]:
        roots = [os.path.abspath(root) for root in self.settings.allowed_input_roots if root]
        if self.settings.comfy_input_root:
            roots.append(os.path.abspath(self.settings.comfy_input_root))
        seen: list[str] = []
        for root in roots:
            if root not in seen:
                seen.append(root)
        return seen

    def resolve_allowed_root(self, root: str) -> str:
        absolute = os.path.abspath(root)
        for allowed in self.allowed_input_roots():
            if path_is_within(allowed, absolute):
                return absolute
        raise GachaError(
            "PATH_NOT_ALLOWED",
            "目录不在已登记的输入根目录内。请先在设置中登记输入根。",
            details={"root": absolute, "allowed": self.allowed_input_roots()},
        )

    def scan_folder(self, root: str, subdir: str = "") -> dict[str, Any]:
        root = self.resolve_allowed_root(root)
        target = os.path.abspath(os.path.join(root, subdir or ""))
        if not path_is_within(root, target):
            raise GachaError("PATH_NOT_ALLOWED", "子目录越出输入根。")
        if not os.path.isdir(target):
            raise not_found("目录不存在。", path=target)
        items: list[FolderItem] = []
        for entry in iter_direct_files(target):
            name = entry.name
            ext = os.path.splitext(name)[1].lower()
            rel = os.path.relpath(entry.path, root).replace(os.sep, "/")
            if ext not in IMAGE_EXTENSIONS:
                continue
            item = FolderItem(name=name, rel_path=rel, abs_path=entry.path)
            try:
                data = self._read_stable(entry.path)
                item.info = validate_image_bytes(
                    data.data,
                    max_pixels=self.settings.decode_max_pixels,
                    sha256=data.info.sha256,
                )
            except GachaError as exc:
                item.error = {"code": exc.code, "message": exc.message, "details": exc.details}
            except Exception as exc:
                item.error = {"code": "UNSUPPORTED_MEDIA", "message": "无法读取图片。", "details": {"error": str(exc)}}
            items.append(item)
        items.sort(key=lambda item: natural_sort_key(item.name))
        # Hash-based check is repeated at job creation; the scan is a preview.
        return {
            "root": root,
            "subdir": subdir,
            "items": [item.to_dict() for item in items],
            "valid_count": sum(1 for item in items if item.info is not None),
            "invalid_count": sum(1 for item in items if item.info is None),
            "order": [item.rel_path for item in items],
            "recursive": False,
        }

    # ------------------------------------------------------ stable reading
    def _read_stable(self, path: str) -> ReadImage:
        from .util import is_link_like

        if is_link_like(path):
            raise GachaError("PATH_NOT_ALLOWED", "不接受符号链接/junction 文件作为原图。", details={"path": path})
        if not os.path.isfile(path):
            raise GachaError("INPUT_MISSING", "输入文件不存在。", path=path)
        before = os.stat(path)
        if before.st_size > self.settings.upload_max_file:
            raise GachaError(
                "PAYLOAD_TOO_LARGE",
                "输入文件超过允许大小。",
                details={"path": path, "size": before.st_size, "limit": self.settings.upload_max_file},
            )
        with open(path, "rb") as handle:
            data = handle.read(self.settings.upload_max_file + 1)
        after = os.stat(path)
        if len(data) > self.settings.upload_max_file:
            raise GachaError("PAYLOAD_TOO_LARGE", "输入文件超过允许大小。", details={"path": path})
        if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            raise GachaError(
                "INPUT_CHANGED",
                "读取过程中源文件发生变化，已阻断。",
                details={"path": path},
            )
        info = validate_image_bytes(data, max_pixels=self.settings.decode_max_pixels)
        return ReadImage(data=data, info=info, path=path, mtime_ns=after.st_mtime_ns)

    def read_reference(self, path: str) -> ReadImage:
        self.resolve_allowed_root(os.path.dirname(path))
        from .util import is_link_like

        if is_link_like(path):
            raise GachaError("PATH_NOT_ALLOWED", "引用模式不接受符号链接/junction 文件。", details={"path": path})
        return self._read_stable(path)

    # ---------------------------------------------------------- snapshots
    def snapshot_source(
        self,
        *,
        job: Mapping[str, Any],
        source_id: str,
        source_order: int,
        original_name: str,
        source_bytes: bytes,
        image_info: ImageInfo,
        original_path: Optional[str] = None,
    ) -> dict[str, Any]:
        source_dir = os.path.join(job["job_dir"], "sources", source_id)
        ensure_dir(source_dir)
        snapshot_path = os.path.join(source_dir, "original" + image_info.extension)
        if os.path.exists(snapshot_path):
            existing_hash, existing_size = sha256_file(snapshot_path)
            if existing_hash != image_info.sha256 or existing_size != len(source_bytes):
                raise GachaError(
                    "SNAPSHOT_FAILED",
                    "任务快照目录已有不同内容，拒绝覆盖。",
                    details={"path": snapshot_path},
                )
            result = type("R", (), {"sha256": existing_hash, "size": existing_size})()
        else:
            result = copy_bytes_verified(source_bytes, snapshot_path)
        if result.sha256 != image_info.sha256:
            raise GachaError("SNAPSHOT_FAILED", "快照哈希与源哈希不一致。", source_id=source_id)
        reread = self._read_stable(snapshot_path)
        if reread.info.sha256 != image_info.sha256:
            raise GachaError("SNAPSHOT_FAILED", "快照重新解码校验失败。", source_id=source_id)
        return {
            "source_dir": source_dir,
            "snapshot_path": snapshot_path,
            "sha256": result.sha256,
            "size": result.size,
            "width": reread.info.width,
            "height": reread.info.height,
            "format": reread.info.format,
            "original_path": original_path,
        }

    def verify_source(self, source: Mapping[str, Any], *, protection_mode: str) -> ReadImage:
        if protection_mode == "snapshot":
            snapshot_path = source.get("snapshot_path")
            if not snapshot_path or not os.path.isfile(snapshot_path):
                raise GachaError("INPUT_MISSING", "原图快照缺失。", source_id=source["id"])
            read = self._read_stable(snapshot_path)
        else:
            original_path = source.get("original_path")
            if not original_path:
                raise GachaError("INPUT_MISSING", "引用原图路径缺失。", source_id=source["id"])
            read = self.read_reference(original_path)
        if read.info.sha256 != source["sha256"]:
            raise GachaError(
                "INPUT_CHANGED",
                "原图内容与登记哈希不一致，已阻断该任务。",
                details={
                    "source_id": source["id"],
                    "expected": source["sha256"],
                    "actual": read.info.sha256,
                },
            )
        return read

    def relocate_reference(self, source: Mapping[str, Any], new_path: str) -> ReadImage:
        if not source.get("original_path"):
            raise GachaError("WORKFLOW_UNSUPPORTED", "快照模式不支持重新定位原图。")
        absolute = os.path.abspath(new_path)
        self.resolve_allowed_root(os.path.dirname(absolute))
        read = self._read_stable(absolute)
        if read.info.sha256 != source["sha256"]:
            raise GachaError(
                "INPUT_CHANGED",
                "新路径内容与原记录哈希不一致，不能接到旧原图。",
                details={"source_id": source["id"], "expected": source["sha256"], "actual": read.info.sha256},
            )
        return read

    def canonical_snapshot_bytes(self, source: Mapping[str, Any], mode: str) -> bytes:
        return self.verify_source(source, protection_mode=mode).data
