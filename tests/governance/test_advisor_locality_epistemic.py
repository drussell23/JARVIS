"""Targeted Locality Bounding + Epistemic Humility — Advisor degraded-scan repair.

Root cause (soak bt-2026-07-21-205755): on a COLD cache in a fresh
worktree the Advisor's global importer scan burned its entire budget
traversing the tree (``files_examined=1 elapsed_ms=39189.8``), then
FABRICATED ``blast=50`` presented downstream as measured evidence —
and the cooperative async path injected it into ``advise()`` without
the synthetic label, so fabricated data could satisfy the hard-BLOCK
predicates Slice 21 Fix B fenced synthetic values out of. The
fabricated cap was also written into the shared TTL cache, poisoning
every subsequent op on the same key.

This suite pins the structural repair:

* **Locality pivot.** Budget exhaustion on a mock massive repo
  triggers Targeted Locality Bounding — a bounded O(K) scan of the
  targets' dependency neighborhood (package dir + direct-importee
  dirs via the canonical ``reverse_dep_resolver`` extractor + test
  dirs) that returns an honest MEASURED LOWER BOUND, not the cap.
* **Epistemic humility.** When the localized scan ALSO cannot resolve
  (severe coldness), the advisor returns a NEUTRAL blast=0 with
  provenance ``unknown`` — no fabricated risk payload, no BLOCK on
  data nobody collected — and records a NOTIFY_APPLY escalation in
  the epistemic ledger.
* **GATE floor.** ``_advisor_epistemic_notify_floor`` (wired on BOTH
  GATE paths) converts the recorded uncertainty into a NOTIFY_APPLY
  minimum: stricter-wins, never downgrades, appealable.
* **Cache hygiene.** An unknown result is NEVER cached (the old code
  cached the fabricated cap, poisoning the key for the TTL).
* **Cold-root memo.** The first exhaustion per root is remembered;
  the next op skips the global burn and goes straight to the O(K)
  localized path.
* **Rollback honesty.** Locality master OFF restores the legacy cap
  return — but labeled ``synthetic_cap``, closing the Fix B bypass
  (a fabricated cap may contribute caution, never hard-BLOCK).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest

from backend.core.ouroboros.governance import advisor_locality, bounded_walker
from backend.core.ouroboros.governance import operation_advisor
from backend.core.ouroboros.governance.advisor_locality import (
    ESCALATION_NOTIFY_APPLY,
    PROVENANCE_LOCAL_LOWER_BOUND,
    PROVENANCE_MEASURED,
    PROVENANCE_SYNTHETIC,
    PROVENANCE_UNKNOWN,
    peek_blast_epistemics,
)
from backend.core.ouroboros.governance.operation_advisor import (
    AdvisoryDecision,
    OperationAdvisor,
    _BLAST_PROVENANCE_SHARED,
    _BLAST_RADIUS_CACHE_SHARED,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_shared_state():
    """Module-level shared stores must not leak between tests."""
    _BLAST_RADIUS_CACHE_SHARED.clear()
    _BLAST_PROVENANCE_SHARED.clear()
    advisor_locality._reset_cold_roots_for_tests()
    advisor_locality._reset_epistemic_ledger_for_tests()
    saved_oracle = operation_advisor._active_oracle
    operation_advisor._active_oracle = None
    yield
    _BLAST_RADIUS_CACHE_SHARED.clear()
    _BLAST_PROVENANCE_SHARED.clear()
    advisor_locality._reset_cold_roots_for_tests()
    advisor_locality._reset_epistemic_ledger_for_tests()
    operation_advisor._active_oracle = saved_oracle


@pytest.fixture
def cold_massive_repo(tmp_path: Path) -> Path:
    """Mock massive repository with a well-defined locality neighborhood.

    * ``pkg/mod_target.py`` — the mutation target (imports helpers.util,
      so ``helpers/`` becomes a direct-importee locality root).
    * 3 genuine importers, all INSIDE the locality neighborhood:
      ``pkg/uses_target.py``, ``helpers/consumer.py``,
      ``tests/test_mod_target.py``.
    * A large ``bulk/`` distractor forest representing the cold 30k-file
      worktree the global scan drowns in.
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "mod_target.py").write_text(
        "import helpers.util\n\n\ndef f():\n    return helpers.util.X\n"
    )
    (pkg / "uses_target.py").write_text("from pkg import mod_target\n")

    helpers = tmp_path / "helpers"
    helpers.mkdir()
    (helpers / "__init__.py").write_text("")
    (helpers / "util.py").write_text("X = 1\n")
    (helpers / "consumer.py").write_text("import pkg.mod_target\n")

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_mod_target.py").write_text("import pkg.mod_target\n")

    bulk = tmp_path / "bulk"
    for i in range(30):
        d = bulk / f"d{i:03d}"
        d.mkdir(parents=True)
        for j in range(5):
            (d / f"f{j}.py").write_text("# noise\n")
    return tmp_path


