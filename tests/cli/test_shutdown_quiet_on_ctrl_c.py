"""Ctrl+C at exit must not print a traceback over the goodbye.

The operator report:

    ^CException ignored in atexit callback: <_final_semaphore_cleanup>
    ...
      File ".../graceful_shutdown.py", line 2406, in cleanup_all_semaphores_sync
        import torch.multiprocessing as torch_mp
    ...
    KeyboardInterrupt:

Two independent defects produced that, and either alone is enough to bring it
back:

1. The handler IMPORTED TORCH at interpreter shutdown — seconds of
   C-extension loading during teardown, which is what stretched the shutdown
   window wide enough for a Ctrl+C to land inside it. It is also unnecessary:
   torch can only own multiprocessing children if torch was already imported,
   so importing it to check CREATES the subsystem it then tears down.

2. The handler was registered raw, so `KeyboardInterrupt` — a BaseException,
   which no `except Exception` catches — escaped into atexit's reporter.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "backend/core/resilience/graceful_shutdown.py"


# --------------------------------------------------------------------------
# 1. no heavyweight import at shutdown
# --------------------------------------------------------------------------

def test_the_cleanup_does_not_import_torch() -> None:
    """Look up, never import. Asserted on the shipped source because the
    import was three call-frames deep in a path only exercised at exit."""
    src = _SRC.read_text()
    assert "import torch.multiprocessing as torch_mp" not in src, (
        "torch is imported again inside a shutdown handler"
    )
    assert 'sys.modules.get("torch.multiprocessing")' in src


def test_running_the_cleanup_leaves_torch_unloaded() -> None:
    """The behavioural half: a process that never used torch must not acquire
    it by shutting down."""
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys;"
         "import backend.core.resilience.graceful_shutdown as gs;"
         "gs.cleanup_all_semaphores_sync();"
         "print('TORCH' if 'torch' in sys.modules else 'CLEAN')"],
        capture_output=True, text=True, timeout=120, cwd=str(_REPO),
        env={**os.environ, "PYTHONPATH": str(_REPO)},
    )
    assert "CLEAN" in proc.stdout, (
        f"shutdown pulled torch into a process that never used it: "
        f"{proc.stdout[-200:]}{proc.stderr[-300:]}"
    )


def test_torch_children_are_still_reaped_when_torch_IS_loaded() -> None:
    """The fix must not silently stop doing the job. When torch is genuinely
    present the same branch runs — proven with a stand-in module so the test
    does not cost a real torch import."""
    probe = (
        "import sys, types;"
        "m = types.ModuleType('torch.multiprocessing');"
        "seen = [];"
        "m.active_children = lambda: seen.append(1) or [];"
        "sys.modules['torch.multiprocessing'] = m;"
        "import backend.core.resilience.graceful_shutdown as gs;"
        "gs.cleanup_all_semaphores_sync();"
        "print('REAPED' if seen else 'SKIPPED')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True,
        timeout=120, cwd=str(_REPO),
        env={**os.environ, "PYTHONPATH": str(_REPO)},
    )
    assert "REAPED" in proc.stdout, (
        f"torch children are no longer reaped when torch IS loaded: "
        f"{proc.stdout[-200:]}"
    )


# --------------------------------------------------------------------------
# 2. nothing escapes the handler
# --------------------------------------------------------------------------

def test_the_shutdown_handlers_are_guarded() -> None:
    """`KeyboardInterrupt` is a BaseException — the `except Exception` these
    handlers already carried never caught it, which is why the traceback
    appeared despite the guards being visible in the source."""
    src = _SRC.read_text()
    assert "guarded_atexit_register(" in src
    assert "atexit.register(_final_semaphore_cleanup)" not in src
    assert "atexit.register(_final_thread_cleanup)" not in src


@pytest.mark.timeout(120)
def test_ctrl_c_during_exit_prints_nothing(tmp_path: Path) -> None:
    """The end-to-end symptom: SIGINT delivered while an exit handler runs.

    Guarded, the process leaves silently. Unguarded, atexit prints
    'Exception ignored in atexit callback' plus a full traceback over the
    operator's goodbye."""
    script = tmp_path / "exiting.py"
    script.write_text(
        "import os, signal, sys, time\n"
        f"sys.path.insert(0, {str(_REPO)!r})\n"
        "from backend.core.ouroboros.governance.exit_guard import "
        "guarded_atexit_register\n"
        "def slow_cleanup():\n"
        "    # Stands in for a handler that takes real time — which is what\n"
        "    # gives a Ctrl+C a window to land in.\n"
        "    os.kill(os.getpid(), signal.SIGINT)\n"
        "    time.sleep(0.2)\n"
        "guarded_atexit_register(slow_cleanup)\n"
        "print('BODY_DONE', flush=True)\n"
    )
    proc = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True,
        timeout=60, cwd=str(_REPO),
        env={**os.environ, "PYTHONPATH": str(_REPO)},
    )
    assert "BODY_DONE" in proc.stdout
    assert "Exception ignored in atexit" not in proc.stderr, (
        f"the traceback is back:\n{proc.stderr[-600:]}"
    )
    assert "KeyboardInterrupt" not in proc.stderr
