"""The prompt is graded, and the grade is reachable from a real assembly.

The dependency pruner walks the whole set down FULL → SIGNATURES → NAMES. At
NAMES the model is handed bare identifiers with no argument lists, so every
call it writes is a guess, and the AttributeError that follows is caused by
the prompt rather than by the model. That condition used to be a single INFO
line; a soak could burn hundreds of iterations against it and file the result
under "model quality".

These tests pin two things: the grading is positional (so a rung added later
is graded, not skipped), and the watchdog is actually WIRED -- a real
``fit_dependencies`` call records a verdict.
"""

from __future__ import annotations

import logging

import pytest

from backend.core.ouroboros.governance.ast_signature_pruner import (
    LADDER,
    fit_dependencies,
)
from backend.core.ouroboros.governance.context_fidelity import (
    FidelityWatchdog,
    get_watchdog,
    observe,
)

RUNGS = tuple(d.value for d in LADDER)

MODULE = '''"""A dependency with a real surface."""


def connect(host: str, port: int = 5432, *, timeout: float = 1.0) -> str:
    """Open a connection."""
    payload = "x" * 400
    return f"{host}:{port}:{timeout}:{len(payload)}"


def disconnect(handle: str, *, force: bool = False) -> bool:
    """Close it."""
    payload = "y" * 400
    return bool(handle) and (force or len(payload) > 0)
'''


@pytest.fixture
def watchdog():
    wd = FidelityWatchdog(window=8)
    return wd


# ---------------------------------------------------------------------------
# Grading is positional
# ---------------------------------------------------------------------------


def test_full_rung_is_ok(watchdog):
    v = watchdog.judge("Import source", RUNGS[0], RUNGS, 2, 100, 1000)
    assert v.severity == "ok"
    assert v.starved is False


def test_signatures_rung_is_degraded_not_starved(watchdog):
    """Bodies are gone but arity and keywords survive — the model can still
    write a correct call, so this is not starvation."""
    v = watchdog.judge("Import source", RUNGS[1], RUNGS, 2, 100, 1000)
    assert v.severity == "degraded"
    assert v.starved is False


def test_names_rung_is_starvation(watchdog):
    v = watchdog.judge("Import source", RUNGS[-1], RUNGS, 2, 25, 375)
    assert v.severity == "starved"
    assert v.starved is True
    assert "no argument lists" in v.reason


def test_overflow_outranks_starvation(watchdog):
    """Over budget at the floor rung: uninformative AND too big."""
    v = watchdog.judge("Test context", RUNGS[-1], RUNGS, 9, 900, 375)
    assert v.severity == "overflow"
    assert v.starved is True
    assert v.headroom > 1.0


def test_a_rung_added_later_is_graded_by_position(watchdog):
    """The gate must not be keyed to the literal name 'names'."""
    ladder = ("full", "signatures", "outlines", "names")
    assert watchdog.judge("x", "outlines", ladder, 1, 10, 100).severity == "degraded"
    assert watchdog.judge("x", "names", ladder, 1, 10, 100).severity == "starved"


def test_unknown_rung_does_not_crash(watchdog):
    assert watchdog.judge("x", "martian", RUNGS, 1, 10, 100).severity == "ok"


# ---------------------------------------------------------------------------
# Log volume is logarithmic, not per-generation
# ---------------------------------------------------------------------------


def test_persistent_starvation_warns_logarithmically(watchdog, caplog):
    caplog.set_level(logging.WARNING)
    for _ in range(32):
        watchdog.record(
            watchdog.judge("Import source", RUNGS[-1], RUNGS, 1, 25, 375)
        )
    lines = [r for r in caplog.records if "ContextStarvation" in r.getMessage()]
    # 1, 2, 4, 8, 16, 32 -> six, not thirty-two.
    assert len(lines) == 6, [r.getMessage() for r in lines]


def test_healthy_assembly_is_silent(watchdog, caplog):
    caplog.set_level(logging.WARNING)
    for _ in range(10):
        watchdog.record(watchdog.judge("Import source", RUNGS[0], RUNGS, 1, 10, 1000))
    assert not [r for r in caplog.records if "ContextStarvation" in r.getMessage()]


def test_recovery_resets_the_streak(watchdog, caplog):
    for _ in range(4):
        watchdog.record(watchdog.judge("L", RUNGS[-1], RUNGS, 1, 25, 375))
    watchdog.record(watchdog.judge("L", RUNGS[0], RUNGS, 1, 25, 1000))
    caplog.set_level(logging.WARNING)
    watchdog.record(watchdog.judge("L", RUNGS[-1], RUNGS, 1, 25, 375))
    # A fresh streak warns again rather than staying latched shut.
    assert [r for r in caplog.records if "ContextStarvation" in r.getMessage()]


# ---------------------------------------------------------------------------
# Bounded and unkillable
# ---------------------------------------------------------------------------


def test_history_is_bounded(watchdog):
    for _ in range(200):
        watchdog.record(watchdog.judge("L", RUNGS[0], RUNGS, 1, 1, 10))
    assert len(watchdog.recent()) == 8


def test_summary_reports_the_starved_share(watchdog):
    for _ in range(3):
        watchdog.record(watchdog.judge("L", RUNGS[-1], RUNGS, 1, 25, 375))
    watchdog.record(watchdog.judge("L", RUNGS[0], RUNGS, 1, 25, 1000))
    summary = watchdog.summary()
    assert summary["observed"] == 4
    assert summary["starved"] == 3
    assert summary["starved_share"] == pytest.approx(0.75)
    assert summary["labels"]["L"]["counts"]["starved"] == 3


@pytest.mark.parametrize(
    "args",
    [
        ("L", "names", (), 1, 10, 100),
        ("L", "names", RUNGS, 0, 0, 0),
        ("", "", RUNGS, -1, -1, -1),
    ],
)
def test_degenerate_input_never_raises(watchdog, args):
    watchdog.record(watchdog.judge(*args))


# ---------------------------------------------------------------------------
# The reachability proof: a REAL assembly records a verdict
# ---------------------------------------------------------------------------


def test_a_real_starved_assembly_is_observed():
    """The budget that shipped was 375 tokens against 4,289 chars of
    signatures. Reproduce that shape and assert the watchdog sees it."""
    get_watchdog().reset()
    pruned, used = fit_dependencies(
        [("db/client.py", MODULE)], budget_tokens=30, label="Import source",
    )
    assert pruned[0].detail.value == RUNGS[-1]
    verdicts = get_watchdog().recent()
    assert verdicts, "fit_dependencies recorded nothing — the watchdog is dead code"
    assert verdicts[-1].starved is True
    assert verdicts[-1].label == "Import source"


def test_a_real_healthy_assembly_is_observed_as_ok():
    get_watchdog().reset()
    pruned, _ = fit_dependencies(
        [("db/client.py", MODULE)], budget_tokens=4096, label="Import source",
    )
    assert pruned[0].detail.value == RUNGS[0]
    assert get_watchdog().recent()[-1].severity == "ok"


def test_the_module_level_seam_records():
    get_watchdog().reset()
    v = observe("Test context", RUNGS[-1], RUNGS, 2, 25, 375)
    assert v.starved is True
    assert get_watchdog().summary()["starved"] == 1
