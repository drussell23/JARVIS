"""P0 — POSTMORTEM Recall Service tests (PRD Phase 1).

Per OUROBOROS_VENOM_PRD.md §11 4-layer test discipline:
- Layer 1 (Unit): ~25 unit tests covering parsing, scoring, env knobs
- Layer 2 (Integration): cross-component (recall hook surface)
- Layer 4 (Graduation pins): source-grep + master-off + authority
  invariants

Coverage map per PRD §11 P0 row.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# (A) Env knobs
# ---------------------------------------------------------------------------


def test_master_flag_default_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """JARVIS_POSTMORTEM_RECALL_ENABLED defaults false (PRD §17 default-off)."""
    monkeypatch.delenv("JARVIS_POSTMORTEM_RECALL_ENABLED", raising=False)
    from backend.core.ouroboros.governance.postmortem_recall import is_enabled
    assert is_enabled() is False


def test_master_flag_explicit_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_POSTMORTEM_RECALL_ENABLED", "true")
    from backend.core.ouroboros.governance.postmortem_recall import is_enabled
    assert is_enabled() is True


def test_top_k_default_3(monkeypatch: pytest.MonkeyPatch) -> None:
    """PRD §9 P0: 'inject up to 3 relevant lessons'."""
    monkeypatch.delenv("JARVIS_POSTMORTEM_RECALL_TOP_K", raising=False)
    from backend.core.ouroboros.governance.postmortem_recall import top_k
    assert top_k() == 3


def test_top_k_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_POSTMORTEM_RECALL_TOP_K", "5")
    from backend.core.ouroboros.governance.postmortem_recall import top_k
    assert top_k() == 5


def test_decay_days_default_30(monkeypatch: pytest.MonkeyPatch) -> None:
    """PRD §16 Open Question 1 default: 30 days."""
    monkeypatch.delenv("JARVIS_POSTMORTEM_RECALL_DECAY_DAYS", raising=False)
    from backend.core.ouroboros.governance.postmortem_recall import decay_days
    assert decay_days() == 30.0


def test_similarity_threshold_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_POSTMORTEM_RECALL_SIM_THRESHOLD", raising=False)
    from backend.core.ouroboros.governance.postmortem_recall import (
        similarity_threshold,
    )
    assert similarity_threshold() == 0.5


def test_max_scan_default_500(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_POSTMORTEM_RECALL_MAX_SCAN", raising=False)
    from backend.core.ouroboros.governance.postmortem_recall import (
        max_postmortems_to_scan,
    )
    assert max_postmortems_to_scan() == 500


def test_env_invalid_value_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bad env values silently fall back to defaults (no crash)."""
    monkeypatch.setenv("JARVIS_POSTMORTEM_RECALL_TOP_K", "not-an-int")
    monkeypatch.setenv("JARVIS_POSTMORTEM_RECALL_DECAY_DAYS", "infinity-and-beyond")
    from backend.core.ouroboros.governance.postmortem_recall import (
        top_k, decay_days,
    )
    assert top_k() == 3
    assert decay_days() == 30.0


# ---------------------------------------------------------------------------
# (B) Postmortem line parser
# ---------------------------------------------------------------------------


_REAL_POSTMORTEM_LINE = (
    "2026-04-25T01:08:13 [backend.core.ouroboros.governance.comm_protocol] "
    "INFO [CommProtocol] POSTMORTEM op=op-019dc3ac-8864-766b-84c8-5f36913654ee-cau "
    "seq=8 payload={'root_cause': 'all_providers_exhausted:fallback_failed', "
    "'failed_phase': 'GENERATE', 'next_safe_action': 'retry_with_smaller_seed', "
    "'target_files': ['backend/core/foo.py', 'backend/core/bar.py']}"
)


