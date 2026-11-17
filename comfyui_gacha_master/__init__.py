"""ComfyUI Gacha Master V1.0.

Backend core for the Gacha Master custom node.  The package is deliberately
importable without ComfyUI, torch, aiohttp or Pillow so the domain, storage,
scheduler and recovery code can be tested on its own.  ComfyUI integration is
loaded lazily by ``integration.py``.
"""

__version__ = "1.0.0"
SCHEMA_VERSION = 1
API_PREFIX = "/gacha_master/v1"
