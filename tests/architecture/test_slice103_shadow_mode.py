"""Slice 103 — Production Shadow Mode (the actuator boundary).

Proves the fail-closed shadow gate: by DEFAULT the production graduation engine
accrues evidence and writes mathematical receipts, but NEVER executes the OS-level
flip. Only an EXPLICIT operator un-shadow (JARVIS_GRADUATION_SHADOW_MODE=false)
authorizes a real override that the boot applier can act on. The human is the sole
actuator.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance import graduation_override_ledger as GOL


def _standard_decision(flag="JARVIS_DOMAIN_ENTROPY_ENGINE_ENABLED"):
    return SimpleNamespace(
        flag_name=flag,
        tier=SimpleNamespace(value="standard"),
        disposition=SimpleNamespace(value="auto_flip"),
        evidence={"clean": 5, "required": 3},
        evidence_sha256="deadbeef",
    )


def _safety_decision(flag="JARVIS_SEMANTIC_GUARD_ENABLED"):
    return SimpleNamespace(
        flag_name=flag,
        tier=SimpleNamespace(value="safety"),
        disposition=SimpleNamespace(value="auto_flip"),
        evidence={}, evidence_sha256="x",
    )


@pytest.fixture(autouse=True)
def _ledgers(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_GRADUATION_OVERRIDE_LEDGER_PATH", str(tmp_path / "overrides.jsonl"))
    monkeypatch.setenv("JARVIS_GRADUATION_SHADOW_LEDGER_PATH", str(tmp_path / "shadow.jsonl"))
    monkeypatch.setenv("JARVIS_GRADUATION_OVERRIDE_APPLY_ENABLED", "true")
    yield


# === Fail-closed: default + garbage stay in shadow (no flip) ================

def test_shadow_is_the_default(monkeypatch):
    monkeypatch.delenv("JARVIS_GRADUATION_SHADOW_MODE", raising=False)
    assert GOL.shadow_mode_enabled() is True  # fail-closed default


def test_garbage_value_stays_in_shadow(monkeypatch):
    monkeypatch.setenv("JARVIS_GRADUATION_SHADOW_MODE", "banana")
    assert GOL.shadow_mode_enabled() is True


# === Shadow mode: receipts accrue, NO override, NO flip =====================

def test_shadow_records_receipt_but_writes_no_override(monkeypatch):
    monkeypatch.delenv("JARVIS_GRADUATION_SHADOW_MODE", raising=False)  # default shadow
    ok = GOL.record_graduation(_standard_decision())
    assert ok is False                                 # not actuated
    assert GOL.all_overrides() == ()                   # override ledger EMPTY
    # ...but the receipt DID accrue in the shadow ledger (evidence/audit).
    assert GOL.shadow_ledger_path().exists()
    assert GOL.shadow_ledger_path().stat().st_size > 0


def test_shadow_applier_flips_nothing(monkeypatch):
    monkeypatch.delenv("JARVIS_GRADUATION_SHADOW_MODE", raising=False)
    GOL.record_graduation(_standard_decision())
    fake_env = {}
    applied = GOL.apply_overrides_to_environ(fake_env)
    assert applied == ()
    assert fake_env == {}                              # nothing ignited


# === Un-shadowed (explicit operator authorization): real flip ===============

def test_unshadowed_writes_override_and_flips(monkeypatch):
    monkeypatch.setenv("JARVIS_GRADUATION_SHADOW_MODE", "false")  # operator authorizes
    ok = GOL.record_graduation(_standard_decision())
    assert ok is True
    overrides = {r.flag_name for r in GOL.all_overrides()}
    assert "JARVIS_DOMAIN_ENTROPY_ENGINE_ENABLED" in overrides
    fake_env = {}
    applied = GOL.apply_overrides_to_environ(fake_env)
    assert "JARVIS_DOMAIN_ENTROPY_ENGINE_ENABLED" in applied
    assert fake_env["JARVIS_DOMAIN_ENTROPY_ENGINE_ENABLED"] == "true"


# === The tier gate still dominates: SAFETY never actuates, shadow or not ====

def test_safety_tier_refused_even_when_unshadowed(monkeypatch):
    monkeypatch.setenv("JARVIS_GRADUATION_SHADOW_MODE", "false")
    ok = GOL.record_graduation(_safety_decision())
    assert ok is False
    assert GOL.all_overrides() == ()                   # SAFETY never reaches the ledger
