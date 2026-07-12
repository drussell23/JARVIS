"""Slice 6 Task 6 — THE Run #16 scenario, pinned against the real repo:
the exact signal that died at VERIFY (pass_rate=0.75) must now carry
the source under test in target_files. If this test ever goes red, the
attribution bridge has regressed and autonomous test-failure repair is
structurally dead again (ops can only mutate the test file)."""
from __future__ import annotations

from pathlib import Path

from backend.core.ouroboros.governance.intent.test_source_attribution import (
    attribute_test_to_sources,
)
from backend.core.ouroboros.governance.intent.test_watcher import (
    TestFailure,
    TestWatcher,
)

_REPO = str(Path(__file__).resolve().parents[3])
_TEST = "tests/governance/a1_ignition_vector/test_leaf_predicates.py"
_SOURCE = "backend/core/ouroboros/a1_ignition_vector/leaf_predicates.py"


def test_run16_pair_attributes_directly() -> None:
    attr = attribute_test_to_sources(_TEST, repo_root=_REPO)
    assert _SOURCE in attr.source_loci
    assert attr.method == "direct_import"


def test_run16_signal_scope_contains_source() -> None:
    w = TestWatcher(repo="jarvis", repo_path=_REPO)
    f = TestFailure(
        test_id=f"{_TEST}::test_clamp01",
        file_path=_TEST,
        error_text="AssertionError: clamp01(2.0) != 1.0",
    )
    w.process_failures([f])
    signals = w.process_failures([f])
    assert len(signals) == 1
    assert _SOURCE in signals[0].target_files
    assert _TEST in signals[0].target_files
    assert signals[0].evidence["attribution"]["status"] == "resolved"
