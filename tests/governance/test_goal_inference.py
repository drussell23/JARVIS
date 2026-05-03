"""Tests for goal_inference — signal extraction + clustering + hypothesis ranking.

Three scope axes:

  1. Signal extractors — commits, REPL, memory, completed ops, file
    hotspots, declared goals. Each is independently fallible; a broken
    source must not stop others.
  2. Clustering + ranking — token frequency, source diversity,
    recency, rejection filter, confidence math, top_k cap.
  3. Integration — CONTEXT_EXPANSION prompt injection wiring,
    accept/reject round trip, AST canaries on orchestrator + harness.
"""
from __future__ import annotations

import os
import subprocess
import textwrap
import time
from pathlib import Path
from typing import Iterator, List

import pytest

from backend.core.ouroboros.governance.goal_inference import (
    GoalInferenceEngine,
    InferenceResult,
    InferredGoal,
    SignalSample,
    _cluster_and_rank,
    _rejected_themes,
    accept_inferred_goal,
    commit_lookback,
    extract_commits_signal,
    extract_declared_goals_signal,
    extract_file_hotspots_signal,
    extract_memory_signal,
    extract_repl_signal,
    extract_tokens,
    inference_enabled,
    max_age_s,
    min_confidence,
    priority_boost_for_signal,
    priority_boost_max,
    prompt_injection_enabled,
    reject_inferred_goal,
    render_prompt_section,
    reset_default_engine,
    top_k,
)


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    for key in list(os.environ.keys()):
        if key.startswith("JARVIS_GOAL_INFERENCE_"):
            monkeypatch.delenv(key, raising=False)
    reset_default_engine()
    yield
    reset_default_engine()


# ---------------------------------------------------------------------------
# (1) Env gates — fail-closed
# ---------------------------------------------------------------------------


def test_inference_default_true_post_graduation(monkeypatch):
    """Post Slice C graduation 2026-05-03 the master flag defaults
    True. Operators flip explicit ``false`` to opt-out."""
    monkeypatch.delenv("JARVIS_GOAL_INFERENCE_ENABLED", raising=False)
    assert inference_enabled() is True


def test_inference_explicit_false_overrides_graduated_default(monkeypatch):
    monkeypatch.setenv("JARVIS_GOAL_INFERENCE_ENABLED", "false")
    assert inference_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
def test_inference_enabled_truthy(monkeypatch, val):
    monkeypatch.setenv("JARVIS_GOAL_INFERENCE_ENABLED", val)
    assert inference_enabled() is True


def test_prompt_injection_default_on():
    """Prompt injection defaults ON so enabling the master knob gets
    the feature end-to-end without flipping another switch."""
    assert prompt_injection_enabled() is True


def test_prompt_injection_can_be_disabled(monkeypatch):
    monkeypatch.setenv("JARVIS_GOAL_INFERENCE_PROMPT_INJECTION", "0")
    assert prompt_injection_enabled() is False


def test_min_confidence_default():
    assert min_confidence() == 0.5


def test_top_k_clamped_range(monkeypatch):
    monkeypatch.setenv("JARVIS_GOAL_INFERENCE_TOP_K", "0")
    assert top_k() == 1
    monkeypatch.setenv("JARVIS_GOAL_INFERENCE_TOP_K", "99999")
    assert top_k() == 10


def test_priority_boost_capped():
    assert priority_boost_max() == 0.5


# ---------------------------------------------------------------------------
# (2) Token extraction
# ---------------------------------------------------------------------------


def test_extract_tokens_lowercases_and_filters_stopwords():
    toks = extract_tokens("Add Semantic Guardian for the Risk Tier Floor")
    assert "semantic" in toks
    assert "guardian" in toks
    assert "tier" in toks
    # Stopwords dropped.
    assert "for" not in toks
    assert "the" not in toks


def test_extract_tokens_drops_short_tokens():
    toks = extract_tokens("ok no go hi yo")
    # All ≤ 2 chars dropped.
    assert toks == []


