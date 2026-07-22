"""Test-Cache-First boot hydration — the state-hashing blind-spot fix.

Mandated bulletproof #1: a ``.pytest_cache`` containing a persistent failure
must produce an enqueued (synthetic) repair signal at boot EVEN WHEN the
working-tree / Merkle diff sees zero changed files (``walk_changed=0``). This
is the exact foil from soak bt-2026-07-22-085824, where an already-committed
red test was invisible to the hash diff and no repair was ever dispatched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.intake.sensors.test_failure_sensor import (
    TestFailureSensor,
)
from backend.core.ouroboros.governance.intent.test_watcher import TestWatcher


class _RecordingRouter:
    """Minimal async router capturing ingested envelopes."""

    def __init__(self) -> None:
        self.ingested: list = []

    async def ingest(self, envelope) -> str:
        self.ingested.append(envelope)
        return "enqueued"


def _write_lastfailed(repo_root: Path, nodeids) -> None:
    cache = repo_root / ".pytest_cache" / "v" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    # pytest's own format: {nodeid: true}
    (cache / "lastfailed").write_text(
        json.dumps({nid: True for nid in nodeids}), encoding="utf-8"
    )


async def test_cache_first_enqueues_synthetic_signal_with_zero_merkle_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Attribution off → deterministic (no import tracing of a synthetic node).
    monkeypatch.setenv("JARVIS_TEST_SOURCE_ATTRIBUTION_ENABLED", "false")
    monkeypatch.setenv("JARVIS_TEST_FAILURE_CACHE_FIRST_ENABLED", "true")

    nodeid = "tests/governance/saga/test_topological_sort_tiebreak.py::test_x"
    _write_lastfailed(tmp_path, [nodeid])

    watcher = TestWatcher(repo="cache-first-repo", repo_path=str(tmp_path))
    router = _RecordingRouter()
    sensor = TestFailureSensor(
        repo="cache-first-repo", router=router, test_watcher=watcher
    )

    # The working tree is CLEAN — tmp_path is not a git repo, so
    # diff_working_tree yields nothing (walk_changed == 0). The ONLY reason a
    # signal can appear is the pytest-cache-first layer.
    changed = await watcher.diff_working_tree()
    assert changed == [], f"precondition: clean tree, got {changed}"

    ingested = await sensor.hydrate_on_boot()

    # A synthetic failure signal was enqueued despite the zero-diff tree.
    assert ingested >= 1, "cache-first must ingest the persistent red"
    assert len(router.ingested) == 1
    env = router.ingested[0]
    # The enqueued envelope targets the cached failing test file.
    targets = " ".join(getattr(env, "target_files", ()) or ())
    assert "test_topological_sort_tiebreak.py" in targets


async def test_cache_first_disabled_is_inert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVIS_TEST_FAILURE_CACHE_FIRST_ENABLED", "false")
    # Boot hydration off too, so the only possible source is the cache layer.
    monkeypatch.setenv("JARVIS_TESTWATCHER_BOOT_HYDRATION_ENABLED", "false")
    _write_lastfailed(tmp_path, ["tests/foo_test.py::test_y"])

    watcher = TestWatcher(repo="r", repo_path=str(tmp_path))
    router = _RecordingRouter()
    sensor = TestFailureSensor(repo="r", router=router, test_watcher=watcher)

    ingested = await sensor.hydrate_on_boot()
    assert ingested == 0
    assert router.ingested == []


async def test_cache_first_empty_cache_no_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No lastfailed file at all → nothing to seed, no crash.
    monkeypatch.setenv("JARVIS_TEST_FAILURE_CACHE_FIRST_ENABLED", "true")
    watcher = TestWatcher(repo="r", repo_path=str(tmp_path))
    router = _RecordingRouter()
    sensor = TestFailureSensor(repo="r", router=router, test_watcher=watcher)

    ingested = await sensor.hydrate_from_pytest_cache()
    assert ingested == 0
    assert router.ingested == []
