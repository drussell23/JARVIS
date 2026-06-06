"""Slice 120 — The Sovereign Layer-4 Roadmap Authority (BOUNDED).

The marquee proofs:
  1. Fail-CLOSED crypto: a forged/tampered/expired/keyless roadmap grants NO
     unattended autonomy → the system degrades to per-PR human review.
  2. The un-signable floor (§1): even a perfectly-VALID roadmap cannot suppress
     approval for SAFETY-tier / Order-2 (M10) / recursion-breach / governance
     ops. The Zero-Order Doll's gate on cognitive self-modification is absolute.
  3. Bounded budget: the roadmap can only TIGHTEN — a request for depth 99 or
     $10,000 is clamped to the autonomous Slice-104 / hard-ceiling maxima.
"""

from __future__ import annotations

import secrets

import pytest

from backend.core.ouroboros.governance import layer4_roadmap_authority as L4
from backend.core.ouroboros.governance.layer4_roadmap_authority import (
    RoadmapVerdictKind,
    effective_budget_usd,
    effective_recursion_depth,
    is_safety_operation,
    layer4_enabled,
    may_suppress_approval,
    sign_roadmap_body,
    unattended_mode_authorized,
    verify_signed_roadmap,
)

_KEY = secrets.token_bytes(32)
_NOW = 1_900_000_000


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv("JARVIS_LAYER4_OPERATOR_KEY", _KEY.hex())
    monkeypatch.setenv("JARVIS_LAYER4_ROADMAP_ENABLED", "1")
    yield


def _roadmap(**over):
    body = {
        "authorized_scopes": ["docs", "test-hardening"],
        "max_budget_usd": 12.0,
        "max_recursion_depth": 2,
        "expires_at": _NOW + 1_000_000,
    }
    body.update(over)
    body["signature"] = sign_roadmap_body(body, _KEY)
    return body


# ---------------------------------------------------------------------------
def test_master_default_false(monkeypatch):
    monkeypatch.delenv("JARVIS_LAYER4_ROADMAP_ENABLED", raising=False)
    assert layer4_enabled() is False


class TestCryptoFailClosed:
    def test_valid_roadmap_verifies(self, keyed):
        auth = verify_signed_roadmap(_roadmap(), now=_NOW)
        assert auth.kind is RoadmapVerdictKind.VALID
        assert auth.scopes == frozenset({"docs", "test-hardening"})

    def test_no_operator_key_fails_closed(self, monkeypatch):
        monkeypatch.delenv("JARVIS_LAYER4_OPERATOR_KEY", raising=False)
        monkeypatch.delenv("JARVIS_LAYER4_OPERATOR_KEYFILE", raising=False)
        body = {"authorized_scopes": ["docs"], "signature": "x.y"}
        auth = verify_signed_roadmap(body, now=_NOW)
        assert auth.kind is RoadmapVerdictKind.MISSING
        assert auth.is_valid is False

    def test_missing_signature_field_fails_closed(self, keyed):
        auth = verify_signed_roadmap({"authorized_scopes": ["docs"]}, now=_NOW)
        assert auth.kind is RoadmapVerdictKind.INVALID_FORMAT

    def test_forged_signature_rejected(self, keyed):
        body = _roadmap()
        # Re-sign with an ATTACKER key — valid format, wrong HMAC.
        body["signature"] = sign_roadmap_body(body, secrets.token_bytes(32))
        auth = verify_signed_roadmap(body, now=_NOW)
        assert auth.kind is RoadmapVerdictKind.INVALID_SIGNATURE
        assert auth.is_valid is False

    def test_tampered_body_rejected(self, keyed):
        body = _roadmap()
        # Operator signed scopes=[docs,test-hardening]; attacker widens scope
        # AFTER signing → body hash no longer matches the signed hash.
        body["authorized_scopes"] = ["docs", "test-hardening", "rewrite-cage"]
        auth = verify_signed_roadmap(body, now=_NOW)
        assert auth.kind is RoadmapVerdictKind.TAMPERED
        assert auth.is_valid is False

    def test_expired_roadmap_revokes_autonomy(self, keyed):
        body = _roadmap(expires_at=_NOW - 1)
        auth = verify_signed_roadmap(body, now=_NOW)
        assert auth.kind is RoadmapVerdictKind.EXPIRED
        assert unattended_mode_authorized(auth) is False