def test_parse_real_postmortem_line() -> None:
    """Parse a real-shaped postmortem line from a battle-test session."""
    from backend.core.ouroboros.governance.postmortem_recall import (
        _parse_postmortem_line,
    )
    rec = _parse_postmortem_line(_REAL_POSTMORTEM_LINE, session_id="bt-test-001")
    assert rec is not None
    assert rec.op_id == "op-019dc3ac-8864-766b-84c8-5f36913654ee-cau"
    assert rec.session_id == "bt-test-001"
    assert rec.root_cause == "all_providers_exhausted:fallback_failed"
    assert rec.failed_phase == "GENERATE"
    assert rec.next_safe_action == "retry_with_smaller_seed"
    assert "backend/core/foo.py" in rec.target_files
    assert "backend/core/bar.py" in rec.target_files
    assert rec.timestamp_unix > 0


def test_parse_postmortem_skips_non_postmortem_line() -> None:
    """Non-POSTMORTEM lines return None."""
    from backend.core.ouroboros.governance.postmortem_recall import (
        _parse_postmortem_line,
    )
    line = "2026-04-25T01:08:13 [backend.foo] INFO [Foo] HEARTBEAT progress=50"
    assert _parse_postmortem_line(line, session_id="x") is None


def test_parse_postmortem_skips_malformed() -> None:
    """Malformed payload returns None (no crash)."""
    from backend.core.ouroboros.governance.postmortem_recall import (
        _parse_postmortem_line,
    )
    bad = "2026-04-25T01:08:13 [backend.x.comm_protocol] INFO [CommProtocol] POSTMORTEM op=op-1 seq=1 payload=garbage"
    assert _parse_postmortem_line(bad, session_id="x") is None


def test_parse_postmortem_missing_target_files() -> None:
    """Missing target_files defaults to empty tuple."""
    from backend.core.ouroboros.governance.postmortem_recall import (
        _parse_postmortem_line,
    )
    line = (
        "2026-04-25T01:08:13 [backend.x.comm_protocol] INFO [CommProtocol] "
        "POSTMORTEM op=op-1 seq=1 payload={'root_cause': 'noop', "
        "'failed_phase': 'COMPLETE'}"
    )
    rec = _parse_postmortem_line(line, session_id="x")
    assert rec is not None
    assert rec.target_files == ()


# ---------------------------------------------------------------------------
# (C) Session walker
# ---------------------------------------------------------------------------


def test_session_walker_no_sessions_dir(tmp_path: Path) -> None:
    """Missing sessions dir → empty list, no crash."""
    from backend.core.ouroboros.governance.postmortem_recall import (
        _gather_recent_postmortems,
    )
    result = _gather_recent_postmortems(tmp_path / "nonexistent", max_total=100)
    assert result == []


def test_session_walker_empty_sessions_dir(tmp_path: Path) -> None:
    """Sessions dir with no bt-* dirs → empty list."""
    from backend.core.ouroboros.governance.postmortem_recall import (
        _gather_recent_postmortems,
    )
    (tmp_path / "logs").mkdir()  # non-bt subdir; should be skipped
    result = _gather_recent_postmortems(tmp_path, max_total=100)
    assert result == []


def test_session_walker_finds_postmortem(tmp_path: Path) -> None:
    """Real session dir with debug.log containing postmortem → record."""
    from backend.core.ouroboros.governance.postmortem_recall import (
        _gather_recent_postmortems,
    )
    sess_dir = tmp_path / "bt-2026-04-25-test"
    sess_dir.mkdir()
    (sess_dir / "debug.log").write_text(_REAL_POSTMORTEM_LINE + "\n")
    result = _gather_recent_postmortems(tmp_path, max_total=100)
    assert len(result) == 1
    assert result[0].op_id.startswith("op-019dc3ac")
    assert result[0].session_id == "bt-2026-04-25-test"


