"""Tier-2 batch-3 fallout — async dispatch registry seam regression spine.

Batch 3 made ``last_session_summary.format_for_prompt``/``load`` and
``continuity_repl.dispatch_continuity_command`` async, and added
``_sync`` bridges (``load_sync``, ``verify_pre_commit``/
``verify_governance_state_sync``) that probe ``asyncio.get_running_loop()``
and degrade to an empty/DISABLED result when a loop IS running. The
505 existing tests all called the dispatchers directly (never through
the registry on a live loop), so none of them caught that
``repl_dispatch_registry.try_dispatch`` — the ONLY real caller,
invoked from SerpentFlow's running ``_loop`` coroutine — was still
``def`` (sync) and called ``fn(line)`` without awaiting. Any
``async def dispatch_<verb>_command`` therefore returned an
un-awaited coroutine object, and the registry's tri-attribute
projection (``getattr(result, "matched"/"ok"/"text", ...)``) silently
degraded to ``ok=False, text=""`` — 3 operator surfaces
(``/continuity``, ``/story``, ``/commit status``) always reported
disabled/empty even when the underlying substrate was enabled and
populated.

This module is the seam those 505 tests skipped: every test here
drives its target THROUGH ``try_dispatch`` (or the synthetic-module
equivalent) on a REAL running event loop and asserts the genuine
payload comes back — a dropped ``await`` anywhere in the chain must
fail these tests.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    for key in list(__import__("os").environ.keys()):
        if key.startswith((
            "JARVIS_SESSION_CONTINUITY_",
            "JARVIS_SESSION_STORY_",
            "JARVIS_LAST_SESSION_SUMMARY_",
            "JARVIS_OPERATOR_COMMIT_AUTHORITY_",
            "JARVIS_GOVERNANCE_MANIFEST_",
            "JARVIS_COMMIT_AUTHORITY_",
            "JARVIS_REPL_DISPATCH_AUTODISCOVERY_",
        )):
            monkeypatch.delenv(key, raising=False)

    from backend.core.ouroboros.governance import (
        last_session_summary as lss,
    )
    from backend.core.ouroboros.battle_test import (
        repl_dispatch_registry as rdr,
    )
    lss.reset_default_summary()
    rdr.reset_registry_for_tests()
    yield
    lss.reset_default_summary()
    rdr.reset_registry_for_tests()


def _write_session_summary(
    project_root: Path,
    session_id: str,
    **fields: Any,
) -> Path:
    """Mirrors ``test_last_session_summary.py``'s ``_write_summary``
    fixture shape — a well-formed ``.ouroboros/sessions/<id>/
    summary.json`` the canonical LastSessionSummary parser accepts."""
    session_dir = project_root / ".ouroboros" / "sessions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    payload: Dict[str, Any] = {
        "session_id": session_id,
        "stop_reason": "idle_timeout",
        "duration_s": 120.0,
        "stats": {
            "attempted": 3, "completed": 2, "failed": 1,
            "cancelled": 0, "queued": 0,
        },
        "cost_total": 0.42,
        "cost_breakdown": {"claude": 0.42},
        "branch_stats": {
            "commits": 1, "files_changed": 2,
            "insertions": 10, "deletions": 2,
        },
        "strategic_drift": {"ratio": 0.1, "status": "ok"},
        "convergence_state": "CONVERGING",
    }
    payload.update(fields)
    path = session_dir / "summary.json"
    path.write_text(json.dumps(payload))
    return path


# ---------------------------------------------------------------------------
# (1) /continuity through try_dispatch on a running loop
# ---------------------------------------------------------------------------


async def test_continuity_panel_through_try_dispatch_on_running_loop(
    monkeypatch,
):
    """Regression: pre-fix, ``dispatch_continuity_command`` (already
    ``async def``) was called by the sync ``try_dispatch`` without
    awaiting — ``DispatchOutcome(ok=False, text="")`` every time,
    regardless of the master flag. Post-fix, the registry awaits it
    and the real panel text comes back."""
    monkeypatch.setenv("JARVIS_SESSION_CONTINUITY_ENABLED", "true")
    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (
        try_dispatch,
    )
    out = await try_dispatch("/continuity panel")
    assert out.matched is True
    assert out.ok is True
    assert out.text  # non-empty — the bug always produced ""
    assert "continuity" in out.text.lower() or "session" in out.text.lower()


# ---------------------------------------------------------------------------
# (2) /story session through try_dispatch on a running loop
# ---------------------------------------------------------------------------


async def test_story_session_through_try_dispatch_on_running_loop(
    monkeypatch, tmp_path,
):
    """Regression: pre-fix, ``story_repl.dispatch_story_command`` was
    sync and bridged to ``session_story.aggregate_session_story`` ->
    ``LastSessionSummary.load_sync`` -> degraded to ``[]`` on a
    running loop, so ``/story session`` always reported "no
    parseable session records" even with a real session on disk.
    Post-fix, ``dispatch_story_command`` is async and
    ``aggregate_session_story`` awaits ``load()`` directly."""
    from backend.core.ouroboros.governance import (
        last_session_summary as lss,
    )
    monkeypatch.setenv("JARVIS_SESSION_STORY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_LAST_SESSION_SUMMARY_ENABLED", "true")
    _write_session_summary(tmp_path, "bt-2026-07-02-000000")
    lss.reset_default_summary()
    # Seed the process-wide singleton with tmp_path BEFORE dispatch —
    # session_story.aggregate_session_story calls get_default_summary()
    # with no args, which returns whichever project_root won the race
    # to construct the singleton.
    lss.get_default_summary(project_root=tmp_path)

    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (
        try_dispatch,
    )
    out = await try_dispatch("/story session")
    assert out.matched is True
    assert out.ok is True
    assert "no parseable session" not in out.text.lower()
    assert "you ran 3 op" in out.text.lower()


# ---------------------------------------------------------------------------
# (3) /commit status through try_dispatch on a running loop
# ---------------------------------------------------------------------------


async def test_commit_status_authorized_end_to_end_through_try_dispatch(
    monkeypatch, tmp_path,
):
    """Full round trip: ``/commit status`` dispatched via
    ``try_dispatch`` on a real running event loop (the ONLY way
    SerpentFlow ever calls it) must reach the REAL async verifier and
    report AUTHORIZED — not silently degrade because
    ``dispatch_commit_command``'s coroutine was never awaited."""
    monkeypatch.setenv(
        "JARVIS_OPERATOR_COMMIT_AUTHORITY_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_COMMIT_AUTHORITY_GRANTS_PATH",
        str(tmp_path / "grants.jsonl"),
    )
    monkeypatch.setenv(
        "JARVIS_COMMIT_AUTHORITY_SECRET_PATH",
        str(tmp_path / "secret"),
    )
    monkeypatch.setenv(
        "JARVIS_COMMIT_AUTHORITY_PRESENCE_FILE",
        str(tmp_path / "presence.json"),
    )

    from backend.core.ouroboros.governance import (
        operator_commit_authority as oca,
        commit_repl as cr,
    )

    # commit_repl resolves repo_root/branch via subprocess `git`;
    # pin both to the isolated tmp_path so the grant/presence we mint
    # below actually matches what /commit status resolves.
    monkeypatch.setattr(cr, "_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(cr, "_current_branch", lambda _r: "test-branch")

    grant_out = oca.issue_grant(
        channel="ide", operator_label="tier2-batch3-integration-test",
        branch="test-branch", repo_root=tmp_path,
    )
    assert grant_out.ok is True, grant_out.error

    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (
        try_dispatch,
    )
    result = await try_dispatch("/commit status")
    assert result.matched is True
    assert result.ok is True
    assert "dry_verify" in result.text
    assert "authorized" in result.text.lower(), result.text


async def test_commit_status_async_governance_gate_not_degraded(
    monkeypatch, tmp_path,
):
    """Direct regression guard for the ORIGINAL root cause:
    ``verify_pre_commit_async``'s operator-channel branch must await
    ``governance_manifest.verify_governance_state`` directly — NEVER
    fall through to the on-loop-degrading
    ``verify_governance_state_sync`` bridge (which silently returns a
    DISABLED-shaped comparison whenever a loop is already running,
    the exact bug this fix closes for the REPL/CLI/IDE/daemon
    channels' governance hash-cap check).

    Poisons ``verify_governance_state_sync`` to raise if called; a
    regression back to the sync bridge would have that exception
    swallowed by ``_governance_gate``'s defensive except-pass, which
    silently turns DRIFT into a pass-through AUTHORIZED verdict — so
    asserting the DRIFT-driven denial (not AUTHORIZED) discriminates
    the two code paths precisely."""
    monkeypatch.setenv(
        "JARVIS_OPERATOR_COMMIT_AUTHORITY_ENABLED", "true",
    )
    from backend.core.ouroboros.governance import (
        operator_commit_authority as oca,
        governance_manifest as gm,
    )

    class _DriftComparison:
        verdict = gm.ManifestVerdict.DRIFT

    def _poison_sync(**kwargs):
        raise AssertionError(
            "verify_governance_state_sync must NOT be called from "
            "verify_pre_commit_async's operator-channel branch"
        )

    async def _real_async(**kwargs):
        return _DriftComparison()

    monkeypatch.setattr(
        gm, "verify_governance_state_sync", _poison_sync,
    )
    monkeypatch.setattr(gm, "verify_governance_state", _real_async)

    grant_out = oca.issue_grant(
        channel="repl", operator_label="drift-guard-test",
        branch="feat", repo_root=tmp_path,
    )
    assert grant_out.ok is True, grant_out.error

    ctx = oca.CommitAuthorityContext(
        channel="repl",
        repo_root=str(tmp_path),
        branch="feat",
        staged_files=("backend/core/ouroboros/governance/x.py",),
    )
    v = await oca.verify_pre_commit_async(ctx)
    assert v.verdict is oca.CommitAuthorityVerdict.DENIED_GOVERNANCE_DRIFT
    assert v.governance_verdict == "drift"


# ---------------------------------------------------------------------------
# (4) Unit test — try_dispatch awaits a coroutine-returning dispatcher
# ---------------------------------------------------------------------------


async def test_try_dispatch_awaits_coroutine_returning_dispatcher(
    tmp_path, monkeypatch,
):
    """The exact seam: an ``async def dispatch_<verb>_command``
    module-level callable, auto-discovered and routed through
    ``try_dispatch``. Pre-fix this returns
    ``DispatchOutcome(ok=False, text="")`` (coroutine has no
    ``.ok``/``.text``); post-fix the awaited result's real payload
    comes back."""
    pkg = tmp_path / "synth_async_repl_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "asyncgood_repl.py").write_text(
        "from dataclasses import dataclass\n"
        "@dataclass(frozen=True)\n"
        "class R:\n"
        "    matched: bool = True\n"
        "    ok: bool = True\n"
        "    text: str = 'async-good!'\n"
        "async def dispatch_asyncgood_command(line):\n"
        "    return R()\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    for k in list(sys.modules.keys()):
        if k.startswith("synth_async_repl_pkg"):
            del sys.modules[k]

    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (
        prime_registry, list_verbs, try_dispatch,
    )
    prime_registry(
        packages=["synth_async_repl_pkg"],
        excluded_verbs=[],
        force=True,
    )
    verbs = list_verbs()
    assert "asyncgood" in verbs

    out = await try_dispatch("/asyncgood")
    assert out.matched is True
    assert out.ok is True
    assert out.text == "async-good!"
