"""Slice 97 Stage 1 — signed cross-repo ripple mesh.

REAL crypto throughout (no mock HMAC). The "100% certainty the handshake
holds, malformed/replayed/unauthorized DROPPED, zero false-positives"
matrix:

  1. valid handshake round-trips
  2. tampered payload → DROPPED_BAD_SIGNATURE
  3. wrong PSK → DROPPED_BAD_SIGNATURE
  4. replay → DROPPED_REPLAY
  5. expired / fresh
  6. malformed → DROPPED_MALFORMED (never raises)
  7. wrong origin → DROPPED_WRONG_ORIGIN
  8. cross-compat (emitter + aegis.lease) + no-remote-exec
  9. emitter master-off → DISABLED, writes nothing
 10. emitter on → immutable receipt, re-verify → VERIFIED
 11. never-raises on broken env / unwritable ledger

Run:
  JARVIS_CROSS_REPO_RIPPLE_EMIT_ENABLED=true PYTHONPATH=. \
    python3 -m pytest tests/architecture/test_slice97_ripple_mesh.py -v
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from backend.core.ouroboros.cross_repo_mesh.ripple_contract import (
    RIPPLE_SCHEMA_VERSION,
    NonceSeen,
    RippleKind,
    RipplePayload,
    VerifyVerdict,
    sign_ripple,
    verify_ripple,
)
from backend.core.ouroboros.cross_repo_mesh import ripple_emitter as emitter
from backend.core.ouroboros.cross_repo_mesh.ripple_emitter import (
    EmitResult,
    build_ripple,
    emit_ripple,
)


PSK_A = b"shared-secret-A-32-bytes-padding!"
PSK_B = b"shared-secret-B-32-bytes-padding!"
NOW = 1_000_000.0


def _mk_payload(*, nonce: str = "nonce-1", source_repo: str = "jarvis",
                issued_at: float = NOW, ttl_s: float = 3600.0) -> RipplePayload:
    return RipplePayload(
        schema_version=RIPPLE_SCHEMA_VERSION,
        ripple_kind=RippleKind.CONTRACT_CHANGED.value,
        source_repo=source_repo,
        intent="contract rule R-17 merged; consumers may re-pin",
        payload_sha256="a" * 64,
        nonce=nonce,
        issued_at_unix=issued_at,
        ttl_s=ttl_s,
    )


# ---------------------------------------------------------------------------
# 1. Valid handshake — round-trip integrity.
# ---------------------------------------------------------------------------


def test_valid_handshake_round_trips():
    payload = build_ripple(
        RippleKind.CAPABILITY_GRADUATED,
        "DirectionInferrer graduated to default-true",
        {"flag": "JARVIS_DIRECTION_INFERRER_ENABLED", "value": True},
        now_unix=NOW,
    )
    token = sign_ripple(payload, PSK_A)
    verdict, got = verify_ripple(token, PSK_A, now_unix=NOW + 10)

    assert verdict is VerifyVerdict.VERIFIED
    assert got is not None
    assert got.ripple_kind == RippleKind.CAPABILITY_GRADUATED.value
    assert got.intent == "DirectionInferrer graduated to default-true"
    assert got.payload_sha256 == payload.payload_sha256
    assert got.nonce == payload.nonce
    assert got.source_repo == "jarvis"


# ---------------------------------------------------------------------------
# 2. Tampered payload byte → DROPPED_BAD_SIGNATURE.
# ---------------------------------------------------------------------------


def test_tampered_payload_dropped_bad_signature():
    payload = _mk_payload()
    token = sign_ripple(payload, PSK_A)
    payload_b64, sig_b64 = token.split(".", 1)
    # Flip a character in the payload segment.
    flipped = ("A" if payload_b64[5] != "A" else "B")
    tampered = payload_b64[:5] + flipped + payload_b64[6:]
    bad_token = f"{tampered}.{sig_b64}"

    verdict, got = verify_ripple(bad_token, PSK_A, now_unix=NOW)
    assert verdict is VerifyVerdict.DROPPED_BAD_SIGNATURE
    assert got is None


# ---------------------------------------------------------------------------
# 3. Wrong PSK → DROPPED_BAD_SIGNATURE.
# ---------------------------------------------------------------------------


def test_wrong_psk_dropped_bad_signature():
    token = sign_ripple(_mk_payload(), PSK_A)
    verdict, got = verify_ripple(token, PSK_B, now_unix=NOW)
    assert verdict is VerifyVerdict.DROPPED_BAD_SIGNATURE
    assert got is None


# ---------------------------------------------------------------------------
# 4. Replay → 2nd is DROPPED_REPLAY (shared seen-set).
# ---------------------------------------------------------------------------


def test_replay_dropped_second_time_plain_set():
    token = sign_ripple(_mk_payload(nonce="replay-nonce"), PSK_A)
    seen = set()
    v1, p1 = verify_ripple(token, PSK_A, now_unix=NOW, seen_nonces=seen)
    v2, p2 = verify_ripple(token, PSK_A, now_unix=NOW, seen_nonces=seen)
    assert v1 is VerifyVerdict.VERIFIED and p1 is not None
    assert v2 is VerifyVerdict.DROPPED_REPLAY and p2 is None


def test_replay_dropped_second_time_nonceseen():
    token = sign_ripple(_mk_payload(nonce="replay-nonce-2"), PSK_A)
    seen = NonceSeen(capacity=16)
    v1, _ = verify_ripple(token, PSK_A, now_unix=NOW, seen_nonces=seen)
    v2, _ = verify_ripple(token, PSK_A, now_unix=NOW, seen_nonces=seen)
    assert v1 is VerifyVerdict.VERIFIED
    assert v2 is VerifyVerdict.DROPPED_REPLAY


def test_failed_verify_does_not_pollute_replay_ledger():
    # A bad-signature / wrong-origin / expired ripple must NOT register its
    # nonce — otherwise an attacker could pre-burn a legitimate nonce.
    good = _mk_payload(nonce="precious-nonce")
    token = sign_ripple(good, PSK_A)
    seen = set()
    # Wrong PSK → bad sig; nonce never even decoded.
    verify_ripple(token, PSK_B, now_unix=NOW, seen_nonces=seen)
    # The legit verify still succeeds.
    v, p = verify_ripple(token, PSK_A, now_unix=NOW, seen_nonces=seen)
    assert v is VerifyVerdict.VERIFIED and p is not None


# ---------------------------------------------------------------------------
# 5. Expired / fresh / not-yet.
# ---------------------------------------------------------------------------


def test_expired_dropped():
    token = sign_ripple(_mk_payload(issued_at=NOW, ttl_s=100.0), PSK_A)
    verdict, got = verify_ripple(token, PSK_A, now_unix=NOW + 101)
    assert verdict is VerifyVerdict.DROPPED_EXPIRED
    assert got is None


def test_fresh_within_window_verified():
    token = sign_ripple(_mk_payload(issued_at=NOW, ttl_s=100.0), PSK_A)
    verdict, got = verify_ripple(token, PSK_A, now_unix=NOW + 50)
    assert verdict is VerifyVerdict.VERIFIED
    assert got is not None


def test_not_yet_issued_dropped_expired():
    token = sign_ripple(_mk_payload(issued_at=NOW, ttl_s=100.0), PSK_A)
    verdict, got = verify_ripple(token, PSK_A, now_unix=NOW - 5)
    assert verdict is VerifyVerdict.DROPPED_EXPIRED
    assert got is None


# ---------------------------------------------------------------------------
# 6. Malformed → DROPPED_MALFORMED, never raises.
# ---------------------------------------------------------------------------


# Structurally-malformed (not <b64>.<b64>): zero/2+ dots or empty segment →
# DROPPED_MALFORMED before any crypto.
@pytest.mark.parametrize(
    "bad",
    [
        "",
        "no-dot-at-all",
        "too.many.dots",
        ".",
        "a.",
        ".b",
    ],
)
def test_malformed_dropped_never_raises(bad):
    verdict, got = verify_ripple(bad, PSK_A, now_unix=NOW)
    assert verdict is VerifyVerdict.DROPPED_MALFORMED
    assert got is None


# Single-dot junk that is structurally token-shaped but whose signature can
# never match → DROPPED_BAD_SIGNATURE. Still a SILENT DROP (no raise, no
# exec) — indistinguishable from a forged token, which is the point.
@pytest.mark.parametrize("bad", ["@@@.@@@", "not-base64!.also-not!"])
def test_token_shaped_junk_dropped_bad_signature(bad):
    verdict, got = verify_ripple(bad, PSK_A, now_unix=NOW)
    assert verdict is VerifyVerdict.DROPPED_BAD_SIGNATURE
    assert got is None


def test_all_garbage_inputs_are_silent_drops_never_raise():
    # Whatever the bucket, every garbage input is a DROP verdict (never
    # VERIFIED, never an exception).
    for bad in ["", ".", "a.b.c", "@@@.@@@", "\x00\x01", "1234567890"]:
        verdict, got = verify_ripple(bad, PSK_A, now_unix=NOW)
        assert verdict is not VerifyVerdict.VERIFIED
        assert got is None


def test_malformed_non_string_token():
    verdict, got = verify_ripple(None, PSK_A, now_unix=NOW)  # type: ignore[arg-type]
    assert verdict is VerifyVerdict.DROPPED_MALFORMED
    assert got is None


def test_malformed_valid_sig_but_payload_not_ripple_shape():
    # A correctly-signed token whose payload is missing ripple fields →
    # DROPPED_MALFORMED (not bad-sig). Signed with PSK_A so sig passes.
    from backend.core.ouroboros.cross_repo_mesh.ripple_contract import (
        _b64url_encode,
        _canonical_json,
        _sign,
    )

    junk = {"hello": "world"}
    pb = _b64url_encode(_canonical_json(junk))
    token = f"{pb}.{_sign(PSK_A, pb)}"
    verdict, got = verify_ripple(token, PSK_A, now_unix=NOW)
    assert verdict is VerifyVerdict.DROPPED_MALFORMED
    assert got is None


# ---------------------------------------------------------------------------
# 7. Wrong origin → DROPPED_WRONG_ORIGIN.
# ---------------------------------------------------------------------------


def test_wrong_origin_dropped():
    token = sign_ripple(_mk_payload(source_repo="evil"), PSK_A)
    verdict, got = verify_ripple(
        token, PSK_A, now_unix=NOW, expected_origins=("jarvis",)
    )
    assert verdict is VerifyVerdict.DROPPED_WRONG_ORIGIN
    assert got is None


def test_expected_origin_match_verified():
    token = sign_ripple(_mk_payload(source_repo="jarvis"), PSK_A)
    verdict, got = verify_ripple(
        token, PSK_A, now_unix=NOW, expected_origins=("jarvis", "prime")
    )
    assert verdict is VerifyVerdict.VERIFIED
    assert got is not None


# ---------------------------------------------------------------------------
# 8. Cross-compat / no-remote-exec.
# ---------------------------------------------------------------------------


def test_cross_compat_aegis_lease_encode_matches_portable_sign():
    # The portable sign_ripple and aegis.lease._encode_token produce the
    # IDENTICAL wire token for the same canonical dict + key.
    from backend.core.ouroboros.aegis import lease

    payload = _mk_payload(nonce="xcompat-nonce")
    portable_token = sign_ripple(payload, PSK_A)
    lease_token = lease._encode_token(PSK_A, payload.to_canonical_dict())
    assert portable_token == lease_token

    # And the aegis-lease-signed token verifies VERIFIED under the portable
    # verifier (a sibling repo vendoring only ripple_contract can trust it).
    verdict, got = verify_ripple(lease_token, PSK_A, now_unix=NOW)
    assert verdict is VerifyVerdict.VERIFIED
    assert got is not None and got.nonce == "xcompat-nonce"


def test_verify_ripple_has_no_exec_side_effect():
    # The payload's intent is a plain string; verify_ripple returns a
    # verdict+payload only and never invokes intent. We prove "no callable
    # side effect" by embedding a string that WOULD be dangerous if exec'd
    # and asserting it is returned verbatim, untouched.
    danger = "__import__('os').system('echo PWNED')"
    payload = RipplePayload(
        schema_version=RIPPLE_SCHEMA_VERSION,
        ripple_kind=RippleKind.CONSTITUTIONAL_RULE_MERGED.value,
        source_repo="jarvis",
        intent=danger,
        payload_sha256="b" * 64,
        nonce="exec-probe",
        issued_at_unix=NOW,
        ttl_s=3600.0,
    )
    token = sign_ripple(payload, PSK_A)
    verdict, got = verify_ripple(token, PSK_A, now_unix=NOW)
    assert verdict is VerifyVerdict.VERIFIED
    assert got is not None
    # Returned verbatim — a STRING, never invoked.
    assert got.intent == danger
    assert isinstance(got.intent, str)
    # verify_ripple returns exactly a 2-tuple (verdict, payload), nothing
    # callable.
    assert not callable(got)


def test_portable_contract_is_stdlib_only_source():
    import backend.core.ouroboros.cross_repo_mesh.ripple_contract as mod

    src = Path(mod.__file__).read_text()
    assert "from backend" not in src
    assert "import backend" not in src
    # No actual exec/dynamic-import CALLS or subprocess usage. (The docstring
    # legitimately uses the prose word "subprocess" to state the guarantee, so
    # we match call/usage shapes, not the bare word.)
    for banned in ("eval(", "exec(", "subprocess.", "import subprocess",
                   "os.system", "__import__("):
        assert banned not in src, f"portable contract must not contain {banned!r}"


# ---------------------------------------------------------------------------
# 9. Emitter master-off → DISABLED, writes nothing.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_master_off_disabled_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_CROSS_REPO_RIPPLE_EMIT_ENABLED", raising=False)
    ledger = tmp_path / "ripples.jsonl"
    monkeypatch.setenv("JARVIS_CROSS_REPO_RIPPLE_LEDGER", str(ledger))
    monkeypatch.setenv("JARVIS_CROSS_REPO_EMIT_PSK", PSK_A.decode())

    payload = build_ripple(
        RippleKind.CONTRACT_CHANGED, "x", {"a": 1}, now_unix=NOW
    )
    result = await emit_ripple(payload)
    assert isinstance(result, EmitResult)
    assert result.verdict is VerifyVerdict.DISABLED
    assert result.token is None
    assert not ledger.exists()


@pytest.mark.asyncio
async def test_emit_on_but_psk_unset_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CROSS_REPO_RIPPLE_EMIT_ENABLED", "true")
    monkeypatch.delenv("JARVIS_CROSS_REPO_EMIT_PSK", raising=False)
    ledger = tmp_path / "ripples.jsonl"
    monkeypatch.setenv("JARVIS_CROSS_REPO_RIPPLE_LEDGER", str(ledger))

    payload = build_ripple(
        RippleKind.CONTRACT_CHANGED, "x", {"a": 1}, now_unix=NOW
    )
    result = await emit_ripple(payload)
    assert result.verdict is VerifyVerdict.DISABLED
    assert result.detail == "psk_unset"
    assert not ledger.exists()


# ---------------------------------------------------------------------------
# 10. Emitter on → immutable receipt; read back + verify → VERIFIED.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_on_writes_immutable_receipt_and_reverifies(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CROSS_REPO_RIPPLE_EMIT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CROSS_REPO_EMIT_PSK", PSK_A.decode())
    ledger = tmp_path / "ripples.jsonl"
    monkeypatch.setenv("JARVIS_CROSS_REPO_RIPPLE_LEDGER", str(ledger))

    payload = build_ripple(
        RippleKind.CONSTITUTIONAL_RULE_MERGED,
        "rule C-9 merged",
        {"rule": "C-9"},
        now_unix=NOW,
    )
    result = await emit_ripple(payload)
    assert result.verdict is VerifyVerdict.VERIFIED
    assert result.token is not None
    assert result.ledger_written is True
    assert ledger.exists()

    # Read the receipt back and re-verify the persisted token.
    lines = ledger.read_text().strip().splitlines()
    assert len(lines) == 1
    receipt = json.loads(lines[0])
    assert receipt["ripple_kind"] == RippleKind.CONSTITUTIONAL_RULE_MERGED.value
    assert receipt["nonce"] == payload.nonce

    verdict, got = verify_ripple(receipt["token"], PSK_A, now_unix=NOW + 1)
    assert verdict is VerifyVerdict.VERIFIED
    assert got is not None and got.intent == "rule C-9 merged"

    # A second emit appends a second immutable line (append-only ledger).
    payload2 = build_ripple(
        RippleKind.CONTRACT_CHANGED, "another", {"k": 2}, now_unix=NOW
    )
    await emit_ripple(payload2)
    assert len(ledger.read_text().strip().splitlines()) == 2


# ---------------------------------------------------------------------------
# 11. Never-raises on broken env / unwritable ledger.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_never_raises_on_unwritable_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CROSS_REPO_RIPPLE_EMIT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CROSS_REPO_EMIT_PSK", PSK_A.decode())
    # Point the ledger at a path whose parent is a file → mkdir/open fails.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file")
    monkeypatch.setenv(
        "JARVIS_CROSS_REPO_RIPPLE_LEDGER", str(blocker / "nested" / "x.jsonl")
    )

    payload = build_ripple(
        RippleKind.CONTRACT_CHANGED, "x", {"a": 1}, now_unix=NOW
    )
    # Must not raise; signs fine, ledger write fails best-effort.
    result = await emit_ripple(payload)
    assert result.verdict is VerifyVerdict.VERIFIED
    assert result.token is not None
    assert result.ledger_written is False


def test_build_ripple_fresh_nonce_each_call():
    p1 = build_ripple(RippleKind.CONTRACT_CHANGED, "x", {"a": 1}, now_unix=NOW)
    p2 = build_ripple(RippleKind.CONTRACT_CHANGED, "x", {"a": 1}, now_unix=NOW)
    assert p1.nonce != p2.nonce
    # Same canonical object → same sha256 (deterministic content hash).
    assert p1.payload_sha256 == p2.payload_sha256


def test_build_ripple_accepts_string_kind():
    p = build_ripple("contract_changed", "x", {"a": 1}, now_unix=NOW)
    assert p.ripple_kind == "contract_changed"


def test_register_shipped_invariants_never_raises():
    invs = emitter.register_shipped_invariants()
    assert isinstance(invs, list)
    # When the meta module is importable, the three pins are present and
    # they HOLD on the actual shipped source.
    if invs:
        names = {i.invariant_name for i in invs}
        assert {
            "ripple_contract_stdlib_only",
            "ripple_contract_no_exec",
            "emitter_authority_asymmetry",
        } <= names
        import ast as _ast

        # Derive repo root from this test file (tests/architecture/...).
        repo_root = Path(__file__).resolve().parents[2]
        for inv in invs:
            src = (repo_root / inv.target_file).read_text()
            violations = inv.validate(_ast.parse(src), src)
            assert violations == (), f"{inv.invariant_name}: {violations}"
