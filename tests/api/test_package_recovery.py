"""Dynamic Package Recovery — the Self-Healing engine (Phase 12, Slice E)."""
from __future__ import annotations

import sys

import pytest

from backend.api import package_recovery as pr


# ---------------------------------------------------------------------------
# missing-module extraction
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("No module named 'uuid6'", "uuid6"),
    ("doubleword: No module named 'uuid6'", "uuid6"),
    ("No module named foo.bar", "foo.bar"),
    ("ModuleNotFoundError: No module named \"pkg\"", "pkg"),
    ("connection reset by peer", None),
    ("", None),
    (None, None),
])
def test_extract_missing_module(text, expected):
    assert pr.extract_missing_module(text) == expected


# ---------------------------------------------------------------------------
# governed allowlist (supply-chain guard)
# ---------------------------------------------------------------------------

def test_allowlist_seed_contains_uuid6():
    assert pr.load_allowlist().get("uuid6") == "uuid6"


def test_allowlist_env_extension_and_pin(monkeypatch):
    monkeypatch.setenv("JARVIS_PKG_RECOVERY_MAP", '{"widget": "widget==1.2.3"}')
    monkeypatch.setenv("JARVIS_PKG_RECOVERY_ALLOW", "gadget, gizmo")
    allow = pr.load_allowlist()
    assert allow["widget"] == "widget==1.2.3"   # pinned via JSON
    assert allow["gadget"] == "gadget"          # identity via comma list
    assert allow["gizmo"] == "gizmo"
    assert allow["uuid6"] == "uuid6"            # seed preserved


def test_allowlist_ignores_malformed_json(monkeypatch):
    monkeypatch.setenv("JARVIS_PKG_RECOVERY_MAP", "{not json")
    assert pr.load_allowlist().get("uuid6") == "uuid6"   # falls back to seed


# ---------------------------------------------------------------------------
# the recovery engine — fully faked (no real pip, no real network)
# ---------------------------------------------------------------------------

class _FakeRun:
    """Records the argv and returns a canned pip returncode."""
    def __init__(self, returncode=0, stderr=""):
        self.returncode = returncode
        self.stderr = stderr
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        class _R:
            returncode = self.returncode
            stderr = self.stderr
        return _R()


@pytest.mark.asyncio
async def test_recover_installs_into_active_interpreter_and_reimports():
    run = _FakeRun(returncode=0)
    eng = pr.DynamicPackageRecovery(
        runner=run, import_probe=lambda m: True)      # module present post-install
    res = await eng.recover("uuid6")
    assert res.state is pr.RecoveryState.RECOVERED
    assert res.ok
    # scoped pip install into THIS interpreter (the hermetic venv under --headless)
    argv = run.calls[0][0]
    assert argv[0] == sys.executable
    assert argv[1:5] == ["-m", "pip", "install", "--no-input"]
    assert "uuid6" in argv
    # timeout was passed to the subprocess (mandate — never hang)
    assert "timeout" in run.calls[0][1]


@pytest.mark.asyncio
async def test_recover_refuses_module_not_in_allowlist():
    run = _FakeRun()
    eng = pr.DynamicPackageRecovery(runner=run, import_probe=lambda m: True)
    res = await eng.recover("evilpkg")
    assert res.state is pr.RecoveryState.NOT_ALLOWED
    assert not run.calls               # NO pip subprocess ever spawned


@pytest.mark.asyncio
async def test_recover_degrades_when_install_fails():
    run = _FakeRun(returncode=1, stderr="could not find a version")
    eng = pr.DynamicPackageRecovery(runner=run, import_probe=lambda m: True)
    res = await eng.recover("uuid6")
    assert res.state is pr.RecoveryState.INSTALL_FAILED
    assert not res.ok


@pytest.mark.asyncio
async def test_recover_reports_reimport_failed_when_still_missing():
    run = _FakeRun(returncode=0)
    eng = pr.DynamicPackageRecovery(
        runner=run, import_probe=lambda m: False)     # pip ok but still not importable
    res = await eng.recover("uuid6")
    assert res.state is pr.RecoveryState.REIMPORT_FAILED


@pytest.mark.asyncio
async def test_recover_is_idempotent_per_session():
    run = _FakeRun(returncode=1)   # install "fails" so it stays in attempted set
    eng = pr.DynamicPackageRecovery(runner=run, import_probe=lambda m: False)
    await eng.recover("uuid6")
    await eng.recover("uuid6")
    assert len(run.calls) == 1     # only ONE install attempt per session (no storm)


@pytest.mark.asyncio
async def test_recover_disabled_by_master_switch(monkeypatch):
    monkeypatch.setenv("JARVIS_PKG_RECOVERY_ENABLED", "false")
    run = _FakeRun()
    eng = pr.DynamicPackageRecovery(runner=run, import_probe=lambda m: True)
    res = await eng.recover("uuid6")
    assert res.state is pr.RecoveryState.DISABLED
    assert not run.calls


@pytest.mark.asyncio
async def test_recover_rejects_unsafe_resolved_spec(monkeypatch):
    monkeypatch.setenv("JARVIS_PKG_RECOVERY_MAP", '{"uuid6": "uuid6; rm -rf /"}')
    run = _FakeRun()
    eng = pr.DynamicPackageRecovery(runner=run, import_probe=lambda m: True)
    res = await eng.recover("uuid6")
    assert res.state is pr.RecoveryState.INVALID_SPEC
    assert not run.calls               # never reaches pip
