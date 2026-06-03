"""Slice 72 — Generative Target-Existence Guard & Contextual Prompt Insulation.

The deterministic gate: for a benchmark op, every candidate target file MUST
already exist inside the prepared worktree. A miss (e.g. the model emitting a
host-framework path like ``backend/core/process_manager.py`` for a qutebrowser
problem) is surfaced as self-correcting GENERATE_RETRY feedback instead of
crashing APPLY with ENOENT. Host self-development (which legitimately creates
new files) is untouched — the orchestrator only invokes this for
``signal_source == "swe_bench_pro"``.
"""
from __future__ import annotations

from pathlib import Path

from backend.core.ouroboros.governance.target_existence_guard import (
    find_missing_targets,
    build_retry_feedback,
    guard_enabled,
    missing_target_error_message,
    TARGET_MISSING_PREFIX,
)


def _worktree(tmp: Path) -> Path:
    (tmp / "qutebrowser" / "utils").mkdir(parents=True)
    (tmp / "qutebrowser" / "utils" / "guiprocess.py").write_text("x = 1\n")
    return tmp


def test_existing_target_is_not_missing(tmp_path):
    wt = _worktree(tmp_path)
    cands = [{"file_path": "qutebrowser/utils/guiprocess.py", "full_content": "x = 2\n"}]
    assert find_missing_targets(cands, wt) == []


def test_host_namespace_path_is_flagged_missing(tmp_path):
    """The exact bt-2026-06-03 failure: a JARVIS path for a qutebrowser repo."""
    wt = _worktree(tmp_path)
    cands = [{"file_path": "backend/core/process_manager.py", "full_content": "..."}]
    assert find_missing_targets(cands, wt) == ["backend/core/process_manager.py"]


def test_multi_file_candidate_each_target_checked(tmp_path):
    wt = _worktree(tmp_path)
    cands = [{
        "files": [
            {"file_path": "qutebrowser/utils/guiprocess.py", "full_content": "a"},
            {"file_path": "backend/core/nope.py", "full_content": "b"},
        ]
    }]
    assert find_missing_targets(cands, wt) == ["backend/core/nope.py"]


def test_path_escaping_worktree_is_missing(tmp_path):
    """A ../ climb or absolute host path escapes the worktree → flagged."""
    wt = _worktree(tmp_path)
    cands = [{"file_path": "../../../etc/passwd"}]
    assert find_missing_targets(cands, wt) == ["../../../etc/passwd"]


def test_none_write_root_is_inert(tmp_path):
    """No per-op write root resolved (non-benchmark / unresolved) → never block."""
    cands = [{"file_path": "anything/at/all.py"}]
    assert find_missing_targets(cands, None) == []


def test_directory_target_is_not_a_file(tmp_path):
    wt = _worktree(tmp_path)
    cands = [{"file_path": "qutebrowser/utils"}]  # a dir, not a file
    assert find_missing_targets(cands, wt) == ["qutebrowser/utils"]


def test_non_dict_and_empty_candidates_safe(tmp_path):
    wt = _worktree(tmp_path)
    assert find_missing_targets([], wt) == []
    assert find_missing_targets([None, "junk", 42], wt) == []


def test_dedupes_missing_across_candidates(tmp_path):
    wt = _worktree(tmp_path)
    cands = [
        {"file_path": "backend/core/x.py"},
        {"file_path": "backend/core/x.py"},
    ]
    assert find_missing_targets(cands, wt) == ["backend/core/x.py"]


def test_retry_feedback_is_actionable():
    fb = build_retry_feedback(["backend/core/process_manager.py"])
    assert "do not exist" in fb
    assert "backend/core/process_manager.py" in fb
    assert "search_code" in fb and "glob_files" in fb


def test_error_message_carries_sentinel_prefix():
    msg = missing_target_error_message(["a.py", "b.py"])
    assert msg.startswith(TARGET_MISSING_PREFIX)
    assert "a.py" in msg and "b.py" in msg


def test_guard_default_enabled(monkeypatch):
    monkeypatch.delenv("JARVIS_SWE_BENCH_TARGET_EXISTENCE_GUARD_ENABLED", raising=False)
    assert guard_enabled() is True
    monkeypatch.setenv("JARVIS_SWE_BENCH_TARGET_EXISTENCE_GUARD_ENABLED", "false")
    assert guard_enabled() is False


# --- AST-pins: the guard is wired into the live orchestrator generation loop ---

def _orch_src() -> str:
    p = Path(__file__).resolve().parents[2] / (
        "backend/core/ouroboros/governance/orchestrator.py"
    )
    return p.read_text(encoding="utf-8")


def test_orchestrator_invokes_target_guard_gated_on_swe_bench():
    src = _orch_src()
    assert "_find_missing_targets(" in src, "gate must call the guard helper"
    assert "_target_guard_enabled()" in src, "gate must honor the master flag"
    # Gated on the swe_bench source so host self-dev (new files) is untouched.
    assert 'signal_source", "") == "swe_bench_pro"' in src
    # Routed through the GENERATE_RETRY error path (raises, sets generation=None).
    assert "_target_missing_error_message(" in src


def test_orchestrator_has_target_missing_retry_feedback_branch():
    src = _orch_src()
    assert "_err_str.startswith(_TARGET_MISSING_PREFIX)" in src
    assert "_target_missing_retry_feedback(" in src


# --- Phase 3: contextual prompt insulation for benchmark ops ---

def test_should_insulate_only_for_benchmark(monkeypatch):
    from backend.core.ouroboros.governance.target_existence_guard import (
        should_insulate_prompt, prompt_insulation_enabled,
    )
    monkeypatch.delenv("JARVIS_BENCHMARK_PROMPT_INSULATION_ENABLED", raising=False)
    assert prompt_insulation_enabled() is True
    assert should_insulate_prompt("swe_bench_pro") is True
    assert should_insulate_prompt("voice_command") is False
    assert should_insulate_prompt("") is False
    assert should_insulate_prompt(None) is False
    # Master flag off → never insulate (host context restored).
    monkeypatch.setenv("JARVIS_BENCHMARK_PROMPT_INSULATION_ENABLED", "false")
    assert should_insulate_prompt("swe_bench_pro") is False


def test_orchestrator_gates_strategic_and_goal_injection_on_insulation():
    src = _orch_src()
    # Both host-context injections must consult the insulation gate.
    assert src.count("_should_insulate_prompt(") >= 2, (
        "both Strategic Direction and Goal injections must be insulation-gated"
    )
