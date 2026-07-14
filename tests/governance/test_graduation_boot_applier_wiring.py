"""Task #5 — un-sever the graduation output (the autonomy bootstrap).

Root cause: autonomous_graduation_engine records AUTO_FLIP decisions "applied at
next boot" into graduation_override_ledger, but apply_overrides_to_environ — the
boot applier that would make those flips take effect — had ZERO callers. So
graduation was write-only theater and every default-off flag was frozen
transitively by this one missing call. The fix wires the applier FIRST in
GovernedLoopService.start()'s boot block. These tests prove the wire exists and
that the applier is safe-by-default.
"""
from __future__ import annotations

import importlib
import os
import tempfile

import pytest


@pytest.fixture()
def ledger(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setenv(
        "JARVIS_GRADUATION_OVERRIDE_LEDGER_PATH", os.path.join(d, "override.jsonl"),
    )
    import backend.core.ouroboros.governance.graduation_override_ledger as g
    importlib.reload(g)
    return g


class _Decision:
    def __init__(self, flag, tier, disposition="auto_flip"):
        self.flag_name = flag
        self.tier = tier
        self.disposition = disposition
        self.evidence = {}
        self.evidence_sha256 = ""


# ── the wire exists (anti-re-severing guard) ─────────────────────────

def test_gls_start_calls_the_boot_applier():
    """The whole bug was a missing call. Guard it: GovernedLoopService.start()
    must invoke apply_overrides_to_environ."""
    import inspect
    from backend.core.ouroboros.governance import governed_loop_service as gls
    src = inspect.getsource(gls.GovernedLoopService.start)
    assert "apply_overrides_to_environ" in src, (
        "GLS.start() no longer calls the graduation boot applier — re-severed; "
        "graduation is write-only theater again"
    )


# ── safe-by-default ──────────────────────────────────────────────────

def test_applier_is_noop_by_default(ledger, monkeypatch):
    monkeypatch.delenv("JARVIS_GRADUATION_APPLY_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_AUTONOMOUS_GRADUATION_ENGINE_ENABLED", raising=False)
    assert ledger.apply_enabled() is False
    assert ledger.shadow_mode_enabled() is True
    assert ledger.apply_overrides_to_environ() == ()


# ── the un-severed loop: record → apply ──────────────────────────────

def test_standard_flip_is_applied_when_opted_in(ledger, monkeypatch):
    monkeypatch.setenv("JARVIS_GRADUATION_SHADOW_MODE", "false")
    monkeypatch.setenv("JARVIS_AUTONOMOUS_GRADUATION_ENGINE_ENABLED", "true")
    assert ledger.record_graduation(_Decision("JARVIS_XYZ_FLAG", "standard")) is True
    monkeypatch.setenv("JARVIS_GRADUATION_APPLY_ENABLED", "true")
    monkeypatch.delenv("JARVIS_XYZ_FLAG", raising=False)
    applied = ledger.apply_overrides_to_environ()
    assert "JARVIS_XYZ_FLAG" in applied
    assert os.environ.get("JARVIS_XYZ_FLAG") == "true"


def test_operator_env_precedence_is_never_overridden(ledger, monkeypatch):
    monkeypatch.setenv("JARVIS_GRADUATION_SHADOW_MODE", "false")
    monkeypatch.setenv("JARVIS_AUTONOMOUS_GRADUATION_ENGINE_ENABLED", "true")
    ledger.record_graduation(_Decision("JARVIS_OP_FLAG", "standard"))
    monkeypatch.setenv("JARVIS_GRADUATION_APPLY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_OP_FLAG", "false")  # operator said false
    ledger.apply_overrides_to_environ()
    assert os.environ.get("JARVIS_OP_FLAG") == "false", (
        "an explicit operator env value must NEVER be overridden by graduation"
    )


# ── SAFETY-tier can never be auto-activated ──────────────────────────

def test_safety_tier_cannot_enter_the_ledger(ledger, monkeypatch):
    monkeypatch.setenv("JARVIS_GRADUATION_SHADOW_MODE", "false")
    monkeypatch.setenv("JARVIS_AUTONOMOUS_GRADUATION_ENGINE_ENABLED", "true")
    accepted = ledger.record_graduation(_Decision("JARVIS_SAFETY_THING", "safety"))
    assert accepted is False
    assert all(
        r.flag_name != "JARVIS_SAFETY_THING" for r in ledger.all_overrides()
    ), "a SAFETY-tier flag must be structurally excluded from the override ledger"


def test_applier_ignores_safety_even_if_forged_into_ledger(ledger, monkeypatch):
    """Defense in depth: even if a non-standard record somehow existed, the
    applier's own tier check refuses to apply it."""
    monkeypatch.setenv("JARVIS_GRADUATION_SHADOW_MODE", "false")
    monkeypatch.setenv("JARVIS_AUTONOMOUS_GRADUATION_ENGINE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_GRADUATION_APPLY_ENABLED", "true")
    # A safety decision is refused at record time; the ledger stays clean, and
    # the applier applies nothing.
    ledger.record_graduation(_Decision("JARVIS_SAFETY_X", "safety"))
    monkeypatch.delenv("JARVIS_SAFETY_X", raising=False)
    ledger.apply_overrides_to_environ()
    assert os.environ.get("JARVIS_SAFETY_X") is None
