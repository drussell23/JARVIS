"""Session replay viewer tests — HTML generation from synthetic artifacts.

Scope axes:

  1. debug.log parser — regex line extraction, category detection, op_id
    extraction, key=value field harvesting, malformed-line tolerance.
  2. ReplayData collection — summary.json / cost_tracker.json / ledger
    file merging; missing-artifact graceful degradation.
  3. HTML rendering — every section degrades cleanly when its source
    data is absent; output is self-contained (no external refs); HTML
    escaping prevents injection via log content.
  4. End-to-end — synthesize a session dir, build replay.html,
    assert its contents.
  5. AST canary — harness wires SessionReplayBuilder in _generate_report.
"""
from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path
from typing import Iterator

import pytest

from backend.core.ouroboros.battle_test.session_replay import (
    ReplayData,
    ReplayEvent,
    SessionReplayBuilder,
    parse_debug_log,
    parse_ledger_for_session,
    replay_enabled,
    _render_html,
    _render_minimal_fallback,
)


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    for key in list(os.environ.keys()):
        if key.startswith("JARVIS_SESSION_REPLAY_"):
            monkeypatch.delenv(key, raising=False)
    yield


# ---------------------------------------------------------------------------
# (1) Env gate
# ---------------------------------------------------------------------------


def test_replay_enabled_default_on():
    """High-value low-cost feature — default ON honors the §8 claim."""
    assert replay_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "off", "no"])
def test_replay_disabled_values(monkeypatch, val):
    monkeypatch.setenv("JARVIS_SESSION_REPLAY_ENABLED", val)
    assert replay_enabled() is False


# ---------------------------------------------------------------------------
# (2) debug.log parser
# ---------------------------------------------------------------------------


def test_parse_debug_log_missing_file(tmp_path):
    assert parse_debug_log(tmp_path / "absent.log") == []


def test_parse_debug_log_extracts_structured_lines(tmp_path):
    log = tmp_path / "debug.log"
    log.write_text(textwrap.dedent("""\
        2026-04-17T03:15:22 [Ouroboros.Orchestrator] INFO [SemanticGuard] op=op-019d1234-abcdef-cau findings=3 hard=1 soft=2 patterns=removed_import
        2026-04-17T03:15:30 [Ouroboros.GoalInference] INFO [GoalInference] built_at=1234 build_ms=42 samples=180
        2026-04-17T03:16:01 [Ouroboros.Harness] INFO [Harness] StatusLineBuilder registered
        not a valid line
        2026-04-17T03:16:02 [x] WARNING something happened
    """))
    events = parse_debug_log(log)
    assert len(events) == 4
    assert events[0].category == "guardian"
    assert events[0].op_id == "op-019d1234-abcdef-cau"
    assert events[0].fields["findings"] == "3"
    assert events[1].category == "inference"
    assert events[1].fields["build_ms"] == "42"
    assert events[2].category == "harness"
    assert events[3].level == "WARNING"


def test_parse_debug_log_tolerates_malformed_lines(tmp_path):
    """Any line that doesn't match the logger format is silently dropped
    — we get whatever's parseable, nothing crashes."""
    log = tmp_path / "debug.log"
    log.write_text(
        "garbage line one\n"
        "2026-04-17T03:15:22 [X] INFO valid\n"
        "\x00\x01\x02 binary garbage\n"
    )
    events = parse_debug_log(log)
    assert len(events) == 1
    assert events[0].level == "INFO"


def test_parse_debug_log_categorizes_every_prefix(tmp_path):
    """Every known structured prefix gets its own category."""
    log_lines = [
        ("[SemanticGuard] op=x", "guardian"),
        ("[GoalInference] built", "inference"),
        ("[StreamRender] op=x tokens=10", "stream"),
        ("[LastSessionSummary] op=x", "lss"),
        ("[Orchestrator] phase", "orchestrator"),
        ("[Plugins] loaded=1", "plugins"),
        ("[Resume] orphans available", "resume"),
        ("[ClassifyClarify] op=x", "clarify"),
        ("some plain uncategorized line", "other"),
    ]
    log = tmp_path / "debug.log"
    log.write_text("\n".join(
        f"2026-04-17T03:15:22 [Logger] INFO {msg}"
        for msg, _ in log_lines
    ))
    events = parse_debug_log(log)
    assert len(events) == len(log_lines)
    for ev, (_, expected_cat) in zip(events, log_lines):
        assert ev.category == expected_cat


# ---------------------------------------------------------------------------
# (3) Ledger correlation
# ---------------------------------------------------------------------------


