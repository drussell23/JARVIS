"""Slice 11.5 regression spine — incremental Merkle updates + bus subscriber.

Pins:
  §1 MerkleFsEvent dataclass shape
  §2 update_incremental — create / modify / delete / moved
  §3 Recompute propagation — leaf + ancestors only (O(log N))
  §4 Idempotency — same hash means no change
  §5 Excluded paths skipped
  §6 Cold-cache no-op
  §7 MerkleEventSubscriber — handle / flush / debounce / metrics
  §8 Bus subscription via duck-type protocol
  §9 NEVER raises — defensive surface
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List

import pytest

from backend.core.ouroboros.governance import (
    merkle_cartographer as mc,
)
from backend.core.ouroboros.governance.merkle_cartographer import (
    MerkleCartographer,
    MerkleEventSubscriber,
    MerkleFsEvent,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "foo.py").write_text("def foo(): return 1\n")
    (backend / "bar.py").write_text("def bar(): return 2\n")
    sub = backend / "core"
    sub.mkdir()
    (sub / "util.py").write_text("X = 42\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_foo.py").write_text("def test_foo(): pass\n")
    return tmp_path


@pytest.fixture
async def hydrated_cartographer(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> MerkleCartographer:
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    c = MerkleCartographer(repo_root=repo)
    await c.update_full()
    return c


# ===========================================================================
# §1 — MerkleFsEvent
# ===========================================================================


def test_event_shape() -> None:
    ev = MerkleFsEvent(kind="modified", relpath="backend/foo.py")
    assert ev.kind == "modified"
    assert ev.relpath == "backend/foo.py"
    assert ev.is_directory is False


def test_event_frozen() -> None:
    ev = MerkleFsEvent(kind="modified", relpath="backend/foo.py")
    with pytest.raises(Exception):
        ev.kind = "deleted"  # type: ignore[misc]


# ===========================================================================
# §2 — update_incremental — create / modify / delete
# ===========================================================================


@pytest.mark.asyncio
async def test_incremental_modify_changes_leaf_hash(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    c = MerkleCartographer(repo_root=repo)
    await c.update_full()

    initial_hash = c._leaf_index["backend/foo.py"]  # noqa: SLF001
    # Modify the file
    (repo / "backend" / "foo.py").write_text(
        "def foo(): return 999\n",
    )
    # Send incremental event
    changed = await c.update_incremental([
        MerkleFsEvent(kind="modified", relpath="backend/foo.py"),
    ])
    assert "backend/foo.py" in changed
    new_hash = c._leaf_index["backend/foo.py"]  # noqa: SLF001
    assert new_hash != initial_hash


@pytest.mark.asyncio
async def test_incremental_delete_removes_leaf(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    c = MerkleCartographer(repo_root=repo)
    await c.update_full()

    assert "backend/bar.py" in c._leaf_index  # noqa: SLF001
    # Delete the file
    (repo / "backend" / "bar.py").unlink()
    changed = await c.update_incremental([
        MerkleFsEvent(kind="deleted", relpath="backend/bar.py"),
    ])
    assert "backend/bar.py" in changed
    assert "backend/bar.py" not in c._leaf_index  # noqa: SLF001


@pytest.mark.asyncio
async def test_incremental_create_new_file(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    c = MerkleCartographer(repo_root=repo)
    await c.update_full()

    # Create new file
    (repo / "backend" / "new_module.py").write_text("Y = 1\n")
    changed = await c.update_incremental([
        MerkleFsEvent(kind="created", relpath="backend/new_module.py"),
    ])
    assert "backend/new_module.py" in changed
    assert (
        "backend/new_module.py" in c._leaf_index  # noqa: SLF001
    )


@pytest.mark.asyncio
async def test_incremental_propagates_root_hash(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Modifying ANY leaf must change the root hash."""
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    c = MerkleCartographer(repo_root=repo)
    await c.update_full()
    root_before = c._root.hash  # noqa: SLF001

    (repo / "backend" / "foo.py").write_text("def foo(): return 999\n")
    await c.update_incremental([
        MerkleFsEvent(kind="modified", relpath="backend/foo.py"),
    ])
    root_after = c._root.hash  # noqa: SLF001
    assert root_before != root_after