@pytest.fixture
def exhausted_global_scan(monkeypatch: pytest.MonkeyPatch):
    """Force deterministic global-scan budget exhaustion.

    Reproduces the soak's failure shape EXACTLY: the bounded walker
    burns the whole wall-clock budget traversing a cold tree and
    yields (almost) nothing. The timeout is floored to 0.1s and the
    patched walker sleeps past it, so the elapsed-time exhaustion
    heuristic fires on both scan paths. Locality's own walker binding
    (imported at advisor_locality module load) is untouched — the
    localized pivot still sees the real filesystem.
    """
    monkeypatch.setenv("JARVIS_BLAST_RADIUS_TIMEOUT_S", "0.1")

    def _starved_walker(root: Path, **kwargs) -> Iterator[str]:
        # A cold traversal: each directory entry costs real I/O time
        # and nothing useful surfaces — 25 x 10ms burns well past 95%
        # of the 0.1s budget while yielding only non-Python noise
        # (files_examined stays 0, mirroring the soak's
        # ``files_examined=1 elapsed_ms=39189.8`` shape).
        for i in range(25):
            time.sleep(0.01)
            yield str(Path(root) / f"cold_noise_{i}.txt")

    monkeypatch.setattr(bounded_walker, "iter_bounded_files", _starved_walker)
    yield


def _advisor(repo: Path) -> OperationAdvisor:
    return OperationAdvisor(project_root=repo)


TARGETS = ("pkg/mod_target.py",)


# ---------------------------------------------------------------------------
# 1. Targeted Locality Bounding — the O(N) -> O(K) pivot
# ---------------------------------------------------------------------------


async def test_budget_exhaustion_pivots_to_localized_lower_bound(
    cold_massive_repo: Path, exhausted_global_scan, caplog,
) -> None:
    """Cold-cache exhaustion must yield a MEASURED localized lower
    bound (the 3 neighborhood importers), never the fabricated cap."""
    caplog.set_level("INFO")
    advisor = _advisor(cold_massive_repo)

    advisory = await advisor.advise_async(
        TARGETS, "refactor mod_target", "op-locality-1",
    )

    assert advisory.blast_provenance == PROVENANCE_LOCAL_LOWER_BOUND
    # 3 genuine importers live in the neighborhood; the fabricated cap
    # was 50. Tolerate small over-count (substring matching) but the
    # cap must be gone.
    assert 1 <= advisory.blast_radius <= 10
    assert advisory.blast_radius != 50
    assert advisory.decision != AdvisoryDecision.BLOCK
    assert any(
        "pivoting to targeted locality bounding" in r.message
        for r in caplog.records
    )
    # No fabricated "50 files import these targets" narrative.
    assert not any("50 files import" in reason for reason in advisory.reasons)


async def test_localized_result_is_cached_with_provenance(
    cold_massive_repo: Path, exhausted_global_scan,
) -> None:
    """The localized lower bound is cached (honest value, honest
    provenance) — NOT the conservative cap."""
    advisor = _advisor(cold_massive_repo)
    advisory = await advisor.advise_async(
        TARGETS, "refactor mod_target", "op-locality-2",
    )

    key = (frozenset(TARGETS), str(cold_massive_repo))
    assert key in _BLAST_RADIUS_CACHE_SHARED
    _ts, cached_count = _BLAST_RADIUS_CACHE_SHARED[key]
    assert cached_count == advisory.blast_radius
    assert cached_count != 50
    assert _BLAST_PROVENANCE_SHARED[key] == PROVENANCE_LOCAL_LOWER_BOUND