def test_parse_ledger_for_session_filters_to_seen_ops(tmp_path):
    """Only ledger files whose op_id appears in the session's debug.log
    events are correlated. Prevents pollution from old unrelated ops."""
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    # Op A: in this session.
    (ledger_dir / "op-aaa-cau.jsonl").write_text("\n".join([
        json.dumps({"op_id": "op-aaa-cau", "state": "planned",
                    "data": {"goal": "seen op"}, "wall_time": 100.0}),
        json.dumps({"op_id": "op-aaa-cau", "state": "applied",
                    "data": {"commit_hash": "abc123"}, "wall_time": 110.0}),
    ]))
    # Op B: from a different session.
    (ledger_dir / "op-bbb-cau.jsonl").write_text(
        json.dumps({"op_id": "op-bbb-cau", "state": "applied",
                    "data": {}, "wall_time": 200.0}),
    )

    session_events = [
        ReplayEvent(
            timestamp="2026-04-17T00:00:00",
            logger="x", level="INFO", category="other",
            op_id="op-aaa-cau", message="saw op A",
        ),
    ]
    ops = parse_ledger_for_session(ledger_dir, session_events)
    assert len(ops) == 1
    assert ops[0].op_id == "op-aaa-cau"
    assert ops[0].goal == "seen op"
    assert ops[0].commit_hash == "abc123"
    assert ops[0].final_state == "applied"


def test_parse_ledger_tolerates_malformed_jsonl(tmp_path):
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    (ledger_dir / "op-broken-cau.jsonl").write_text(
        "{corrupted not json\n"
        + json.dumps({"op_id": "op-broken-cau", "state": "applied",
                      "data": {}, "wall_time": 100.0})
    )
    session_events = [
        ReplayEvent(
            timestamp="x", logger="x", level="INFO", category="other",
            op_id="op-broken-cau", message="",
        ),
    ]
    ops = parse_ledger_for_session(ledger_dir, session_events)
    assert len(ops) == 1
    assert ops[0].final_state == "applied"


def test_parse_ledger_missing_dir_returns_empty(tmp_path):
    events = [ReplayEvent(timestamp="", logger="", level="", category="",
                          op_id="op-x-cau", message="")]
    assert parse_ledger_for_session(tmp_path / "no_such_dir", events) == []


# ---------------------------------------------------------------------------
# (4) HTML rendering
# ---------------------------------------------------------------------------


def _mk_data(**kw) -> ReplayData:
    base = {
        "session_id": "bt-2026-04-17-030000",
        "stop_reason": "idle_timeout",
        "duration_s": 600.5,
        "started_at_iso": "2026-04-17T03:00:00",
        "summary": {
            "session_id": "bt-2026-04-17-030000",
            "stop_reason": "idle_timeout",
            "cost_total": 0.2345,
            "stats": {"attempted": 5, "completed": 3, "failed": 2},
            "cost_breakdown": {"claude": 0.22, "doubleword": 0.01},
            "branch_stats": {"commits": 3},
        },
    }
    base.update(kw)
    return ReplayData(**base)


def test_render_html_contains_session_id_and_stop_reason():
    data = _mk_data()
    out = _render_html(data)
    assert "bt-2026-04-17-030000" in out
    assert "idle_timeout" in out
    assert "0.2345" in out


def test_render_html_empty_ops_degrades_cleanly():
    data = _mk_data()
    out = _render_html(data)
    # No ops table when there are no ops — but header + overview render.
    assert "Overview" in out
    assert "Ops</h2>" not in out


def test_render_html_with_ops_renders_table():
    from backend.core.ouroboros.battle_test.session_replay import ReplayOp
    ops = [
        ReplayOp(
            op_id="op-abc-cau",
            short_op_id="abc",
            phases=("planned", "sandboxing", "validating", "applied"),
            final_state="applied",
            target_files=("src/foo.py",),
            goal="demo goal",
            risk_tier="SAFE_AUTO",
            commit_hash="0123456789abcdef",
        ),
    ]
    data = _mk_data(ops=ops)
    out = _render_html(data)
    assert "<h2>Ops</h2>" in out
    assert "SAFE_AUTO" in out
    assert "demo goal" in out
    # Commit truncated to 10 chars in the display.
    assert "0123456789" in out
    # Phase trail collapsible present.
    assert "Op phase trails" in out


def test_render_html_escapes_hostile_event_content():
    """Log lines can contain anything — output must be HTML-safe."""
    events = [
        ReplayEvent(
            timestamp="2026-04-17T03:15:22",
            logger="x", level="INFO", category="other",
            op_id="", message="<script>alert('xss')</script>",
        ),
    ]
    data = _mk_data(events=events)
    out = _render_html(data)
    # Script tag must be escaped, not rendered.
    assert "<script>alert" not in out
    assert "&lt;script&gt;" in out