@pytest.mark.asyncio
async def test_incremental_empty_event_list_noop(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    c = MerkleCartographer(repo_root=repo)
    await c.update_full()
    changed = await c.update_incremental([])
    assert changed == set()


@pytest.mark.asyncio
async def test_incremental_persists_snapshot(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After incremental update, the on-disk snapshot is updated."""
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    c = MerkleCartographer(repo_root=repo)
    await c.update_full()
    snapshot_before = (repo / "merkle_current.json").stat().st_mtime
    await asyncio.sleep(0.01)

    (repo / "backend" / "foo.py").write_text("def foo(): return 5\n")
    await c.update_incremental([
        MerkleFsEvent(kind="modified", relpath="backend/foo.py"),
    ])
    snapshot_after = (repo / "merkle_current.json").stat().st_mtime
    assert snapshot_after > snapshot_before


@pytest.mark.asyncio
async def test_incremental_records_history_row(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    c = MerkleCartographer(repo_root=repo)
    await c.update_full()
    (repo / "backend" / "foo.py").write_text("def foo(): return 5\n")
    await c.update_incremental([
        MerkleFsEvent(kind="modified", relpath="backend/foo.py"),
    ])
    history_path = repo / "merkle_history.jsonl"
    assert history_path.exists()
    text = history_path.read_text(encoding="utf-8")
    assert '"transition_kind": "incremental"' in text


# ===========================================================================
# §3 — Idempotency
# ===========================================================================


@pytest.mark.asyncio
async def test_incremental_no_change_returns_empty_set(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Modifying a file with the SAME content (same hash) should
    not register as changed."""
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    c = MerkleCartographer(repo_root=repo)
    await c.update_full()
    # Re-write the SAME content
    (repo / "backend" / "foo.py").write_text(
        "def foo(): return 1\n",
    )
    changed = await c.update_incremental([
        MerkleFsEvent(kind="modified", relpath="backend/foo.py"),
    ])
    # Same content → same hash → not changed
    assert changed == set()


@pytest.mark.asyncio
async def test_incremental_dedupe_within_batch(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple events for the same path in one batch are deduped."""
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    c = MerkleCartographer(repo_root=repo)
    await c.update_full()
    (repo / "backend" / "foo.py").write_text("def foo(): return 5\n")
    changed = await c.update_incremental([
        MerkleFsEvent(kind="modified", relpath="backend/foo.py"),
        MerkleFsEvent(kind="modified", relpath="backend/foo.py"),
        MerkleFsEvent(kind="modified", relpath="backend/foo.py"),
    ])
    # Only one entry in changed set
    assert changed == {"backend/foo.py"}


# ===========================================================================
# §4 — Excluded paths skipped
# ===========================================================================


@pytest.mark.asyncio
async def test_incremental_excluded_dir_skipped(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    c = MerkleCartographer(repo_root=repo)
    await c.update_full()

    # Create a venv dir + file
    (repo / "venv").mkdir(exist_ok=True)
    (repo / "venv" / "lib.py").write_text("EXCLUDED = True\n")
    changed = await c.update_incremental([
        MerkleFsEvent(kind="created", relpath="venv/lib.py"),
    ])
    assert "venv/lib.py" not in changed


@pytest.mark.asyncio
async def test_incremental_unincluded_top_level_skipped(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Top-level dirs not in included_top_level_dirs are ignored."""
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    c = MerkleCartographer(repo_root=repo)
    await c.update_full()

    (repo / "spurious").mkdir()
    (repo / "spurious" / "noise.py").write_text("X = 1\n")
    changed = await c.update_incremental([
        MerkleFsEvent(kind="created", relpath="spurious/noise.py"),
    ])
    assert "spurious/noise.py" not in changed


# ===========================================================================
# §5 — Cold cache no-op
# ===========================================================================


@pytest.mark.asyncio
async def test_incremental_cold_cache_returns_empty(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a prior update_full, incremental returns empty set —
    caller must establish the baseline first."""
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    c = MerkleCartographer(repo_root=repo)
    # NO update_full — cold cache
    changed = await c.update_incremental([
        MerkleFsEvent(kind="modified", relpath="backend/foo.py"),
    ])
    assert changed == set()


# ===========================================================================
# §6 — MerkleEventSubscriber
# ===========================================================================


@pytest.mark.asyncio
async def test_subscriber_handle_buffers_events(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    c = MerkleCartographer(repo_root=repo)
    await c.update_full()
    sub = MerkleEventSubscriber(
        c, debounce_seconds=10.0, flush_threshold=100,
    )
    await sub.handle("fs.changed.modified", {
        "relative_path": "backend/foo.py",
        "is_directory": False,
    })
    # Event buffered, not yet flushed (high debounce + threshold)
    assert sub.metrics["events_received"] == 1
    assert sub.metrics["pending_count"] == 1
    assert sub.metrics["batches_flushed"] == 0


@pytest.mark.asyncio
async def test_subscriber_flush_drains_buffer(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    c = MerkleCartographer(repo_root=repo)
    await c.update_full()
    (repo / "backend" / "foo.py").write_text("def foo(): return 7\n")
    sub = MerkleEventSubscriber(
        c, debounce_seconds=10.0, flush_threshold=100,
    )
    await sub.handle("fs.changed.modified", {
        "relative_path": "backend/foo.py",
        "is_directory": False,
    })
    n = await sub.flush()
    assert n == 1
    assert sub.metrics["batches_flushed"] == 1
    assert sub.metrics["pending_count"] == 0


@pytest.mark.asyncio
async def test_subscriber_threshold_flush(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When pending count reaches flush_threshold, flush is auto-fired."""
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    c = MerkleCartographer(repo_root=repo)
    await c.update_full()
    sub = MerkleEventSubscriber(
        c, debounce_seconds=100.0, flush_threshold=2,
    )
    await sub.handle("fs.changed.modified", {
        "relative_path": "backend/foo.py",
        "is_directory": False,
    })
    await sub.handle("fs.changed.modified", {
        "relative_path": "backend/bar.py",
        "is_directory": False,
    })
    # threshold reached → flushed
    assert sub.metrics["pending_count"] == 0
    assert sub.metrics["batches_flushed"] >= 1


@pytest.mark.asyncio
async def test_subscriber_master_flag_off_handle_noop(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    monkeypatch.delenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", raising=False)
    c = MerkleCartographer(repo_root=repo)
    sub = MerkleEventSubscriber(c)
    await sub.handle("fs.changed.modified", {
        "relative_path": "backend/foo.py",
    })
    # Master flag off → no event recorded
    assert sub.metrics["events_received"] == 0


@pytest.mark.asyncio
async def test_subscriber_handle_topic_to_kind_mapping(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fs.changed.modified → kind="modified", etc."""
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    c = MerkleCartographer(repo_root=repo)
    await c.update_full()

    captured: List[MerkleFsEvent] = []
    original_update = c.update_incremental

    async def _capture(events):
        captured.extend(events)
        return await original_update(events)

    c.update_incremental = _capture  # type: ignore[assignment]

    sub = MerkleEventSubscriber(c, debounce_seconds=0.05)
    await sub.handle("fs.changed.deleted", {
        "relative_path": "backend/foo.py",
        "is_directory": False,
    })
    await sub.flush()
    assert len(captured) == 1
    assert captured[0].kind == "deleted"


@pytest.mark.asyncio
async def test_subscriber_skips_directory_create_modify(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Directory create/modify events have no leaf to hash — skip."""
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    c = MerkleCartographer(repo_root=repo)
    await c.update_full()
    sub = MerkleEventSubscriber(c, debounce_seconds=10.0)
    await sub.handle("fs.changed.created", {
        "relative_path": "backend/newdir",
        "is_directory": True,
    })
    assert sub.metrics["events_received"] == 0


@pytest.mark.asyncio
async def test_subscriber_ignores_payload_without_path(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    c = MerkleCartographer(repo_root=repo)
    sub = MerkleEventSubscriber(c, debounce_seconds=10.0)
    await sub.handle("fs.changed.modified", {})  # no path field
    assert sub.metrics["events_received"] == 0


# ===========================================================================
# §7 — Bus subscription via duck-type protocol
# ===========================================================================


class _FakeBus:
    """Duck-typed TrinityEventBus stand-in for tests."""

    def __init__(self) -> None:
        self.subscriptions: List[tuple] = []

    async def subscribe(self, topic_pattern: str, handler: Any) -> None:
        self.subscriptions.append((topic_pattern, handler))


@pytest.mark.asyncio
async def test_subscriber_subscribe_to_bus(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    c = MerkleCartographer(repo_root=repo)
    sub = MerkleEventSubscriber(c)
    bus = _FakeBus()
    ok = await sub.subscribe_to_bus(bus)
    assert ok is True
    assert len(bus.subscriptions) == 1
    pattern, handler = bus.subscriptions[0]
    assert pattern == "fs.changed.*"
    assert handler == sub.handle


@pytest.mark.asyncio
async def test_subscriber_subscribe_to_bus_handles_failure(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bus that raises must not propagate — subscriber returns False."""

    class _BrokenBus:
        async def subscribe(self, *args, **kwargs):
            raise RuntimeError("bus is broken")

    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    c = MerkleCartographer(repo_root=repo)
    sub = MerkleEventSubscriber(c)
    ok = await sub.subscribe_to_bus(_BrokenBus())
    assert ok is False


# ===========================================================================
# §8 — NEVER raises (defensive surface)
# ===========================================================================


@pytest.mark.asyncio
async def test_subscriber_handle_never_raises_on_garbage_payload(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    c = MerkleCartographer(repo_root=repo)
    sub = MerkleEventSubscriber(c)
    # Various garbage payloads must not raise
    await sub.handle("fs.changed.modified", None)
    await sub.handle("fs.changed.modified", 42)
    await sub.handle("fs.changed.modified", "string-payload")
    await sub.handle("not.fs.event", {"path": "x.py"})


@pytest.mark.asyncio
async def test_incremental_never_raises_on_garbage_event(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    c = MerkleCartographer(repo_root=repo)
    await c.update_full()
    # Empty relpath
    changed = await c.update_incremental([
        MerkleFsEvent(kind="modified", relpath=""),
    ])
    assert changed == set()
    # Path traversal — won't fall under included dirs
    changed = await c.update_incremental([
        MerkleFsEvent(kind="modified", relpath="../etc/passwd"),
    ])
    assert "../etc/passwd" not in changed


# ===========================================================================
# §9 — End-to-end: bus → subscriber → cartographer → updated state
# ===========================================================================


@pytest.mark.asyncio
async def test_e2e_bus_event_to_state_update(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full chain: simulated bus event publishes → subscriber
    handles → cartographer state on disk reflects the change."""
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    c = MerkleCartographer(repo_root=repo)
    await c.update_full()
    pre_hash = c._leaf_index["backend/foo.py"]  # noqa: SLF001
    sub = MerkleEventSubscriber(c, debounce_seconds=10.0)

    # Modify file
    (repo / "backend" / "foo.py").write_text(
        "def foo(): return 999\n",
    )
    # Dispatch event
    await sub.handle("fs.changed.modified", {
        "relative_path": "backend/foo.py",
        "is_directory": False,
        "extension": ".py",
    })
    await sub.flush()
    post_hash = c._leaf_index["backend/foo.py"]  # noqa: SLF001
    assert pre_hash != post_hash
