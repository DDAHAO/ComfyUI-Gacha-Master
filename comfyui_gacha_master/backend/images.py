"""Image decoding, validation and thumbnails.

Pillow is required at runtime (ComfyUI already ships it).  Header parsing is
done before Pillow opens the file so oversized images are rejected before a
potential decompression bomb reaches the decoder.  Animated/multi-frame files
are rejected rather than silently using the first frame.
"""

from __future__ import annotations

import io
import os
import struct
import warnings
from dataclasses import dataclass
from typing import Any, BinaryIO, Optional, Tuple

from .errors import GachaError
from .util import ensure_dir, publish_noreplace, sha256_bytes, write_temp_file

ALLOWED_FORMATS = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}
MIME_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


@dataclass
class ImageInfo:
    format: str
    width: int
    height: int
    mode: str
    frames: int
    extension: str
    mime: str
    size: int
    sha256: str
    orientation: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "width": self.width,
            "height": self.height,
            "mode": self.mode,
            "frames": self.frames,
            "extension": self.extension,
            "mime": self.mime,
            "size": self.size,
            "sha256": self.sha256,
            "orientation": self.orientation,
        }


def _require_pillow():
    try:
        from PIL import Image, ImageOps  # noqa: F401

        return Image, ImageOps
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise GachaError(
            "SERVICE_NOT_READY",
            "缺少 Pillow，无法验证图片。请在 ComfyUI 的 Python 环境中安装 Pillow。",
            details={"error": str(exc)},
        )


def _png_size(data: bytes) -> Optional[Tuple[int, int]]:
    if len(data) < 24 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    if data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def _jpeg_size(data: bytes) -> Optional[Tuple[int, int]]:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    i = 2
    length = len(data)
    while i + 9 < length:
        if data[i] != 0xFF:
            i += 1
            continue
        while i < length and data[i] == 0xFF:
            i += 1
        if i >= length:
            return None
        marker = data[i]
        i += 1
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            continue
        if i + 2 > length:
            return None
        segment_length = struct.unpack(">H", data[i : i + 2])[0]
        if segment_length < 2 or i + segment_length > length:
            return None
        if marker in {
            0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
            0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
        }:
            if i + 7 > length:
                return None
            height, width = struct.unpack(">HH", data[i + 3 : i + 7])
            return int(width), int(height)
        i += segment_length
    return None


def _webp_size(data: bytes) -> Optional[Tuple[int, int]]:
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    chunk = data[12:16]
    if chunk == b"VP8X":
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height
    if chunk == b"VP8 ":
        if data[23:26] != b"\x9d\x01\x2a":
            return None
        width = struct.unpack("<H", data[26:28])[0] & 0x3FFF
        height = struct.unpack("<H", data[28:30])[0] & 0x3FFF
        return width, height
    if chunk == b"VP8L":
        if data[20] != 0x2F:
            return None
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    return None


def header_dimensions(data: bytes) -> Optional[Tuple[int, int]]:
    return _png_size(data) or _jpeg_size(data) or _webp_size(data)