class TestUnsignableFloor:
    """§1: no signature suppresses the human gate on these classes."""

    def test_valid_roadmap_suppresses_authorized_safe_scope(self, keyed):
        auth = verify_signed_roadmap(_roadmap(), now=_NOW)
        # A GREEN op inside an authorized scope → may run unattended.
        assert may_suppress_approval(auth, op_scope="docs", risk_tier="SAFE_AUTO") is True

    def test_unauthorized_scope_not_suppressed(self, keyed):
        auth = verify_signed_roadmap(_roadmap(), now=_NOW)
        assert may_suppress_approval(auth, op_scope="rewrite-cage", risk_tier="SAFE_AUTO") is False

    def test_order2_rsi_never_suppressed_even_with_valid_roadmap(self, keyed):
        auth = verify_signed_roadmap(_roadmap(), now=_NOW)
        # M10 / Order-2 cognitive self-modification — the human ALWAYS signs.
        assert may_suppress_approval(auth, op_scope="docs", is_order2_rsi=True) is False

    def test_recursion_breach_never_suppressed(self, keyed):
        auth = verify_signed_roadmap(_roadmap(), now=_NOW)
        assert may_suppress_approval(auth, op_scope="docs", recursion_exceeded=True) is False

    def test_governance_touch_never_suppressed(self, keyed):
        auth = verify_signed_roadmap(_roadmap(), now=_NOW)
        assert may_suppress_approval(auth, op_scope="docs", touches_governance=True) is False

    def test_approval_required_tier_never_suppressed(self, keyed):
        auth = verify_signed_roadmap(_roadmap(), now=_NOW)
        assert may_suppress_approval(auth, op_scope="docs", risk_tier="APPROVAL_REQUIRED") is False

    def test_blocked_tier_never_suppressed(self, keyed):
        auth = verify_signed_roadmap(_roadmap(), now=_NOW)
        assert may_suppress_approval(auth, op_scope="docs", risk_tier="BLOCKED") is False

    def test_is_safety_operation_taxonomy(self):
        assert is_safety_operation(is_order2_rsi=True) is True
        assert is_safety_operation(recursion_exceeded=True) is True
        assert is_safety_operation(touches_governance=True) is True
        assert is_safety_operation(risk_tier="APPROVAL_REQUIRED") is True
        assert is_safety_operation(risk_tier="SAFE_AUTO") is False

    def test_floor_holds_even_when_master_off(self, monkeypatch):
        # Defense in depth: a safety op is refused suppression regardless of the
        # master flag (the floor is checked before the flag).
        monkeypatch.setenv("JARVIS_LAYER4_ROADMAP_ENABLED", "1")
        auth = verify_signed_roadmap(_roadmap(), now=_NOW) if False else L4._INVALID
        assert may_suppress_approval(auth, op_scope="docs", is_order2_rsi=True) is False


class TestMasterGate:
    def test_master_off_no_unattended(self, monkeypatch):
        monkeypatch.setenv("JARVIS_LAYER4_OPERATOR_KEY", _KEY.hex())
        monkeypatch.delenv("JARVIS_LAYER4_ROADMAP_ENABLED", raising=False)
        auth = verify_signed_roadmap(_roadmap(), now=_NOW)
        # Signature is VALID, but master off → no unattended mode, no suppression.
        assert auth.is_valid is True
        assert unattended_mode_authorized(auth) is False
        assert may_suppress_approval(auth, op_scope="docs", risk_tier="SAFE_AUTO") is False


class TestBoundedBudget:
    def test_budget_clamped_to_hard_ceiling(self, keyed, monkeypatch):
        monkeypatch.setenv("JARVIS_LAYER4_HARD_MAX_BUDGET_USD", "20.0")
        auth = verify_signed_roadmap(_roadmap(max_budget_usd=10_000.0), now=_NOW)
        assert effective_budget_usd(auth) == 20.0  # roadmap tightens, never raises

    def test_budget_honored_when_below_ceiling(self, keyed, monkeypatch):
        monkeypatch.setenv("JARVIS_LAYER4_HARD_MAX_BUDGET_USD", "50.0")
        auth = verify_signed_roadmap(_roadmap(max_budget_usd=12.0), now=_NOW)
        assert effective_budget_usd(auth) == 12.0

    def test_recursion_depth_clamped_to_slice104_cap(self, keyed, monkeypatch):
        monkeypatch.setenv("JARVIS_MAX_RECURSION_DEPTH", "3")
        auth = verify_signed_roadmap(_roadmap(max_recursion_depth=99), now=_NOW)
        # Signature asks for 99; the autonomous cap clamps to 3.
        assert effective_recursion_depth(auth) == 3

    def test_invalid_roadmap_zero_budget(self, keyed):
        auth = verify_signed_roadmap({"authorized_scopes": ["docs"]}, now=_NOW)
        assert effective_budget_usd(auth) == 0.0
        assert effective_recursion_depth(auth) == 0