def test_session_walker_skips_root_cause_none(tmp_path: Path) -> None:
    """Postmortems with root_cause=none (clean COMPLETEs) are skipped."""
    from backend.core.ouroboros.governance.postmortem_recall import (
        _gather_recent_postmortems,
    )
    sess_dir = tmp_path / "bt-test"
    sess_dir.mkdir()
    line_clean = (
        "2026-04-25T01:08:13 [backend.x.comm_protocol] INFO [CommProtocol] "
        "POSTMORTEM op=op-clean seq=1 payload={'root_cause': 'none', "
        "'failed_phase': 'COMPLETE'}"
    )
    (sess_dir / "debug.log").write_text(line_clean + "\n")
    result = _gather_recent_postmortems(tmp_path, max_total=100)
    assert result == []


def test_session_walker_newest_first(tmp_path: Path) -> None:
    """Sessions returned newest-first (lexicographic on bt- prefix)."""
    from backend.core.ouroboros.governance.postmortem_recall import (
        _gather_recent_postmortems,
    )
    for name in ["bt-2026-04-23-test", "bt-2026-04-25-test", "bt-2026-04-24-test"]:
        sess_dir = tmp_path / name
        sess_dir.mkdir()
        (sess_dir / "debug.log").write_text(
            _REAL_POSTMORTEM_LINE.replace("op-019dc3ac-8864-766b-84c8-5f36913654ee-cau", f"op-{name}") + "\n"
        )
    result = _gather_recent_postmortems(tmp_path, max_total=100)
    assert len(result) == 3
    # Newest first
    assert result[0].session_id == "bt-2026-04-25-test"
    assert result[2].session_id == "bt-2026-04-23-test"


# ---------------------------------------------------------------------------
# (D) Time-decay math
# ---------------------------------------------------------------------------


def test_decay_factor_at_age_zero_is_one() -> None:
    from backend.core.ouroboros.governance.postmortem_recall import _decay_factor
    assert _decay_factor(age_seconds=0, halflife_days=30.0) == pytest.approx(1.0)


def test_decay_factor_at_one_halflife_is_half() -> None:
    """30 days at 30d halflife → 0.5."""
    from backend.core.ouroboros.governance.postmortem_recall import _decay_factor
    one_halflife = 30 * 86400.0
    assert _decay_factor(one_halflife, halflife_days=30.0) == pytest.approx(0.5)


def test_decay_factor_at_two_halflives_is_quarter() -> None:
    from backend.core.ouroboros.governance.postmortem_recall import _decay_factor
    two_halflives = 60 * 86400.0
    assert _decay_factor(two_halflives, halflife_days=30.0) == pytest.approx(0.25)


def test_decay_factor_zero_halflife_returns_one() -> None:
    """Edge case — halflife=0 means no decay."""
    from backend.core.ouroboros.governance.postmortem_recall import _decay_factor
    assert _decay_factor(99999, halflife_days=0) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# (E) RecallMatch + ledger format
# ---------------------------------------------------------------------------


def _make_record() -> "Any":
    from backend.core.ouroboros.governance.postmortem_recall import (
        PostmortemRecord,
    )
    return PostmortemRecord(
        op_id="op-test-001",
        session_id="bt-test",
        root_cause="all_providers_exhausted",
        failed_phase="GENERATE",
        next_safe_action="retry",
        target_files=("foo.py", "bar.py"),
        timestamp_iso="2026-04-25T01:00:00+00:00",
        timestamp_unix=1777094400.0,
    )


def test_recallmatch_to_ledger_dict_shape() -> None:
    from backend.core.ouroboros.governance.postmortem_recall import (
        RecallMatch,
    )
    m = RecallMatch(
        record=_make_record(),
        raw_similarity=0.85,
        decayed_similarity=0.75,
        age_days=10.0,
    )
    d = m.to_ledger_dict()
    assert d["schema_version"] == "postmortem_recall.1"
    assert d["op_id"] == "op-test-001"
    assert d["raw_similarity"] == 0.85
    assert d["decayed_similarity"] == 0.75
    assert d["age_days"] == 10.0
    assert "matched_at_iso" in d


