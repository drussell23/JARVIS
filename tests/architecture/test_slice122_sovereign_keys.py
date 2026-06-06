"""Slice 122 — Sovereign Cryptographic Key & Dynamic Synthesis Matrix.

Marquee proofs:
  1. Full operator round-trip: provision → synthesize draft → sign → the loop
     (Slice 120) verifies it VALID with only the PUBLIC key.
  2. The asymmetric air-gap (§1): the loop has only the public key at rest — a
     wrong passphrase cannot sign, and a roadmap signed by a DIFFERENT key is
     rejected. The loop cannot forge.
  3. Fail-closed: tampered body / no pubkey / wrong alg → not VALID → per-PR.
  4. The synthesizer drafts a conservative, authority-free, Slice-120-shaped
     proposal (safe scopes only; recursion clamped).
"""

from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import layer4_roadmap_authority as L4
from backend.core.ouroboros.governance import sovereign_keys as SK
from backend.core.ouroboros.governance import roadmap_synthesizer as RS

_NOW = 1_900_000_000


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_SOVEREIGN_KEY_DIR", str(tmp_path))
    monkeypatch.delenv("JARVIS_LAYER4_OPERATOR_PUBKEY", raising=False)
    yield tmp_path


def test_master_default_false(monkeypatch):
    monkeypatch.delenv("JARVIS_SOVEREIGN_KEYS_ENABLED", raising=False)
    assert SK.sovereign_keys_enabled() is False


class TestProvisioning:
    def test_provision_persists_public_only(self, store):
        res = SK.provision("correct horse battery staple")
        assert SK.is_provisioned() is True
        assert (store / "layer4_operator.pub").exists()
        assert (store / "layer4_key.salt").exists()
        # The PRIVATE key / passphrase are NEVER written to disk.
        on_disk = b"".join(p.read_bytes() for p in store.iterdir())
        assert b"correct horse battery staple" not in on_disk
        assert res.public_key_b64

    def test_provision_is_idempotent_guarded(self, store):
        SK.provision("pw-one")
        with pytest.raises(FileExistsError):
            SK.provision("pw-two")
        # Rotation is explicit.
        SK.provision("pw-two", overwrite=True)

    def test_wrong_passphrase_cannot_load_private_key(self, store):
        SK.provision("right-pass")
        with pytest.raises(ValueError):
            SK.load_private_key("wrong-pass")
        # Right passphrase works.
        assert SK.load_private_key("right-pass") is not None


class TestRoundTrip:
    def test_provision_synthesize_sign_verify(self, store, monkeypatch):
        SK.provision("operator-pass")
        draft = RS.synthesize_draft(now=_NOW)
        signed = SK.sign_roadmap(draft, "operator-pass")
        assert signed["signature_alg"] == "ed25519"
        # The loop verifies with ONLY the public key (no passphrase, no privkey).
        monkeypatch.setenv("JARVIS_LAYER4_ROADMAP_ENABLED", "1")
        auth = L4.verify_signed_roadmap(signed, now=_NOW)
        assert auth.kind is L4.RoadmapVerdictKind.VALID, auth.detail
        assert "docs" in auth.scopes
        # And the un-signable floor STILL holds on a fully-valid signed roadmap.
        assert L4.may_suppress_approval(auth, op_scope="docs", risk_tier="SAFE_AUTO") is True
        assert L4.may_suppress_approval(auth, op_scope="docs", is_order2_rsi=True) is False


class TestAirGapCannotForge:
    def test_roadmap_signed_by_foreign_key_rejected(self, store, monkeypatch):
        # Operator provisions in `store`; an ATTACKER provisions a different key
        # in a separate dir and signs a roadmap granting itself wide scope.
        SK.provision("real-operator")
        operator_pub = (store / "layer4_operator.pub").read_text()

        forged = {"authorized_scopes": ["governance", "rewrite-cage"],
                  "max_budget_usd": 1e9, "expires_at": _NOW + 1000}
        attacker_dir = store / "attacker"
        monkeypatch.setenv("JARVIS_SOVEREIGN_KEY_DIR", str(attacker_dir))
        SK.provision("attacker-pass")
        forged_signed = SK.sign_roadmap(forged, "attacker-pass")

        # The loop is configured with the OPERATOR's public key only.
        monkeypatch.setenv("JARVIS_LAYER4_OPERATOR_PUBKEY", operator_pub)
        auth = L4.verify_signed_roadmap(forged_signed, now=_NOW)
        # Foreign signature → rejected. The loop cannot forge or be tricked.
        assert auth.is_valid is False
        assert auth.kind is L4.RoadmapVerdictKind.INVALID_SIGNATURE

    def test_tampered_signed_body_rejected(self, store, monkeypatch):
        SK.provision("op")
        signed = SK.sign_roadmap(RS.synthesize_draft(now=_NOW), "op")
        # Attacker widens scope after signing.
        signed["authorized_scopes"] = list(signed["authorized_scopes"]) + ["rewrite-cage"]
        monkeypatch.setenv("JARVIS_LAYER4_ROADMAP_ENABLED", "1")
        auth = L4.verify_signed_roadmap(signed, now=_NOW)
        assert auth.is_valid is False  # body hash no longer matches the signature

    def test_no_pubkey_fails_closed(self, store, monkeypatch):
        SK.provision("op")
        signed = SK.sign_roadmap(RS.synthesize_draft(now=_NOW), "op")
        # Remove the pubkey the loop would verify against.
        (store / "layer4_operator.pub").unlink()
        monkeypatch.delenv("JARVIS_LAYER4_OPERATOR_PUBKEY", raising=False)
        auth = L4.verify_signed_roadmap(signed, now=_NOW)
        assert auth.kind is L4.RoadmapVerdictKind.MISSING


class TestSynthesizerConservative:
    def test_draft_is_authority_free_and_safe(self):
        draft = RS.synthesize_draft(now=_NOW)
        assert "signature" not in draft  # unsigned → grants nothing
        # No authority-bearing scope is ever proposed.
        for forbidden in ("governance", "rewrite-cage", "m10", "order2", "order-2"):
            assert forbidden not in draft["authorized_scopes"]
        # Recursion clamped to the autonomous cap; bounded expiry.
        assert draft["max_recursion_depth"] <= RS._safe_recursion_depth()
        assert draft["expires_at"] > draft["generated_at"]

    def test_expired_signed_roadmap_revoked(self, store, monkeypatch):
        SK.provision("op")
        draft = RS.synthesize_draft(now=_NOW, window_days=0)  # expires immediately
        signed = SK.sign_roadmap(draft, "op")
        monkeypatch.setenv("JARVIS_LAYER4_ROADMAP_ENABLED", "1")
        auth = L4.verify_signed_roadmap(signed, now=_NOW + 10)
        assert auth.kind is L4.RoadmapVerdictKind.EXPIRED
