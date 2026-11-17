"""ComfyUI node registrations for Gacha Master."""

from __future__ import annotations

try:
    from .save_image import NODE_CLASS_MAPPINGS as SAVE_CLASSES
    from .save_image import NODE_DISPLAY_NAME_MAPPINGS as SAVE_NAMES
except Exception:  # ComfyUI not present during standalone tests
    SAVE_CLASSES = {}
    SAVE_NAMES = {}

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
NODE_CLASS_MAPPINGS.update(SAVE_CLASSES)
NODE_DISPLAY_NAME_MAPPINGS.update(SAVE_NAMES)

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