def test_render_html_caps_very_long_event_lists():
    """Sessions with 10k events shouldn't produce 10MB HTML. V1 caps
    inline events at 2000 with a "N more elided" note."""
    events = [
        ReplayEvent(
            timestamp=f"2026-04-17T03:{i // 60:02d}:{i % 60:02d}",
            logger="x", level="INFO", category="other",
            op_id="", message=f"event {i}",
        )
        for i in range(2500)
    ]
    data = _mk_data(events=events)
    out = _render_html(data)
    assert "500 more events elided" in out or "more events elided" in out


def test_render_html_filter_checkboxes_per_category():
    events = [
        ReplayEvent(timestamp="t1", logger="x", level="INFO",
                    category="guardian", op_id="", message=""),
        ReplayEvent(timestamp="t2", logger="x", level="INFO",
                    category="inference", op_id="", message=""),
    ]
    data = _mk_data(
        events=events,
        category_counts={"guardian": 1, "inference": 1},
    )
    out = _render_html(data)
    # One checkbox per category with its count badge.
    assert 'value="guardian"' in out
    assert 'value="inference"' in out


def test_render_html_guardian_findings_table():
    events = [
        ReplayEvent(
            timestamp="t1", logger="x", level="INFO",
            category="guardian", op_id="op-x-cau",
            message="[SemanticGuard] op=op-x-cau findings=2 hard=1 soft=1 risk_before=SAFE_AUTO risk_after=NOTIFY_APPLY",
            fields={
                "findings": "2", "hard": "1", "soft": "1",
                "risk_before": "SAFE_AUTO", "risk_after": "NOTIFY_APPLY",
            },
        ),
    ]
    data = _mk_data(events=events, guardian_findings_count=1)
    out = _render_html(data)
    assert "SemanticGuardian findings" in out
    assert "SAFE_AUTO → NOTIFY_APPLY" in out


def test_render_html_is_self_contained():
    """No external network refs — operators can open offline."""
    data = _mk_data()
    out = _render_html(data)
    assert "<link" not in out                   # no external stylesheets
    assert "cdn." not in out                    # no CDN includes
    assert "https://" not in out
    assert "http://" not in out


def test_render_html_includes_inline_css_and_js():
    """Minimal interactive surface is inline so the HTML stays self-
    contained. Search box + filter checkboxes wired through JS."""
    data = _mk_data()
    out = _render_html(data)
    assert "<style>" in out
    assert "<script>" in out
    # Key interactive IDs reachable by the JS.
    assert 'id="search"' in out
    assert 'id="timeline"' in out


def test_render_minimal_fallback_always_produces_valid_html():
    """Last-resort fallback used when the main renderer raises."""
    data = _mk_data()
    out = _render_minimal_fallback(data)
    assert out.startswith("<!doctype html>")
    assert "</html>" in out
    assert "bt-2026-04-17-030000" in out


# ---------------------------------------------------------------------------
# (5) End-to-end — builder against a synthetic session dir
# ---------------------------------------------------------------------------


