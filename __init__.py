"""ComfyUI custom node entry point for Gacha Master.

The heavy backend is importable without ComfyUI; this file only registers the
nodes, the web directory and the server routes.
"""

from __future__ import annotations

import logging

from .comfyui_gacha_master.nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

WEB_DIRECTORY = "./comfyui_gacha_master/web"

logger = logging.getLogger("gacha.entry")

try:
    from .comfyui_gacha_master.integration import register as _register_routes

    _register_routes()
except Exception:
    logger.exception("Gacha Master route registration failed; node registration remains available")

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