async def test_cold_root_memo_skips_global_rescan(
    cold_massive_repo: Path, exhausted_global_scan, monkeypatch, caplog,
) -> None:
    """After one exhaustion the root is memoized cold: the NEXT op
    (different targets → cache miss) must go straight to the O(K)
    localized path without re-paying the global burn."""
    caplog.set_level("INFO")
    advisor = _advisor(cold_massive_repo)
    await advisor.advise_async(TARGETS, "first op", "op-cold-1")
    assert advisor_locality.is_cold_root(cold_massive_repo)

    def _must_not_run(root: Path, **kwargs) -> Iterator[str]:
        raise AssertionError(
            "global walker invoked despite cold-root memo"
        )

    monkeypatch.setattr(bounded_walker, "iter_bounded_files", _must_not_run)

    caplog.clear()
    advisory = await advisor.advise_async(
        ("pkg/uses_target.py",), "second op on cold tree", "op-cold-2",
    )
    assert advisory.blast_provenance in (
        PROVENANCE_LOCAL_LOWER_BOUND, PROVENANCE_UNKNOWN,
    )
    assert any(
        "blast_radius_cold_root_shortcut" in r.message
        for r in caplog.records
    )


async def test_sync_path_parity(
    cold_massive_repo: Path, exhausted_global_scan, monkeypatch,
) -> None:
    """The sync twin (cooperative master OFF → legacy executor path →
    sync ``advise``) must degrade identically — twin-path drift is the
    bug class the AST Parity Sentinel exists for."""
    monkeypatch.setenv("JARVIS_ADVISOR_BLAST_COOPERATIVE_ENABLED", "false")
    advisor = _advisor(cold_massive_repo)
    advisory = advisor.advise(
        TARGETS, "refactor mod_target (sync)", "op-sync-1",
    )
    assert advisory.blast_provenance == PROVENANCE_LOCAL_LOWER_BOUND
    assert 1 <= advisory.blast_radius <= 10
    assert advisory.decision != AdvisoryDecision.BLOCK


# ---------------------------------------------------------------------------
# 2. Epistemic Humility — unknown is neutral, never fabricated
# ---------------------------------------------------------------------------


@pytest.fixture
def unresolvable_locality(monkeypatch: pytest.MonkeyPatch):
    """Starve the LOCALIZED scan too: yield budget 1 means the walker
    returns before yielding a single candidate — zero files examined
    → epistemically unresolved."""
    monkeypatch.setenv("JARVIS_ADVISOR_LOCALITY_MAX_SCANNED", "1")
    yield


