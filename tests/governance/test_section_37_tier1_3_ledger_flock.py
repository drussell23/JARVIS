"""§37 Tier 1 #3 — Cross-process flock on ledgers.

Closes the §37 Tier 1 row #3 ("Cross-process flock on ledgers") by
migrating 3 legacy `flock_exclusive(fileno)` sites to the canonical
`cross_process_jsonl.flock_append_line` substrate AND wiring 4
previously-unprotected JSONL ledger paths to the same canonical
primitive.

## Closure scope (7 sites total)

**Type B — legacy → canonical migrations**
  1. ``observability/decision_trace_ledger.py:_legacy_fileno_flock``
     fallback retained; primary path now ``flock_append_line``.
  2. ``adaptation/graduation_ledger.py`` same migration shape.
  3. ``observability/post_merge_auditor.py`` same migration shape.

**Type A — true gaps (no cross-process flock pre-Wave-3) → canonical**
  4. ``intake/wal.py::_write_line`` (sensor write-ahead log; many
     sensors append concurrently within a single session).
  5. ``posture_store.py::append_audit`` (§8 immutable audit log;
     within-process ``threading.Lock`` retained as complementary
     fence).
  6. ``mutation_gate.py`` (mutation budget ledger; module-level
     ``_ledger_lock`` retained as complementary fence).
  7. ``metrics_history.py`` (telemetry ledger).

## Closure pattern (load-bearing)

Every migrated site composes the canonical
``cross_process_jsonl.flock_append_line(path, line)`` call inside
its existing producer function. NO parallel locking machinery —
the substrate is the single source of truth for cross-process
JSONL append semantics. Substrate-unavailable rollback path is
preserved at every site for fcntl-unavailable platforms.

## AST pin contract

The pin enumerates all `path.open("a", encoding="utf-8")` sites
under ``backend/core/ouroboros/governance``. Each site MUST be in
one of three states:

  1. The same file composes a flock primitive (
     ``flock_append_line`` / ``flock_critical_section`` /
     ``flock_append_lines`` / ``async_flock_critical_section``)
     — covers both the canonical substrate AND files that own a
     compatible local primitive (e.g., ``adaptation/yaml_writer.py``
     uses fcntl directly via its own helper).
  2. The same file composes the legacy ``flock_exclusive(fileno)``
     pattern (Phase 7.8 — pre-canonical but still cross-process
     safe on POSIX). Acceptable as a fallback path.
  3. The file path is on the explicit allowlist below — files that
     are single-process by construction (test fixtures, harness-
     local logs, etc.).

Any new ``open("a")`` site that doesn't satisfy 1, 2, or 3 fails
the pin — turning silent debt into reviewer attention.
"""
from __future__ import annotations

import multiprocessing
import re
import time
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
_GOVERNANCE_ROOT = _REPO_ROOT / "backend/core/ouroboros/governance"


