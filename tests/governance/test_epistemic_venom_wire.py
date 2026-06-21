from __future__ import annotations
import inspect
from backend.core.ouroboros.governance import tool_executor


def test_run_accepts_prefetched_candidates_and_governor_kwargs():
    sig = inspect.signature(tool_executor.ToolLoopCoordinator.run)
    assert "prefetched_candidates" in sig.parameters
    assert "governor" in sig.parameters


def test_seed_prefix_builder_is_bounded_and_labelled():
    from backend.core.ouroboros.governance.epistemic_prefetch import PrefetchEntry
    entries = (PrefetchEntry("dep.py", "abc", 0.9, "CALL_GRAPH", "def f(): pass"),)
    prefix = tool_executor._build_prefetch_seed_prefix(entries)
    assert "dep.py" in prefix
    assert "def f(): pass" in prefix
    assert ("memory" in prefix.lower()) or ("pre-fetched" in prefix.lower())


def test_seed_prefix_empty_for_no_entries():
    assert tool_executor._build_prefetch_seed_prefix(()) == ""


def test_seed_prefix_skips_entries_without_excerpt():
    from backend.core.ouroboros.governance.epistemic_prefetch import PrefetchEntry
    entries = (PrefetchEntry("dep.py", "abc", 0.9, "CALL_GRAPH", ""),)  # no excerpt
    prefix = tool_executor._build_prefetch_seed_prefix(entries)
    # an entry with empty excerpt contributes no body; with only such entries the
    # builder returns "" (nothing to seed)
    assert prefix == "" or "dep.py" not in prefix


def test_governance_deadlock_error_exists():
    assert issubclass(tool_executor.GovernanceDeadlockError, Exception)
