"""A1-T4 — [A1Trace] breadcrumb regression spine.

Behavioural tests on the ``a1_trace`` helper (emission, gating, fail-soft)
plus structural assertions that each of the five intake->FSM hop sites
calls ``a1trace`` with the right hop label (deep call sites can't be unit
-driven without booting the whole stack; the chain is exercised live in the
T5 integration test + the operator soak).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import a1_trace


def test_emits_hop_and_goal(caplog):
    """The MESSAGE is the contract; the level is a routing decision.

    `g-123` was never emitted by the roadmap, so this hop is out of the A1
    proof's scope and lands at DEBUG -- in `debug.log`, which is the sink
    every auditor reads. The text is byte-identical to what it always was.
    """
    with caplog.at_level(logging.DEBUG, logger=a1_trace.logger.name):
        a1_trace.a1trace("ingest", "g-123", router="attached")
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        m == "[A1Trace] ingest goal=g-123 router=attached" for m in msgs
    ), msgs


def test_in_scope_hop_reaches_the_operator_without_faking_severity(caplog):
    """The proof still reaches the terminal -- by AUDIENCE, not by severity.

    Replaces `test_warning_level_so_it_survives_silent_boot`, which pinned
    the workaround rather than the requirement. The requirement is that a
    soak operator sees the five ordered hops; WARNING was merely the only
    lever that existed before `silent_boot.operator_extra`. Asserting the
    old lever would forbid ever fixing the 376-line-per-session cost of it.
    """
    with caplog.at_level(logging.DEBUG, logger=a1_trace.logger.name):
        a1_trace.a1trace("emit", "g-1")
    assert caplog.records, "no record emitted"
    rec = caplog.records[-1]
    assert rec.getMessage() == "[A1Trace] emit goal=g-1"
    # Honest severity ...
    assert rec.levelno == logging.INFO
    # ... and an explicit audience decision that carries it to the terminal.
    assert getattr(rec, "operator", False) is True


def test_out_of_scope_hop_is_not_promoted_to_the_operator(caplog):
    """A sensor op must not spend the operator's attention.

    188 `[A1Trace][emit-probe] MISSING ... source=non-roadmap` lines in one
    session is the measurement this whole change answers.
    """
    a1_trace._emit_ledger.clear()
    with caplog.at_level(logging.DEBUG, logger=a1_trace.logger.name):
        a1_trace.a1trace("ingest", "op-sensor-42")
    rec = caplog.records[-1]
    assert rec.levelno == logging.DEBUG
    assert getattr(rec, "operator", False) is False


def test_scope_never_narrows_when_the_ledger_cannot_answer(monkeypatch, caplog):
    """Probe OFF means no oracle -- and no oracle must not mean silence.

    `emit_probe` returns early when its flag is off, so `_emit_ledger` stays
    empty forever. A scope test that trusted an empty ledger would demote the
    ENTIRE proof to DEBUG in exactly the configuration where someone had
    already turned half the instrument off.
    """
    monkeypatch.setenv("JARVIS_A1_EMIT_PROBE_ENABLED", "false")
    a1_trace._emit_ledger.clear()
    with caplog.at_level(logging.DEBUG, logger=a1_trace.logger.name):
        a1_trace.a1trace("accept", "g-unknown")
    rec = caplog.records[-1]
    assert rec.levelno == logging.INFO
    assert getattr(rec, "operator", False) is True


def test_gated_off_is_silent(caplog, monkeypatch):
    monkeypatch.setenv("JARVIS_A1_TRACE_ENABLED", "false")
    with caplog.at_level(logging.DEBUG, logger=a1_trace.logger.name):
        a1_trace.a1trace("dequeue", "g-9")
    assert not caplog.records


def test_none_kwargs_skipped(caplog):
    with caplog.at_level(logging.DEBUG, logger=a1_trace.logger.name):
        a1_trace.a1trace("submit", "g-7", phase=None, target="GLS")
    msg = caplog.records[-1].getMessage()
    assert "phase=" not in msg
    assert msg == "[A1Trace] submit goal=g-7 target=GLS"


def test_fail_soft_never_raises():
    # Exotic goal_id object must not raise.
    class _Boom:
        def __str__(self):  # noqa: D401
            raise RuntimeError("boom")

    # Should swallow internally and not propagate.
    a1_trace.a1trace("accept", _Boom())


# --- Structural: every hop site is instrumented ---------------------------

_REPO = Path(__file__).resolve().parents[2]
_GOV = _REPO / "backend" / "core" / "ouroboros" / "governance"


def _src(rel: str) -> str:
    return (_GOV / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "rel, hop",
    [
        ("roadmap_orchestrator.py", "emit"),
        ("intake/unified_intake_router.py", "ingest"),
        ("intake/unified_intake_router.py", "dequeue"),
        ("intake/unified_intake_router.py", "submit"),
        ("orchestrator.py", "accept"),
    ],
)
def test_hop_site_calls_a1trace(rel, hop):
    src = _src(rel)
    assert "a1trace" in src, f"{rel} does not import/call a1trace"
    assert re.search(rf'a1trace\(\s*["\']{hop}["\']', src), (
        f"{rel} missing a1trace('{hop}', ...) call"
    )