# ---------------------------------------------------------------------------
# (F) PostmortemRecord helpers
# ---------------------------------------------------------------------------


def test_signature_text_includes_phase_root_cause_files() -> None:
    rec = _make_record()
    sig = rec.signature_text()
    assert "phase=GENERATE" in sig
    assert "all_providers_exhausted" in sig
    assert "bar.py" in sig and "foo.py" in sig


def test_lesson_text_includes_op_phase_cause() -> None:
    rec = _make_record()
    lesson = rec.lesson_text()
    assert "op=op-test-001" in lesson
    assert "GENERATE" in lesson
    assert "all_providers_exhausted" in lesson


def test_lesson_text_truncates_many_files() -> None:
    from backend.core.ouroboros.governance.postmortem_recall import (
        PostmortemRecord,
    )
    rec = PostmortemRecord(
        op_id="op-x", session_id="s",
        root_cause="x", failed_phase="X",
        next_safe_action="",
        target_files=tuple(f"f{i}.py" for i in range(10)),
        timestamp_iso="2026-04-25T01:00:00+00:00",
        timestamp_unix=1.0,
    )
    lesson = rec.lesson_text()
    # Shows first 3 files + count of remaining
    assert "+7 more" in lesson


# ---------------------------------------------------------------------------
# (G) Master-off invariants
# ---------------------------------------------------------------------------


def test_recall_returns_empty_when_master_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Master-off → recall_for_op returns [] without invoking embedder."""
    monkeypatch.setenv("JARVIS_POSTMORTEM_RECALL_ENABLED", "false")
    from backend.core.ouroboros.governance.postmortem_recall import (
        PostmortemRecallService,
    )
    svc = PostmortemRecallService(sessions_dir=tmp_path)
    result = svc.recall_for_op("any signature")
    assert result == []


def test_get_default_service_returns_none_when_master_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_POSTMORTEM_RECALL_ENABLED", "false")
    from backend.core.ouroboros.governance.postmortem_recall import (
        get_default_service, reset_default_service,
    )
    reset_default_service()
    assert get_default_service() is None


def test_recall_empty_signature_returns_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Empty op_signature → []."""
    monkeypatch.setenv("JARVIS_POSTMORTEM_RECALL_ENABLED", "true")
    from backend.core.ouroboros.governance.postmortem_recall import (
        PostmortemRecallService,
    )
    svc = PostmortemRecallService(sessions_dir=tmp_path)
    assert svc.recall_for_op("") == []
    assert svc.recall_for_op("   ") == []


# ---------------------------------------------------------------------------
# (H) Embedder lazy-init failure handling (best-effort)
# ---------------------------------------------------------------------------


