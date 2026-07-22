"""WAL Signal Liveness Tombstoning + Sandboxed Ephemeral Instantiation +
shipping-path target gate.

Root cause chain (soak bt-2026-07-22-005943): a WAL-replayed TestFailure
signal chased ``backend/soak_probes/soak_probe_math.py`` — an UNTRACKED file
the preemption shield stashed at boot, absent from every visible tree — and
the universal target-existence gate never ran because it was wired ONLY on
the inline orchestrator twin (the extracted ``generate_runner`` has been the
shipping GENERATE path since 2026-04-23: the wired-but-inert trap). The op
burned a full GENERATE round, then APPLY hard-ENOENT'd on the missing parent
directory in the ephemeral worktree.

This suite pins the three-layer repair:

1. **WAL tombstoning** (mandated edge case 1) — a replayed signal whose
   every target is absent from the observation root is cleanly tombstoned
   via the WAL's native status mechanism WITHOUT waking the orchestrator
   (nothing enqueued); live-target and untargeted signals replay untouched.
2. **Sandboxed Ephemeral Instantiation** (mandated edge case 2) — the
   ChangeEngine creates a nested parent chain and writes a genuinely-new
   file, strictly AFTER the canonical sandbox checks; escapes still raise.
3. **Shipping-path wiring** — the target-existence gate + its retry-feedback
   branch now live in ``generate_runner.py`` (AST pins), killing the
   inert-twin class this soak exposed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.change_engine import (
    ChangeEngine,
    ChangeRequest,
)
from backend.core.ouroboros.governance.ledger import OperationLedger
from backend.core.ouroboros.governance.risk_engine import (
    ChangeType,
    OperationProfile,
)


# ---------------------------------------------------------------------------
# 1. WAL Signal Liveness Tombstoning
# ---------------------------------------------------------------------------


def _make_router_skeleton(tmp_path: Path):
    """The minimal attribute surface ``_replay_wal`` touches — the real
    router's boot is out of scope for a replay-boundary unit test."""
    from backend.core.ouroboros.governance.intake.unified_intake_router import (
        UnifiedIntakeRouter,
    )
    from backend.core.ouroboros.governance.intake.wal import WAL

    router = UnifiedIntakeRouter.__new__(UnifiedIntakeRouter)
    router._wal = WAL(path=tmp_path / "intake.wal")
    router._queue = asyncio.Queue()
    router._priority_queue = None
    router._config = SimpleNamespace(project_root=tmp_path)
    return router


def _wal_entry(lease_id: str, target_files, tmp_path: Path):
    from backend.core.ouroboros.governance.intake.wal import WALEntry

    return WALEntry(
        lease_id=lease_id,
        envelope_dict={
            "schema_version": "2c.1",
            "source": "test_failure",
            "description": "ghost hunt",
            "target_files": list(target_files),
            "repo": "jarvis",
            "confidence": 0.9,
            "urgency": "normal",
            "dedup_key": f"dedup-{lease_id}",
            "causal_id": f"causal-{lease_id}",
            "signal_id": lease_id,
            "idempotency_key": f"idem-{lease_id}",
            "lease_id": lease_id,
            "evidence": {},
            "requires_human_ack": False,
            "submitted_at": 1.0,
        },
        status="pending",
        ts_monotonic=1.0,
        ts_utc="2026-07-22T00:00:00Z",
    )


async def test_ghost_signal_tombstoned_without_waking_orchestrator(
    tmp_path: Path,
) -> None:
    """Mandated edge case 1: a WAL signal pointing at a non-existent file
    is instantly tombstoned — nothing reaches the dispatch queue, and the
    WAL no longer reports it pending."""
    router = _make_router_skeleton(tmp_path)
    ghost = _wal_entry(
        "lease-ghost-1",
        ["backend/soak_probes/soak_probe_math.py"],  # exists nowhere
        tmp_path,
    )
    router._wal.append(ghost)
    assert len(router._wal.pending_entries()) == 1

    await router._replay_wal()

    assert router._queue.qsize() == 0, (
        "ghost signal reached the dispatch queue — orchestrator was woken"
    )
    assert router._wal.pending_entries() == [], (
        "ghost entry still pending — tombstone status not recorded"
    )


async def test_live_target_signal_replays_normally(tmp_path: Path) -> None:
    (tmp_path / "real_module.py").write_text("X = 1\n")
    router = _make_router_skeleton(tmp_path)
    router._wal.append(
        _wal_entry("lease-live-1", ["real_module.py"], tmp_path),
    )
    await router._replay_wal()
    assert router._queue.qsize() == 1


