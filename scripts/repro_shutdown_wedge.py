"""Faithful repro: a parent that DRAINS the pipe, exactly like the HUD.

The first attempt spawned the child with `stdout=PIPE` and never read it, so
the 64KB buffer filled during boot and the child was already blocked in
write() before anything was killed. That is a real failure mode but it is NOT
the HUD's: `BrainstemLauncher` attaches a `readabilityHandler` to both pipes
and drains them continuously, right up until the instant it is SIGKILLed.

So this drains in a background thread, lets the child boot fully, and only
then kills the reader — which is the actual sequence on the machine.

  MODE=drain   drain the pipe, then SIGKILL the parent   (faithful to the HUD)
  MODE=file    stdout to a file, then SIGKILL the parent (control)
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODE = sys.argv[1] if len(sys.argv) > 1 else "drain"

env = dict(os.environ)
env["PYTHONPATH"] = f"{REPO}/backend:{REPO}"
env["JARVIS_PROCESS_ROLE"] = "hud"
# The HUD loads brainstem/.env before spawning; without it a different set of
# subsystems starts, which is the last untested difference from the real case.
try:
    for line in open(f"{REPO}/brainstem/.env"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
except Exception as e:
    print("no .env:", e)
env["JARVIS_PARENT_PID"] = str(os.getpid())
env["JARVIS_PARENT_WATCH_GRACE_S"] = "20"
env["JARVIS_PARENT_WATCH_LOG"] = os.path.join(
    os.environ.get("REPRO_OUT_DIR", tempfile.gettempdir()), f"watch-{MODE}.log")

OUT = os.environ.get("REPRO_OUT_DIR", tempfile.mkdtemp(prefix="shutdown-wedge-"))
mirror = open(os.path.join(OUT, f"child-{MODE}.log"), "w")

if MODE == "file":
    p = subprocess.Popen([sys.executable, "-u", "-m", "brainstem"],
                         cwd=REPO, env=env, stdout=mirror,
                         stderr=subprocess.STDOUT)
else:
    p = subprocess.Popen([sys.executable, "-u", "-m", "brainstem"],
                         cwd=REPO, env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT)

    def _drain() -> None:
        """What `readabilityHandler` does: read continuously, forever."""
        assert p.stdout is not None
        for line in p.stdout:
            mirror.write(line.decode("utf-8", "replace"))
            mirror.flush()

    threading.Thread(target=_drain, daemon=True).start()

print(f"child={p.pid} mode={MODE}", flush=True)

# Boot fully. With the pipe drained this now actually completes.
deadline = time.time() + 150
booted = False
while time.time() < deadline:
    time.sleep(2)
    try:
        if "Application startup complete" in open(os.path.join(OUT, f"child-{MODE}.log")).read():
            booted = True
            break
    except Exception:
        pass
print(f"booted={booted}", flush=True)
if p.poll() is not None:
    print(f"child died during boot rc={p.returncode}", flush=True)
    raise SystemExit(1)

time.sleep(3)
print("killing self (SIGKILL) — the drain stops at this instant", flush=True)
sys.stdout.flush()
mirror.flush()
os.kill(os.getpid(), 9)