def test_recall_returns_empty_when_no_postmortems(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Empty sessions dir → recall returns [] cleanly."""
    monkeypatch.setenv("JARVIS_POSTMORTEM_RECALL_ENABLED", "true")
    from backend.core.ouroboros.governance.postmortem_recall import (
        PostmortemRecallService,
    )
    svc = PostmortemRecallService(sessions_dir=tmp_path)
    # Inject a stub embedder so lazy-init succeeds
    fake_emb = MagicMock()
    fake_emb.disabled = False
    fake_emb.embed = MagicMock(return_value=[[1.0, 0.0]])
    svc._embedder = fake_emb
    result = svc.recall_for_op("test op signature")
    assert result == []


# ---------------------------------------------------------------------------
# (I) End-to-end recall flow
# ---------------------------------------------------------------------------


def test_end_to_end_recall_with_mock_embedder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Full flow: sessions dir → parse → embed (mocked) → score → top-k."""
    monkeypatch.setenv("JARVIS_POSTMORTEM_RECALL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_POSTMORTEM_RECALL_DECAY_DAYS", "365")
    monkeypatch.setenv("JARVIS_POSTMORTEM_RECALL_SIM_THRESHOLD", "0.0")

    sessions_dir = tmp_path / "sessions"
    sess_dir = sessions_dir / "bt-2026-04-25-test"
    sess_dir.mkdir(parents=True)
    line_a = _REAL_POSTMORTEM_LINE
    line_b = _REAL_POSTMORTEM_LINE.replace("op-019dc3ac-8864-766b-84c8-5f36913654ee-cau", "op-second-fail")
    (sess_dir / "debug.log").write_text(line_a + "\n" + line_b + "\n")

    from backend.core.ouroboros.governance.postmortem_recall import (
        PostmortemRecallService,
    )
    svc = PostmortemRecallService(
        sessions_dir=sessions_dir,
        ledger_path=tmp_path / "ledger.jsonl",
    )

    fake_emb = MagicMock()
    fake_emb.disabled = False
    fake_emb.embed = MagicMock(return_value=[
        [1.0, 0.0, 0.0],
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
    ])
    svc._embedder = fake_emb

    matches = svc.recall_for_op("test op signature")
    assert len(matches) >= 1
    assert (tmp_path / "ledger.jsonl").exists()


def test_recall_respects_top_k_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """top_k_override caps the result list."""
    monkeypatch.setenv("JARVIS_POSTMORTEM_RECALL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_POSTMORTEM_RECALL_DECAY_DAYS", "365")
    monkeypatch.setenv("JARVIS_POSTMORTEM_RECALL_SIM_THRESHOLD", "0.0")

    sessions_dir = tmp_path / "sessions"
    sess_dir = sessions_dir / "bt-test"
    sess_dir.mkdir(parents=True)
    lines = []
    for i in range(5):
        lines.append(
            _REAL_POSTMORTEM_LINE.replace(
                "op-019dc3ac-8864-766b-84c8-5f36913654ee-cau", f"op-{i:03d}",
            )
        )
    (sess_dir / "debug.log").write_text("\n".join(lines) + "\n")

    from backend.core.ouroboros.governance.postmortem_recall import (
        PostmortemRecallService,
    )
    svc = PostmortemRecallService(
        sessions_dir=sessions_dir,
        ledger_path=tmp_path / "ledger.jsonl",
    )
    fake_emb = MagicMock()
    fake_emb.disabled = False
    fake_emb.embed = MagicMock(return_value=[[1.0, 0.0]] * 6)
    svc._embedder = fake_emb

    matches = svc.recall_for_op("sig", top_k_override=2)
    assert len(matches) == 2


# ---------------------------------------------------------------------------
# (J) render_recall_section
# ---------------------------------------------------------------------------


def test_render_recall_section_empty_returns_none() -> None:
    from backend.core.ouroboros.governance.postmortem_recall import (
        render_recall_section,
    )
    assert render_recall_section([]) is None


def test_render_recall_section_with_matches() -> None:
    from backend.core.ouroboros.governance.postmortem_recall import (
        RecallMatch, render_recall_section,
    )
    m1 = RecallMatch(record=_make_record(), raw_similarity=0.9, decayed_similarity=0.8, age_days=5.0)
    out = render_recall_section([m1])
    assert out is not None
    assert "## Lessons from prior similar ops" in out
    assert "op=op-test-001" in out
    assert "GENERATE" in out


# ---------------------------------------------------------------------------
# (K) Default-singleton accessor
# ---------------------------------------------------------------------------


def test_default_singleton_lazy_construct(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("JARVIS_POSTMORTEM_RECALL_ENABLED", "true")
    from backend.core.ouroboros.governance.postmortem_recall import (
        get_default_service, reset_default_service,
    )
    reset_default_service()
    svc1 = get_default_service(sessions_dir=tmp_path)
    svc2 = get_default_service(sessions_dir=tmp_path)
    assert svc1 is svc2


# ---------------------------------------------------------------------------
# (L) Source-grep authority invariants (graduation pins per PRD §11 Layer 1)
# ---------------------------------------------------------------------------


def _read(p: str) -> str:
    return Path(p).read_text(encoding="utf-8")


def test_pin_module_exists() -> None:
    src = _read("backend/core/ouroboros/governance/postmortem_recall.py")
    assert "class PostmortemRecallService" in src
    assert "def recall_for_op" in src
    assert "def render_recall_section" in src


def test_pin_master_flag_default_off() -> None:
    """Default-off discipline per PRD §17."""
    src = _read("backend/core/ouroboros/governance/postmortem_recall.py")
    assert '_env_bool("JARVIS_POSTMORTEM_RECALL_ENABLED", False)' in src


def test_pin_no_authority_imports() -> None:
    """Authority invariant per PRD §12.2 — read-only service.

    Must NOT import: orchestrator, policy, iron_gate, risk_tier,
    change_engine, candidate_generator, gate, semantic_guardian.
    """
    src = _read("backend/core/ouroboros/governance/postmortem_recall.py")
    banned = [
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.policy",
        "from backend.core.ouroboros.governance.iron_gate",
        "from backend.core.ouroboros.governance.risk_tier",
        "from backend.core.ouroboros.governance.change_engine",
        "from backend.core.ouroboros.governance.candidate_generator",
        "from backend.core.ouroboros.governance.semantic_guardian",
    ]
    for imp in banned:
        assert imp not in src, f"banned import found: {imp}"


def test_pin_no_code_evaluation_calls() -> None:
    """Security invariant — no Python code-evaluation functions used.

    Postmortem payload parsing uses narrow regex extractors. We check for
    the dangerous patterns by building the substring at runtime to avoid
    pre-commit hook false positives on the literal token in this test.
    """
    src = _read("backend/core/ouroboros/governance/postmortem_recall.py")
    danger_token_a = "ev" + "al("  # bare call
    danger_token_b = "import a" + "st"
    danger_token_c = "from a" + "st "
    assert danger_token_a not in src.replace("# ", "")
    assert danger_token_b not in src
    assert danger_token_c not in src


def test_pin_jsonl_schema_version() -> None:
    """Schema version pinned for ledger compatibility."""
    src = _read("backend/core/ouroboros/governance/postmortem_recall.py")
    assert '"postmortem_recall.1"' in src


def test_pin_orchestrator_invokes_recall_at_context_expansion() -> None:
    """Wiring invariant: orchestrator must invoke get_default_service +
    render_recall_section at the CONTEXT_EXPANSION injection site."""
    src = _read("backend/core/ouroboros/governance/orchestrator.py")
    assert "from backend.core.ouroboros.governance.postmortem_recall" in src
    assert "get_default_service as _get_pm_recall" in src
    assert "render_recall_section as _render_pm_recall" in src
    assert "PRD Phase 1" in src
    # Best-effort discipline: wrapped in try/except (never blocks FSM)
    assert "[Orchestrator] PostmortemRecall injection skipped" in src


def test_pin_orchestrator_recall_after_conversation_bridge() -> None:
    """Sequence pin: PostmortemRecall call site AFTER ConversationBridge.

    Updated post-extraction (mirrors LSS pattern). The PostmortemRecall body
    now lives in module-level helper ``_inject_postmortem_recall_impl``;
    sequencing is enforced at the call site in ``_run_pipeline`` rather than
    log-string position. The ConversationBridge inline block remains in
    ``_run_pipeline`` (no extraction yet) — its log-string is the anchor.
    """
    src = _read("backend/core/ouroboros/governance/orchestrator.py")
    bridge_idx = src.find("ConversationBridge injection skipped")
    recall_call_idx = src.find("ctx = _inject_postmortem_recall_impl(ctx)")
    assert bridge_idx > 0, "ConversationBridge marker missing"
    assert recall_call_idx > 0, "PostmortemRecall call site missing"
    assert bridge_idx < recall_call_idx, (
        "PostmortemRecall call site must follow ConversationBridge inline "
        "block (per CONTEXT_EXPANSION ordering)"
    )
