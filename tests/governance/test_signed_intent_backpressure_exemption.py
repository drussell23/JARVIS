"""Signed operator intent must not park behind unsigned sensor noise.

Intake step 4 rejects an envelope when the queue is past its backpressure
threshold and the source is not in a static exemption set. That set sits
UPSTREAM of every adaptive mechanism in the router: the SensorGovernor consult
and the QoS aging/starvation escalation both live at step 4b, after this
return. A signal bounced at 4 never reaches the machinery built to rescue a
starved one.

Measured, soak bt-2026-08-30-072704: the operator's signed goal
`docs-skip-tools-gate-drift` emitted with a valid HMAC and was rejected here
146 times, the reader ledger recording the literal return value of this gate
as its idempotency key -- `"backpressure"`.

The exemption is derived from the SIGNATURE, never from `source == "roadmap"`.
A dev-mode read (REQUIRE_SIGNATURE=false) and an attacker-dropped `.jarvis/`
file both present that same string.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.intake import unified_intake_router as R


class _Verdict:
    def __init__(self, value):
        self.value = value


class _Goal:
    goal_id = "g-signed"


class _Doc:
    goals = (_Goal(),)

    def __init__(self, signature_valid=True):
        self.signature_valid = signature_valid


class _Env:
    """Envelope carrying only a POINTER, exactly as production does."""
    def __init__(self, goal_id="g-signed"):
        self.evidence = {"provenance": {"goal_id": goal_id}}
        self.source = "roadmap"


def _patch_reader(monkeypatch, verdict, doc):
    from backend.core.ouroboros.governance import delegated_provenance
    monkeypatch.setattr(
        delegated_provenance, "_verified_roadmap", lambda: (verdict, doc)
    )


def test_a_validly_signed_goal_is_exempt(monkeypatch):
    """The positive control. Without it every refusal below could pass for
    the trivial reason that the helper never returns True at all."""
    _patch_reader(monkeypatch, _Verdict("valid"), _Doc(True))
    assert R._carries_verified_operator_authority(_Env()) is True


@pytest.mark.parametrize(
    "verdict_value",
    ["invalid_signature", "tampered", "missing", "invalid_format", "expired"],
)
def test_an_unverified_roadmap_earns_no_exemption(monkeypatch, verdict_value):
    """Any non-valid verdict must NOT preempt the queue. Otherwise anyone able
    to write `.jarvis/roadmap.yaml` could push work past backpressure without
    holding the HMAC key."""
    _patch_reader(monkeypatch, _Verdict(verdict_value), _Doc(True))
    assert R._carries_verified_operator_authority(_Env()) is False


def test_the_cryptographic_fact_is_demanded_too(monkeypatch):
    """The reader permits an unsigned dev-mode that reports `valid` for a
    document carrying no signature. The verdict alone is insufficient."""
    _patch_reader(monkeypatch, _Verdict("valid"), _Doc(False))
    assert R._carries_verified_operator_authority(_Env()) is False


def test_a_goal_id_absent_from_the_document_earns_nothing(monkeypatch):
    """The envelope supplies a pointer, not a claim. A fabricated evidence
    block must name a goal that actually exists in the verified roadmap."""
    _patch_reader(monkeypatch, _Verdict("valid"), _Doc(True))
    assert R._carries_verified_operator_authority(_Env("g-forged")) is False


def test_source_name_alone_confers_nothing(monkeypatch):
    """source == "roadmap" with no provenance pointer is not authority."""
    _patch_reader(monkeypatch, _Verdict("valid"), _Doc(True))

    class _Bare:
        source = "roadmap"
        evidence = {}

    assert R._carries_verified_operator_authority(_Bare()) is False


def test_a_broken_reader_fails_closed(monkeypatch):
    """A reader that raises must degrade to the PREVIOUS behaviour (subject to
    backpressure), never open the gate."""
    from backend.core.ouroboros.governance import delegated_provenance

    def _boom():
        raise RuntimeError("reader down")

    monkeypatch.setattr(delegated_provenance, "_verified_roadmap", _boom)
    assert R._carries_verified_operator_authority(_Env()) is False


def test_master_flag_off_restores_legacy_behaviour(monkeypatch):
    """Falsey master => byte-identical to the static-set-only gate."""
    _patch_reader(monkeypatch, _Verdict("valid"), _Doc(True))
    monkeypatch.setenv("JARVIS_SIGNED_INTENT_BACKPRESSURE_EXEMPT", "false")
    assert R._carries_verified_operator_authority(_Env()) is False


def test_default_is_on(monkeypatch):
    """Unset means enabled -- the fix is live without operator action."""
    monkeypatch.delenv(
        "JARVIS_SIGNED_INTENT_BACKPRESSURE_EXEMPT", raising=False
    )
    assert R._operator_authority_exemption_enabled() is True


def test_the_static_set_still_stands():
    """The pre-existing exemptions are untouched -- this ADDS a category, it
    does not replace one."""
    assert "voice_human" in R._BACKPRESSURE_EXEMPT
    assert "test_failure" in R._BACKPRESSURE_EXEMPT
    # And `roadmap` is deliberately NOT here: the name is not the authority.
    assert "roadmap" not in R._BACKPRESSURE_EXEMPT


def test_the_gate_actually_calls_the_helper():
    """WIRING PIN. A correct helper that nothing invokes is the failure mode
    this whole change exists to avoid -- and it is invisible to every test
    above, which call the helper directly. Pin the call site by AST so the
    helper cannot be silently orphaned by a later edit.

    Also pins the ORDER: the authority check must come AFTER the depth probe,
    so the roadmap re-read happens only for an envelope actually about to be
    rejected. Reversing them turns a rare-path verification into a hot-path
    one on every single ingest.
    """
    import ast
    import io as _io

    src = _io.open(R.__file__, encoding="utf-8").read()
    tree = ast.parse(src)
    called = {
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "_carries_verified_operator_authority" in called, (
        "helper is defined but never called -- the gate is unwired"
    )

    # Bounded window starting at the gate comment; both terms must appear
    # inside it, with the cheap probe first.
    start = src.index("# 4. Backpressure check")
    gate = src[start:start + 2000]
    assert "intake_queue_depth" in gate, "depth probe missing from the gate"
    assert "_carries_verified_operator_authority" in gate, (
        "authority check missing from the gate"
    )
    assert gate.index("intake_queue_depth") < gate.index(
        "_carries_verified_operator_authority"
    ), "authority check must be last so it short-circuits"