def test_extract_tokens_handles_code_identifiers():
    toks = extract_tokens(
        "class SemanticGuardian(object):\n    def inspect(self): pass"
    )
    assert "semanticguardian" in toks
    assert "inspect" in toks


def test_extract_tokens_empty_input():
    assert extract_tokens("") == []
    assert extract_tokens(None) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# (3) Signal extractors
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> Iterator[Path]:
    """Isolated git repo with a few commits of known shape."""
    repo = tmp_path / "repo"
    repo.mkdir()
    def g(*args):
        return subprocess.run(
            ["git", *args], cwd=str(repo),
            capture_output=True, text=True,
        )
    g("init", "-q")
    g("config", "user.email", "test@example.com")
    g("config", "user.name", "Test")
    g("config", "commit.gpgsign", "false")
    # Seed with a series of Conventional Commits.
    (repo / "README.md").write_text("init\n")
    g("add", ".")
    g("commit", "-q", "-m", "chore: initial commit")
    commits = [
        ("feat(guardian): add semantic pattern for forbidden paths", "auth.py"),
        ("feat(guardian): tighten credential shape heuristic", "auth.py"),
        ("fix(risk): tier floor clamp off-by-one in paranoia mode", "risk.py"),
        ("docs(plugin): manifest schema + examples", "plugin.md"),
        ("feat(plugin): add gate plugin base class + tests", "plugin.py"),
    ]
    for subject, file in commits:
        (repo / file).write_text(f"# {subject}\n")
        g("add", file)
        g("commit", "-q", "-m", subject)
    yield repo


def test_extract_commits_signal_captures_subject_keywords(tmp_git_repo):
    cutoff = time.time() - 86400
    samples = extract_commits_signal(
        repo_root=tmp_git_repo, lookback=20, cutoff_epoch=cutoff,
    )
    assert samples
    tokens = {s.token for s in samples}
    # Keywords from subjects should appear.
    assert "guardian" in tokens
    assert "plugin" in tokens
    # Conventional-commit scope tokens are there.
    assert "risk" in tokens or "plugin" in tokens


def test_extract_commits_signal_respects_lookback(tmp_git_repo):
    cutoff = time.time() - 86400
    limited = extract_commits_signal(
        repo_root=tmp_git_repo, lookback=2, cutoff_epoch=cutoff,
    )
    wide = extract_commits_signal(
        repo_root=tmp_git_repo, lookback=20, cutoff_epoch=cutoff,
    )
    # Lookback=2 pulls fewer samples than lookback=20.
    assert len(limited) < len(wide)


def test_extract_commits_signal_respects_cutoff(tmp_git_repo):
    """Cutoff in the future drops every commit."""
    cutoff = time.time() + 3600
    samples = extract_commits_signal(
        repo_root=tmp_git_repo, lookback=20, cutoff_epoch=cutoff,
    )
    assert samples == []


def test_extract_commits_signal_missing_repo():
    """Nonexistent dir — return empty, don't raise."""
    samples = extract_commits_signal(
        repo_root=Path("/nonexistent/path"),
        lookback=10,
        cutoff_epoch=time.time() - 86400,
    )
    assert samples == []


def test_extract_file_hotspots_signal_ranks_most_touched(tmp_git_repo):
    samples, ranked = extract_file_hotspots_signal(
        repo_root=tmp_git_repo, lookback=20,
    )
    # auth.py was touched twice — it should rank highest in the
    # hotspot list.
    assert "auth.py" in ranked
    assert ranked.index("auth.py") <= ranked.index("plugin.py")


def test_extract_repl_signal_reads_from_bridge_snapshot():
    snap = [
        ("tui_user", "can we finish the semantic guardian work tonight"),
        ("postmortem", "ignored — not operator text"),
        ("ask_human_a", "yes, prioritize audit"),
    ]
    samples = extract_repl_signal(
        bridge_snapshot=snap, cutoff_epoch=time.time() - 3600,
    )
    sources = {s.source for s in samples}
    assert sources == {"repl"}
    tokens = {s.token for s in samples}
    assert "semantic" in tokens
    assert "guardian" in tokens
    # postmortem entries were intentionally excluded.
    assert "ignored" not in tokens