async def test_untargeted_signal_never_tombstoned(tmp_path: Path) -> None:
    """Untargeted envelopes (only the exempt sources may carry no
    target_files — swe_bench_pro is the documented contract) must never
    be touched by the liveness gate."""
    router = _make_router_skeleton(tmp_path)
    entry = _wal_entry("lease-untargeted", [], tmp_path)
    entry.envelope_dict["source"] = "swe_bench_pro"
    router._wal.append(entry)
    await router._replay_wal()
    assert router._queue.qsize() == 1


async def test_partial_liveness_keeps_signal(tmp_path: Path) -> None:
    """One live target keeps a multi-target signal actionable."""
    (tmp_path / "alive.py").write_text("X = 1\n")
    router = _make_router_skeleton(tmp_path)
    router._wal.append(
        _wal_entry("lease-partial", ["alive.py", "gone.py"], tmp_path),
    )
    await router._replay_wal()
    assert router._queue.qsize() == 1


async def test_liveness_master_off_replays_ghosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_WAL_TARGET_LIVENESS_ENABLED", "false")
    router = _make_router_skeleton(tmp_path)
    router._wal.append(_wal_entry("lease-legacy", ["gone.py"], tmp_path))
    await router._replay_wal()
    assert router._queue.qsize() == 1  # legacy behavior byte-identical


# ---------------------------------------------------------------------------
# 2. Sandboxed Ephemeral Instantiation
# ---------------------------------------------------------------------------


def _profile(target: Path) -> OperationProfile:
    return OperationProfile(
        files_affected=[target],
        change_type=ChangeType.MODIFY,
        blast_radius=1,
        crosses_repo_boundary=False,
        touches_security_surface=False,
        touches_supervisor=False,
        test_scope_confidence=1.0,
    )


async def test_engine_scaffolds_nested_ephemeral_directory(
    tmp_path: Path,
) -> None:
    """Mandated edge case 2: a new file in a nested, NOT-yet-existing
    package directory is written successfully — the parent chain is
    instantiated only after the canonical sandbox checks pass."""
    project_root = tmp_path / "worktree"
    project_root.mkdir()
    ledger = OperationLedger(storage_dir=tmp_path / "ledger")
    engine = ChangeEngine(project_root=project_root, ledger=ledger)

    target = project_root / "backend" / "new_pkg" / "sub" / "module.py"
    assert not target.parent.exists()

    result = await engine.execute(ChangeRequest(
        goal="scaffold a new nested module",
        target_file=target,
        proposed_content="NEW = True\n",
        profile=_profile(target),
        op_id="op-scaffold-1",
    ))

    assert result.success is True, f"execute failed: {result}"
    assert target.is_file()
    assert "NEW = True" in target.read_text()


async def test_engine_scaffold_never_escapes_sandbox(tmp_path: Path) -> None:
    """The instantiation runs strictly AFTER containment: an escaping
    target still dies in the sandbox and no directory is created outside."""
    project_root = tmp_path / "worktree"
    project_root.mkdir()
    outside = tmp_path / "outside"
    ledger = OperationLedger(storage_dir=tmp_path / "ledger")
    engine = ChangeEngine(project_root=project_root, ledger=ledger)

    target = outside / "evil" / "module.py"
    result = await engine.execute(ChangeRequest(
        goal="escape attempt",
        target_file=target,
        proposed_content="EVIL = True\n",
        profile=_profile(target),
        op_id="op-escape-1",
    ))

    assert result.success is False
    assert not outside.exists(), "sandbox escape scaffolded a directory"


# ---------------------------------------------------------------------------
# 3. Shipping-path wiring pins (the inert-twin killer)
# ---------------------------------------------------------------------------


def _runner_src() -> str:
    import inspect
    from backend.core.ouroboros.governance.phase_runners import generate_runner
    return Path(inspect.getfile(generate_runner)).read_text(encoding="utf-8")


def test_generate_runner_carries_target_gate() -> None:
    """The SHIPPING GENERATE path must run the target-existence gate —
    soak bt-2026-07-22-005943 proved the inline-only wiring was inert."""
    src = _runner_src()
    assert "_find_missing_targets," in src or "_find_missing_targets(" in src
    assert "asyncio.to_thread(" in src
    assert "_target_guard_universal_enabled()" in src
    assert "allow_new_files=" in src
    assert "_target_missing_error_message(" in src


def test_generate_runner_carries_retry_feedback_branch() -> None:
    src = _runner_src()
    assert "_TARGET_MISSING_PREFIX" in src
    assert "_target_missing_retry_feedback(" in src
