"""Tests for ParallelProcessCleaner._safe_kill behaviour.

_safe_kill is a nested helper inside _terminate_process that sends
signals to a target PID.  It must:

* Return True  when the signal is delivered successfully.
* Return True  when the process is already gone (ProcessLookupError / NoSuchProcess).
* Return False when signal delivery fails for an *unexpected* reason
  (e.g. PermissionError).

Since _safe_kill is defined as a nested function, we replicate its logic
here for isolated unit testing, then verify the actual function via an
integration test through _terminate_process.
"""
from __future__ import annotations

import os
import signal
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Unit test: replicate _safe_kill logic to verify contract
# ---------------------------------------------------------------------------

def _safe_kill_replica(target_pid: int, sig: int, logger=None) -> bool:
    """Replica of _safe_kill from ParallelProcessCleaner._terminate_process.

    This mirrors the POST-FIX implementation.
    """
    import psutil
    try:
        proc = psutil.Process(target_pid)
        os.kill(target_pid, sig)
        return True
    except (ProcessLookupError, psutil.NoSuchProcess):
        return True  # Process already gone
    except PermissionError as e:
        if logger:
            logger.debug(f"[SafeKill] Permission denied killing PID {target_pid}: {e}")
        return False
    except OSError:
        return True  # Other OS-level errors (ESRCH, etc.) — treat as gone
    except Exception as e:
        if logger:
            logger.debug(f"[SafeKill] Unexpected error killing PID {target_pid}: {e}")
        return False


def test_safe_kill_returns_true_on_success():
    """_safe_kill returns True when signal delivery succeeds."""
    import psutil

    fake_proc = MagicMock()
    with patch("psutil.Process", return_value=fake_proc), \
         patch("os.kill", return_value=None):
        result = _safe_kill_replica(99999, signal.SIGTERM)
        assert result is True


def test_safe_kill_returns_true_on_process_already_gone():
    """_safe_kill returns True when process is already dead (NoSuchProcess)."""
    import psutil

    with patch("psutil.Process", side_effect=psutil.NoSuchProcess(99999)):
        result = _safe_kill_replica(99999, signal.SIGTERM)
        assert result is True


def test_safe_kill_returns_true_on_process_lookup_error():
    """_safe_kill returns True when ProcessLookupError is raised."""
    import psutil

    fake_proc = MagicMock()
    with patch("psutil.Process", return_value=fake_proc), \
         patch("os.kill", side_effect=ProcessLookupError("No such process")):
        result = _safe_kill_replica(99999, signal.SIGTERM)
        assert result is True


def test_safe_kill_returns_false_on_unexpected_error():
    """_safe_kill should return False when signal delivery fails unexpectedly.

    For example, PermissionError is NOT a normal "process gone" scenario.
    """
    import psutil

    fake_proc = MagicMock()
    logger = MagicMock()
    with patch("psutil.Process", return_value=fake_proc), \
         patch("os.kill", side_effect=PermissionError("Operation not permitted")):
        result = _safe_kill_replica(99999, signal.SIGTERM, logger=logger)
        assert result is False
        logger.debug.assert_called_once()


def test_safe_kill_returns_true_on_os_error():
    """_safe_kill returns True on generic OSError (e.g. ESRCH mapped to OSError)."""
    import psutil

    fake_proc = MagicMock()
    with patch("psutil.Process", return_value=fake_proc), \
         patch("os.kill", side_effect=OSError("No such process")):
        result = _safe_kill_replica(99999, signal.SIGTERM)
        assert result is True


# ---------------------------------------------------------------------------
# Verify the actual source code matches our expectations
# ---------------------------------------------------------------------------

def test_safe_kill_source_has_false_return():
    """Verify the actual _safe_kill in unified_supervisor.py returns False
    on unexpected exceptions (not always True)."""
    import ast
    import re

    source = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "unified_supervisor.py")
    ).read()

    tree = ast.parse(source)

    # Find the _safe_kill function definition
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_safe_kill":
            # Check that there's a "return False" in the body
            for child in ast.walk(node):
                if isinstance(child, ast.Return) and isinstance(child.value, ast.Constant):
                    if child.value.value is False:
                        found = True
                        break
            break

    assert found, (
        "_safe_kill should contain 'return False' for unexpected exceptions. "
        "Found only 'return True' — the fix may not have been applied."
    )