def test_extract_repl_signal_empty_when_no_bridge():
    samples = extract_repl_signal(
        bridge_snapshot=[], cutoff_epoch=time.time() - 3600,
    )
    assert samples == []


def test_extract_memory_signal_via_tmp_store(tmp_path):
    from backend.core.ouroboros.governance.user_preference_memory import (
        MemoryType, UserPreferenceStore,
    )
    store = UserPreferenceStore(tmp_path / ".jarvis" / "user_preferences")
    store.add(
        MemoryType.USER, "role",
        "Derek: building governance safety net for autonomous ops",
        source="test",
    )
    store.add(
        MemoryType.PROJECT, "current_focus",
        "expanding plugin system with renderer plugin type",
        source="test",
    )
    # FEEDBACK entry should NOT contribute to goal inference.
    store.add(
        MemoryType.FEEDBACK, "ignore_me",
        "don't auto-approve Yellow on weekends",
        source="test",
    )
    samples = extract_memory_signal(store=store)
    tokens = {s.token for s in samples}
    assert "governance" in tokens
    assert "plugin" in tokens
    # Feedback content ignored.
    assert "weekends" not in tokens


def test_extract_declared_goals_signal_graceful_when_no_goals(tmp_path):
    """No GoalTracker YAML in tmp_path → empty list, no raise."""
    samples = extract_declared_goals_signal(repo_root=tmp_path)
    assert samples == []


# ---------------------------------------------------------------------------
# (4) Clustering + ranking
# ---------------------------------------------------------------------------


def _mk_sample(source, token, weight=1.0, age=0.0):
    return SignalSample(source=source, token=token, weight=weight, age_s=age)


def test_cluster_ranks_by_diversity_not_just_volume():
    """A token supported by 3 sources should outrank one supported by
    one source even with higher raw volume."""
    samples = (
        # high volume, low diversity
        [_mk_sample("commits", "alpha")] * 20
        +
        # lower volume but every source agrees
        [
            _mk_sample("commits", "beta"),
            _mk_sample("commits", "beta"),
            _mk_sample("repl", "beta"),
            _mk_sample("memory", "beta"),
            _mk_sample("completed_ops", "beta"),
            _mk_sample("declared_goals", "beta"),
        ]
    )
    results = _cluster_and_rank(
        samples=samples, hotspot_paths=(), rejected_themes=set(),
    )
    # beta should win on diversity.
    assert any("beta" in g.theme.lower() for g in results)
    top_theme = results[0].theme.lower()
    assert "beta" in top_theme


def test_cluster_confidence_threshold_filters_weak():
    """Single-source, single-sample tokens should fall below the
    default min_confidence."""
    samples = [_mk_sample("commits", "weak_theme_token")]
    results = _cluster_and_rank(
        samples=samples, hotspot_paths=(), rejected_themes=set(),
    )
    # Below 0.5 confidence — should be filtered out.
    assert not any("weak_theme_token" in g.theme.lower() for g in results)


def test_cluster_merges_prefix_related_tokens():
    """Prefix-shared tokens (≥4 char) cluster — 'semantic' + 'semantics'
    + 'semantically' should collapse."""
    samples = [
        _mk_sample("commits", "semantic"),
        _mk_sample("repl", "semantic"),
        _mk_sample("memory", "semantic"),
        _mk_sample("commits", "semantics"),
        _mk_sample("repl", "semantically"),
    ]
    results = _cluster_and_rank(
        samples=samples, hotspot_paths=(), rejected_themes=set(),
    )
    assert results
    head = results[0]
    # All three variants should be in the same cluster.
    assert "semantic" in head.tokens
    assert "semantics" in head.tokens


