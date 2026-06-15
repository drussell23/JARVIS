"""Slice 256 C.2 — the ephemeral live-fire validator.
Deterministic in-sandbox proofs (fake runner + a real temp-module NameError catch);
plus a sandbox-off real-kernel positive that skips when the kernel import is blocked."""
import importlib.util as _u
import sys as _sys
import textwrap

import pytest

_spec = _u.spec_from_file_location(
    "live_kernel_validator",
    "backend/core/ouroboros/governance/live_kernel_validator.py",
)
lkv = _u.module_from_spec(_spec)
_sys.modules["live_kernel_validator"] = lkv
_spec.loader.exec_module(lkv)

KERNEL_FILES = ["unified_supervisor.py"]


def _fake_runner(stdout: str, *, rc: int = 0, raises=None):
    async def runner(script, timeout_s):
        if raises is not None:
            raise raises
        return rc, stdout, ""
    return runner


@pytest.mark.asyncio
async def test_clean_probe_passes():
    v = lkv.LiveKernelValidator(
        subprocess_runner=_fake_runner('LIVEFIRE_RESULT:{"ok": true, "exercised": ["f"]}')
    )
    r = await v.validate_patch(changed_files=KERNEL_FILES, affected_symbols=["f"])
    assert r.ok is True and r.exercised == ["f"]


@pytest.mark.asyncio
async def test_nameerror_probe_fails_with_traceback():
    out = ('LIVEFIRE_RESULT:{"ok": false, "exception_type": "NameError", '
           '"traceback": "NameError: name \'logger\' is not defined"}')
    v = lkv.LiveKernelValidator(subprocess_runner=_fake_runner(out))
    r = await v.validate_patch(changed_files=KERNEL_FILES, affected_symbols=["_instantiate"])
    assert r.ok is False
    assert r.exception_type == "NameError"
    assert "logger" in r.traceback


@pytest.mark.asyncio
async def test_timeout_fails_secure():
    import asyncio
    v = lkv.LiveKernelValidator(
        subprocess_runner=_fake_runner("", raises=asyncio.TimeoutError()), timeout_s=0.1
    )
    r = await v.validate_patch(changed_files=KERNEL_FILES, affected_symbols=["x"])
    assert r.ok is False and r.timed_out is True


@pytest.mark.asyncio
async def test_missing_marker_fails_secure():
    v = lkv.LiveKernelValidator(subprocess_runner=_fake_runner("garbage, no marker"))
    r = await v.validate_patch(changed_files=KERNEL_FILES, affected_symbols=["x"])
    assert r.ok is False and r.exception_type == "ProbeProtocolError"


@pytest.mark.asyncio
async def test_non_kernel_patch_is_skipped():
    called = {"n": 0}
    async def spy(script, t):
        called["n"] += 1
        return 0, "", ""
    v = lkv.LiveKernelValidator(subprocess_runner=spy)
    r = await v.validate_patch(changed_files=["tests/x.py", "docs/y.md"], affected_symbols=["x"])
    assert r.ok is True and called["n"] == 0   # didn't even spawn — not our surface


@pytest.mark.asyncio
async def test_real_subprocess_catches_nameerror_in_function_body(tmp_path):
    """The headline negative proof, IN-SANDBOX: a real subprocess imports a temp module
    whose function body has the exact Slice-255 bug class (a NameError) and the validator
    catches it. No unified_supervisor import → runs anywhere."""
    mod = tmp_path / "_broken_livefire_probe.py"
    mod.write_text(textwrap.dedent("""
        def boom():
            return _undefined_symbol_xyz  # the Slice-255 NameError class
    """))
    v = lkv.LiveKernelValidator(timeout_s=30)  # default real subprocess runner
    r = await v.validate_patch(
        changed_files=["backend/core/anything.py"],
        affected_symbols=["boom"],
        module="_broken_livefire_probe",
        path_insert=str(tmp_path),
    )
    assert r.ok is False
    assert r.exception_type == "NameError"
    assert "_undefined_symbol_xyz" in r.traceback


@pytest.mark.asyncio
async def test_real_subprocess_clean_module_passes(tmp_path):
    mod = tmp_path / "_clean_livefire_probe.py"
    mod.write_text("def ok_fn():\n    return 42\n")
    v = lkv.LiveKernelValidator(timeout_s=30)
    r = await v.validate_patch(
        changed_files=["backend/core/anything.py"],
        affected_symbols=["ok_fn"],
        module="_clean_livefire_probe",
        path_insert=str(tmp_path),
    )
    assert r.ok is True and "ok_fn" in r.exercised
