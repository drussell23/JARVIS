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


def test_single_session_load(monkeypatch, tmp_path):
    _enable(monkeypatch)
    _write_summary(tmp_path, "bt-2026-04-15-230849", duration_s=1034.0)
    summary = lss.LastSessionSummary(tmp_path)
    records = summary.load()
    assert len(records) == 1
    r = records[0]
    assert r.session_id == "bt-2026-04-15-230849"
    assert r.stop_reason == "idle_timeout"
    assert r.duration_s == 1034.0
    assert r.stats_attempted == 1


# ---------------------------------------------------------------------------
# (2) Missing sessions dir → graceful empty
# ---------------------------------------------------------------------------


def test_missing_sessions_dir_graceful(monkeypatch, tmp_path):
    _enable(monkeypatch)
    # No .ouroboros/sessions/ anywhere under tmp_path.
    summary = lss.LastSessionSummary(tmp_path)
    assert summary.load() == []
    assert summary.format_for_prompt() is None


# ---------------------------------------------------------------------------
# (3) Malformed JSON → graceful empty
# ---------------------------------------------------------------------------


def test_malformed_json_graceful(monkeypatch, tmp_path):
    _enable(monkeypatch)
    session_dir = tmp_path / ".ouroboros" / "sessions" / "bt-2026-04-15-123456"
    session_dir.mkdir(parents=True)
    (session_dir / "summary.json").write_text("{not-valid-json")

    summary = lss.LastSessionSummary(tmp_path)
    assert summary.load() == []
    assert summary.stats().malformed_files >= 1


# ---------------------------------------------------------------------------
# (4) Lex-max selection picks the newest session
# ---------------------------------------------------------------------------


def test_lex_max_selection_picks_newest(monkeypatch, tmp_path):
    _enable(monkeypatch)
    _write_summary(tmp_path, "bt-2026-04-10-100000")
    _write_summary(tmp_path, "bt-2026-04-15-230849")
    _write_summary(tmp_path, "bt-2026-04-12-150000")

    summary = lss.LastSessionSummary(tmp_path)
    records = summary.load(n_sessions=1)
    assert len(records) == 1
    assert records[0].session_id == "bt-2026-04-15-230849"


# ---------------------------------------------------------------------------
# (5) Self-skip when lex-max matches active session id
# ---------------------------------------------------------------------------


def test_self_skip_when_lex_max_is_active(monkeypatch, tmp_path):
    _enable(monkeypatch)
    _write_summary(tmp_path, "bt-2026-04-10-100000")
    _write_summary(tmp_path, "bt-2026-04-15-230849")  # lex-max
    _write_summary(tmp_path, "bt-2026-04-12-150000")

    lss.set_active_session_id("bt-2026-04-15-230849")
    summary = lss.LastSessionSummary(tmp_path)
    records = summary.load(n_sessions=1)
    # Self skipped → the previous one (bt-2026-04-12-150000) wins.
    assert len(records) == 1
    assert records[0].session_id == "bt-2026-04-12-150000"


def test_self_skip_only_session_returns_empty(monkeypatch, tmp_path):
    """If the only session on disk is ourselves, return empty (no fake summary)."""
    _enable(monkeypatch)
    _write_summary(tmp_path, "bt-2026-04-15-230849")
    lss.set_active_session_id("bt-2026-04-15-230849")
    summary = lss.LastSessionSummary(tmp_path)
    assert summary.load() == []
    assert summary.format_for_prompt() is None


# ---------------------------------------------------------------------------
# (6) N=1 default; N>3 clamped to 3; N=0 empty
# ---------------------------------------------------------------------------


def test_n_sessions_clamped_to_hard_max(monkeypatch, tmp_path):
    _enable(monkeypatch, N_SESSIONS="10")  # user requests 10 → clamped to 3
    for i in range(5):
        _write_summary(tmp_path, f"bt-2026-04-0{i}-000000")
    summary = lss.LastSessionSummary(tmp_path)
    records = summary.load()
    assert len(records) == 3  # hard max


def test_n_zero_returns_empty(monkeypatch, tmp_path):
    _enable(monkeypatch, N_SESSIONS="0")
    _write_summary(tmp_path, "bt-2026-04-15-230849")
    summary = lss.LastSessionSummary(tmp_path)
    assert summary.load() == []


# ---------------------------------------------------------------------------
# (7) MAX_CHARS cap trims rendered output
# ---------------------------------------------------------------------------


def test_max_chars_cap_trims_output(monkeypatch, tmp_path):
    _enable(monkeypatch, MAX_CHARS="400")
    # Three sessions with realistic content — rendered will exceed 400.
    for i in range(3):
        _write_summary(tmp_path, f"bt-2026-04-0{i+1}-120000")
    monkeypatch.setenv("JARVIS_LAST_SESSION_SUMMARY_N_SESSIONS", "3")

    summary = lss.LastSessionSummary(tmp_path)
    rendered = summary.format_for_prompt()
    assert rendered is not None
    assert len(rendered) <= 400
    assert rendered.endswith("...")


