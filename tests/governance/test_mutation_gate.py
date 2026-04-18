"""MutationGate — APPLY-phase execution boundary regression spine.

Coverage:
  * Env master switch (gate_enabled() default-off).
  * Allowlist precedence: env > YAML, combined dedup.
  * Path matching: exact + prefix + reject-near-miss.
  * Verdict tiers: allow / upgrade_to_approval / block / skip.
  * Score → decision mapping honors the allow/block thresholds.
  * Batch merge: worst decision wins.
  * Cache integration: second call with same content uses outcome cache.
  * AST canary: authority invariant + split-authority claim.
"""
from __future__ import annotations

import os
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance import mutation_cache as MC
from backend.core.ouroboros.governance import mutation_gate as MG
from backend.core.ouroboros.governance.mutation_tester import (
    Mutant, MutantOutcome, MutationResult,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for k in list(os.environ.keys()):
        if k.startswith("JARVIS_MUTATION_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("JARVIS_MUTATION_CACHE_DIR", str(tmp_path / "cache"))
    MC._catalog_lru.clear()
    MC._outcome_lru.clear()
    yield


def _write(path: Path, src: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(src), encoding="utf-8")


# ---------------------------------------------------------------------------
# Env / allowlist
# ---------------------------------------------------------------------------


def test_gate_disabled_by_default():
    assert MG.gate_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
def test_gate_env_truthy(monkeypatch, val):
    monkeypatch.setenv("JARVIS_MUTATION_GATE_ENABLED", val)
    assert MG.gate_enabled() is True


def test_allowlist_env_parsing(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_MUTATION_GATE_CRITICAL_PATHS",
        "backend/core/, backend/api/auth.py, ,",
    )
    assert MG.load_allowlist() == [
        "backend/core/", "backend/api/auth.py",
    ]


def test_is_path_critical_prefix_boundary():
    allow = ["backend/core/"]
    assert MG.is_path_critical(Path("backend/core/x.py"), allowlist=allow)
    assert MG.is_path_critical(Path("backend/core/sub/x.py"), allowlist=allow)
    # Near-miss — must NOT match.
    assert not MG.is_path_critical(Path("backend/core_test/x.py"), allowlist=allow)
    assert not MG.is_path_critical(Path("backend/other.py"), allowlist=allow)


def test_is_path_critical_exact_match():
    allow = ["backend/api/auth.py"]
    assert MG.is_path_critical(Path("backend/api/auth.py"), allowlist=allow)
    assert not MG.is_path_critical(Path("backend/api/auth2.py"), allowlist=allow)


def test_empty_allowlist_rejects_everything():
    assert not MG.is_path_critical(Path("any/file.py"), allowlist=[])


# ---------------------------------------------------------------------------
# Threshold / decision mapping
# ---------------------------------------------------------------------------


def test_score_thresholds_clamp(monkeypatch):
    monkeypatch.setenv("JARVIS_MUTATION_GATE_ALLOW_THRESHOLD", "2.5")
    assert MG.allow_threshold() == 1.0
    monkeypatch.setenv("JARVIS_MUTATION_GATE_BLOCK_THRESHOLD", "-0.1")
    assert MG.block_threshold() == 0.0


def test_map_score_to_decision_default_bands(monkeypatch):
    # defaults: allow >= 0.75, block < 0.40
    assert MG._map_score_to_decision(0.80) == "allow"
    assert MG._map_score_to_decision(0.50) == "upgrade_to_approval"
    assert MG._map_score_to_decision(0.20) == "block"


# ---------------------------------------------------------------------------
# Verdict shapes
# ---------------------------------------------------------------------------


def test_evaluate_skips_when_gate_disabled(tmp_path):
    sut = tmp_path / "s.py"
    _write(sut, "def f(): return 1")
    verdict = MG.evaluate_file(sut, [tmp_path / "t.py"])
    assert verdict.decision == "skip"
    assert verdict.reason == "gate_disabled"


def test_evaluate_skips_when_not_critical(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_MUTATION_GATE_ENABLED", "1")
    # empty allowlist → nothing critical
    sut = tmp_path / "s.py"
    _write(sut, "def f(): return 1")
    verdict = MG.evaluate_file(sut, [tmp_path / "t.py"])
    assert verdict.decision == "skip"
    assert verdict.reason == "path_not_critical"


def test_evaluate_skips_when_sut_missing(monkeypatch, tmp_path):
    # force=True bypasses master/allowlist but still needs the file.
    verdict = MG.evaluate_file(tmp_path / "nope.py", [], force=True)
    assert verdict.decision == "skip"
    assert verdict.reason == "sut_missing"


def test_evaluate_skips_when_no_test_files(tmp_path):
    sut = tmp_path / "s.py"
    _write(sut, "def f(): return 1")
    verdict = MG.evaluate_file(sut, [], force=True)
    assert verdict.decision == "skip"
    assert verdict.reason == "no_test_files"


# ---------------------------------------------------------------------------
# Real run — force=True bypasses allowlist, exercises full path
# ---------------------------------------------------------------------------


def _mk_conftest(tmp_path):
    (tmp_path / "conftest.py").write_text(
        "import sys, os\nsys.path.insert(0, os.path.dirname(__file__))\n",
        encoding="utf-8",
    )


def test_evaluate_force_runs_full_path_high_score(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _mk_conftest(tmp_path)
    sut = tmp_path / "sut.py"
    tst = tmp_path / "test_sut.py"
    _write(sut, """
        def clamp(x, lo, hi):
            if x < lo: return lo
            if x > hi: return hi
            return x
    """)
    _write(tst, """
        from sut import clamp
        def test_below(): assert clamp(-5, 0, 10) == 0
        def test_above(): assert clamp(50, 0, 10) == 10
        def test_inside(): assert clamp(5, 0, 10) == 5
        def test_lo_boundary(): assert clamp(0, 0, 10) == 0
        def test_hi_boundary(): assert clamp(10, 0, 10) == 10
    """)
    # Tight cap so the test runs fast.
    monkeypatch.setenv("JARVIS_MUTATION_GATE_MAX_MUTANTS", "5")
    monkeypatch.setenv("JARVIS_MUTATION_GATE_PER_TIMEOUT_S", "30")
    verdict = MG.evaluate_file(sut, [tst], force=True)
    assert verdict.total_mutants > 0
    assert verdict.decision in ("allow", "upgrade_to_approval"), (
        f"strong tests should land in allow-or-approval band; "
        f"got decision={verdict.decision} score={verdict.score:.2f}"
    )


def test_evaluate_uses_outcome_cache_on_second_call(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _mk_conftest(tmp_path)
    sut = tmp_path / "sut.py"
    tst = tmp_path / "test_sut.py"
    _write(sut, "def add(a,b): return a+b")
    _write(tst, """
        from sut import add
        def test_one(): assert add(1,2) == 3
        def test_two(): assert add(-1,1) == 0
    """)
    monkeypatch.setenv("JARVIS_MUTATION_GATE_MAX_MUTANTS", "3")
    monkeypatch.setenv("JARVIS_MUTATION_GATE_PER_TIMEOUT_S", "30")
    first = MG.evaluate_file(sut, [tst], force=True)
    assert first.cache_hits == 0
    assert first.cache_misses > 0
    second = MG.evaluate_file(sut, [tst], force=True)
    assert second.cache_hits == first.total_mutants
    assert second.cache_misses == 0


# ---------------------------------------------------------------------------
# merge_verdicts
# ---------------------------------------------------------------------------


def _v(dec, score=0.5, survivors=()):
    return MG.GateVerdict(
        decision=dec, score=score, grade="B",
        allow_threshold=0.75, block_threshold=0.40,
        total_mutants=10, caught=int(10 * score),
        survived=10 - int(10 * score),
        reason=dec,
        survivors=tuple(survivors),
    )


def test_merge_worst_decision_wins():
    assert MG.merge_verdicts([_v("allow"), _v("block")]).decision == "block"
    assert MG.merge_verdicts([_v("skip"), _v("upgrade_to_approval")]).decision \
        == "upgrade_to_approval"
    assert MG.merge_verdicts([_v("allow"), _v("allow")]).decision == "allow"
    assert MG.merge_verdicts([]).decision == "skip"


def test_merge_aggregates_score_and_survivors():
    fake_mut = Mutant(
        op="bool_flip", source_file="x.py", line=1, col=0,
        original="True", mutated="False", patched_src="",
    )
    fake_out = MutantOutcome(
        mutant=fake_mut, caught=False, reason="survived", duration_s=0.1,
    )
    merged = MG.merge_verdicts([
        _v("allow", score=0.9),
        _v("upgrade_to_approval", score=0.5, survivors=[fake_out]),
    ])
    # Total caught = 9 + 5 = 14 of 20 = 0.70
    assert merged.total_mutants == 20
    assert merged.caught == 14
    assert abs(merged.score - 0.70) < 1e-6
    assert len(merged.survivors) == 1


# ---------------------------------------------------------------------------
# AST canaries — authority + split semantics
# ---------------------------------------------------------------------------


def test_module_declares_authority_split():
    src = Path(
        "backend/core/ouroboros/governance/mutation_gate.py"
    ).read_text(encoding="utf-8")
    assert "pure measurer" in src.lower() or "measure" in src.lower()
    assert "decision maker" in src.lower() or "decide" in src.lower()
    assert "orchestrator" in src.lower()
    # The split must be documented as deliberate.
    assert "Authority" in src


def test_module_default_block_threshold_is_conservative():
    src = Path(
        "backend/core/ouroboros/governance/mutation_gate.py"
    ).read_text(encoding="utf-8")
    # Defaults should be conservative — operator explicitly chose 0.40
    # as the auto-block floor. A future commit must not silently lower
    # this to something permissive.
    assert "0.40" in src
    assert "0.75" in src