def validate_image_bytes(data: bytes, *, max_pixels: int, sha256: Optional[str] = None) -> ImageInfo:
    if not data:
        raise GachaError("UNSUPPORTED_MEDIA", "图片文件为空。")
    dimensions = header_dimensions(data)
    if dimensions is not None:
        width, height = dimensions
        if width <= 0 or height <= 0:
            raise GachaError("UNSUPPORTED_MEDIA", "图片尺寸无效。")
        if width * height > max_pixels:
            raise GachaError(
                "PAYLOAD_TOO_LARGE",
                "图片像素数超过允许上限。",
                details={"width": width, "height": height, "max_pixels": max_pixels},
            )
    Image, ImageOps = _require_pillow()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            # verify() must be the first operation on a freshly opened image.
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                fmt = (image.format or "").upper()
                if fmt not in ALLOWED_FORMATS:
                    raise GachaError(
                        "UNSUPPORTED_MEDIA",
                        "V1.0 仅接受静态 PNG、JPEG、WebP 图片。",
                        details={"format": fmt or "unknown"},
                    )
                width, height = image.size
                if width <= 0 or height <= 0:
                    raise GachaError("UNSUPPORTED_MEDIA", "图片尺寸无效。")
                if width * height > max_pixels:
                    raise GachaError(
                        "PAYLOAD_TOO_LARGE",
                        "图片像素数超过允许上限。",
                        details={"width": width, "height": height, "max_pixels": max_pixels},
                    )
                frames = int(getattr(image, "n_frames", 1))
                if frames > 1:
                    raise GachaError(
                        "UNSUPPORTED_MEDIA",
                        "动画或多帧图片不受支持，请使用静态单帧图片。",
                        details={"frames": frames, "format": fmt},
                    )
                mode = image.mode
                orientation = None
                try:
                    exif = image.getexif()
                    orientation = exif.get(274) if exif else None
                except Exception:
                    orientation = None
                # Delegate the actual pixel decode to the decoder and require
                # it to complete without truncation.
                image.load()
        return ImageInfo(
            format=fmt,
            width=width,
            height=height,
            mode=mode,
            frames=1,
            extension=ALLOWED_FORMATS[fmt],
            mime=MIME_TYPES[fmt],
            size=len(data),
            sha256=sha256 or sha256_bytes(data),
            orientation=int(orientation) if orientation is not None else None,
        )
    except GachaError:
        raise
    except Image.DecompressionBombError as exc:
        raise GachaError("PAYLOAD_TOO_LARGE", "图片超过解码器允许的像素上限。", details={"error": str(exc)})
    except Image.DecompressionBombWarning as exc:
        raise GachaError("PAYLOAD_TOO_LARGE", "图片超过解码器允许的像素上限。", details={"error": str(exc)})
    except Exception as exc:
        raise GachaError(
            "UNSUPPORTED_MEDIA",
            "图片无法完整解码，可能已损坏或格式不受支持。",
            details={"error": f"{type(exc).__name__}: {exc}"},
        )


def validate_image_file(path: str, *, max_pixels: int, max_bytes: Optional[int] = None) -> ImageInfo:
    size = os.path.getsize(path)
    if max_bytes is not None and size > max_bytes:
        raise GachaError(
            "PAYLOAD_TOO_LARGE",
            "图片文件超过允许大小。",
            details={"path": path, "size": size, "limit": max_bytes},
        )
    with open(path, "rb") as handle:
        data = handle.read()
    return validate_image_bytes(data, max_pixels=max_pixels)


def make_thumbnail(source_path: str, destination_path: str, *, edge: int = 320) -> dict[str, Any]:
    """Create a JPEG thumbnail, never overwriting an existing cache file."""
    Image, ImageOps = _require_pillow()
    if os.path.exists(destination_path):
        try:
            with Image.open(destination_path) as existing:
                existing.load()
            return {"path": destination_path, "cached": True}
        except Exception:
            raise GachaError("OUTPUT_CONFLICT", "缩略图缓存文件损坏，拒绝覆盖。", details={"path": destination_path})
    try:
        with Image.open(source_path) as image:
            image.load()
            image = ImageOps.exif_transpose(image)
            image = image.convert("RGB")
            image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=85, optimize=True)
            data = buffer.getvalue()
    except Exception as exc:
        raise GachaError(
            "OUTPUT_CORRUPT",
            "无法生成缩略图，原图可能已损坏。",
            details={"path": source_path, "error": f"{type(exc).__name__}: {exc}"},
        )
    ensure_dir(os.path.dirname(os.path.abspath(destination_path)))
    temp_path = write_temp_file(os.path.dirname(destination_path), "thumbnail", data)
    try:
        if os.path.exists(destination_path):
            os.unlink(temp_path)
            return {"path": destination_path, "cached": True}
        try:
            publish_noreplace(temp_path, destination_path)
        except FileExistsError:
            # A concurrent reader generated the same content-addressed cache
            # entry first.  That is a cache hit, not a failed image.
            if os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            return {"path": destination_path, "cached": True}
    finally:
        if os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass
    return {"path": destination_path, "cached": False, "edge": edge}
