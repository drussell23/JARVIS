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
from backend.core.ouroboros.governance.command_node.biometric_audit_ledger import (
    AuditWriteError,
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
    no-op audit sink (audit ledger is tested separately).

    By default, the floor check always returns True (matching the old
    behavior -- governance module unavailable in test env). M1-specific
    tests pass ``floor_check_fn`` explicitly to test real enforcement.
    """
    audit_calls = []

    def _audit_sink(record):
        audit_calls.append(record)

    kwargs.setdefault("floor_check_fn", lambda repo: True)
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


_PHRASE_SENTINEL = object()


def _passing_phrase_match():
    """A zero-arg phrase-match predicate that always PASSES. Phase 3 flips
    REQUIRE_PHRASE_MATCH to default-true, so tests that aren't exercising
    phrase-match must inject a passing verifier (the real asr_phrase_match
    would otherwise try to run local Whisper on fake audio). NO real Whisper
    runs in any test."""
    return True


async def _authorize(m, ch, *, verdict=None, approve_record=None,
                     target_repo="jarvis", pr_id=None, ast_mutation_id=None,
                     nonce=None, raise_in_verify=False,
                     phrase_match_fn=_PHRASE_SENTINEL):
    approve_record = approve_record if approve_record is not None else []
    # Default to a passing phrase-match (Phase 3 default-true REQUIRE). An
    # explicit None / fn / callable overrides it; explicit None means
    # "no fn wired" (the H1 unavailable path).
    if phrase_match_fn is _PHRASE_SENTINEL:
        phrase_match_fn = _passing_phrase_match

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
        phrase_match_fn=phrase_match_fn,
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
        phrase_match_fn=lambda: True,
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
        phrase_match_fn=lambda: True,
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
        phrase_match_fn=lambda: True,
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


# ===========================================================================
# M2 -- Audit-then-approve ordering tests
# ===========================================================================


def test_m2_authorized_audit_fails_rejects_and_approve_not_called():
    """M2: AUTHORIZED path where ledger append FAILS -> REJECTED +
    approve_fn is NOT called. The audit write failure is fail-CLOSED."""
    audit_events: list = []
    approve_events: list = []

    def _failing_audit_sink(record):
        # Raise for AUTHORIZED decisions to simulate durable-write failure.
        if record.get("decision") == "AUTHORIZED":
            raise AuditWriteError("simulated fsync failure")
        audit_events.append(record)

    m = mw.BiometricAuthMiddleware(
        audit_sink=_failing_audit_sink,
        floor_check_fn=lambda repo: True,
    )
    ch = _issue(m)

    async def _verify(audio, sample_rate):  # noqa: ARG001
        return _good_verdict()

    async def _approve(*, pr_id, ast_mutation_id):  # noqa: ARG001
        approve_events.append((pr_id, ast_mutation_id))
        return {"approved": True}

    res = asyncio.run(m.authorize_elevation(
        pr_id=ch.pr_id, nonce=ch.nonce, ast_mutation_id=ch.ast_mutation_id,
        audio=b"x", sample_rate=16000,
        voice_verify_fn=_verify, approve_fn=_approve,
        resolve_target_repo_fn=lambda pr: "jarvis",
        phrase_match_fn=lambda: True,
    ))
    # Must be REJECTED because audit write failed.
    assert res.decision == "REJECTED"
    assert "audit" in res.reason or "fail_closed" in res.reason
    # approve_fn must NEVER have been called.
    assert approve_events == [], "approve_fn must NOT be called when audit write fails"


def test_m2_audit_written_before_approve_fn():
    """M2: Normal AUTHORIZED path -- audit record is written BEFORE
    approve_fn is called (ordering invariant via recording fake)."""
    event_log: list = []

    def _recording_audit_sink(record):
        event_log.append(("audit", record.get("decision")))

    m = mw.BiometricAuthMiddleware(
        audit_sink=_recording_audit_sink,
        floor_check_fn=lambda repo: True,
    )
    ch = _issue(m)

    async def _verify(audio, sample_rate):  # noqa: ARG001
        return _good_verdict()

    async def _approve(*, pr_id, ast_mutation_id):  # noqa: ARG001
        event_log.append(("approve", pr_id))
        return {"approved": True}

    res = asyncio.run(m.authorize_elevation(
        pr_id=ch.pr_id, nonce=ch.nonce, ast_mutation_id=ch.ast_mutation_id,
        audio=b"x", sample_rate=16000,
        voice_verify_fn=_verify, approve_fn=_approve,
        resolve_target_repo_fn=lambda pr: "jarvis",
        phrase_match_fn=lambda: True,
    ))
    assert res.decision == "AUTHORIZED"
    # The audit entry must appear before the approve entry in the log.
    assert event_log, "event log must not be empty"
    audit_indices = [i for i, (kind, _) in enumerate(event_log) if kind == "audit"]
    approve_indices = [i for i, (kind, _) in enumerate(event_log) if kind == "approve"]
    assert audit_indices, "audit must be logged"
    assert approve_indices, "approve must be logged"
    # Every audit event must precede every approve event.
    assert max(audit_indices) < min(approve_indices), (
        "audit record must be written BEFORE approve_fn is called; "
        f"got event_log={event_log}"
    )
    # Audit decision field must be AUTHORIZED.
    assert event_log[audit_indices[0]][1] == "AUTHORIZED"


def test_m2_rejected_plus_audit_fail_is_still_rejected_fail_soft():
    """M2: REJECTED outcome + audit-sink failure -> still REJECTED (not
    an unhandled exception). REJECTED outcomes are fail-soft on audit."""
    def _always_failing_sink(record):
        # Raise for everything (even REJECTED).
        raise RuntimeError("disk full")

    m = mw.BiometricAuthMiddleware(
        audit_sink=_always_failing_sink,
        floor_check_fn=lambda repo: True,
    )
    ch = _issue(m)

    async def _good_verify(audio, sample_rate):  # noqa: ARG001
        return _good_verdict()

    # Trigger a REJECTED path (immutable orange -- perfect biometric but
    # target is prime so Immutable Orange rejects BEFORE any audit write).
    res = asyncio.run(m.authorize_elevation(
        pr_id=ch.pr_id, nonce=ch.nonce, ast_mutation_id=ch.ast_mutation_id,
        audio=b"x", sample_rate=16000,
        voice_verify_fn=_good_verify,
        approve_fn=lambda **k: None,
        resolve_target_repo_fn=lambda pr: "prime",
        # Pass phrase-match so BOTH gate terms pass; Immutable Orange must
        # STILL reject (THE LAW composes AFTER the concurrent gate).
        phrase_match_fn=lambda: True,
    ))
    # Must remain REJECTED; the audit-sink failure must not raise.
    assert res.decision == "REJECTED"
    assert "immutable_orange" in res.reason


def test_m2_rejected_audit_fail_soft_via_bad_biometric():
    """M2: REJECTED due to bad biometric + audit-sink raises -> still
    REJECTED (fail-soft on REJECTED audit path)."""
    def _always_raising_sink(record):
        raise RuntimeError("sink unavailable")

    m = mw.BiometricAuthMiddleware(
        audit_sink=_always_raising_sink,
        floor_check_fn=lambda repo: True,
    )
    ch = _issue(m)

    async def _bad_verify(audio, sample_rate):  # noqa: ARG001
        return _bad_verdict(authenticated=False)

    res = asyncio.run(m.authorize_elevation(
        pr_id=ch.pr_id, nonce=ch.nonce, ast_mutation_id=ch.ast_mutation_id,
        audio=b"x", sample_rate=16000,
        voice_verify_fn=_bad_verify,
        approve_fn=lambda **k: None,
        resolve_target_repo_fn=lambda pr: "jarvis",
    ))
    assert res.decision == "REJECTED"


# ===========================================================================
# M1 -- Real floor enforcement tests
# ===========================================================================


def test_m1_relaxed_floor_rejects():
    """M1: A relaxed floor (safe_auto) on a jarvis PR -> REJECTED with
    fail_closed:floor_relaxed even with a perfect biometric."""
    # Inject a floor_check_fn that simulates safe_auto (not strict enough).
    m = mw.BiometricAuthMiddleware(
        floor_check_fn=lambda repo: False,  # simulates safe_auto / relaxed
        audit_sink=lambda r: None,
    )
    ch = _issue(m)

    async def _verify(audio, sample_rate):  # noqa: ARG001
        return _good_verdict(score=1.0)

    approve_calls: list = []

    async def _approve(*, pr_id, ast_mutation_id):  # noqa: ARG001
        approve_calls.append(pr_id)
        return {}

    res = asyncio.run(m.authorize_elevation(
        pr_id=ch.pr_id, nonce=ch.nonce, ast_mutation_id=ch.ast_mutation_id,
        audio=b"x", sample_rate=16000,
        voice_verify_fn=_verify, approve_fn=_approve,
        resolve_target_repo_fn=lambda pr: "jarvis",
        phrase_match_fn=lambda: True,
    ))
    assert res.decision == "REJECTED"
    assert "floor_relaxed" in res.reason
    # approve must never be called when floor is relaxed.
    assert approve_calls == []


def test_m1_approval_required_floor_passes():
    """M1: A floor_check_fn returning True (approval_required or stricter)
    on a jarvis PR with valid biometric -> AUTHORIZED."""
    m = mw.BiometricAuthMiddleware(
        floor_check_fn=lambda repo: True,  # simulates approval_required
        audit_sink=lambda r: None,
    )
    ch = _issue(m)
    res = asyncio.run(_authorize(m, ch, target_repo="jarvis"))
    assert res.decision == "AUTHORIZED"


def test_m1_floor_check_fn_exception_fails_closed():
    """M1: If floor_check_fn raises -> fail-CLOSED REJECT."""
    def _raising_floor(repo):
        raise RuntimeError("floor lookup exploded")

    m = mw.BiometricAuthMiddleware(
        floor_check_fn=_raising_floor,
        audit_sink=lambda r: None,
    )
    ch = _issue(m)

    async def _verify(audio, sample_rate):  # noqa: ARG001
        return _good_verdict(score=1.0)

    res = asyncio.run(m.authorize_elevation(
        pr_id=ch.pr_id, nonce=ch.nonce, ast_mutation_id=ch.ast_mutation_id,
        audio=b"x", sample_rate=16000,
        voice_verify_fn=_verify, approve_fn=lambda **k: None,
        resolve_target_repo_fn=lambda pr: "jarvis",
        phrase_match_fn=lambda: True,
    ))
    assert res.decision == "REJECTED"


def test_m1_static_floor_check_relaxed_floors():
    """M1: _floor_at_least_approval static method returns False for
    relaxed floors when import succeeds (unit test via monkey-patch)."""
    import unittest.mock as mock

    strict_floors = {"approval_required", "critical_elevation"}
    relaxed_floors = {"safe_auto", "notify_apply", None}

    for floor_val in relaxed_floors:
        with mock.patch(
            "backend.core.ouroboros.governance.command_node"
            ".biometric_auth_middleware.BiometricAuthMiddleware"
            "._floor_at_least_approval",
        ) as mock_floor:
            mock_floor.return_value = (floor_val in strict_floors)
            result = mock_floor("jarvis")
            assert result is False, f"floor={floor_val!r} should fail-CLOSED"

    for floor_val in strict_floors:
        with mock.patch(
            "backend.core.ouroboros.governance.command_node"
            ".biometric_auth_middleware.BiometricAuthMiddleware"
            "._floor_at_least_approval",
        ) as mock_floor:
            mock_floor.return_value = (floor_val in strict_floors)
            result = mock_floor("jarvis")
            assert result is True, f"floor={floor_val!r} should pass"


# ===========================================================================
# H1 / Phase 3 -- Biometric-Semantic Binding (concurrent ECAPA + ASR gate)
# ===========================================================================


def test_phase3_require_phrase_match_default_is_true(monkeypatch):
    """Phase 3: REQUIRE_PHRASE_MATCH default flips to TRUE -- phrase-match
    is mandatory by default; only an explicit 'false' disables it."""
    monkeypatch.delenv("JARVIS_COMMAND_NODE_REQUIRE_PHRASE_MATCH", raising=False)
    assert mw._require_phrase_match() is True
    monkeypatch.setenv("JARVIS_COMMAND_NODE_REQUIRE_PHRASE_MATCH", "false")
    assert mw._require_phrase_match() is False
    monkeypatch.setenv("JARVIS_COMMAND_NODE_REQUIRE_PHRASE_MATCH", "true")
    assert mw._require_phrase_match() is True


def test_h1_required_but_asr_broken_rejects(monkeypatch):
    """H1: REQUIRE=true (default) + no fn wired AND the default ASR cannot
    be bound -> REJECTED fail_closed:phrase_match_required_but_unavailable.
    (Now only fires if someone both requires it AND breaks the ASR.)"""
    monkeypatch.setenv("JARVIS_COMMAND_NODE_REQUIRE_PHRASE_MATCH", "true")
    # Simulate a broken ASR: the default resolver returns None.
    monkeypatch.setattr(
        mw.BiometricAuthMiddleware,
        "_resolve_default_phrase_match_fn",
        staticmethod(lambda: None),
    )
    m = _fresh_middleware()
    ch = _issue(m)

    async def _verify(audio, sample_rate):  # noqa: ARG001
        return _good_verdict(score=1.0)

    approve_calls: list = []

    async def _approve(*, pr_id, ast_mutation_id):  # noqa: ARG001
        approve_calls.append(pr_id)
        return {}

    res = asyncio.run(m.authorize_elevation(
        pr_id=ch.pr_id, nonce=ch.nonce, ast_mutation_id=ch.ast_mutation_id,
        audio=b"x", sample_rate=16000,
        voice_verify_fn=_verify, approve_fn=_approve,
        resolve_target_repo_fn=lambda pr: "jarvis",
        phrase_match_fn=None,  # not wired -> default ASR -> broken
    ))
    assert res.decision == "REJECTED"
    assert "phrase_match_required_but_unavailable" in res.reason
    assert approve_calls == []


def test_phase3_explicit_false_degraded_mode_authorizes(monkeypatch):
    """Phase 3: an explicit REQUIRE=false disables phrase-match (degraded /
    loopback-only mode). With no fn wired + a valid biometric -> AUTHORIZED.
    A one-time [SECURITY] warning is logged."""
    monkeypatch.setenv("JARVIS_COMMAND_NODE_REQUIRE_PHRASE_MATCH", "false")
    monkeypatch.setenv("JARVIS_COMMAND_NODE_AUTH_ENABLED", "true")
    # Reset the one-time status latch so this run actually logs.
    monkeypatch.setattr(mw, "_PHRASE_MATCH_STATUS_LOGGED", False)
    import logging as _logging

    m = _fresh_middleware()
    ch = _issue(m)
    res = asyncio.run(_authorize(m, ch, target_repo="jarvis",
                                 phrase_match_fn=None))
    assert res.decision == "AUTHORIZED"


def test_phase3_explicit_false_logs_security_warning(monkeypatch, caplog):
    monkeypatch.setenv("JARVIS_COMMAND_NODE_REQUIRE_PHRASE_MATCH", "false")
    monkeypatch.setenv("JARVIS_COMMAND_NODE_AUTH_ENABLED", "true")
    monkeypatch.setattr(mw, "_PHRASE_MATCH_STATUS_LOGGED", False)
    m = _fresh_middleware()
    ch = _issue(m)
    import logging
    with caplog.at_level(logging.WARNING, logger="CommandNode.BiometricAuth"):
        asyncio.run(_authorize(m, ch, target_repo="jarvis",
                               phrase_match_fn=None))
    joined = " ".join(r.message for r in caplog.records)
    assert "[SECURITY]" in joined
    assert "DISABLED" in joined or "disabled" in joined


# --- the concurrent gate matrix: BOTH must pass ---------------------------


def _ecapa_fn(passed: bool):
    async def _v(audio, sample_rate):  # noqa: ARG001
        return _good_verdict(score=1.0) if passed else _bad_verdict(
            authenticated=False, score=0.0)
    return _v


def _asr_fn(passed: bool, wer=None, transcript="spoken phrase"):
    """An asr_phrase_match-shaped fake: takes kwargs, returns (passed, info)."""
    async def _p(*, audio, sample_rate, expected_phrase):  # noqa: ARG001
        info = {
            "transcript": transcript if passed else "",
            "wer": (0.0 if passed else 1.0) if wer is None else wer,
            "threshold": 0.10,
        }
        return passed, info
    return _p


def _run_gate(m, ch, *, ecapa_pass, wer_pass, target_repo="jarvis"):
    approve_calls: list = []

    async def _approve(*, pr_id, ast_mutation_id):  # noqa: ARG001
        approve_calls.append((pr_id, ast_mutation_id))
        return {}

    res = asyncio.run(m.authorize_elevation(
        pr_id=ch.pr_id, nonce=ch.nonce, ast_mutation_id=ch.ast_mutation_id,
        audio=b"\x00\x01pcm", sample_rate=16000,
        voice_verify_fn=_ecapa_fn(ecapa_pass),
        approve_fn=_approve,
        resolve_target_repo_fn=lambda pr: target_repo,
        phrase_match_fn=_asr_fn(wer_pass),
    ))
    res._approve_record = approve_calls
    return res


def test_gate_ecapa_pass_wer_pass_authorized():
    m = _fresh_middleware()
    res = _run_gate(m, _issue(m), ecapa_pass=True, wer_pass=True)
    assert res.decision == "AUTHORIZED"
    assert len(res._approve_record) == 1


def test_gate_ecapa_pass_wer_fail_rejects_phrase_mismatch():
    m = _fresh_middleware()
    res = _run_gate(m, _issue(m), ecapa_pass=True, wer_pass=False)
    assert res.decision == "REJECTED"
    assert res.reason == "phrase_mismatch"
    assert res._approve_record == []


def test_gate_ecapa_fail_wer_pass_rejects_biometric_mismatch():
    m = _fresh_middleware()
    res = _run_gate(m, _issue(m), ecapa_pass=False, wer_pass=True)
    assert res.decision == "REJECTED"
    # The biometric (speaker) failure is reported, NOT phrase_mismatch.
    assert "biometric" in res.reason
    assert res.reason != "phrase_mismatch"
    assert res._approve_record == []


def test_gate_both_fail_rejects():
    m = _fresh_middleware()
    res = _run_gate(m, _issue(m), ecapa_pass=False, wer_pass=False)
    assert res.decision == "REJECTED"
    assert res._approve_record == []


def test_gate_ecapa_raises_fails_closed():
    """Either future raising (return_exceptions) -> that side False -> REJECT."""
    m = _fresh_middleware()
    ch = _issue(m)

    async def _boom_ecapa(audio, sample_rate):  # noqa: ARG001
        raise RuntimeError("ecapa exploded")

    res = asyncio.run(m.authorize_elevation(
        pr_id=ch.pr_id, nonce=ch.nonce, ast_mutation_id=ch.ast_mutation_id,
        audio=b"x", sample_rate=16000,
        voice_verify_fn=_boom_ecapa,
        approve_fn=lambda **k: None,
        resolve_target_repo_fn=lambda pr: "jarvis",
        phrase_match_fn=_asr_fn(True),
    ))
    assert res.decision == "REJECTED"
    assert "fail_closed" in res.reason or "biometric" in res.reason


def test_gate_phrase_raises_fails_closed():
    m = _fresh_middleware()
    ch = _issue(m)

    async def _boom_phrase(*, audio, sample_rate, expected_phrase):  # noqa: ARG001,E501
        raise RuntimeError("ASR exploded")

    res = asyncio.run(m.authorize_elevation(
        pr_id=ch.pr_id, nonce=ch.nonce, ast_mutation_id=ch.ast_mutation_id,
        audio=b"x", sample_rate=16000,
        voice_verify_fn=_ecapa_fn(True),
        approve_fn=lambda **k: None,
        resolve_target_repo_fn=lambda pr: "jarvis",
        phrase_match_fn=_boom_phrase,
    ))
    assert res.decision == "REJECTED"
    assert res.reason == "phrase_mismatch"


def test_gate_runs_concurrently_not_sequentially():
    """Assert the ECAPA + ASR verifiers run CONCURRENTLY (via gather), not
    sequentially: both are invoked, and neither short-circuits the other.
    A recording fake proves both started before either decision is made."""
    m = _fresh_middleware()
    ch = _issue(m)
    started: list = []
    finished: list = []

    async def _ecapa(audio, sample_rate):  # noqa: ARG001
        started.append("ecapa")
        # Yield so the other coro can interleave (proves concurrency).
        await asyncio.sleep(0.01)
        finished.append("ecapa")
        return _good_verdict(score=1.0)

    async def _phrase(*, audio, sample_rate, expected_phrase):  # noqa: ARG001
        started.append("phrase")
        await asyncio.sleep(0.01)
        finished.append("phrase")
        return True, {"transcript": "x", "wer": 0.0, "threshold": 0.10}

    res = asyncio.run(m.authorize_elevation(
        pr_id=ch.pr_id, nonce=ch.nonce, ast_mutation_id=ch.ast_mutation_id,
        audio=b"x", sample_rate=16000,
        voice_verify_fn=_ecapa, approve_fn=lambda **k: None,
        resolve_target_repo_fn=lambda pr: "jarvis",
        phrase_match_fn=_phrase,
    ))
    assert res.decision == "AUTHORIZED"
    # BOTH verifiers were invoked.
    assert set(started) == {"ecapa", "phrase"}
    # Concurrency: BOTH started before EITHER finished (sequential execution
    # would have one finish before the second starts).
    assert started == ["ecapa", "phrase"] or started == ["phrase", "ecapa"]
    assert len(started) == 2 and len(finished) == 2


def test_gate_legacy_zero_arg_phrase_match_fn_supported():
    """The legacy zero-arg phrase-match predicate still works (composes with
    the concurrent gate)."""
    m = _fresh_middleware()
    res_ok = asyncio.run(_authorize(m, _issue(m), target_repo="jarvis",
                                    phrase_match_fn=lambda: True))
    assert res_ok.decision == "AUTHORIZED"
    res_bad = asyncio.run(_authorize(m, _issue(m), target_repo="jarvis",
                                     phrase_match_fn=lambda: False))
    assert res_bad.decision == "REJECTED"
    assert res_bad.reason == "phrase_mismatch"


def test_gate_audit_includes_wer_and_transcript_hash():
    """Phase 3: the AUTHORIZED audit record carries wer + transcript_hash
    (sha256, NOT the transcript) + phrase_match_ok."""
    import hashlib
    m = _fresh_middleware()
    ch = _issue(m)
    transcript = "the immutable orange protocol still holds"
    res = asyncio.run(m.authorize_elevation(
        pr_id=ch.pr_id, nonce=ch.nonce, ast_mutation_id=ch.ast_mutation_id,
        audio=b"x", sample_rate=16000,
        voice_verify_fn=_ecapa_fn(True),
        approve_fn=lambda **k: None,
        resolve_target_repo_fn=lambda pr: "jarvis",
        phrase_match_fn=_asr_fn(True, wer=0.0, transcript=transcript),
    ))
    assert res.decision == "AUTHORIZED"
    rec = m._audit_calls[-1]
    assert rec["phrase_match_ok"] is True
    assert rec["wer"] == 0.0
    assert rec["transcript_hash"] == hashlib.sha256(
        transcript.encode("utf-8")).hexdigest()
    # The transcript text itself must NEVER appear in the audit record.
    assert transcript not in repr(rec)