# ---------------------------------------------------------------------------
# (8) Control-char sanitizer on all fields
# ---------------------------------------------------------------------------


def test_sanitizer_strips_control_chars(monkeypatch, tmp_path):
    _enable(monkeypatch)
    _write_summary(
        tmp_path, "bt-2026-04-15-230849",
        stop_reason="idle_timeout\x1b[31m\x00smuggled",
        convergence_state="CONVERGED\n\tinjected",
    )
    summary = lss.LastSessionSummary(tmp_path)
    rendered = summary.format_for_prompt() or ""
    assert "\x1b" not in rendered
    assert "\x00" not in rendered
    assert "\n\t" not in rendered
    # Alphanumeric preserved.
    assert "idle_timeout" in rendered
    assert "CONVERGED" in rendered


# ---------------------------------------------------------------------------
# (9) Secret-shape redaction (via public redact_secrets)
# ---------------------------------------------------------------------------


def test_redaction_via_public_helper(monkeypatch, tmp_path):
    _enable(monkeypatch)
    _write_summary(
        tmp_path, "bt-2026-04-15-230849",
        convergence_state="CONVERGED with token sk-abcdefghij1234567890xyz",
    )
    summary = lss.LastSessionSummary(tmp_path)
    rendered = summary.format_for_prompt() or ""
    assert "sk-abcdefghij1234567890xyz" not in rendered
    assert "[REDACTED:openai-key]" in rendered


# ---------------------------------------------------------------------------
# (10) format_for_prompt None when disabled or empty
# ---------------------------------------------------------------------------


def test_format_for_prompt_none_when_disabled(tmp_path):
    # Env unset — master switch off.
    _write_summary(tmp_path, "bt-2026-04-15-230849")
    summary = lss.LastSessionSummary(tmp_path)
    assert summary.format_for_prompt() is None


def test_format_for_prompt_none_when_prompt_gate_off(monkeypatch, tmp_path):
    _enable(monkeypatch, PROMPT_INJECTION_ENABLED="false")
    _write_summary(tmp_path, "bt-2026-04-15-230849")
    summary = lss.LastSessionSummary(tmp_path)
    # load() still works, format_for_prompt respects sub-gate.
    assert summary.load() != []
    assert summary.format_for_prompt() is None


# ---------------------------------------------------------------------------
# (11) Fenced block + authority-invariant copy present
# ---------------------------------------------------------------------------


def test_fenced_block_and_authority_copy_present(monkeypatch, tmp_path):
    _enable(monkeypatch)
    _write_summary(tmp_path, "bt-2026-04-15-230849")
    summary = lss.LastSessionSummary(tmp_path)
    rendered = summary.format_for_prompt()
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


def test_zero_attempted_ops_appends_deterministic_note(monkeypatch, tmp_path):
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
    rendered = summary.format_for_prompt() or ""
    assert (
        "note: stop_reason=idle_timeout; harness reported zero attempted ops."
        in rendered
    )


def test_nonzero_attempted_ops_no_note(monkeypatch, tmp_path):
    _enable(monkeypatch)
    _write_summary(tmp_path, "bt-2026-04-15-230849")  # default attempted=1
    summary = lss.LastSessionSummary(tmp_path)
    rendered = summary.format_for_prompt() or ""
    assert "zero attempted ops" not in rendered


# ---------------------------------------------------------------------------
# (13) Singleton round-trip + inject_metrics contract
# ---------------------------------------------------------------------------


def test_singleton_round_trip():
    a = lss.get_default_summary()
    b = lss.get_default_summary()
    assert a is b
    lss.reset_default_summary()
    c = lss.get_default_summary()
    assert a is not c


def test_inject_metrics_shape(monkeypatch, tmp_path):
    _enable(monkeypatch)
    _write_summary(tmp_path, "bt-2026-04-15-230849")
    summary = lss.LastSessionSummary(tmp_path)
    enabled, n, sid, chars, hash8 = summary.inject_metrics()
    assert enabled is True
    assert n == 1
    assert sid == "bt-2026-04-15-230849"
    assert chars > 0
    assert len(hash8) == 8
    assert all(c in "0123456789abcdef" for c in hash8)


def test_inject_metrics_disabled_shape(tmp_path):
    _write_summary(tmp_path, "bt-2026-04-15-230849")
    summary = lss.LastSessionSummary(tmp_path)
    enabled, n, sid, chars, hash8 = summary.inject_metrics()
    assert enabled is False
    assert n == 0
    assert sid == ""
    assert chars == 0
    assert hash8 == ""


# ---------------------------------------------------------------------------
# (14) Integration: real OperationContext ordering
# ---------------------------------------------------------------------------


def test_integration_ordering_with_real_op_context(monkeypatch, tmp_path):
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

    lss_section = lss.LastSessionSummary(tmp_path).format_for_prompt()
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