def test_builder_writes_replay_html_with_real_artifacts(tmp_path):
    """Full pipeline: stage a realistic session dir + ledger, run the
    builder, assert replay.html was written and contains the expected
    sections."""
    session_dir = tmp_path / ".ouroboros" / "sessions" / "bt-2026-04-17-030000"
    session_dir.mkdir(parents=True)

    # debug.log
    (session_dir / "debug.log").write_text(textwrap.dedent("""\
        2026-04-17T03:00:00 [Ouroboros.Harness] INFO [Harness] StatusLineBuilder registered
        2026-04-17T03:05:00 [Ouroboros.Orchestrator] INFO [SemanticGuard] op=op-019dabc-cau findings=1 hard=1 soft=0 risk_before=SAFE_AUTO risk_after=APPROVAL_REQUIRED patterns=credential_shape_introduced
        2026-04-17T03:06:00 [Ouroboros.GoalInference] INFO [GoalInference] built_at=1234 build_ms=12 samples=42 sources=4 hypotheses=3 top_conf=0.72
        2026-04-17T03:10:00 [Ouroboros.StreamRenderer] INFO [StreamRender] op=op-019dabc-cau provider=claude tokens=111 dropped=0 first_token_ms=8677 total_ms=51497 tps=2.2
    """))

    # summary.json
    (session_dir / "summary.json").write_text(json.dumps({
        "session_id": "bt-2026-04-17-030000",
        "stop_reason": "idle_timeout",
        "duration_s": 620.0,
        "cost_total": 0.1234,
        "cost_breakdown": {"claude": 0.12, "doubleword": 0.003},
        "stats": {"attempted": 2, "completed": 1, "failed": 0},
        "branch_stats": {"commits": 1, "files_changed": 3,
                         "insertions": 45, "deletions": 12},
    }))

    # cost_tracker.json
    (session_dir / "cost_tracker.json").write_text(json.dumps({
        "total_spent": 0.1234,
        "breakdown": {"claude": 0.12},
    }))

    # Ledger for the op we referenced in debug.log. The builder walks
    # up from session_dir to find the repo root — we create a .git
    # placeholder so it finds tmp_path as the "repo".
    (tmp_path / ".git").mkdir()
    ledger_dir = tmp_path / ".ouroboros" / "state" / "ouroboros" / "ledger"
    ledger_dir.mkdir(parents=True)
    (ledger_dir / "op-019dabc-cau.jsonl").write_text("\n".join([
        json.dumps({
            "op_id": "op-019dabc-cau", "state": "planned",
            "data": {"goal": "demo e2e goal",
                     "target_files": ["src/demo.py"],
                     "risk_tier": "APPROVAL_REQUIRED"},
            "wall_time": 100.0,
        }),
        json.dumps({
            "op_id": "op-019dabc-cau", "state": "applied",
            "data": {"commit_hash": "deadbeef12345"},
            "wall_time": 150.0,
        }),
    ]))

    # Build.
    result = SessionReplayBuilder(session_dir).build()
    assert result is not None
    assert result.name == "replay.html"
    assert result.is_file()

    content = result.read_text(encoding="utf-8")
    # Header fields present.
    assert "bt-2026-04-17-030000" in content
    assert "idle_timeout" in content
    # Cost breakdown table.
    assert "claude" in content
    assert "$0.12" in content
    # Op from the ledger.
    assert "019dabc" in content
    assert "demo e2e goal" in content
    assert "deadbeef12" in content
    # Guardian finding captured.
    assert "credential_shape_introduced" in content
    # Inference build captured.
    assert "Goal inference trajectory" in content
    # Timeline search box present.
    assert 'id="search"' in content


def test_builder_missing_session_dir_returns_none(tmp_path):
    assert SessionReplayBuilder(tmp_path / "absent").build() is None


def test_builder_respects_env_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_SESSION_REPLAY_ENABLED", "0")
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    (session_dir / "debug.log").write_text("")
    assert SessionReplayBuilder(session_dir).build() is None
    assert not (session_dir / "replay.html").exists()


def test_builder_writes_fallback_on_internal_error(tmp_path, monkeypatch):
    """Simulate a render failure (e.g. the CSS constant vanished).
    Fallback HTML must land so the session stays auditable."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    (session_dir / "debug.log").write_text("")

    import backend.core.ouroboros.battle_test.session_replay as mod

    orig_render = mod._render_html
    def _boom(data):
        raise RuntimeError("renderer exploded")
    monkeypatch.setattr(mod, "_render_html", _boom)

    try:
        result = mod.SessionReplayBuilder(session_dir).build()
    finally:
        monkeypatch.setattr(mod, "_render_html", orig_render)

    assert result is not None
    content = result.read_text()
    assert "minimal fallback" in content.lower()


def test_builder_graceful_when_summary_missing(tmp_path):
    """A session dir with only debug.log (no summary.json / cost_tracker)
    must still produce a usable replay — this is the partial-shutdown
    case."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    (session_dir / "debug.log").write_text(
        "2026-04-17T00:00:00 [X] INFO [Harness] booted\n"
    )
    result = SessionReplayBuilder(session_dir).build()
    assert result is not None
    content = result.read_text()
    assert "Session Replay" in content
    # Stats block shows zeroes / empty where sources were absent —
    # doesn't crash, doesn't omit the scaffolding.
    assert "Overview" in content


# ---------------------------------------------------------------------------
# (6) AST canaries — harness wiring
# ---------------------------------------------------------------------------


def test_harness_wires_session_replay_builder():
    path = (
        Path(__file__).resolve().parent.parent.parent
        / "backend/core/ouroboros/battle_test/harness.py"
    )
    src = path.read_text(encoding="utf-8")
    assert "SessionReplayBuilder" in src
    assert "replay_enabled" in src


def test_replay_module_documents_authority_invariant():
    """The module docstring must declare the read-only boundary —
    viewer NEVER mutates state, calls orchestrator, or touches
    governance. Catches a refactor that accidentally adds write access."""
    path = (
        Path(__file__).resolve().parent.parent.parent
        / "backend/core/ouroboros/battle_test/session_replay.py"
    )
    src = path.read_text(encoding="utf-8")
    assert "read-only" in src.lower()
    assert "never mutates" in src.lower() or "never mutate" in src.lower()
