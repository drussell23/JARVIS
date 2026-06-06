"""Slice 118 — Aegis Lease-Forgery Falsification.

Deterministic, cryptographic proof that the FSM's egress path fails-closed: the
three lease-forgery vectors (no lease / forged HMAC / expired) are all rejected
by the REAL Aegis verifier, a valid lease is accepted, and the Blue ledger
records tamper-evident receipts. No daemon, no network — the verifier + the
ledger are exercised directly.
"""

from __future__ import annotations

import json

import pytest

from backend.core.ouroboros.governance import aegis_lease_forgery as F
from backend.core.ouroboros.governance.aegis_lease_forgery import (
    ATTACK_LEASE_FORGERY,
    bridge_fails_closed,
    forge_lease_vectors,
    run_lease_forgery_siege,
)
from backend.core.ouroboros.governance.red_blue_matrix import BlueEvidenceLedger, verify_ledger

_K = b"slice118-deterministic-test-key-32b!"  # fixed key → deterministic
_NOW = 1_780_000_000.0


class TestForgeryVectors:
    def test_no_lease_is_invalid_format(self):
        v = run_lease_forgery_siege(K=_K, now=_NOW, ledger=_null_ledger())
        assert v["verdicts"]["no_lease"] == "invalid_format"

    def test_forged_hmac_is_invalid_signature(self):
        v = run_lease_forgery_siege(K=_K, now=_NOW, ledger=_null_ledger())
        assert v["verdicts"]["forged_hmac"] == "invalid_signature"

    def test_expired_lease_is_expired(self):
        v = run_lease_forgery_siege(K=_K, now=_NOW, ledger=_null_ledger())
        assert v["verdicts"]["expired_lease"] == "expired"

    def test_valid_control_is_accepted(self):
        # Load-bearing: the verifier rejects forgeries SPECIFICALLY, not by
        # blanket-denying everything (which would be a dead engine, not a cage).
        v = run_lease_forgery_siege(K=_K, now=_NOW, ledger=_null_ledger())
        assert v["verdicts"]["valid_control"] == "valid"
        assert v["valid_accepted"] is True

    def test_all_three_forgeries_rejected(self):
        v = run_lease_forgery_siege(K=_K, now=_NOW, ledger=_null_ledger())
        assert v["all_forgeries_rejected"] is True

    def test_vectors_are_deterministic(self):
        a = forge_lease_vectors(_K, _NOW)
        b = forge_lease_vectors(_K, _NOW)
        assert [(n, t, e) for n, t, e, _ in a] == [(n, t, e) for n, t, e, _ in b]


class TestBlueReceipts:
    def test_siege_writes_chained_receipts(self, tmp_path):
        led = BlueEvidenceLedger(tmp_path / "evidence.jsonl")
        run_lease_forgery_siege(K=_K, now=_NOW, ledger=led)
        recs = [json.loads(l) for l in led.path.read_text().splitlines() if l.strip()]
        assert len(recs) == 4  # 3 forgeries + 1 valid control
        # Receipts store payload_sha256 (the hash), not the raw payload — so we
        # distinguish forgery vs control by the blocked flag.
        forgery_recs = [r for r in recs if r["blocked"]]
        assert len(forgery_recs) == 3
        assert all(r["blocked_by"] == "aegis_lease_verifier" for r in forgery_recs)
        # The valid control is recorded as accepted (not blocked).
        valid_rec = [r for r in recs if not r["blocked"]]
        assert len(valid_rec) == 1
        # Tamper-evident chain holds.
        ok, reason = verify_ledger(led.path)
        assert ok, reason

    def test_receipts_carry_attack_class(self, tmp_path):
        led = BlueEvidenceLedger(tmp_path / "e.jsonl")
        run_lease_forgery_siege(K=_K, now=_NOW, ledger=led)
        recs = [json.loads(l) for l in led.path.read_text().splitlines() if l.strip()]
        assert all(r["attack_class"] == ATTACK_LEASE_FORGERY for r in recs)


class TestBridgeInvariant:
    def test_bridge_acquire_lease_fails_closed(self):
        # The provider path cannot proceed without a lease: acquire_call_lease
        # RAISES on failure (no silent fallback to direct upstream creds).
        assert bridge_fails_closed() is True


def _null_ledger():
    """A throwaway ledger that doesn't touch disk — for verdict-only assertions."""
    class _Null:
        def record(self, **kw):
            return None
    return _Null()