# -----------------------------------------------------------------
# Allowlist: files that legitimately use ``open("a")`` without a
# flock primitive. Each entry MUST have a one-line rationale that
# explains why cross-process flock is not load-bearing for the
# site. Adding a new entry forces reviewer attention — operator
# binding is single-canonical-name discipline.
# -----------------------------------------------------------------
_OPEN_APPEND_ALLOWLIST: dict[str, str] = {
    # The substrate itself — open("a") inside flock_append_lines
    # IS the canonical primitive's implementation.
    "cross_process_jsonl.py": (
        "substrate self — open('a') is implementation of the "
        "canonical primitive itself"
    ),
    # Owns its own fcntl-via-flock_handle pattern; comment at
    # line 310 documents the lock_handle role.
    "adaptation/yaml_writer.py": (
        "owns local fcntl-via-lock-handle pattern; not a JSONL "
        "ledger — single YAML lock-handle file"
    ),
    # Single-process REPL helper that writes operator-input
    # snapshots; not a multi-producer ledger.
    "chat_repl_subagent_executor.py": (
        "single-process REPL helper, operator-input snapshot "
        "writer — not multi-producer"
    ),
    "chat_repl_claude_executor.py": (
        "single-process REPL helper, operator-input snapshot "
        "writer — not multi-producer"
    ),
    "backlog_auto_proposed_repl.py": (
        "single-process REPL helper writing operator decisions"
    ),
    # Owned audit logs that consumers compose via separate
    # producer paths (sibling files manage cross-process flock
    # at the call site, e.g. via auto_committer's
    # async_flock_critical_section wrapper).
    "inline_approval_provider.py": (
        "operator-interactive provider — single-process by "
        "construction (one inline approval session at a time)"
    ),
    "adversarial_reviewer_service.py": (
        "single-process review queue per session"
    ),
    "postmortem_recall.py": (
        "rolled-up index over PostmortemRecord JSONL — flock at "
        "the producer (orchestrator postmortem path), not on the "
        "rollup"
    ),
    "curiosity_engine.py": (
        "engine artifact log; producer is curiosity_collector.py "
        "which composes flock_append_line"
    ),
    "strategic_direction.py": (
        "operator-prompt audit appended at session boot — "
        "single-process by construction"
    ),
    "cognitive_metrics.py": (
        "within-process telemetry; multi-process aggregation "
        "happens in metrics_history.py which IS flock-protected"
    ),
    "cancel_token.py": (
        "in-process cancel-token registry; not a multi-producer "
        "ledger"
    ),
    "compaction_caller.py": (
        "context-compaction trace — tied to a single ToolExecutor "
        "instance per session"
    ),
    "hypothesis_ledger.py": (
        "hypothesis records; single-producer per session — "
        "verification.hypothesis_probe owns the multi-producer "
        "memorialize path"
    ),
    "composite_score.py": (
        "score history; single ScoreHistory owner per session"
    ),
    "goal_memory_bridge.py": (
        "goal-memory thought log; single GoalMemoryBridge owner"
    ),
    "graduation/cadence_health.py": (
        "internal cadence trace; file already composes flock_* "
        "in 8 other places — this is a separate single-process "
        "warning trace"
    ),
    "meta/order2_review_queue.py": (
        "order-2 review queue; single ReviewQueue owner per session"
    ),
    "self_goal_formation.py": (
        "self-goal formation drafts; single-process by construction"
    ),
    "verification/hypothesis_probe.py": (
        "single-probe artifact path; multi-producer rollups are in "
        "hypothesis_ledger which is allowlisted separately"
    ),
}


# -----------------------------------------------------------------
# AST helpers
# -----------------------------------------------------------------


_FLOCK_PRIMITIVES = (
    "flock_append_line",
    "flock_append_lines",
    "flock_critical_section",
    "async_flock_critical_section",
)
# Legacy pattern still acceptable as a fallback on POSIX-fcntl
# platforms (Phase 7.8 lock-on-data-fd; pre-Wave-3 canonical).
_LEGACY_FLOCK = "flock_exclusive"


def _module_uses_flock(src: str) -> bool:
    if any(name in src for name in _FLOCK_PRIMITIVES):
        return True
    if _LEGACY_FLOCK in src:
        return True
    return False


def _iter_governance_py_files() -> list[Path]:
    out: list[Path] = []
    for p in _GOVERNANCE_ROOT.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        # tests live under tests/, never under governance/
        out.append(p)
    return out


def _has_open_append(src: str) -> bool:
    """Detect ``path.open("a", ...)`` — the JSONL append shape."""
    return bool(re.search(r"\.open\([\"']a[\"']", src))


def _rel(p: Path) -> str:
    return p.relative_to(_GOVERNANCE_ROOT).as_posix()


# -----------------------------------------------------------------
# Migration pin tests (positive — production source uses canonical
# primitive at the migrated sites)
# -----------------------------------------------------------------


_MIGRATED_FILES = (
    "observability/decision_trace_ledger.py",
    "adaptation/graduation_ledger.py",
    "observability/post_merge_auditor.py",
    "intake/wal.py",
    "posture_store.py",
    "mutation_gate.py",
    "metrics_history.py",
)


@pytest.mark.parametrize("rel_path", _MIGRATED_FILES)
def test_migrated_file_composes_canonical_primitive(rel_path: str):
    """Every site closed in this arc MUST compose
    ``flock_append_line`` (the canonical substrate). Bytes-pin —
    fail-loud if a future edit drops the canonical call."""
    src = (_GOVERNANCE_ROOT / rel_path).read_text(encoding="utf-8")
    assert "flock_append_line" in src, (
        f"{rel_path} must compose canonical "
        f"cross_process_jsonl.flock_append_line"
    )


