"""In-process live-fire of the DEPLOYED validator wiring against a real broken kernel
candidate — the Metric B behavior the full TTY dogfood would exercise, minus the
LLM-driven GOAL generation. No TTY, no providers, no cost.

Proves end-to-end:
  1. affects_kernel() recognises the dogfood target (backend/core/...).
  2. The real LiveKernelValidator subprocess CATCHES a kernel candidate that survives
     ast.parse but fails to import (the exact class of bug ast.parse misses).
  3. A clean candidate PASSES (no false positive).
  4. The exact deployed hook logic (deploy_live_validator_fsm.py) rebinds the frozen
     ValidationResult → passed=False / failure_class="build" on failure, and leaves a
     clean candidate untouched.
"""
from __future__ import annotations

import asyncio
import dataclasses
import tempfile
import textwrap
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.live_kernel_validator import LiveKernelValidator
from backend.core.ouroboros.governance.op_context import ValidationResult

# A kernel patch that PARSES but does not IMPORT — a stray undefined reference, exactly
# the AttributeError/NameError class ast.parse green-lights and live-fire catches.
_BROKEN = """
import collections
class KernelMemoryRingBuffer:
    def __init__(self):
        self._buf = collections.deque(maxlen=128)
    def append(self, item):
        self._buf.append(item)
_BOOT_SANITY = _UNDEFINED_MODULE_LEVEL_NAME   # NameError at import
"""

_CLEAN = """
import collections
class KernelMemoryRingBuffer:
    def __init__(self):
        self._buf = collections.deque(maxlen=128)
    def append(self, item):
        self._buf.append(item)
"""

_MODULE = "jarmatrix_dogfood_kernel"
_CHANGED = ["backend/core/jarmatrix_dogfood_kernel.py"]


def _livefire(content: str):
    d = Path(tempfile.mkdtemp())
    (d / f"{_MODULE}.py").write_text(textwrap.dedent(content))
    v = LiveKernelValidator()
    return asyncio.run(v.validate_patch(
        changed_files=_CHANGED,
        affected_symbols=["KernelMemoryRingBuffer"],
        module=_MODULE,
        path_insert=str(d),
    ))


def _apply_deployed_hook(validation: ValidationResult, res) -> ValidationResult:
    """The EXACT wiring injected by deploy_live_validator_fsm.py."""
    if getattr(validation, "passed", False) and not res.ok:
        validation = dataclasses.replace(
            validation,
            passed=False,
            failure_class="build",
            error="live-fire boot failure: " + str(res.exception_type),
            short_summary=("live-fire boot failure: " + str(res.exception_type)
                           + ": " + (res.traceback or ""))[:300],
        )
    return validation


def test_affects_kernel_recognises_dogfood_target():
    assert LiveKernelValidator.affects_kernel(_CHANGED) is True


def test_livefire_catches_broken_kernel_candidate():
    res = _livefire(_BROKEN)
    assert res.ok is False
    assert "NameError" in (res.exception_type + " " + res.traceback)


def test_livefire_passes_clean_kernel_candidate():
    res = _livefire(_CLEAN)
    assert res.ok is True, res.traceback


def test_deployed_hook_routes_broken_candidate_as_build():
    res = _livefire(_BROKEN)
    assert res.ok is False
    validation = ValidationResult(
        passed=True, best_candidate={"file_path": _CHANGED[0]},
        validation_duration_s=0.0, error=None,
    )
    out = _apply_deployed_hook(validation, res)
    assert out.passed is False
    assert out.failure_class == "build"
    assert "live-fire boot failure" in out.error
    # original frozen instance untouched (route-back produced a NEW value)
    assert validation.passed is True


def test_deployed_hook_leaves_clean_candidate_passed():
    res = _livefire(_CLEAN)
    assert res.ok is True
    validation = ValidationResult(
        passed=True, best_candidate={}, validation_duration_s=0.0, error=None,
    )
    out = _apply_deployed_hook(validation, res)
    assert out.passed is True and out.failure_class is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-s"]))
