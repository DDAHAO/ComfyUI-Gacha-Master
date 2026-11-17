#!/usr/bin/env python3
"""Run ComfyUI's main.py and translate a stop file into Ctrl+C.

Kept as a maintained copy of the acceptance wrapper: no product behavior, just
the normal KeyboardInterrupt shutdown path a user gets from Ctrl+C.
"""
import json
import os
import runpy
import signal
import sys
import threading
import time

stop = os.environ["GACHA_STOP_FILE"]
comfy = os.environ["GACHA_COMFY_ROOT"]


def watch():
    while not os.path.exists(stop):
        time.sleep(0.2)
    signal.raise_signal(signal.SIGINT)


threading.Thread(target=watch, daemon=True).start()
os.chdir(comfy)
if comfy not in sys.path:
    sys.path.insert(0, comfy)
sys.argv = [os.path.join(comfy, "main.py")] + json.loads(os.environ.get("COMFY_ARGS", "[]"))
runpy.run_path(os.path.join(comfy, "main.py"), run_name="__main__")
