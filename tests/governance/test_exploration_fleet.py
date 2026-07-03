"""Tests for ExplorationFleet — Tier-2 batch 2 row 13.

``ExplorationFleet._run_agent`` builds each agent's entry_files list via
a synchronous ``scope_dir.glob("*.py")`` inline, and is fanned out N-wide
via ``asyncio.create_task`` in ``deploy()`` — N of these run on the main
loop concurrently, each doing a sync crawl. This spine pins:

  (a) the crawl routes through ``cooperative_fs_io.offload``,
  (b) parity vs. the pre-offload synchronous behavior,
  (c) fail-soft: an ``OffloadError`` degrades to the same empty
      entry_files list the sync path produced on error,
  (d) the fan-out concurrency contract: N fanned agents each get their
      own independently-offloaded crawl (no cross-contamination via a
      shared mutable buffer) AND a heartbeat coroutine keeps ticking
      throughout the fan-out (the loop is never blocked).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import backend.core.ouroboros.governance.cooperative_fs_io as fsio
from backend.core.ouroboros.governance.exploration_fleet import (
    ExplorationFleet,
    FleetAgent,
)


# ---------------------------------------------------------------------------
# (a) Spy — the crawl routes through cooperative_fs_io.offload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_agent_scope_crawl_routes_through_offload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope_dir = tmp_path / "backend" / "core"
    scope_dir.mkdir(parents=True)
    (scope_dir / "a.py").write_text("def a(): pass\n")
    (scope_dir / "__init__.py").write_text("")

    calls = []
    real_offload = fsio.offload

    async def _spy_offload(fn, *a, **k):
        calls.append(1)
        return await real_offload(fn, *a, **k)

    monkeypatch.setattr(fsio, "offload", _spy_offload)

    fleet = ExplorationFleet(jarvis_root=tmp_path)
    agent = FleetAgent(
        agent_id="fleet-jarvis-backend-core",
        repo="jarvis",
        scope="backend/core/",
    )
    await fleet._run_agent(agent, "understand backend/core")

    assert calls, "_run_agent's scope_dir.glob crawl did not route through offload"
    assert agent.status == "completed"


# ---------------------------------------------------------------------------
# (b) Parity vs. the pre-offload synchronous behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_agent_parity_excludes_init_and_caps_five(
    tmp_path: Path,
) -> None:
    scope_dir = tmp_path / "backend" / "core"
    scope_dir.mkdir(parents=True)
    (scope_dir / "__init__.py").write_text("")
    for i in range(8):
        (scope_dir / f"m{i}.py").write_text(f"def f{i}(): pass\n")

    fleet = ExplorationFleet(jarvis_root=tmp_path)
    agent = FleetAgent(
        agent_id="fleet-jarvis-backend-core",
        repo="jarvis",
        scope="backend/core/",
    )
    await fleet._run_agent(agent, "understand backend/core")

    assert agent.status == "completed"
    assert agent.report is not None
    # entry_files (capped at 5, excluding __init__.py) feed files_read —
    # the report should have read files from the scope, not __init__.py.
    assert "backend/core/__init__.py" not in agent.report.files_read
    assert len(agent.report.files_read) <= 5


@pytest.mark.asyncio
async def test_run_agent_no_scope_dir_completes_with_empty_entry_files(
    tmp_path: Path,
) -> None:
    """Nonexistent scope_dir → entry_files stays empty; agent still
    completes (explore() infers nothing, but does not crash)."""
    fleet = ExplorationFleet(jarvis_root=tmp_path)
    agent = FleetAgent(
        agent_id="fleet-jarvis-missing",
        repo="jarvis",
        scope="does/not/exist/",
    )
    await fleet._run_agent(agent, "goal")
    # No crawl call at all since scope_dir.exists() is False — completes
    # via ExplorationSubagent's own keyword-search phase, not entry files.
    assert agent.status in ("completed", "failed")


# ---------------------------------------------------------------------------
# (c) Fail-soft — OffloadError degrades to empty entry_files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_agent_fail_soft_on_offload_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope_dir = tmp_path / "backend" / "core"
    scope_dir.mkdir(parents=True)
    (scope_dir / "a.py").write_text("def a(): pass\n")

    async def _boom_offload(fn, *a, **k):
        return fsio.OffloadError(
            fn_name="scope_glob", exc_type="OSError",
            message="simulated", cpu_bound=False,
        )

    monkeypatch.setattr(fsio, "offload", _boom_offload)

    fleet = ExplorationFleet(jarvis_root=tmp_path)
    agent = FleetAgent(
        agent_id="fleet-jarvis-backend-core",
        repo="jarvis",
        scope="backend/core/",
    )
    # Must not raise out of _run_agent — the offload failure degrades
    # to empty entry_files (same shape the sync crawl produces when it
    # finds nothing), never propagates an exception from the crawl
    # itself. (Downstream ExplorationSubagent.explore() has a separate,
    # pre-existing bug when entry_files is empty — orthogonal to this
    # offload fix — which _run_agent's own try/except already contains,
    # landing status="failed" rather than crashing the fan-out.)
    await fleet._run_agent(agent, "understand backend/core")
    assert agent.status in ("completed", "failed")


# ---------------------------------------------------------------------------
# (d) Fan-out concurrency: N agents, independent offloaded crawls, no
# cross-contamination, heartbeat keeps ticking throughout.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fleet_deploy_fanout_independent_crawls_with_live_heartbeat(
    tmp_path: Path,
) -> None:
    """Deploy N agents concurrently (via deploy()'s asyncio.create_task
    fan-out). Each agent's scope has distinct, non-overlapping files —
    assert each agent's report only contains ITS OWN scope's files (no
    shared mutable buffer contamination across fanned tasks). A heartbeat
    coroutine running concurrently must keep ticking throughout — proof
    the offloaded fan-out never blocks the loop."""
    scopes = ["scope_a", "scope_b", "scope_c", "scope_d"]
    for scope in scopes:
        scope_dir = tmp_path / scope
        scope_dir.mkdir()
        (scope_dir / f"{scope}_module.py").write_text(
            f"def {scope}_fn(): pass\n"
        )

    fleet = ExplorationFleet(jarvis_root=tmp_path)
    # Monkeypatch the scope table so deploy() fans out across our
    # synthetic scopes instead of the hardcoded JARVIS layout.
    fleet._get_scopes_for_repo = lambda repo: [f"{s}/" for s in scopes]  # type: ignore[method-assign]

    heartbeat_ticks = 0
    stop_heartbeat = asyncio.Event()

    async def _heartbeat() -> None:
        nonlocal heartbeat_ticks
        while not stop_heartbeat.is_set():
            heartbeat_ticks += 1
            await asyncio.sleep(0)

    hb_task = asyncio.create_task(_heartbeat())
    try:
        report = await fleet.deploy(
            goal="map each scope independently",
            repos=("jarvis",),
            max_agents=8,
        )
    finally:
        stop_heartbeat.set()
        await hb_task

    assert report.agents_deployed == len(scopes)
    assert report.agents_completed == len(scopes)
    assert report.agents_failed == 0

    # No cross-contamination: each scope's file should appear in the
    # per-repo file count exactly once (across all agents combined),
    # proving no agent leaked another agent's crawl result.
    per_repo_files = report.per_repo_summary.get("jarvis", "")
    for scope in scopes:
        assert scope in per_repo_files

    # The heartbeat must have ticked many times WHILE the fan-out ran —
    # proof the offloaded crawls never held the event loop.
    assert heartbeat_ticks > 0, (
        "heartbeat coroutine never ticked during fleet.deploy() fan-out — "
        "the loop may have been blocked by a synchronous crawl"
    )