@pytest.fixture
def zero_coverage_repo(tmp_path: Path) -> Path:
    """Neighborhood exists but has NO tests — under the old fabrication
    (blast=50 + coverage=0) this shape hit the hard-BLOCK predicate."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "mod_target.py").write_text("def f():\n    return 1\n")
    (pkg / "uses_target.py").write_text("from pkg import mod_target\n")
    bulk = tmp_path / "bulk"
    for i in range(10):
        d = bulk / f"d{i:03d}"
        d.mkdir(parents=True)
        (d / "noise.py").write_text("# noise\n")
    return tmp_path


async def test_unresolved_scan_returns_neutral_unknown_not_block(
    zero_coverage_repo: Path, exhausted_global_scan, unresolvable_locality,
) -> None:
    """Severe coldness (global AND localized scans unresolved) must
    produce a neutral unknown — NOT the fabricated high-risk payload
    that previously satisfied BLOCK on zero-coverage targets."""
    advisor = _advisor(zero_coverage_repo)
    # Pin coverage to the old failure shape (blast=50 fabrication +
    # coverage=0 satisfied the hard-BLOCK predicate).
    advisor._compute_test_coverage = (  # type: ignore[method-assign]
        lambda *a, **k: 0.0
    )
    advisory = await advisor.advise_async(
        TARGETS, "refactor mod_target", "op-unknown-1",
    )

    assert advisory.blast_provenance == PROVENANCE_UNKNOWN
    assert advisory.blast_radius == 0
    # The old failure mode: coverage==0 + fabricated blast>=20 → BLOCK.
    assert advisory.decision != AdvisoryDecision.BLOCK
    # Honest narrative, no fabricated importer count.
    assert any("Blast radius UNKNOWN" in r for r in advisory.reasons)
    assert not any("High blast radius" in r for r in advisory.reasons)


async def test_unknown_is_never_cached(
    zero_coverage_repo: Path, exhausted_global_scan, unresolvable_locality,
) -> None:
    """An epistemically-unknown result must NOT poison the shared TTL
    cache (the old code cached the fabricated cap here)."""
    advisor = _advisor(zero_coverage_repo)
    await advisor.advise_async(TARGETS, "refactor", "op-unknown-2")
    key = (frozenset(TARGETS), str(zero_coverage_repo))
    assert key not in _BLAST_RADIUS_CACHE_SHARED
    assert key not in _BLAST_PROVENANCE_SHARED


async def test_unknown_records_notify_apply_escalation(
    zero_coverage_repo: Path, exhausted_global_scan, unresolvable_locality,
) -> None:
    """The epistemic ledger must carry the NOTIFY_APPLY escalation the
    GATE floor consumes."""
    advisor = _advisor(zero_coverage_repo)
    await advisor.advise_async(TARGETS, "refactor", "op-unknown-3")
    rec = peek_blast_epistemics("op-unknown-3")
    assert rec is not None
    assert rec["provenance"] == PROVENANCE_UNKNOWN
    assert rec["escalation"] == ESCALATION_NOTIFY_APPLY


# ---------------------------------------------------------------------------
# 3. GATE floor — uncertainty becomes NOTIFY_APPLY, never a block
# ---------------------------------------------------------------------------


async def test_gate_floor_escalates_unknown_to_notify_apply(
    zero_coverage_repo: Path, exhausted_global_scan, unresolvable_locality,
) -> None:
    """End-to-end: an advise() that resolves unknown must cause
    ``_advisor_epistemic_notify_floor`` to lift SAFE_AUTO →
    NOTIFY_APPLY (stricter-wins), and NEVER downgrade a stricter tier."""
    from backend.core.ouroboros.governance.orchestrator import (
        _advisor_epistemic_notify_floor,
    )
    from backend.core.ouroboros.governance.risk_engine import RiskTier

    advisor = _advisor(zero_coverage_repo)
    await advisor.advise_async(TARGETS, "refactor", "op-gate-1")

    ctx = SimpleNamespace(op_id="op-gate-1")
    tier, note = _advisor_epistemic_notify_floor(ctx, RiskTier.SAFE_AUTO)
    assert tier is RiskTier.NOTIFY_APPLY
    assert note is not None and "UNKNOWN" in note

    # Never downgrades an already-stricter tier.
    tier2, note2 = _advisor_epistemic_notify_floor(
        ctx, RiskTier.APPROVAL_REQUIRED,
    )
    assert tier2 is RiskTier.APPROVAL_REQUIRED
    assert note2 is not None  # visibility note still emitted


async def test_gate_floor_inert_without_ledger_entry() -> None:
    """No epistemic record → the floor is a no-op (fail-soft)."""
    from backend.core.ouroboros.governance.orchestrator import (
        _advisor_epistemic_notify_floor,
    )
    from backend.core.ouroboros.governance.risk_engine import RiskTier

    ctx = SimpleNamespace(op_id="op-that-never-advised")
    tier, note = _advisor_epistemic_notify_floor(ctx, RiskTier.SAFE_AUTO)
    assert tier is RiskTier.SAFE_AUTO
    assert note is None


async def test_gate_floor_master_off(
    zero_coverage_repo: Path, exhausted_global_scan, unresolvable_locality,
    monkeypatch,
) -> None:
    """``JARVIS_ADVISOR_EPISTEMIC_NOTIFY_ENABLED=false`` disarms the
    floor without touching the advisor's honesty properties."""
    from backend.core.ouroboros.governance.orchestrator import (
        _advisor_epistemic_notify_floor,
    )
    from backend.core.ouroboros.governance.risk_engine import RiskTier

    advisor = _advisor(zero_coverage_repo)
    await advisor.advise_async(TARGETS, "refactor", "op-gate-off")
    monkeypatch.setenv("JARVIS_ADVISOR_EPISTEMIC_NOTIFY_ENABLED", "false")
    ctx = SimpleNamespace(op_id="op-gate-off")
    tier, note = _advisor_epistemic_notify_floor(ctx, RiskTier.SAFE_AUTO)
    assert tier is RiskTier.SAFE_AUTO
    assert note is None