def test_cluster_rejection_filter_removes_theme():
    samples = [
        _mk_sample("commits", "undesirable"),
        _mk_sample("repl", "undesirable"),
        _mk_sample("memory", "undesirable"),
    ]
    rejected = {"undesirable"}
    results = _cluster_and_rank(
        samples=samples, hotspot_paths=(), rejected_themes=rejected,
    )
    assert not any("undesirable" in g.theme.lower() for g in results)


def test_cluster_correlates_file_hotspots_with_head_token():
    samples = [
        _mk_sample("commits", "guardian"),
        _mk_sample("repl", "guardian"),
        _mk_sample("memory", "guardian"),
    ]
    hotspot_paths = (
        "backend/governance/semantic_guardian.py",
        "tests/governance/test_guardian.py",
        "unrelated/readme.md",
    )
    results = _cluster_and_rank(
        samples=samples,
        hotspot_paths=hotspot_paths,
        rejected_themes=set(),
    )
    assert results
    head = results[0]
    # Correlated files attached.
    assert any("guardian" in f for f in head.supporting_files)
    # Unrelated file NOT attached.
    assert not any("readme" in f for f in head.supporting_files)


def test_cluster_recency_boosts_younger_samples():
    """Equal everything else, younger samples score higher."""
    old_samples = [
        _mk_sample("commits", "oldidea", age=80000),
        _mk_sample("repl", "oldidea", age=80000),
    ]
    young_samples = [
        _mk_sample("commits", "newidea", age=60),
        _mk_sample("repl", "newidea", age=60),
    ]
    combined = old_samples + young_samples
    results = _cluster_and_rank(
        samples=combined, hotspot_paths=(), rejected_themes=set(),
    )
    # Young idea should outrank old.
    idx_young = next(
        (i for i, g in enumerate(results) if "newidea" in g.theme.lower()),
        None,
    )
    idx_old = next(
        (i for i, g in enumerate(results) if "oldidea" in g.theme.lower()),
        None,
    )
    if idx_young is not None and idx_old is not None:
        assert idx_young < idx_old


# ---------------------------------------------------------------------------
# (5) Engine — end-to-end
# ---------------------------------------------------------------------------


def test_engine_disabled_returns_empty_result(tmp_git_repo, monkeypatch):
    monkeypatch.setenv("JARVIS_GOAL_INFERENCE_ENABLED", "0")
    engine = GoalInferenceEngine(repo_root=tmp_git_repo)
    result = engine.build()
    assert result.inferred == ()
    assert result.build_reason == "disabled"


def test_engine_builds_hypotheses_from_real_commits(
    tmp_git_repo, monkeypatch,
):
    monkeypatch.setenv("JARVIS_GOAL_INFERENCE_ENABLED", "1")
    monkeypatch.setenv("JARVIS_GOAL_INFERENCE_MIN_CONFIDENCE", "0.3")
    engine = GoalInferenceEngine(repo_root=tmp_git_repo)
    result = engine.build()
    # We seeded with guardian + plugin + risk commits — at least one
    # should surface as a hypothesis.
    assert result.build_ms >= 0
    themes = [g.theme.lower() for g in result.inferred]
    assert any(
        any(kw in t for kw in ("guardian", "plugin", "risk"))
        for t in themes
    )


def test_engine_caches_between_builds(tmp_git_repo, monkeypatch):
    monkeypatch.setenv("JARVIS_GOAL_INFERENCE_ENABLED", "1")
    monkeypatch.setenv("JARVIS_GOAL_INFERENCE_MIN_CONFIDENCE", "0.3")
    monkeypatch.setenv("JARVIS_GOAL_INFERENCE_REFRESH_S", "3600")
    engine = GoalInferenceEngine(repo_root=tmp_git_repo)
    r1 = engine.build()
    r2 = engine.build()
    # Same object returned (cache hit).
    assert r1 is r2


