"""§Layer 6 (v2.88) — SUPERSEDED by Phase D (2026-05-17).

ORIGINAL CONTRACT (v2.88): the wall-clock watchdog's Layer 4
escape hatch invoked ``_atexit_fallback_write`` synchronously
BEFORE ``os._exit(75)`` so a wedged-loop kill still left a
parseable partial ``summary.json``.

WHY IT WAS REVERSED: the bt-2026-05-17-024509 postmortem proved
``_atexit_fallback_write`` acquires the logging lock, and that
lock was the *poison* — a wedged producer held it, so the
watchdog's own Layer-4 path blocked on lock acquisition and never
reached ``os._exit``. A summary-before-exit guarantee is worth
nothing if the guarantee itself can deadlock the last line of
defense. Phase D (Decision A) deletes the Layer 3/4 escalation
entirely and replaces it with a **resource-zero** SIGKILL that
touches no shared resource. Per CLAUDE.md, ``debug.log`` — not
``summary.json`` — is the canonical session record, so dropping
the poisonable summary-on-the-watchdog-path is the correct
trade.

The four AST/provenance pins that enforced the (now-deleted)
``_atexit_fallback_write``-before-``os._exit(75)`` contract have
been retired. Their *inverse* — the watchdog path MUST NOT touch
logging / SIGTERM / ``_atexit_fallback_write`` — is the
load-bearing contract now, pinned in
``test_phase_d_resource_zero_watchdog.py``.

WHAT REMAINS HERE: ``_atexit_fallback_write`` itself is still a
live, canonical method — the *signal-handler* path (Ticket B,
Wave 3 v2.79) still composes it for externally-killed sessions.
The two functional tests below pin that still-valid behaviour;
they are intentionally NOT superseded.
"""
from __future__ import annotations

import ast
import atexit
import inspect
import json
from pathlib import Path
from typing import Iterator

import pytest

from backend.core.ouroboros.battle_test.harness import (
    BattleTestHarness,
    HarnessConfig,
)


# -----------------------------------------------------------------
# Phase D supersession marker (2026-05-17)
# -----------------------------------------------------------------
#
# The four v2.88 pins that enforced the
# `_atexit_fallback_write`-before-`os._exit(75)` watchdog contract
# are DELETED, not skipped — the contract they guarded is gone. A
# skipped test rots into noise; a deleted one with this marker is
# an auditable record. The inverse contract (the watchdog path is
# resource-zero) lives in test_phase_d_resource_zero_watchdog.py.


def test_layer6_watchdog_contract_superseded_by_phase_d():
    """Guard against accidental resurrection of the poison path:
    the deleted Layer-4 ``os._exit(75)`` + ``_atexit_fallback_write``
    escalation MUST NOT reappear on the watchdog path, and the
    Phase D spine that owns the replacement contract MUST exist."""
    spine = (
        Path(__file__).with_name(
            "test_phase_d_resource_zero_watchdog.py",
        )
    )
    assert spine.exists(), (
        "Phase D spine must own the resource-zero watchdog contract"
    )
    harness_src = Path(
        inspect.getfile(BattleTestHarness),
    ).read_text(encoding="utf-8")
    # Scope to the watchdog method's AST — the separate
    # _BoundedShutdownWatchdog subsystem legitimately uses
    # os._exit(75), and comments may cite the retired path for
    # provenance. Only a LIVE os._exit(75) call OR a live
    # 'incomplete_kill_layer4' string Constant *inside the
    # watchdog method* re-opens the poison vector.
    fn = next(
        (
            n for n in ast.walk(ast.parse(harness_src))
            if isinstance(n, ast.FunctionDef)
            and n.name == "_start_wall_clock_hard_deadline_thread"
        ),
        None,
    )
    assert fn is not None
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_exit"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            assert node.args[0].value != 75, (
                "a live os._exit(75) inside the watchdog method "
                f"(line {node.lineno}) re-opens the Layer-4 "
                "bt-2026-05-17-024509 poison vector"
            )
        if isinstance(node, ast.Constant) and node.value == (
            "incomplete_kill_layer4"
        ):
            raise AssertionError(
                "the Layer-4 'incomplete_kill_layer4' outcome stamp "
                f"is retired with its path (line {node.lineno})"
            )


# -----------------------------------------------------------------
# Functional integration — invoke the watchdog's Layer 4 path
# directly + assert summary.json gets written
# -----------------------------------------------------------------


@pytest.fixture
def tmp_harness(tmp_path: Path) -> Iterator[BattleTestHarness]:
    """Build a harness rooted in tmp_path. Clean up its atexit handler
    after the test."""
    session_dir = tmp_path / ".ouroboros" / "sessions" / "bt-layer6-test"
    config = HarnessConfig(
        repo_path=tmp_path,
        cost_cap_usd=0.05,
        idle_timeout_s=30.0,
        session_dir=session_dir,
    )
    harness = BattleTestHarness(config)
    yield harness
    atexit.unregister(harness._atexit_fallback_write)


def test_atexit_fallback_writes_session_id_field(tmp_harness):
    """The summary.json written by the fallback MUST include
    a ``session_id`` field — that's the load-bearing piece the
    soak harness uses to link the session in
    ``live_fire_graduation_history.jsonl``."""
    session_dir = tmp_harness._session_dir
    summary_path = session_dir / "summary.json"
    assert not summary_path.exists()

    # Manually invoke the fallback with the Layer 4 outcome marker.
    tmp_harness._atexit_fallback_write(
        session_outcome="incomplete_kill_layer4",
    )

    assert summary_path.exists()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    # Load-bearing fields for soak-harness linkage.
    assert "session_id" in summary, (
        "summary.json MUST include session_id field — without it, "
        "soak harness's _read_most_recent_session linkage falls "
        "back to 'unknown' literal string and graduation evidence "
        "is unparseable"
    )
    assert summary["session_id"] == tmp_harness.session_id
    # Layer 4 outcome stamp survives.
    assert summary.get("session_outcome") == "incomplete_kill_layer4"


def test_atexit_fallback_idempotent_when_summary_already_written(
    tmp_harness,
):
    """If the clean path already wrote summary.json
    (``_summary_written = True``), the fallback is a no-op. This
    invariant matters for Layer 4: Layer 3 SIGTERM may have triggered
    atexit which already wrote a summary; Layer 4's call to
    ``_atexit_fallback_write`` must NOT clobber it."""
    session_dir = tmp_harness._session_dir
    session_dir.mkdir(parents=True, exist_ok=True)
    summary_path = session_dir / "summary.json"

    # Pre-write a clean-path summary.
    canonical = {"session_id": "pre-existing", "stop_reason": "clean_exit"}
    summary_path.write_text(json.dumps(canonical), encoding="utf-8")
    tmp_harness._summary_written = True

    # Layer 4 fires and calls the fallback.
    tmp_harness._atexit_fallback_write(
        session_outcome="incomplete_kill_layer4",
    )

    # Pre-existing summary preserved.
    re_read = json.loads(summary_path.read_text(encoding="utf-8"))
    assert re_read == canonical