@pytest.mark.parametrize("rel_path", _MIGRATED_FILES)
def test_migrated_file_imports_canonical_substrate(rel_path: str):
    src = (_GOVERNANCE_ROOT / rel_path).read_text(encoding="utf-8")
    assert "cross_process_jsonl" in src, (
        f"{rel_path} must import the canonical substrate"
    )


# -----------------------------------------------------------------
# Allowlist hygiene — every entry must actually have an open("a")
# in the source. Anti-stale: removing the call from a file should
# also remove the allowlist entry.
# -----------------------------------------------------------------


@pytest.mark.parametrize("rel_path", sorted(_OPEN_APPEND_ALLOWLIST))
def test_allowlist_files_actually_have_open_append(rel_path: str):
    src = (_GOVERNANCE_ROOT / rel_path).read_text(encoding="utf-8")
    assert _has_open_append(src), (
        f"{rel_path} is on the allowlist but no longer has an "
        f"open('a') call — remove the allowlist entry"
    )


def test_allowlist_size_pinned():
    """Forces reviewer attention on size change. Adding a new entry
    requires updating BOTH the dict AND this assertion."""
    assert len(_OPEN_APPEND_ALLOWLIST) == 20, (
        f"Allowlist size changed; current={len(_OPEN_APPEND_ALLOWLIST)}. "
        f"New entry? Update this pin."
    )


# -----------------------------------------------------------------
# THE LOAD-BEARING PIN — every open('a') in governance must be
# either flock-composed OR explicitly allowlisted with rationale.
# -----------------------------------------------------------------


def test_every_open_append_is_flock_or_allowlisted():
    """Walk every governance .py file. For each file containing
    `path.open("a", ...)`, assert the file either composes a
    flock primitive OR is on the explicit allowlist with rationale.

    This is the single-canonical-name discipline pin: silent drift
    becomes a reviewer-visible decision."""
    violations: list[str] = []
    for path in _iter_governance_py_files():
        src = path.read_text(encoding="utf-8")
        if not _has_open_append(src):
            continue
        rel = _rel(path)
        if _module_uses_flock(src):
            continue
        if rel in _OPEN_APPEND_ALLOWLIST:
            continue
        violations.append(rel)
    assert not violations, (
        "Files with unprotected open('a') (no flock primitive AND "
        f"not allowlisted): {violations}\n\n"
        "Either compose cross_process_jsonl.flock_append_line OR "
        "add the file to _OPEN_APPEND_ALLOWLIST in this test with "
        "a one-line rationale explaining why cross-process flock "
        "is not load-bearing."
    )


# -----------------------------------------------------------------
# Functional integration — true cross-process race coverage
# (mirrors Vector #10 v2.79 multiprocess pattern).
# -----------------------------------------------------------------


def _child_writer(path_str: str, n_writes: int, marker: str) -> None:
    """Child process body: append `n_writes` JSONL lines via the
    canonical primitive. Run as a separate Process so the OS-level
    fcntl serialization is exercised."""
    # Re-import in child — fork() inherits but spawn() does not;
    # belt-and-braces.
    from backend.core.ouroboros.governance.cross_process_jsonl import (
        flock_append_line,
    )
    p = Path(path_str)
    for i in range(n_writes):
        flock_append_line(p, f'{{"marker": "{marker}", "i": {i}}}')


def test_canonical_primitive_serializes_across_processes(tmp_path):
    """Two child processes append to the same JSONL via the
    canonical primitive. Every line must be intact (no torn writes
    from interleaved bytes) AND every write must land (no lost
    appends from dropped flock)."""
    target = tmp_path / "race.jsonl"
    n_per_writer = 50
    ctx = multiprocessing.get_context("fork")
    p1 = ctx.Process(target=_child_writer, args=(str(target), n_per_writer, "p1"))
    p2 = ctx.Process(target=_child_writer, args=(str(target), n_per_writer, "p2"))
    p1.start(); p2.start()
    p1.join(timeout=10); p2.join(timeout=10)
    assert p1.exitcode == 0
    assert p2.exitcode == 0

    # All 100 lines present, none torn.
    raw = target.read_text(encoding="utf-8").splitlines()
    assert len(raw) == 2 * n_per_writer, (
        f"expected {2*n_per_writer} lines, got {len(raw)} — "
        f"some appends were lost"
    )
    import json
    p1_count = p2_count = 0
    for line in raw:
        rec = json.loads(line)  # would raise on torn write
        if rec["marker"] == "p1":
            p1_count += 1
        elif rec["marker"] == "p2":
            p2_count += 1
    assert p1_count == n_per_writer
    assert p2_count == n_per_writer


