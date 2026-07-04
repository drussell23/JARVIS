"""Sixth starvation class kill — the SYNC coverage API becomes index-backed.

Root-cause pins for ``fix/coverage-sync-inline-ast``.

Live smoking gun (session bt-iso-1783142866, MainThread tombstone, loop
wedged 30.1 s → LoopDeadman kill):

    opportunity_miner_sensor.py:1146 _poll_loop → await scan_once()
    opportunity_miner_sensor.py:686  scan_once → file_has_test_coverage(...)
    target_stratification.py:584     _strat_ast_cache[root] = _strat_build_ast_map(root)
    target_stratification.py:429     ast.parse(source)   ← ~3000 test files, GIL-held

``file_has_test_coverage`` kept the legacy inline walk (Strategy 1) + inline
``_strat_build_ast_map`` cold fallback (Strategy 2). OpportunityMiner and
OperationAdvisor call it straight off the asyncio loop thread, so the cold
AST build blocked the loop for tens of seconds.

The fix makes ``file_has_test_coverage`` STRUCTURALLY incapable of walking or
parsing on the caller's thread: it answers from the off-loop ``_CoverageIndex``
when WARM, and when COLD fires the single-flight ``trigger_coverage_index_build``
and returns the NEUTRAL degrade (``True`` = treated-as-covered = zero penalty),
matching the Tier-4 ingest cold convention (``unified_intake_router.py``:
"cold → PROCEED DEGRADED (override 0 — no penalty, no evidence)").

Pins:
  §a  COLD call NEVER invokes ``_strat_build_ast_map`` / ``_iter_test_files`` /
      ``Path.rglob`` — it requests the off-loop build instead.
  §b  WARM call answers BOTH strategies (suffix-name + AST-import), positive
      and negative, correctly from the index.
  §c  COLD returns the neutral ``True`` even for a genuinely-untested file.
  §d  Early-``True`` cases (non-``.py`` / ``test_*`` input) are unchanged and
      short-circuit before any build trigger.
  §e  Source-level: the function body contains no ``_strat_build_ast_map(``
      call and no rglob / walk / ``_iter_test_files`` traversal token.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import backend.core.ouroboros.governance.target_stratification as _ts
from backend.core.ouroboros.governance.target_stratification import (
    file_has_test_coverage,
)


@pytest.fixture(autouse=True)
def _reset_index_state():
    _ts.reset_coverage_index()
    _ts._strat_ast_cache.clear()
    yield
    _ts.reset_coverage_index()
    _ts._strat_ast_cache.clear()


def _install_index(root: Path) -> None:
    """Build + install the coverage index synchronously (test-only shortcut)."""
    idx = _ts._build_coverage_index_sync(
        _ts._resolve_scan_root(root),
        _ts._strat_test_dir_names(),
    )
    with _ts._COVERAGE_IDX_LOCK:
        _ts._coverage_index[_ts._resolve_scan_root(root)] = idx


def _sever_all_traversal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ANY on-thread walk / parse blow up loudly."""

    def _boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError(
            "SYNC file_has_test_coverage walked/parsed on the caller thread"
        )

    monkeypatch.setattr(_ts, "_strat_build_ast_map", _boom)
    monkeypatch.setattr(_ts, "_iter_test_files", _boom)
    monkeypatch.setattr(Path, "rglob", _boom)


# ── §a COLD never walks/parses; it requests the off-loop build ──────────
def test_cold_call_never_walks_and_requests_offload_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A real (untested) tree exists — but the SYNC API must NOT touch it.
    (tmp_path / "tests").mkdir()
    _sever_all_traversal(monkeypatch)

    calls: list[object] = []
    real_trigger = _ts.trigger_coverage_index_build

    def _spy(repo_root):  # noqa: ANN001
        calls.append(repo_root)
        # Return a sentinel WITHOUT scheduling (no loop in this sync test).
        return "skipped_no_loop"

    monkeypatch.setattr(_ts, "trigger_coverage_index_build", _spy)

    result = file_has_test_coverage("backend/core/orphan.py", tmp_path)

    assert result is True  # neutral degrade — never blocks, never False cold
    assert calls, "cold path must request the single-flight off-loop build"
    # Sanity: the real trigger is single-flight (idempotent) — importable.
    assert callable(real_trigger)