# ---------------------------------------------------------------------------
# 4. Rollback honesty + no-regression guarantees
# ---------------------------------------------------------------------------


async def test_locality_master_off_restores_cap_as_synthetic(
    zero_coverage_repo: Path, exhausted_global_scan, monkeypatch,
) -> None:
    """Locality master OFF: legacy cap value returns — but labeled
    synthetic, so it can no longer satisfy the hard-BLOCK predicate
    (closing the latent Slice 21 Fix B bypass on the cooperative
    path). Zero coverage + cap 50 previously BLOCKed."""
    monkeypatch.setenv("JARVIS_ADVISOR_LOCALITY_BOUNDING_ENABLED", "false")
    advisor = _advisor(zero_coverage_repo)
    # Pin the zero-coverage arm deterministically (the heuristic
    # coverage scan is not the subject here — the BLOCK predicate is).
    advisor._compute_test_coverage = (  # type: ignore[method-assign]
        lambda *a, **k: 0.0
    )
    advisory = await advisor.advise_async(
        TARGETS, "refactor mod_target", "op-legacy-1",
    )
    assert advisory.blast_radius == 50  # legacy conservative cap value
    assert advisory.blast_provenance == PROVENANCE_SYNTHETIC
    assert advisory.decision != AdvisoryDecision.BLOCK
    assert any("CAUTION (not blocked)" in r for r in advisory.reasons)


async def test_warm_scan_unchanged_measured_provenance(
    cold_massive_repo: Path,
) -> None:
    """A scan that completes inside its budget is byte-identical to
    the pre-repair behavior: real count, measured provenance, cached."""
    advisor = _advisor(cold_massive_repo)
    advisory = await advisor.advise_async(
        TARGETS, "refactor mod_target", "op-warm-1",
    )
    assert advisory.blast_provenance == PROVENANCE_MEASURED
    # 3 genuine importers exist repo-wide (uses_target, consumer,
    # test_mod_target); substring matching may see no more than that
    # plus the bulk noise files (which do not mention the target).
    assert advisory.blast_radius == 3
    key = (frozenset(TARGETS), str(cold_massive_repo))
    assert _BLAST_PROVENANCE_SHARED[key] == PROVENANCE_MEASURED
    # No cold-root memo, no epistemic escalation for a healthy scan.
    assert not advisor_locality.is_cold_root(cold_massive_repo)
    assert peek_blast_epistemics("op-warm-1") is None


async def test_locality_roots_are_bounded_o_k(
    cold_massive_repo: Path,
) -> None:
    """The derived neighborhood must be the targets' package, its
    direct importees, and test dirs — NEVER the bulk forest (that
    would be the O(N) scan again)."""
    roots = advisor_locality.derive_locality_roots(
        list(TARGETS), cold_massive_repo,
    )
    root_names = {r.name for r in roots}
    assert "pkg" in root_names        # target's package
    assert "helpers" in root_names    # direct importee
    assert "tests" in root_names      # conventional test dir
    assert not any("bulk" in str(r) for r in roots)
    assert len(roots) <= advisor_locality.locality_max_roots()


async def test_heartbeat_not_starved_during_localized_pivot(
    cold_massive_repo: Path, exhausted_global_scan, monkeypatch,
) -> None:
    """The cooperative discipline survives the pivot: a concurrent
    heartbeat must keep ticking while the (exhausted global +
    localized) scan runs on the loop."""
    # Tighten the canonical yield cadence so the ~25-item cold walk
    # gives the loop several scheduling slots (default is 64 — higher
    # than the whole walk).
    monkeypatch.setenv("JARVIS_EVENT_LOOP_YIELD_EVERY_N", "4")
    advisor = _advisor(cold_massive_repo)
    ticks = 0
    stop = asyncio.Event()

    async def _heartbeat() -> None:
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(0.005)

    hb = asyncio.ensure_future(_heartbeat())
    try:
        await advisor.advise_async(TARGETS, "refactor", "op-hb-1")
    finally:
        stop.set()
        await hb
    # The starved walker alone sleeps 0.25s; a starved loop would show
    # single-digit ticks. Generous floor to stay flake-proof.
    assert ticks >= 3
