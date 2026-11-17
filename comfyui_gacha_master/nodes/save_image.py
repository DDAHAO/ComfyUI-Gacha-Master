"""GachaSaveImage: the controlled SaveImage replacement.

This module is imported only inside ComfyUI, where torch, numpy and Pillow are
available.  The node keeps the standard SaveImage return contract (an IMAGE
tuple) while writing into the plugin's exclusive ATTEMPT directory.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ..backend import storage
from ..backend.errors import GachaError


def _disable_metadata() -> bool:
    try:
        from comfy.cli_args import args

        return bool(getattr(args, "disable_metadata", False))
    except Exception:
        return False


class GachaSaveImage:
    CATEGORY = "gacha_master"
    DESCRIPTION = "ComfyUI 抽卡大师受控输出节点；只接受插件发放的执行令牌。"
    OUTPUT_NODE = True
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("images",)
    FUNCTION = "save_images"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "gacha_token": ("STRING", {"default": "", "multiline": False}),
            }
        }

    @classmethod
    def IS_CHANGED(cls, gacha_token="", **kwargs):
        context = storage.ATTEMPT_CONTEXTS.get(gacha_token) if gacha_token else None
        if context is None:
            return gacha_token
        return f"{gacha_token}:{context.execution_hash}:{context.source_sha256}"

    @classmethod
    def VALIDATE_INPUTS(cls, gacha_token="", **kwargs):
        if not gacha_token:
            return "缺少抽卡大师执行令牌。"
        if gacha_token not in storage.ATTEMPT_CONTEXTS:
            return "执行令牌无效或已过期；该节点只能由抽卡大师任务执行副本使用。"
        return True

    def save_images(self, images, gacha_token):
        try:
            import numpy as np
            from PIL import Image
        except Exception as exc:  # pragma: no cover - runtime dependency
            raise RuntimeError(f"GachaSaveImage 需要 torch/numpy/Pillow: {exc}")
        try:
            context = storage.get_attempt_context(gacha_token)
        except GachaError as exc:
            raise RuntimeError(f"{exc.code}: {exc.message}")
        if not hasattr(images, "shape"):
            raise RuntimeError("GachaSaveImage 只接受一个 IMAGE tensor batch。")
        count = int(images.shape[0]) if len(images.shape) >= 1 else 0
        if count <= 0:
            raise RuntimeError("GachaSaveImage 收到零张图片，不能记为成功。")
        pil_images = []
        for index in range(count):
            try:
                array = 255.0 * images[index].detach().cpu().numpy()
                array = np.clip(array, 0, 255).astype(np.uint8)
                pil_images.append(Image.fromarray(array))
            except Exception as exc:
                raise RuntimeError(f"GachaSaveImage 第 {index + 1} 张张量转换失败: {exc}")
        if _disable_metadata():
            context.include_metadata = False
        try:
            manifest = storage.save_batch(gacha_token, pil_images, max_pixels=context.max_pixels)
        except GachaError as exc:
            raise RuntimeError(f"{exc.code}: {exc.message}")
        ui_images = []
        for item in manifest.get("outputs", []):
            rel = str(item["rel_path"])
            entry = {"filename": item["filename"], "type": "gacha", "subfolder": ""}
            output_root = context.comfy_output_root
            if output_root:
                abs_path = os.path.join(context.results_root, *rel.split("/"))
                try:
                    relative = os.path.relpath(abs_path, output_root)
                    if not relative.startswith(".."):
                        entry["subfolder"] = os.path.dirname(relative).replace(os.sep, "/")
                        entry["filename"] = os.path.basename(relative)
                        entry["type"] = "output"
                except ValueError:
                    pass
            entry["gacha_output_id"] = item.get("out_no")
            ui_images.append(entry)
        return {
            "ui": {
                "images": ui_images,
                "gacha_outputs": [
                    {
                        "out_no": item["out_no"],
                        "filename": item["filename"],
                        "sha256": item["sha256"],
                    }
                    for item in manifest.get("outputs", [])
                ],
            },
            "result": (images,),
        }


NODE_CLASS_MAPPINGS = {"GachaSaveImage": GachaSaveImage}
NODE_DISPLAY_NAME_MAPPINGS = {"GachaSaveImage": "Gacha Save Image (抽卡大师)"}