# ── §c COLD returns the neutral True even for a genuinely-untested file ──
def test_cold_returns_neutral_true_for_untested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVIS_STRATIFICATION_AST_IMPORT_ENABLED", "true")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_other.py").write_text(
        "from backend.core.other import foo\ndef test_foo(): pass\n"
    )
    # Index cold (reset by fixture) — untested file must degrade neutral, not
    # walk the tree to resolve False.
    assert file_has_test_coverage("backend/core/orphan.py", tmp_path) is True


# ── §b WARM answers both strategies — suffix name match (positive) ──────
def test_warm_suffix_name_match_positive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVIS_STRATIFICATION_AST_IMPORT_ENABLED", "false")
    (tmp_path / "tests" / "battle_test").mkdir(parents=True)
    (tmp_path / "tests" / "battle_test" / "test_widget_slice4.py").write_text(
        "def test_x(): pass\n"
    )
    _install_index(tmp_path)
    assert file_has_test_coverage("backend/core/widget.py", tmp_path) is True


# ── §b WARM suffix / name — no match (negative) ─────────────────────────
def test_warm_name_no_match_negative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVIS_STRATIFICATION_AST_IMPORT_ENABLED", "false")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_widget.py").write_text("def test_x(): pass\n")
    _install_index(tmp_path)
    # AST off + name doesn't match → genuinely uncovered (warm index answers).
    assert file_has_test_coverage("backend/core/orphan.py", tmp_path) is False


# ── §b WARM answers both strategies — AST import match (positive) ───────
def test_warm_ast_import_match_positive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVIS_STRATIFICATION_AST_IMPORT_ENABLED", "true")
    (tmp_path / "tests").mkdir()
    # A test that imports the module by dotted path (no name-convention match).
    (tmp_path / "tests" / "test_integration_suite.py").write_text(
        "from backend.core.special_util import some_func\n"
        "def test_it(): assert some_func() is not None\n"
    )
    _install_index(tmp_path)
    assert file_has_test_coverage("backend/core/special_util.py", tmp_path) is True


# ── §b WARM AST import — no match (negative) ────────────────────────────
def test_warm_ast_import_no_match_negative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVIS_STRATIFICATION_AST_IMPORT_ENABLED", "true")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_integration_suite.py").write_text(
        "from backend.core.other_module import foo\n"
        "def test_it(): assert foo() is not None\n"
    )
    _install_index(tmp_path)
    assert file_has_test_coverage("backend/core/special_util.py", tmp_path) is False


# ── §b WARM must not walk even to answer (severed traversal) ────────────
def test_warm_answers_without_any_filesystem_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_widget.py").write_text("def test_x(): pass\n")
    _install_index(tmp_path)
    _sever_all_traversal(monkeypatch)
    assert file_has_test_coverage("backend/core/widget.py", tmp_path) is True
    assert file_has_test_coverage("backend/core/nope.py", tmp_path) is False


# ── §d early-True: non-.py input, unchanged, no build trigger ───────────
def test_non_py_input_is_early_true_without_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sever_all_traversal(monkeypatch)
    calls: list[object] = []
    monkeypatch.setattr(
        _ts, "trigger_coverage_index_build", lambda r: calls.append(r)
    )
    assert file_has_test_coverage("README.md", tmp_path) is True
    assert not calls, "non-.py input must short-circuit before any build trigger"


# ── §d early-True: test_* input, unchanged, no build trigger ────────────
def test_test_prefixed_input_is_early_true_without_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sever_all_traversal(monkeypatch)
    calls: list[object] = []
    monkeypatch.setattr(
        _ts, "trigger_coverage_index_build", lambda r: calls.append(r)
    )
    assert file_has_test_coverage("tests/test_widget.py", tmp_path) is True
    assert not calls, "test_* input must short-circuit before any build trigger"


# ── §e source-level: no inline build call, no rglob/walk token ──────────
def test_source_has_no_inline_build_or_walk() -> None:
    src = inspect.getsource(file_has_test_coverage)
    assert "_strat_build_ast_map(" not in src, (
        "SYNC path must not call the inline AST builder"
    )
    assert "rglob" not in src, "SYNC path must not rglob-walk the tree"
    assert ".walk(" not in src, "SYNC path must not os.walk the tree"
    assert "_iter_test_files(" not in src, (
        "SYNC path must not iterate the test tree on the caller thread"
    )
