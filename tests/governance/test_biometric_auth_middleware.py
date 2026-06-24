"""Tests for the Sovereign Command Node Phase 2 Biometric Edge-Gate.

This is the FIRST operator write-path into governance. Every test here
encodes a fail-CLOSED invariant. The biometric is NECESSARY, never
SUFFICIENT: a valid voice match does NOT bypass any backend law.

We inject fake ``voice_verify_fn`` + ``approve_fn`` + ``resolve_target_repo_fn``
so NO real audio / ECAPA / CRITICAL_ELEVATION machinery runs.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance.command_node import (
    biometric_auth_middleware as mw,
)


# --- fixtures / helpers ---------------------------------------------------


def _good_verdict(score: float = 0.95):
    return {
        "authenticated": True,
        "score": score,
        "antispoof_ok": True,
        "liveness_ok": True,
        "voiceprint_id": "owner",
    }


def _bad_verdict(**overrides):
    v = _good_verdict()
    v.update(overrides)
    return v


def _fresh_middleware(**kwargs):
    """A middleware with an isolated in-memory challenge store + a
    no-op audit sink (audit ledger is tested separately)."""
    audit_calls = []

    def _audit_sink(record):
        audit_calls.append(record)

    m = mw.BiometricAuthMiddleware(audit_sink=_audit_sink, **kwargs)
    m._audit_calls = audit_calls  # test introspection only
    return m


def _issue(m, *, pr_id="PR-100", ast_mutation_id="ast-abc",
           blast_radius_hash="br-xyz"):
    return m.issue_challenge(
        pr_id=pr_id,
        ast_mutation_id=ast_mutation_id,
        blast_radius_hash=blast_radius_hash,
    )


async def _authorize(m, ch, *, verdict=None, approve_record=None,
                     target_repo="jarvis", pr_id=None, ast_mutation_id=None,
                     nonce=None, raise_in_verify=False):
    approve_record = approve_record if approve_record is not None else []

    async def _verify(audio, sample_rate):  # noqa: ARG001
        if raise_in_verify:
            raise RuntimeError("ecapa exploded")
        return verdict if verdict is not None else _good_verdict()

    async def _approve(*, pr_id, ast_mutation_id):  # noqa: ARG001
        approve_record.append((pr_id, ast_mutation_id))
        return {"approved": True}

    def _resolve(pr):  # noqa: ARG001
        return target_repo

    res = await m.authorize_elevation(
        pr_id=pr_id if pr_id is not None else ch.pr_id,
        nonce=nonce if nonce is not None else ch.nonce,
        ast_mutation_id=(
            ast_mutation_id if ast_mutation_id is not None
            else ch.ast_mutation_id
        ),
        audio=b"\x00\x01\x02fake-audio",
        sample_rate=16000,
        voice_verify_fn=_verify,
        approve_fn=_approve,
        resolve_target_repo_fn=_resolve,
    )
    res._approve_record = approve_record  # test introspection
    return res


# --- challenge issuance ---------------------------------------------------


def test_issue_challenge_unique_nonce_and_phrase():
    m = _fresh_middleware()
    a = _issue(m, pr_id="PR-1")
    b = _issue(m, pr_id="PR-2")
    assert a.nonce != b.nonce
    assert len(a.nonce) == 64  # token_hex(32) -> 64 hex chars
    assert a.phrase and isinstance(a.phrase, str)
    # phrase is randomized per request (different nonce seed)
    phrases = {_issue(m, pr_id=f"PR-{i}").phrase for i in range(40)}
    assert len(phrases) > 1
    assert a.consumed is False
    assert a.ttl_s > 0


def test_issue_challenge_binds_to_pr_and_mutation():
    m = _fresh_middleware()
    ch = _issue(m, pr_id="PR-42", ast_mutation_id="ast-42",
                blast_radius_hash="br-42")
    assert ch.pr_id == "PR-42"
    assert ch.ast_mutation_id == "ast-42"
    assert ch.blast_radius_hash == "br-42"


# --- freshness / anti-replay ----------------------------------------------


def test_unknown_nonce_rejected():
    m = _fresh_middleware()
    ch = _issue(m)
    res = asyncio.run(_authorize(m, ch, nonce="deadbeef" * 8))
    assert res.decision == "REJECTED"
    assert res.freshness_ok is False


def test_replay_consumed_nonce_rejected():
    m = _fresh_middleware()
    ch = _issue(m)
    first = asyncio.run(_authorize(m, ch))
    assert first.decision == "AUTHORIZED"
    # Replay the SAME nonce -> rejected (single-use).
    second = asyncio.run(_authorize(m, ch))
    assert second.decision == "REJECTED"
    assert second.freshness_ok is False
    assert "replay" in second.reason or "consumed" in second.reason


def test_concurrent_same_nonce_only_one_wins():
    """Atomic consume: two concurrent same-nonce requests -> exactly
    one AUTHORIZED, the other REJECTED on freshness."""
    m = _fresh_middleware()
    ch = _issue(m)

    async def _race():
        return await asyncio.gather(
            _authorize(m, ch),
            _authorize(m, ch),
        )

    a, b = asyncio.run(_race())
    decisions = sorted([a.decision, b.decision])
    assert decisions == ["AUTHORIZED", "REJECTED"]


def test_expired_nonce_rejected():
    m = _fresh_middleware(challenge_ttl_s=0)  # already expired on issue
    ch = _issue(m)
    res = asyncio.run(_authorize(m, ch))
    assert res.decision == "REJECTED"
    assert res.freshness_ok is False


def test_nonce_bound_to_different_pr_rejected():
    m = _fresh_middleware()
    ch = _issue(m, pr_id="PR-A")
    res = asyncio.run(_authorize(m, ch, pr_id="PR-B"))
    assert res.decision == "REJECTED"
    assert res.freshness_ok is False


def test_nonce_bound_to_different_mutation_rejected():
    m = _fresh_middleware()
    ch = _issue(m, ast_mutation_id="ast-A")
    res = asyncio.run(_authorize(m, ch, ast_mutation_id="ast-B"))
    assert res.decision == "REJECTED"
    assert res.freshness_ok is False


# --- biometric ------------------------------------------------------------


def test_low_score_rejected():
    m = _fresh_middleware(auth_threshold=0.85)
    ch = _issue(m)
    res = asyncio.run(_authorize(m, ch, verdict=_bad_verdict(score=0.50)))
    assert res.decision == "REJECTED"
    assert res.ecapa_score == 0.50
    assert res.freshness_ok is True  # freshness passed; biometric failed


def test_not_authenticated_bool_rejected():
    m = _fresh_middleware()
    ch = _issue(m)
    # Score high but the bool says no -> still rejected (don't trust one bool).
    res = asyncio.run(
        _authorize(m, ch, verdict=_bad_verdict(authenticated=False))
    )
    assert res.decision == "REJECTED"


def test_antispoof_fail_rejected():
    m = _fresh_middleware()
    ch = _issue(m)
    res = asyncio.run(_authorize(m, ch, verdict=_bad_verdict(antispoof_ok=False)))
    assert res.decision == "REJECTED"
    assert res.antispoof_ok is False


def test_liveness_fail_rejected():
    m = _fresh_middleware()
    ch = _issue(m)
    res = asyncio.run(_authorize(m, ch, verdict=_bad_verdict(liveness_ok=False)))
    assert res.decision == "REJECTED"


# --- IMMUTABLE ORANGE (THE LAW) -------------------------------------------


@pytest.mark.parametrize("repo", ["prime", "reactor", "PRIME", " Reactor "])
def test_immutable_orange_rejected_even_with_perfect_biometric(repo):
    """A PERFECT biometric on a Mind/Nerves PR STILL cannot authorize."""
    m = _fresh_middleware()
    ch = _issue(m)
    res = asyncio.run(
        _authorize(m, ch, verdict=_good_verdict(score=1.0), target_repo=repo)
    )
    assert res.decision == "REJECTED"
    assert "immutable_orange" in res.reason
    # The approval path must NEVER be called for Mind/Nerves.
    assert res._approve_record == []


@pytest.mark.parametrize("repo", ["jarvis", "JARVIS"])
def test_body_repo_with_valid_biometric_authorized(repo):
    m = _fresh_middleware()
    ch = _issue(m)
    res = asyncio.run(_authorize(m, ch, target_repo=repo))
    assert res.decision == "AUTHORIZED"
    assert len(res._approve_record) == 1  # approve called exactly once


def test_unknown_repo_fail_closed_rejected():
    m = _fresh_middleware()
    ch = _issue(m)
    res = asyncio.run(_authorize(m, ch, target_repo="mystery"))
    assert res.decision == "REJECTED"


# --- happy path -----------------------------------------------------------


def test_valid_body_pr_authorized_calls_approve_once():
    m = _fresh_middleware()
    ch = _issue(m, pr_id="PR-9", ast_mutation_id="ast-9")
    rec = []
    res = asyncio.run(
        _authorize(m, ch, approve_record=rec, target_repo="jarvis")
    )
    assert res.decision == "AUTHORIZED"
    assert res.pr_id == "PR-9"
    assert res.ast_mutation_id == "ast-9"
    assert rec == [("PR-9", "ast-9")]


# --- fail-CLOSED ----------------------------------------------------------


def test_verify_exception_fails_closed():
    m = _fresh_middleware()
    ch = _issue(m)
    res = asyncio.run(_authorize(m, ch, raise_in_verify=True))
    assert res.decision == "REJECTED"  # exception -> REJECT, never AUTHORIZE


def test_approve_failure_does_not_authorize():
    m = _fresh_middleware()
    ch = _issue(m)

    async def _verify(audio, sample_rate):  # noqa: ARG001
        return _good_verdict()

    async def _approve(*, pr_id, ast_mutation_id):  # noqa: ARG001
        raise RuntimeError("approval path down")

    res = asyncio.run(m.authorize_elevation(
        pr_id=ch.pr_id, nonce=ch.nonce, ast_mutation_id=ch.ast_mutation_id,
        audio=b"x", sample_rate=16000,
        voice_verify_fn=_verify, approve_fn=_approve,
        resolve_target_repo_fn=lambda pr: "jarvis",
    ))
    assert res.decision == "REJECTED"


def test_resolve_repo_exception_fails_closed():
    m = _fresh_middleware()
    ch = _issue(m)

    def _resolve(pr):  # noqa: ARG001
        raise RuntimeError("repo lookup down")

    async def _verify(audio, sample_rate):  # noqa: ARG001
        return _good_verdict()

    res = asyncio.run(m.authorize_elevation(
        pr_id=ch.pr_id, nonce=ch.nonce, ast_mutation_id=ch.ast_mutation_id,
        audio=b"x", sample_rate=16000,
        voice_verify_fn=_verify, approve_fn=lambda **k: None,
        resolve_target_repo_fn=_resolve,
    ))
    assert res.decision == "REJECTED"


# --- audio never persisted ------------------------------------------------


def test_audio_not_persisted_only_sha256_in_audit():
    import hashlib
    m = _fresh_middleware()
    ch = _issue(m)
    audio = b"super-secret-voiceprint-bytes"

    async def _verify(audio_in, sample_rate):  # noqa: ARG001
        return _good_verdict()

    asyncio.run(m.authorize_elevation(
        pr_id=ch.pr_id, nonce=ch.nonce, ast_mutation_id=ch.ast_mutation_id,
        audio=audio, sample_rate=16000,
        voice_verify_fn=_verify, approve_fn=lambda **k: {"ok": True},
        resolve_target_repo_fn=lambda pr: "jarvis",
    ))
    assert m._audit_calls, "an audit record must be emitted"
    rec = m._audit_calls[-1]
    # The raw audio bytes must NEVER appear in the audit record.
    blob = repr(rec)
    assert "super-secret-voiceprint-bytes" not in blob
    assert rec["audio_sha256"] == hashlib.sha256(audio).hexdigest()


def test_audit_emitted_on_both_outcomes():
    m = _fresh_middleware()
    # AUTHORIZED
    ch1 = _issue(m)
    asyncio.run(_authorize(m, ch1, target_repo="jarvis"))
    # REJECTED (immutable orange)
    ch2 = _issue(m)
    asyncio.run(_authorize(m, ch2, target_repo="prime"))
    decisions = [r["decision"] for r in m._audit_calls]
    assert "AUTHORIZED" in decisions
    assert "REJECTED" in decisions