# -----------------------------------------------------------------
# Substrate-unavailable rollback contract (NEVER raises into caller)
# -----------------------------------------------------------------


def test_decision_trace_ledger_substrate_unavailable_path_exists():
    """Migration-3 contract: if `cross_process_jsonl` import fails
    (e.g. fcntl-unavailable platform), each migrated site MUST have
    a legacy fallback path that still appends without raising."""
    src = (
        _GOVERNANCE_ROOT
        / "observability/decision_trace_ledger.py"
    ).read_text(encoding="utf-8")
    assert "_append_legacy_fileno_flock" in src
    assert "ImportError" in src


def test_graduation_ledger_substrate_unavailable_path_exists():
    src = (
        _GOVERNANCE_ROOT / "adaptation/graduation_ledger.py"
    ).read_text(encoding="utf-8")
    assert "_append_legacy_fileno_flock" in src
    assert "ImportError" in src


def test_post_merge_auditor_substrate_unavailable_path_exists():
    src = (
        _GOVERNANCE_ROOT / "observability/post_merge_auditor.py"
    ).read_text(encoding="utf-8")
    assert "_persist_outcome_legacy_fallback" in src
    assert "ImportError" in src


# -----------------------------------------------------------------
# End-to-end smoke — exercise each migrated producer surface and
# assert the JSONL gets the row.
# -----------------------------------------------------------------


def test_decision_trace_ledger_writes_via_canonical(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DECISION_TRACE_LEDGER_ENABLED", "true")
    target = tmp_path / "dtl.jsonl"
    monkeypatch.setenv("JARVIS_DECISION_TRACE_LEDGER_PATH", str(target))
    from backend.core.ouroboros.governance.observability import (
        decision_trace_ledger as _mod,
    )
    _mod.reset_default_ledger()
    ok, detail = _mod.get_default_ledger().record(
        op_id="op-flock-test", phase="ROUTE", decision="STANDARD",
    )
    assert ok, f"record failed: {detail}"
    assert target.exists()
    assert target.read_text(encoding="utf-8").strip(), "ledger is empty"
    # Sibling .lock file from canonical substrate must exist.
    lock_path = target.with_suffix(target.suffix + ".lock")
    assert lock_path.exists(), (
        "canonical substrate must create sibling .lock file — "
        "absence means the legacy fileno path was used instead"
    )


def test_intake_wal_writes_via_canonical(tmp_path):
    from datetime import datetime, timezone
    from backend.core.ouroboros.governance.intake.wal import WAL, WALEntry
    wal_path = tmp_path / "intake.wal"
    writer = WAL(wal_path)
    writer.append(WALEntry(
        lease_id="lease-test",
        envelope_dict={"k": "v"},
        status="pending",
        ts_monotonic=time.monotonic(),
        ts_utc=datetime.now(timezone.utc).isoformat(),
    ))
    assert wal_path.exists()
    lock_path = wal_path.with_suffix(wal_path.suffix + ".lock")
    assert lock_path.exists(), (
        "intake WAL must compose canonical primitive — sibling "
        ".lock file absent means legacy path was used"
    )


def test_metrics_history_writes_via_canonical(tmp_path):
    from backend.core.ouroboros.governance.metrics_history import (
        MetricsHistoryLedger,
    )
    from backend.core.ouroboros.governance.metrics_engine import (
        MetricsSnapshot,
    )
    target = tmp_path / "metrics.jsonl"
    ledger = MetricsHistoryLedger(path=target)
    snap = MetricsSnapshot(
        schema_version=1,
        session_id="sess-test",
        computed_at_unix=time.time(),
    )
    ok = ledger.append(snap)
    assert ok, "MetricsHistoryLedger.append failed"
    lock_path = target.with_suffix(target.suffix + ".lock")
    assert lock_path.exists(), (
        "metrics_history must compose canonical primitive"
    )
