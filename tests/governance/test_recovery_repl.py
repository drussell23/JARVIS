"""Tests for /recover REPL + plan store (Slice 4)."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, List

import pytest

from backend.core.ouroboros.governance.recovery_advisor import (
    FailureContext,
    STOP_APPROVAL_REQUIRED,
    STOP_COST_CAP,
    STOP_VALIDATION_EXHAUSTED,
    advise,
)
from backend.core.ouroboros.governance.recovery_announcer import (
    RecoveryAnnouncer,
    reset_default_announcer,
)
from backend.core.ouroboros.governance.recovery_repl import (
    RecoveryDispatchResult,
    dispatch_recovery_command,
    reset_default_plan_provider,
    set_default_plan_provider,
)
from backend.core.ouroboros.governance.recovery_store import (
    RECOVERY_STORE_SCHEMA_VERSION,
    RecoveryPlanStore,
    get_default_plan_store,
    reset_default_plan_store,
)
from backend.core.ouroboros.governance.session_browser import (
    BookmarkStore,
    SessionBrowser,
    SessionIndex,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset():
    reset_default_plan_provider()
    reset_default_plan_store()
    reset_default_announcer()
    for key in (
        "OUROBOROS_NARRATOR_ENABLED",
        "JARVIS_RECOVERY_VOICE_ENABLED",
    ):
        os.environ.pop(key, None)
    yield
    reset_default_plan_provider()
    reset_default_plan_store()
    reset_default_announcer()
    for key in (
        "OUROBOROS_NARRATOR_ENABLED",
        "JARVIS_RECOVERY_VOICE_ENABLED",
    ):
        os.environ.pop(key, None)


def _sample_plan(op_id: str = "op-1", stop_reason: str = STOP_COST_CAP):
    return advise(FailureContext(
        op_id=op_id, stop_reason=stop_reason,
        cost_spent_usd=0.80, cost_cap_usd=0.50,
    ))


# ===========================================================================
# RecoveryPlanStore
# ===========================================================================


def test_store_schema_version_pinned():
    assert RECOVERY_STORE_SCHEMA_VERSION == "recovery_store.v1"


def test_store_records_and_retrieves():
    store = RecoveryPlanStore()
    plan = _sample_plan("op-1")
    store.record(plan)
    assert store.get_plan("op-1") is plan


def test_store_get_unknown_returns_none():
    store = RecoveryPlanStore()
    assert store.get_plan("op-missing") is None


def test_store_record_none_or_empty_op_is_noop():
    store = RecoveryPlanStore()
    store.record(None)  # type: ignore[arg-type]
    from backend.core.ouroboros.governance.recovery_advisor import RecoveryPlan
    store.record(RecoveryPlan(op_id="", failure_summary=""))
    assert store.stats()["size"] == 0


def test_store_recent_plans_newest_first():
    store = RecoveryPlanStore()
    store.record(_sample_plan("op-1"))
    store.record(_sample_plan("op-2"))
    store.record(_sample_plan("op-3"))
    recent = store.recent_plans(limit=5)
    assert [p.op_id for p in recent] == ["op-3", "op-2", "op-1"]


def test_store_recent_plans_respects_limit():
    store = RecoveryPlanStore()
    for i in range(10):
        store.record(_sample_plan(f"op-{i}"))
    recent = store.recent_plans(limit=3)
    assert len(recent) == 3
    assert [p.op_id for p in recent] == ["op-9", "op-8", "op-7"]


def test_store_capacity_evicts_oldest():
    store = RecoveryPlanStore(capacity=16)
    for i in range(20):
        store.record(_sample_plan(f"op-{i:03d}"))
    # op-000..op-003 evicted, op-004..op-019 remain
    assert store.get_plan("op-000") is None
    assert store.get_plan("op-019") is not None
    assert store.stats()["size"] <= 16


def test_store_update_same_op_replaces():
    store = RecoveryPlanStore()
    plan_a = _sample_plan("op-1", stop_reason=STOP_COST_CAP)
    plan_b = _sample_plan("op-1", stop_reason=STOP_VALIDATION_EXHAUSTED)
    store.record(plan_a)
    store.record(plan_b)
    assert store.get_plan("op-1").matched_rule == "validation_exhausted"


def test_store_clear_empties():
    store = RecoveryPlanStore()
    store.record(_sample_plan("op-1"))
    store.clear()
    assert store.stats()["size"] == 0


def test_store_singleton_returns_same():
    a = get_default_plan_store()
    b = get_default_plan_store()
    assert a is b


# ===========================================================================
# REPL match / no-match
# ===========================================================================


def test_non_recover_line_does_not_match():
    res = dispatch_recovery_command("/session help")
    assert res.matched is False


def test_empty_line_does_not_match():
    res = dispatch_recovery_command("")
    assert res.matched is False


# ===========================================================================
# /recover help
# ===========================================================================


def test_help_lists_verbs():
    res = dispatch_recovery_command("/recover help")
    assert res.ok
    for keyword in ("<op-id>", "speak", "session", "Karen"):
        assert keyword in res.text


def test_help_via_question_mark():
    res = dispatch_recovery_command("/recover ?")
    assert res.ok
    assert "recover" in res.text.lower()


# ===========================================================================
# /recover <op-id> — live drill-down
# ===========================================================================


def test_live_recover_renders_plan():
    store = RecoveryPlanStore()
    store.record(_sample_plan("op-live"))
    res = dispatch_recovery_command(
        "/recover op-live", plan_provider=store,
    )
    assert res.ok
    assert "op-live" in res.text
    assert "Try next:" in res.text


def test_live_recover_missing_op_returns_error():
    store = RecoveryPlanStore()
    res = dispatch_recovery_command(
        "/recover op-ghost", plan_provider=store,
    )
    assert not res.ok
    assert "no plan" in res.text.lower()


def test_live_recover_no_provider_returns_error():
    res = dispatch_recovery_command("/recover op-1")
    assert not res.ok
    assert "no plan provider" in res.text.lower()


def test_live_recover_uses_module_default_provider():
    store = get_default_plan_store()
    store.record(_sample_plan("op-default"))
    set_default_plan_provider(store)
    res = dispatch_recovery_command("/recover op-default")
    assert res.ok
    assert "op-default" in res.text


# ===========================================================================
# /recover <op-id> speak
# ===========================================================================


def test_live_recover_speak_queues_when_voice_live(monkeypatch):
    monkeypatch.setenv("OUROBOROS_NARRATOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_RECOVERY_VOICE_ENABLED", "true")
    store = RecoveryPlanStore()
    store.record(_sample_plan("op-speak"))

    spoken = []
    async def _capture(text, voice="Karen"):
        spoken.append((text, voice))
        return True
    announcer = RecoveryAnnouncer(speaker=_capture)

    res = dispatch_recovery_command(
        "/recover op-speak speak",
        plan_provider=store,
        announcer=announcer,
    )
    assert res.ok
    assert "queued for karen voice" in res.text.lower()
    assert announcer.stats()["queued"] == 1


def test_live_recover_speak_reports_disabled_when_off():
    store = RecoveryPlanStore()
    store.record(_sample_plan("op-speak"))
    announcer = RecoveryAnnouncer(speaker=lambda *a, **k: None)
    res = dispatch_recovery_command(
        "/recover op-speak speak",
        plan_provider=store,
        announcer=announcer,
    )
    assert res.ok
    assert "voice disabled" in res.text.lower() or "not enabled" in res.text.lower()


# ===========================================================================
# /recover (no args) — recent list
# ===========================================================================


def test_bare_recover_lists_recent():
    store = RecoveryPlanStore()
    store.record(_sample_plan("op-a"))
    store.record(_sample_plan("op-b"))
    res = dispatch_recovery_command("/recover", plan_provider=store)
    assert res.ok
    assert "op-a" in res.text
    assert "op-b" in res.text


def test_bare_recover_with_empty_store_is_graceful():
    store = RecoveryPlanStore()
    res = dispatch_recovery_command("/recover", plan_provider=store)
    assert res.ok
    assert "no recent" in res.text.lower()


def test_bare_recover_with_no_provider_is_graceful():
    res = dispatch_recovery_command("/recover")
    assert res.ok
    assert "no plan provider" in res.text.lower() or "no recent" in res.text.lower()


# ===========================================================================
# /recover session <sid>
# ===========================================================================


def _make_browser(tmp_path: Path, session_id: str, stop_reason: str):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / session_id).mkdir()
    (sessions / session_id / "summary.json").write_text(json.dumps({
        "stop_reason": stop_reason,
        "stats": {"ops_total": 1, "ops_applied": 0},
    }))
    bm = tmp_path / "bm"
    bm.mkdir()
    browser = SessionBrowser(
        index=SessionIndex(root=sessions),
        bookmarks=BookmarkStore(bookmark_root=bm),
    )
    browser.index.scan()
    return browser


def test_historical_recover_renders_from_session(tmp_path: Path):
    browser = _make_browser(tmp_path, "bt-hist", STOP_COST_CAP)
    res = dispatch_recovery_command(
        "/recover session bt-hist", session_browser=browser,
    )
    assert res.ok
    assert "bt-hist" in res.text
    assert "cost_cap" in res.text.lower() or "cost cap" in res.text.lower()
    assert "Try next:" in res.text


def test_historical_recover_missing_arg_usage():
    res = dispatch_recovery_command("/recover session")
    assert not res.ok
    assert "/recover session" in res.text


def test_historical_recover_unknown_session(tmp_path: Path):
    browser = _make_browser(tmp_path, "bt-hist", STOP_COST_CAP)
    res = dispatch_recovery_command(
        "/recover session bt-ghost", session_browser=browser,
    )
    assert not res.ok
    assert "unknown session" in res.text.lower()


def test_historical_recover_renders_generic_for_unknown_stop_reason(tmp_path: Path):
    browser = _make_browser(tmp_path, "bt-weird", "some_custom_reason")
    res = dispatch_recovery_command(
        "/recover session bt-weird", session_browser=browser,
    )
    assert res.ok
    # Historical session header + generic plan (no hardcoded stop reason)
    assert "bt-weird" in res.text
    assert "debug.log" in res.text.lower()


# ===========================================================================
# Parse errors
# ===========================================================================


def test_malformed_quoting_is_parse_error():
    res = dispatch_recovery_command("/recover 'unclosed")
    assert not res.ok
    assert "parse" in res.text.lower()


# ===========================================================================
# Result dataclass shape
# ===========================================================================


def test_result_shape():
    r = RecoveryDispatchResult(ok=True, text="hi")
    assert r.matched is True


# ===========================================================================
# End-to-end: store + REPL + announcer
# ===========================================================================


def test_end_to_end_record_recall_and_speak(monkeypatch):
    monkeypatch.setenv("OUROBOROS_NARRATOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_RECOVERY_VOICE_ENABLED", "true")
    store = RecoveryPlanStore()
    spoken = []
    async def _capture(text, voice="Karen"):
        spoken.append(text)
        return True
    announcer = RecoveryAnnouncer(speaker=_capture)

    # Pipeline simulates: op fails → store plan → REPL queries → speak
    plan = _sample_plan("op-e2e")
    store.record(plan)

    res1 = dispatch_recovery_command(
        "/recover op-e2e", plan_provider=store,
    )
    assert res1.ok
    assert "op-e2e" in res1.text

    res2 = dispatch_recovery_command(
        "/recover op-e2e speak",
        plan_provider=store, announcer=announcer,
    )
    assert res2.ok
    assert "queued" in res2.text.lower()
    assert announcer.stats()["queued"] == 1

    # Drain — confirm Karen-safe TTS text fires
    asyncio.new_event_loop().run_until_complete(
        announcer.drain_once_for_test(),
    )
    assert len(spoken) == 1
    assert "First," in spoken[0]
