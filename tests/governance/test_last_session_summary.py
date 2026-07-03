"""LastSessionSummary v0.1 tests — 14 total per plan §12.

Covers the plan's DoD: structured summary.json read-only ingestion,
Tier -1 sanitize + secret redaction, lex-max session selection,
self-skip, N caps, char caps, observability, singleton, and an
integration test that asserts the correct injection ordering relative
to Strategic / Bridge / Semantic / Goals in a real OperationContext.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

import pytest

from backend.core.ouroboros.governance import (
    conversation_bridge as cb,
    last_session_summary as lss,
)
from backend.core.ouroboros.governance.op_context import OperationContext


@pytest.fixture(autouse=True)
def _reset_env_and_singletons(monkeypatch):
    for key in list(os.environ.keys()):
        if key.startswith(("JARVIS_LAST_SESSION_SUMMARY_", "JARVIS_CONVERSATION_BRIDGE_")):
            monkeypatch.delenv(key, raising=False)
    lss.reset_default_summary()
    lss.set_active_session_id(None)
    cb.reset_default_bridge()
    yield
    lss.reset_default_summary()
    lss.set_active_session_id(None)
    cb.reset_default_bridge()


def _enable(monkeypatch, **overrides):
    monkeypatch.setenv("JARVIS_LAST_SESSION_SUMMARY_ENABLED", "true")
    for k, v in overrides.items():
        monkeypatch.setenv(f"JARVIS_LAST_SESSION_SUMMARY_{k}", str(v))


def _write_summary(
    project_root: Path,
    session_id: str,
    **fields: Any,
) -> Path:
    """Create ``.ouroboros/sessions/<id>/summary.json`` with defaults + overrides."""
    session_dir = project_root / ".ouroboros" / "sessions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    payload: Dict[str, Any] = {
        "session_id": session_id,
        "stop_reason": "idle_timeout",
        "duration_s": 300.0,
        "stats": {
            "attempted": 1,
            "completed": 0,
            "failed": 0,
            "cancelled": 0,
            "queued": 0,
        },
        "cost_total": 0.1,
        "cost_breakdown": {"claude": 0.1},
        "branch_stats": {
            "commits": 0, "files_changed": 0,
            "insertions": 0, "deletions": 0,
        },
        "strategic_drift": {"ratio": 0.0, "status": "ok"},
        "convergence_state": "INSUFFICIENT_DATA",
    }
    payload.update(fields)
    path = session_dir / "summary.json"
    path.write_text(json.dumps(payload))
    return path


# ---------------------------------------------------------------------------
# (1) Single session load — happy path
# ---------------------------------------------------------------------------


async def test_single_session_load(monkeypatch, tmp_path):
    _enable(monkeypatch)
    _write_summary(tmp_path, "bt-2026-04-15-230849", duration_s=1034.0)
    summary = lss.LastSessionSummary(tmp_path)
    records = await summary.load()
    assert len(records) == 1
    r = records[0]
    assert r.session_id == "bt-2026-04-15-230849"
    assert r.stop_reason == "idle_timeout"
    assert r.duration_s == 1034.0
    assert r.stats_attempted == 1


# ---------------------------------------------------------------------------
# (2) Missing sessions dir → graceful empty
# ---------------------------------------------------------------------------


async def test_missing_sessions_dir_graceful(monkeypatch, tmp_path):
    _enable(monkeypatch)
    # No .ouroboros/sessions/ anywhere under tmp_path.
    summary = lss.LastSessionSummary(tmp_path)
    assert await summary.load() == []
    assert await summary.format_for_prompt() is None


# ---------------------------------------------------------------------------
# (3) Malformed JSON → graceful empty
# ---------------------------------------------------------------------------


async def test_malformed_json_graceful(monkeypatch, tmp_path):
    _enable(monkeypatch)
    session_dir = tmp_path / ".ouroboros" / "sessions" / "bt-2026-04-15-123456"
    session_dir.mkdir(parents=True)
    (session_dir / "summary.json").write_text("{not-valid-json")

    summary = lss.LastSessionSummary(tmp_path)
    assert await summary.load() == []
    assert summary.stats().malformed_files >= 1


# ---------------------------------------------------------------------------
# (4) Lex-max selection picks the newest session
# ---------------------------------------------------------------------------


async def test_lex_max_selection_picks_newest(monkeypatch, tmp_path):
    _enable(monkeypatch)
    _write_summary(tmp_path, "bt-2026-04-10-100000")
    _write_summary(tmp_path, "bt-2026-04-15-230849")
    _write_summary(tmp_path, "bt-2026-04-12-150000")

    summary = lss.LastSessionSummary(tmp_path)
    records = await summary.load(n_sessions=1)
    assert len(records) == 1
    assert records[0].session_id == "bt-2026-04-15-230849"


# ---------------------------------------------------------------------------
# (5) Self-skip when lex-max matches active session id
# ---------------------------------------------------------------------------


async def test_self_skip_when_lex_max_is_active(monkeypatch, tmp_path):
    _enable(monkeypatch)
    _write_summary(tmp_path, "bt-2026-04-10-100000")
    _write_summary(tmp_path, "bt-2026-04-15-230849")  # lex-max
    _write_summary(tmp_path, "bt-2026-04-12-150000")

    lss.set_active_session_id("bt-2026-04-15-230849")
    summary = lss.LastSessionSummary(tmp_path)
    records = await summary.load(n_sessions=1)
    # Self skipped → the previous one (bt-2026-04-12-150000) wins.
    assert len(records) == 1
    assert records[0].session_id == "bt-2026-04-12-150000"


async def test_self_skip_only_session_returns_empty(monkeypatch, tmp_path):
    """If the only session on disk is ourselves, return empty (no fake summary)."""
    _enable(monkeypatch)
    _write_summary(tmp_path, "bt-2026-04-15-230849")
    lss.set_active_session_id("bt-2026-04-15-230849")
    summary = lss.LastSessionSummary(tmp_path)
    assert await summary.load() == []
    assert await summary.format_for_prompt() is None


async def test_defensive_filter_skips_in_flight_session_without_active_id(monkeypatch, tmp_path):
    """Belt-and-suspenders: even without set_active_session_id, dirs whose
    summary.json doesn't exist yet (in-flight session) are skipped.

    Regression guard for the 2026-04-16 live session where the harness
    hadn't wired the lss.set_active_session_id hook yet — load() now
    auto-skips current-session dirs via summary-exists filter.
    """
    _enable(monkeypatch)
    # Create an "in-flight" session dir without summary.json.
    in_flight = tmp_path / ".ouroboros" / "sessions" / "bt-2026-04-16-100000"
    in_flight.mkdir(parents=True)
    # And a completed prior session.
    _write_summary(tmp_path, "bt-2026-04-15-090000")

    # Do NOT set_active_session_id — prove the filter catches it anyway.
    summary = lss.LastSessionSummary(tmp_path)
    records = await summary.load(n_sessions=1)
    assert len(records) == 1
    assert records[0].session_id == "bt-2026-04-15-090000"


# ---------------------------------------------------------------------------
# (6) N=1 default; N>3 clamped to 3; N=0 empty
# ---------------------------------------------------------------------------


async def test_n_sessions_clamped_to_hard_max(monkeypatch, tmp_path):
    _enable(monkeypatch, N_SESSIONS="10")  # user requests 10 → clamped to 3
    for i in range(5):
        _write_summary(tmp_path, f"bt-2026-04-0{i}-000000")
    summary = lss.LastSessionSummary(tmp_path)
    records = await summary.load()
    assert len(records) == 3  # hard max


async def test_n_zero_returns_empty(monkeypatch, tmp_path):
    _enable(monkeypatch, N_SESSIONS="0")
    _write_summary(tmp_path, "bt-2026-04-15-230849")
    summary = lss.LastSessionSummary(tmp_path)
    assert await summary.load() == []


# ---------------------------------------------------------------------------
# (7) MAX_CHARS cap trims rendered output
# ---------------------------------------------------------------------------


async def test_max_chars_cap_trims_output(monkeypatch, tmp_path):
    _enable(monkeypatch, MAX_CHARS="400")
    # Three sessions with realistic content — rendered will exceed 400.
    for i in range(3):
        _write_summary(tmp_path, f"bt-2026-04-0{i+1}-120000")
    monkeypatch.setenv("JARVIS_LAST_SESSION_SUMMARY_N_SESSIONS", "3")

    summary = lss.LastSessionSummary(tmp_path)
    rendered = await summary.format_for_prompt()
    assert rendered is not None
    assert len(rendered) <= 400
    assert rendered.endswith("...")


# ---------------------------------------------------------------------------
# (8) Control-char sanitizer on all fields
# ---------------------------------------------------------------------------


async def test_sanitizer_strips_control_chars(monkeypatch, tmp_path):
    _enable(monkeypatch)
    _write_summary(
        tmp_path, "bt-2026-04-15-230849",
        stop_reason="idle_timeout\x1b[31m\x00smuggled",
        convergence_state="CONVERGED\n\tinjected",
    )
    summary = lss.LastSessionSummary(tmp_path)
    rendered = await summary.format_for_prompt() or ""
    assert "\x1b" not in rendered
    assert "\x00" not in rendered
    assert "\n\t" not in rendered
    # Alphanumeric preserved.
    assert "idle_timeout" in rendered
    assert "CONVERGED" in rendered


# ---------------------------------------------------------------------------
# (9) Secret-shape redaction (via public redact_secrets)
# ---------------------------------------------------------------------------


async def test_redaction_via_public_helper(monkeypatch, tmp_path):
    _enable(monkeypatch)
    _write_summary(
        tmp_path, "bt-2026-04-15-230849",
        convergence_state="CONVERGED with token sk-abcdefghij1234567890xyz",
    )
    summary = lss.LastSessionSummary(tmp_path)
    rendered = await summary.format_for_prompt() or ""
    assert "sk-abcdefghij1234567890xyz" not in rendered
    assert "[REDACTED:openai-key]" in rendered


# ---------------------------------------------------------------------------
# (10) format_for_prompt None when disabled or empty
# ---------------------------------------------------------------------------


async def test_format_for_prompt_none_when_disabled(tmp_path):
    # Env unset — master switch off.
    _write_summary(tmp_path, "bt-2026-04-15-230849")
    summary = lss.LastSessionSummary(tmp_path)
    assert await summary.format_for_prompt() is None


async def test_format_for_prompt_none_when_prompt_gate_off(monkeypatch, tmp_path):
    _enable(monkeypatch, PROMPT_INJECTION_ENABLED="false")
    _write_summary(tmp_path, "bt-2026-04-15-230849")
    summary = lss.LastSessionSummary(tmp_path)
    # load() still works, format_for_prompt respects sub-gate.
    assert await summary.load() != []
    assert await summary.format_for_prompt() is None


# ---------------------------------------------------------------------------
# (11) Fenced block + authority-invariant copy present
# ---------------------------------------------------------------------------


async def test_fenced_block_and_authority_copy_present(monkeypatch, tmp_path):
    _enable(monkeypatch)
    _write_summary(tmp_path, "bt-2026-04-15-230849")
    summary = lss.LastSessionSummary(tmp_path)
    rendered = await summary.format_for_prompt()
    assert rendered is not None
    assert "## Previous Session Closure (untrusted episodic context)" in rendered
    assert '<previous_sessions untrusted="true">' in rendered
    assert "</previous_sessions>" in rendered
    assert "no authority" in rendered.lower()
    assert "FORBIDDEN_PATH" in rendered
    # Dense one-liner format (§15.1) — session_id + stop= present on one line.
    assert "bt-2026-04-15-230849 stop=idle_timeout" in rendered


# ---------------------------------------------------------------------------
# (12) §15.2 deterministic zero-op note
# ---------------------------------------------------------------------------


async def test_zero_attempted_ops_appends_deterministic_note(monkeypatch, tmp_path):
    _enable(monkeypatch)
    _write_summary(
        tmp_path, "bt-2026-04-15-230849",
        stop_reason="idle_timeout",
        stats={
            "attempted": 0, "completed": 0, "failed": 0,
            "cancelled": 0, "queued": 0,
        },
        cost_total=0.0,
        cost_breakdown={},
    )
    summary = lss.LastSessionSummary(tmp_path)
    rendered = await summary.format_for_prompt() or ""
    assert (
        "note: stop_reason=idle_timeout; harness reported zero attempted ops."
        in rendered
    )


async def test_nonzero_attempted_ops_no_note(monkeypatch, tmp_path):
    _enable(monkeypatch)
    _write_summary(tmp_path, "bt-2026-04-15-230849")  # default attempted=1
    summary = lss.LastSessionSummary(tmp_path)
    rendered = await summary.format_for_prompt() or ""
    assert "zero attempted ops" not in rendered


# ---------------------------------------------------------------------------
# (13) Singleton round-trip + inject_metrics contract
# ---------------------------------------------------------------------------


async def test_singleton_round_trip():
    a = lss.get_default_summary()
    b = lss.get_default_summary()
    assert a is b
    lss.reset_default_summary()
    c = lss.get_default_summary()
    assert a is not c


async def test_inject_metrics_shape(monkeypatch, tmp_path):
    _enable(monkeypatch)
    _write_summary(tmp_path, "bt-2026-04-15-230849")
    summary = lss.LastSessionSummary(tmp_path)
    enabled, n, sid, chars, hash8 = await summary.inject_metrics()
    assert enabled is True
    assert n == 1
    assert sid == "bt-2026-04-15-230849"
    assert chars > 0
    assert len(hash8) == 8
    assert all(c in "0123456789abcdef" for c in hash8)


async def test_inject_metrics_disabled_shape(tmp_path):
    _write_summary(tmp_path, "bt-2026-04-15-230849")
    summary = lss.LastSessionSummary(tmp_path)
    enabled, n, sid, chars, hash8 = await summary.inject_metrics()
    assert enabled is False
    assert n == 0
    assert sid == ""
    assert chars == 0
    assert hash8 == ""


# ---------------------------------------------------------------------------
# (14) Integration: real OperationContext ordering
# ---------------------------------------------------------------------------


async def test_integration_ordering_with_real_op_context(monkeypatch, tmp_path):
    """Mirror of bridge integration test — prove injection ordering.

    Composes all five prompt sources through the same ``with_strategic_memory_context``
    builder the orchestrator uses, and asserts: Strategic → Bridge →
    Semantic → LastSession → Goals → UserPrefs.
    """
    _enable(monkeypatch)
    _write_summary(tmp_path, "bt-2026-04-15-230849")

    ctx = OperationContext.create(
        target_files=("foo.py",), description="integration test",
    )

    def _apply(ctx, intent_id, section):
        existing = ctx.strategic_memory_prompt or ""
        new = (existing + "\n\n" + section) if existing else section
        return ctx.with_strategic_memory_context(
            strategic_intent_id=ctx.strategic_intent_id or intent_id,
            strategic_memory_fact_ids=ctx.strategic_memory_fact_ids,
            strategic_memory_prompt=new,
            strategic_memory_digest=ctx.strategic_memory_digest,
        )

    ctx = _apply(ctx, "manifesto", "## Strategic Direction (Manifesto v4)\n\nCore principles go here")
    ctx = _apply(ctx, "bridge", '<conversation untrusted="true">[tui_user] hi</conversation>')
    ctx = _apply(ctx, "semantic", "## Recent Focus (semantic — untrusted prior)\n\nstuff")

    lss_section = await lss.LastSessionSummary(tmp_path).format_for_prompt()
    assert lss_section is not None
    ctx = _apply(ctx, "last-session", lss_section)

    ctx = _apply(ctx, "goals", "## Active Goals (user-defined priorities)\n- **g**: x")
    ctx = _apply(ctx, "prefs", "## User Preferences (persistent memory)\n- FORBIDDEN_PATH: .env")

    prompt = ctx.strategic_memory_prompt
    strat_idx = prompt.index("Strategic Direction (Manifesto")
    bridge_idx = prompt.index('<conversation untrusted="true">')
    semi_idx = prompt.index("Recent Focus (semantic")
    lss_idx = prompt.index("Previous Session Closure")
    goals_idx = prompt.index("## Active Goals")
    prefs_idx = prompt.index("## User Preferences")

    assert strat_idx < bridge_idx < semi_idx < lss_idx < goals_idx < prefs_idx, (
        f"ordering violated: strat={strat_idx} bridge={bridge_idx} "
        f"semi={semi_idx} lss={lss_idx} goals={goals_idx} prefs={prefs_idx}"
    )
    # LastSessionSummary content present.
    assert "bt-2026-04-15-230849" in prompt


# ---------------------------------------------------------------------------
# fs-hot-tier Batch 3 (row 18, 2026-07-03) — LastSessionSummary
# ``_lex_max_session_dirs`` routes the ``.ouroboros/sessions`` iterdir
# scan through cooperative_fs_io.offload(cpu_bound=False) instead of
# running the scan synchronously on the loop thread inside ``load()``.
# ---------------------------------------------------------------------------


class TestLexMaxSessionDirsOffload:
    async def test_routes_through_offload_thread_pool(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        _write_summary(tmp_path, "bt-2026-04-15-230849")
        summary = lss.LastSessionSummary(tmp_path)

        from backend.core.ouroboros.governance import cooperative_fs_io
        calls = {"n": 0, "cpu_bound": None}
        real_offload = cooperative_fs_io.offload

        async def _spy_offload(fn, *args, cpu_bound=False, **kwargs):
            calls["n"] += 1
            calls["cpu_bound"] = cpu_bound
            return await real_offload(fn, *args, cpu_bound=cpu_bound, **kwargs)

        monkeypatch.setattr(cooperative_fs_io, "offload", _spy_offload)
        records = await summary.load()

        assert calls["n"] == 1, "_lex_max_session_dirs must route through offload"
        assert calls["cpu_bound"] is False, (
            "single-level iterdir over a small sessions dir is IO-bound "
            "— must use the thread pool, not a process pool"
        )
        assert len(records) == 1

    async def test_parity_with_direct_iterdir_scan(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        _write_summary(tmp_path, "bt-2026-04-10-100000")
        _write_summary(tmp_path, "bt-2026-04-15-230849")
        summary = lss.LastSessionSummary(tmp_path)
        dirs = await summary._lex_max_session_dirs(10)
        sessions_root = tmp_path / ".ouroboros" / "sessions"
        expected = sorted(
            (p for p in sessions_root.iterdir()
             if p.is_dir() and p.name.startswith("bt-")),
            key=lambda p: p.name, reverse=True,
        )
        assert [d.name for d in dirs] == [p.name for p in expected]

    async def test_offload_error_degrades_to_empty_no_raise(self, monkeypatch, tmp_path):
        from backend.core.ouroboros.governance import cooperative_fs_io
        from backend.core.ouroboros.governance.cooperative_fs_io import (
            OffloadError,
        )
        _enable(monkeypatch)
        _write_summary(tmp_path, "bt-2026-04-15-230849")
        summary = lss.LastSessionSummary(tmp_path)

        async def _boom_offload(fn, *args, **kwargs):
            return OffloadError(
                fn_name="_lex_max_session_dirs_worker",
                exc_type="OSError",
                message="synthetic offload-layer fault",
                cpu_bound=False,
            )

        monkeypatch.setattr(cooperative_fs_io, "offload", _boom_offload)
        assert await summary.load() == []
        assert await summary.format_for_prompt() is None


class TestLoadSyncBridge:
    """load_sync()/format_for_prompt_sync() — the non-async bridge for
    legacy consumers outside the audited CONTEXT_EXPANSION hot path
    (cross_session_harness, cross_session_coherence_rig, session_story,
    graduation/cross_session_coherence)."""

    def test_load_sync_matches_async_load(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        _write_summary(tmp_path, "bt-2026-04-15-230849")
        summary = lss.LastSessionSummary(tmp_path)
        records = summary.load_sync()
        assert len(records) == 1
        assert records[0].session_id == "bt-2026-04-15-230849"

    def test_format_for_prompt_sync_matches_async(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        _write_summary(tmp_path, "bt-2026-04-15-230849")
        summary = lss.LastSessionSummary(tmp_path)
        rendered = summary.format_for_prompt_sync()
        assert rendered is not None
        assert "bt-2026-04-15-230849" in rendered

    async def test_load_sync_from_inside_running_loop_degrades_no_raise(
        self, monkeypatch, tmp_path,
    ):
        """Calling the sync bridge from within an already-running loop
        must never raise (asyncio.run() would) — degrades to []."""
        _enable(monkeypatch)
        _write_summary(tmp_path, "bt-2026-04-15-230849")
        summary = lss.LastSessionSummary(tmp_path)
        result = summary.load_sync()
        assert result == []