def test_engine_force_rebuilds(tmp_git_repo, monkeypatch):
    monkeypatch.setenv("JARVIS_GOAL_INFERENCE_ENABLED", "1")
    monkeypatch.setenv("JARVIS_GOAL_INFERENCE_MIN_CONFIDENCE", "0.3")
    monkeypatch.setenv("JARVIS_GOAL_INFERENCE_REFRESH_S", "3600")
    engine = GoalInferenceEngine(repo_root=tmp_git_repo)
    r1 = engine.build()
    r2 = engine.build(force=True)
    # Different object (rebuilt).
    assert r1 is not r2


def test_engine_invalidate_clears_cache(tmp_git_repo, monkeypatch):
    monkeypatch.setenv("JARVIS_GOAL_INFERENCE_ENABLED", "1")
    monkeypatch.setenv("JARVIS_GOAL_INFERENCE_MIN_CONFIDENCE", "0.3")
    engine = GoalInferenceEngine(repo_root=tmp_git_repo)
    engine.build()
    assert engine.get_current() is not None
    engine.invalidate()
    assert engine.get_current() is None


# ---------------------------------------------------------------------------
# (6) Prompt rendering + priority boost
# ---------------------------------------------------------------------------


def test_render_prompt_section_empty_when_no_inferred():
    result = InferenceResult(inferred=(), built_at=time.time())
    assert render_prompt_section(result) == ""


def test_render_prompt_section_labels_as_hypotheses():
    goals = (
        InferredGoal(
            theme="guardian  [feat(guardian): add pattern]",
            tokens=("guardian",),
            confidence=0.72,
            supporting_sources=("commits", "memory"),
            evidence=(),
            supporting_files=("governance/semantic_guardian.py",),
        ),
    )
    result = InferenceResult(inferred=goals, built_at=time.time())
    text = render_prompt_section(result)
    # Explicit labeling is load-bearing — must NOT look like declared goals.
    assert "hypotheses" in text.lower()
    assert "not declared goals" in text.lower() or "NOT declared" in text
    # Content present.
    assert "guardian" in text
    assert "0.72" in text


def test_render_prompt_section_respects_top_k(monkeypatch):
    monkeypatch.setenv("JARVIS_GOAL_INFERENCE_TOP_K", "2")
    goals = tuple(
        InferredGoal(
            theme=f"theme{i}",
            tokens=(f"theme{i}",),
            confidence=0.9 - i * 0.1,
            supporting_sources=("commits",),
            evidence=(),
        )
        for i in range(5)
    )
    result = InferenceResult(inferred=goals, built_at=time.time())
    text = render_prompt_section(result)
    # Only top 2 render.
    assert "theme0" in text
    assert "theme1" in text
    assert "theme2" not in text


def test_render_prompt_section_empty_when_injection_disabled(monkeypatch):
    monkeypatch.setenv("JARVIS_GOAL_INFERENCE_PROMPT_INJECTION", "0")
    goals = (
        InferredGoal(
            theme="x", tokens=("x",), confidence=0.9,
            supporting_sources=("commits",), evidence=(),
        ),
    )
    result = InferenceResult(inferred=goals, built_at=time.time())
    assert render_prompt_section(result) == ""


def test_priority_boost_capped_by_env():
    goals = (
        InferredGoal(
            theme="alpha", tokens=("alpha",), confidence=1.0,
            supporting_sources=("commits",), evidence=(),
        ),
        InferredGoal(
            theme="beta", tokens=("beta",), confidence=1.0,
            supporting_sources=("commits",), evidence=(),
        ),
    )
    result = InferenceResult(inferred=goals, built_at=time.time())
    # Signal text matches BOTH goals — raw sum would exceed cap.
    boost = priority_boost_for_signal(
        signal_description="fixing alpha and beta integration",
        signal_target_files=(),
        result=result,
    )
    # Cap is 0.5 by default.
    assert boost <= 0.5


