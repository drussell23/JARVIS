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


def test_emits_warning_with_hop_and_goal(caplog):
    with caplog.at_level(logging.WARNING, logger=a1_trace.logger.name):
        a1_trace.a1trace("ingest", "g-123", router="attached")
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        m == "[A1Trace] ingest goal=g-123 router=attached" for m in msgs
    ), msgs


def test_warning_level_so_it_survives_silent_boot(caplog):
    with caplog.at_level(logging.WARNING, logger=a1_trace.logger.name):
        a1_trace.a1trace("emit", "g-1")
    assert caplog.records, "no record emitted"
    assert all(r.levelno >= logging.WARNING for r in caplog.records)


def test_gated_off_is_silent(caplog, monkeypatch):
    monkeypatch.setenv("JARVIS_A1_TRACE_ENABLED", "false")
    with caplog.at_level(logging.WARNING, logger=a1_trace.logger.name):
        a1_trace.a1trace("dequeue", "g-9")
    assert not caplog.records


def test_none_kwargs_skipped(caplog):
    with caplog.at_level(logging.WARNING, logger=a1_trace.logger.name):
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
