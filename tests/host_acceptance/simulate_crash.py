"""Child process used to prove the supervisor keeps a hard-exit record.

It writes a progress file, then exits without unwinding (the same observable
shape as a killed acceptance child), so the parent report must still contain
the last phase and a non-zero exit code.
"""
import json
import os
import sys

progress = sys.argv[1]
with open(progress, "w", encoding="utf-8") as handle:
    json.dump({"run_id": "simulated-hard-exit", "phase": "running", "pid": os.getpid()}, handle)
    handle.flush()
os._exit(42)
