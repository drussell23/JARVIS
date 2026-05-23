"""Lease + SessionToken HMAC, expiry, replay protection — unit tests.

Slice Aegis-1 regression spine, claim #2: lease HMAC mint/validate
roundtrips and rejection is deterministic.
"""
from __future__ import annotations

import time

import pytest

from backend.core.ouroboros.aegis.lease import (
    DEFAULT_LEASE_TTL_S,
    DEFAULT_SESSION_TOKEN_TTL_S,
    Lease,
    NonceLedger,
    SessionToken,
    TokenVerdictKind,
    mint_lease_token,
    mint_session_token,
    validate_lease_token,
    validate_session_token,
)


# ---------------------------------------------------------------------------
# SessionToken
# ---------------------------------------------------------------------------


def _K() -> bytes:
    # Deterministic key for tests. Production uses secrets.token_bytes(32).
    return b"k" * 32


def test_session_token_roundtrip_valid():
    now = time.time()
    wire, payload = mint_session_token(_K(), now_s=now, ttl_s=60)
    assert "." in wire
    verdict = validate_session_token(
        _K(), wire, now_s=now + 1, active_jti={payload.jti},
    )
    assert verdict.kind is TokenVerdictKind.VALID


def test_session_token_expired():
    now = 1000.0
    wire, payload_st = mint_session_token(_K(), now_s=now, ttl_s=60)
    verdict = validate_session_token(
        _K(), wire, now_s=now + 120, active_jti={payload_st.jti},
    )
    assert verdict.kind is TokenVerdictKind.EXPIRED


def test_session_token_revoked_by_jti_not_active():
    now = time.time()
    wire, payload = mint_session_token(_K(), now_s=now)
    verdict = validate_session_token(
        _K(), wire, now_s=now + 1, active_jti=set(),  # not in active set
    )
    assert verdict.kind is TokenVerdictKind.REPLAYED


def test_session_token_invalid_signature_wrong_key():
    now = time.time()
    wire, payload = mint_session_token(_K(), now_s=now)
    verdict = validate_session_token(
        b"\x01" * 32, wire, now_s=now + 1, active_jti={payload.jti},
    )
    assert verdict.kind is TokenVerdictKind.INVALID_SIGNATURE


def test_session_token_invalid_format_missing_dot():
    verdict = validate_session_token(
        _K(), "thisisnottokenshaped", now_s=time.time(), active_jti=set(),
    )
    assert verdict.kind is TokenVerdictKind.INVALID_FORMAT


def test_session_token_invalid_format_empty_segment():
    verdict = validate_session_token(
        _K(), ".sigonly", now_s=time.time(), active_jti=set(),
    )
    assert verdict.kind is TokenVerdictKind.INVALID_FORMAT


def test_session_token_tampered_payload_changes_signature():
    now = time.time()
    wire, payload = mint_session_token(_K(), now_s=now)
    # Flip one char in the payload segment.
    payload_b64, sig_b64 = wire.split(".", 1)
    flipped = ("A" if payload_b64[0] != "A" else "B") + payload_b64[1:]
    tampered = f"{flipped}.{sig_b64}"
    verdict = validate_session_token(
        _K(), tampered, now_s=now + 1, active_jti={payload.jti},
    )
    assert verdict.kind in (
        TokenVerdictKind.INVALID_SIGNATURE,
        TokenVerdictKind.INVALID_FORMAT,
    )


# ---------------------------------------------------------------------------
# Lease
# ---------------------------------------------------------------------------


def _lease(
    *,
    nonce: str = "n1",
    expires_at: float = 1e12,  # far future
) -> Lease:
    return Lease(
        nonce=nonce,
        op_id="op-1",
        route="STANDARD",
        estimated_cost_usd=0.01,
        max_cost_usd=0.015,
        causal_lineage_hash="lineage-hash-placeholder",
        issued_at=1000.0,
        expires_at=expires_at,
    )


def test_lease_token_roundtrip_valid():
    now = 1000.0
    lease = _lease(expires_at=now + 60)
    wire = mint_lease_token(_K(), lease)
    ledger = NonceLedger(capacity=16)
    verdict = validate_lease_token(_K(), wire, now_s=now + 1, nonce_ledger=ledger)
    assert verdict.kind is TokenVerdictKind.VALID
    assert verdict.payload is not None
    assert verdict.payload["nonce"] == "n1"


def test_lease_token_expired():
    lease = _lease(expires_at=1000.0)
    wire = mint_lease_token(_K(), lease)
    ledger = NonceLedger(capacity=16)
    verdict = validate_lease_token(_K(), wire, now_s=2000.0, nonce_ledger=ledger)
    assert verdict.kind is TokenVerdictKind.EXPIRED


def test_lease_token_replay_rejected_second_validation():
    now = 1000.0
    lease = _lease(expires_at=now + 60)
    wire = mint_lease_token(_K(), lease)
    ledger = NonceLedger(capacity=16)
    first = validate_lease_token(_K(), wire, now_s=now + 1, nonce_ledger=ledger)
    second = validate_lease_token(_K(), wire, now_s=now + 1, nonce_ledger=ledger)
    assert first.kind is TokenVerdictKind.VALID
    assert second.kind is TokenVerdictKind.REPLAYED


def test_lease_token_wrong_key_rejected():
    now = 1000.0
    lease = _lease(expires_at=now + 60)
    wire = mint_lease_token(_K(), lease)
    ledger = NonceLedger(capacity=16)
    verdict = validate_lease_token(
        b"\x02" * 32, wire, now_s=now + 1, nonce_ledger=ledger,
    )
    assert verdict.kind is TokenVerdictKind.INVALID_SIGNATURE
    # Crucially: a failed-signature lease must NOT register the nonce
    # (otherwise an attacker spamming bogus signatures could pre-burn
    # legitimate nonces).
    assert not ledger.contains(lease.nonce)


# ---------------------------------------------------------------------------
# NonceLedger
# ---------------------------------------------------------------------------


def test_nonce_ledger_first_register_succeeds():
    ledger = NonceLedger(capacity=8)
    assert ledger.try_register_sync("n1") is True
    assert ledger.contains("n1")


def test_nonce_ledger_replay_rejected():
    ledger = NonceLedger(capacity=8)
    ledger.try_register_sync("n1")
    assert ledger.try_register_sync("n1") is False


def test_nonce_ledger_drop_oldest_when_capacity_exceeded():
    ledger = NonceLedger(capacity=3)
    ledger.try_register_sync("a")
    ledger.try_register_sync("b")
    ledger.try_register_sync("c")
    ledger.try_register_sync("d")  # evicts "a"
    assert not ledger.contains("a")
    assert ledger.contains("b")
    assert ledger.contains("c")
    assert ledger.contains("d")
    # Re-registering an evicted nonce succeeds (the FIFO does not
    # retain memory beyond capacity).
    assert ledger.try_register_sync("a") is True


def test_nonce_ledger_capacity_must_be_positive():
    with pytest.raises(ValueError):
        NonceLedger(capacity=0)


def test_session_token_dataclass_roundtrip_dict():
    st = SessionToken(jti="abc", issued_at=1.0, expires_at=2.0)
    d = st.to_dict()
    recovered = SessionToken.from_dict(d)
    assert recovered == st


def test_lease_dataclass_roundtrip_dict():
    lease = _lease()
    d = lease.to_dict()
    recovered = Lease.from_dict(d)
    assert recovered == lease


def test_defaults_are_sane():
    assert DEFAULT_LEASE_TTL_S == 300
    assert DEFAULT_SESSION_TOKEN_TTL_S == 3600