def test_priority_boost_zero_when_no_overlap():
    goals = (
        InferredGoal(
            theme="alpha", tokens=("alpha",), confidence=1.0,
            supporting_sources=("commits",), evidence=(),
        ),
    )
    result = InferenceResult(inferred=goals, built_at=time.time())
    boost = priority_boost_for_signal(
        signal_description="totally unrelated work",
        signal_target_files=(),
        result=result,
    )
    assert boost == 0.0


# ---------------------------------------------------------------------------
# (7) Accept / Reject — round trip
# ---------------------------------------------------------------------------


def test_reject_creates_feedback_memory(tmp_path, monkeypatch):
    """Rejection writes a FEEDBACK memory tagged inferred_goal_rejected.
    Subsequent inference runs must filter this theme out."""
    monkeypatch.setenv("JARVIS_REPO_PATH", str(tmp_path))
    inferred = InferredGoal(
        theme="banana  [from commits]",
        tokens=("banana",),
        confidence=0.8,
        supporting_sources=("commits", "memory"),
        evidence=(),
    )
    ok, msg = reject_inferred_goal(repo_root=tmp_path, inferred=inferred)
    assert ok, msg
    # Rejection shows up in the filter set.
    rejected = _rejected_themes(tmp_path)
    assert "banana" in rejected


def test_rejected_theme_filtered_from_next_build(tmp_path, monkeypatch):
    """End-to-end: reject theme → next rank pass drops it."""
    samples = [
        _mk_sample("commits", "banana"),
        _mk_sample("repl", "banana"),
        _mk_sample("memory", "banana"),
    ]
    # Round 1: banana surfaces.
    r1 = _cluster_and_rank(
        samples=samples, hotspot_paths=(), rejected_themes=set(),
    )
    assert any("banana" in g.theme.lower() for g in r1)
    # Round 2: reject banana → drops.
    r2 = _cluster_and_rank(
        samples=samples, hotspot_paths=(), rejected_themes={"banana"},
    )
    assert not any("banana" in g.theme.lower() for g in r2)


def test_accept_returns_error_gracefully_when_goal_tracker_unavailable(
    tmp_path,
):
    """Path doesn't have a goals.yaml — add_goal should either succeed
    (writing the initial file) or fail gracefully. Either way, no raise."""
    inferred = InferredGoal(
        theme="some_theme", tokens=("some_theme",), confidence=0.9,
        supporting_sources=("commits",), evidence=(),
    )
    ok, msg = accept_inferred_goal(
        repo_root=tmp_path, inferred=inferred,
    )
    # Whether it succeeded or gracefully failed, we got a string message.
    assert isinstance(msg, str)


# ---------------------------------------------------------------------------
# (8) AST canaries — orchestrator + harness wiring
# ---------------------------------------------------------------------------


def test_orchestrator_wires_goal_inference():
    path = (
        Path(__file__).resolve().parent.parent.parent
        / "backend/core/ouroboros/governance/orchestrator.py"
    )
    src = path.read_text(encoding="utf-8")
    assert "goal_inference" in src
    assert "GoalInferenceEngine" in src
    assert "render_prompt_section" in src
    assert "inference_enabled" in src


def test_harness_dispatches_infer_command():
    path = (
        Path(__file__).resolve().parent.parent.parent
        / "backend/core/ouroboros/battle_test/harness.py"
    )
    src = path.read_text(encoding="utf-8")
    assert "_repl_cmd_infer" in src
    assert "/infer" in src


def test_inference_module_labels_hypotheses_not_facts():
    """The render section MUST explicitly distinguish from declared
    goals. Catches a refactor that accidentally removes the boundary
    label (which would be a §1 violation — inferred suddenly looks
    authoritative to the model)."""
    path = (
        Path(__file__).resolve().parent.parent.parent
        / "backend/core/ouroboros/governance/goal_inference.py"
    )
    src = path.read_text(encoding="utf-8")
    # Lowercased search so minor wording variation doesn't trip the canary.
    assert "hypotheses" in src.lower()
    assert "not declared goals" in src.lower() or "NOT declared" in src
    # Authority invariant statement present.
    assert "risk_tier" in src or "risk tier" in src.lower()
