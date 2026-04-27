"""Slice 11.4 regression spine — Merkle Cartographer foundation.

Pins the foundation primitives — state store, tree hashing, async
walker, has_changed query, persistence, boot-loop protection. **No
consumers wired here**: Slice 11.4 ships the module isolated so a
critical bug here can't disturb production. Slice 11.5 wires it to
the FS event bridge; Slice 11.6 wires sensors.

Test categories:
  §1 Module authority (AST + import shape)
  §2 Env knobs + master flag
  §3 Hash primitives
  §4 MerkleNode serialization round-trip
  §5 MerkleStateStore disk round-trips
  §6 MerkleCartographer coordinator
  §7 Async walker — hashing + change detection
  §8 Boot-loop protection
  §9 Module singleton
"""
from __future__ import annotations

import ast
import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from backend.core.ouroboros.governance import (
    merkle_cartographer as mc,
)
from backend.core.ouroboros.governance.merkle_cartographer import (
    MerkleCartographer,
    MerkleNode,
    MerkleStateStore,
    MerkleTransitionRecord,
    SCHEMA_VERSION,
    hash_combine,
    hash_file_content,
)


SENTINEL_PATH = Path(mc.__file__)


# ---------------------------------------------------------------------------
# Fixtures — synthetic repo structure
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Build a small repo: backend/foo.py + tests/test_foo.py +
    docs/readme.md + an excluded venv/lib.py + an excluded .git/HEAD."""
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
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "readme.md").write_text("# Readme\n")
    venv = tmp_path / "venv"
    venv.mkdir()
    (venv / "lib.py").write_text("EXCLUDED = True\n")
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    return tmp_path


# ===========================================================================
# §1 — Module authority
# ===========================================================================


def test_module_top_level_imports_stdlib_only() -> None:
    """Pinned at AST level — top-level imports must be stdlib only.
    Prevents accidental orchestrator/policy coupling."""
    src = SENTINEL_PATH.read_text(encoding="utf-8")
    module = ast.parse(src)
    allowed = {
        "asyncio", "hashlib", "json", "logging", "os",
        "tempfile", "threading", "time", "dataclasses",
        "datetime", "pathlib", "typing", "__future__",
    }
    for node in module.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root in allowed, (
                    f"top-level import {alias.name} not in stdlib allowlist"
                )
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            assert root in allowed, (
                f"top-level from-import {node.module} not in stdlib allowlist"
            )


def test_no_orchestrator_or_gate_imports() -> None:
    src = SENTINEL_PATH.read_text(encoding="utf-8")
    forbidden = (
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.iron_gate",
        "from backend.core.ouroboros.governance.policy",
        "from backend.core.ouroboros.governance.gate",
        "from backend.core.ouroboros.governance.change_engine",
        "from backend.core.ouroboros.governance.candidate_generator",
    )
    for needle in forbidden:
        assert needle not in src, (
            f"Cartographer must not import {needle!r}"
        )


def test_schema_version_pinned() -> None:
    assert SCHEMA_VERSION == "merkle.1"


def test_module_exports() -> None:
    for name in (
        "MerkleCartographer", "MerkleNode", "MerkleStateStore",
        "MerkleTransitionRecord", "SCHEMA_VERSION",
        "is_cartographer_enabled", "get_default_cartographer",
        "hash_file_content", "hash_combine",
    ):
        assert name in mc.__all__, f"{name} not exported"


# ===========================================================================
# §2 — Env knobs + master flag
# ===========================================================================


def test_master_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", raising=False)
    assert mc.is_cartographer_enabled() is False


def test_master_flag_truthy(monkeypatch: pytest.MonkeyPatch) -> None:
    for val in ("1", "true", "yes", "on"):
        monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", val)
        assert mc.is_cartographer_enabled() is True


def test_state_dir_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(tmp_path))
    assert mc.state_dir() == tmp_path


def test_excluded_dirs_default_includes_critical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JARVIS_MERKLE_EXCLUDE_DIRS", raising=False)
    excl = mc.excluded_dirs()
    # Critical exclusions must be present.
    for d in ("venv", ".git", "node_modules", "__pycache__"):
        assert d in excl, (
            f"{d} must be in default exclusion list"
        )


def test_included_top_level_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JARVIS_MERKLE_INCLUDE_DIRS", raising=False)
    inc = mc.included_top_level_dirs()
    for d in ("backend", "tests", "scripts"):
        assert d in inc


def test_walk_concurrency_default_32(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JARVIS_MERKLE_WALK_CONCURRENCY", raising=False)
    assert mc.walk_concurrency() == 32


def test_state_max_age_default_7_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(
        "JARVIS_MERKLE_FORCE_REINDEX_AFTER_S", raising=False,
    )
    assert mc.state_max_age_s() == 604800.0


# ===========================================================================
# §3 — Hash primitives
# ===========================================================================


def test_hash_file_content_deterministic() -> None:
    content = b"hello world"
    h1 = hash_file_content(content)
    h2 = hash_file_content(content)
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_hash_file_content_distinguishes_changes() -> None:
    h1 = hash_file_content(b"hello")
    h2 = hash_file_content(b"hello!")
    assert h1 != h2


def test_hash_file_content_empty() -> None:
    h = hash_file_content(b"")
    assert h == hash_file_content(b"")
    assert len(h) == 64


def test_hash_combine_order_independent() -> None:
    """The Merkle invariant — child order must NOT affect parent hash."""
    h1 = hash_combine(["a", "b", "c"])
    h2 = hash_combine(["c", "b", "a"])
    h3 = hash_combine(["b", "a", "c"])
    assert h1 == h2 == h3


def test_hash_combine_distinguishes_children() -> None:
    """Any change in a child propagates."""
    h1 = hash_combine(["a", "b"])
    h2 = hash_combine(["a", "c"])
    assert h1 != h2


def test_hash_combine_empty() -> None:
    h = hash_combine([])
    assert len(h) == 64  # SHA-256 of nothing


# ===========================================================================
# §4 — MerkleNode serialization
# ===========================================================================


def test_node_to_json_round_trip_leaf() -> None:
    leaf = MerkleNode(
        relpath="foo.py", is_dir=False,
        hash="abc123", mtime=1234.5, size=42,
    )
    payload = leaf.to_json()
    rebuilt = MerkleNode.from_json(payload)
    assert rebuilt is not None
    assert rebuilt.relpath == "foo.py"
    assert rebuilt.is_dir is False
    assert rebuilt.hash == "abc123"
    assert rebuilt.mtime == 1234.5
    assert rebuilt.size == 42


def test_node_to_json_round_trip_dir() -> None:
    leaf_a = MerkleNode(relpath="x/a.py", is_dir=False, hash="h_a")
    leaf_b = MerkleNode(relpath="x/b.py", is_dir=False, hash="h_b")
    parent = MerkleNode(
        relpath="x", is_dir=True, hash="h_parent",
        children={"a.py": leaf_a, "b.py": leaf_b},
    )
    rebuilt = MerkleNode.from_json(parent.to_json())
    assert rebuilt is not None
    assert rebuilt.is_dir is True
    assert "a.py" in rebuilt.children
    assert "b.py" in rebuilt.children
    assert rebuilt.children["a.py"].hash == "h_a"


def test_node_from_json_rejects_garbage() -> None:
    assert MerkleNode.from_json({}) is not None  # empty dict OK
    # Bad numeric coercions should not crash, just return None
    bad = {"relpath": "x", "is_dir": False, "mtime": "not_a_number"}
    rebuilt = MerkleNode.from_json(bad)
    assert rebuilt is None


def test_all_leaf_paths_collects_descendants() -> None:
    leaf1 = MerkleNode(relpath="a/b/x.py", is_dir=False, hash="h1")
    leaf2 = MerkleNode(relpath="a/c/y.py", is_dir=False, hash="h2")
    inner_b = MerkleNode(
        relpath="a/b", is_dir=True, hash="hb",
        children={"x.py": leaf1},
    )
    inner_c = MerkleNode(
        relpath="a/c", is_dir=True, hash="hc",
        children={"y.py": leaf2},
    )
    root = MerkleNode(
        relpath="a", is_dir=True, hash="ha",
        children={"b": inner_b, "c": inner_c},
    )
    paths = root.all_leaf_paths()
    assert "a/b/x.py" in paths
    assert "a/c/y.py" in paths


# ===========================================================================
# §5 — MerkleStateStore disk round-trips
# ===========================================================================


def test_store_hydrate_empty_returns_none(tmp_path: Path) -> None:
    store = MerkleStateStore(directory=tmp_path)
    assert store.hydrate() is None


def test_store_round_trip(tmp_path: Path) -> None:
    store = MerkleStateStore(directory=tmp_path)
    leaf = MerkleNode(
        relpath="x.py", is_dir=False, hash="abc", size=10, mtime=99.5,
    )
    root = MerkleNode(
        relpath="", is_dir=True, hash="root_h",
        children={"x.py": leaf},
    )
    assert store.write_current(root) is True
    rehydrated = store.hydrate()
    assert rehydrated is not None
    assert rehydrated.hash == "root_h"
    assert "x.py" in rehydrated.children


def test_store_old_snapshot_cold_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Snapshots older than max-age must be rejected — defends
    against a cache that's been stable so long it's suspect."""
    monkeypatch.setenv(
        "JARVIS_MERKLE_FORCE_REINDEX_AFTER_S", "60",
    )
    store = MerkleStateStore(directory=tmp_path)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "written_at_epoch": time.time() - 3600,  # 1h ago, exceeds 60s
        "root": MerkleNode(relpath="", is_dir=True).to_json(),
    }
    store._ensure_dir()
    store.current_path.write_text(
        json.dumps(payload), encoding="utf-8",
    )
    assert store.hydrate() is None


def test_store_schema_mismatch_cold_starts(tmp_path: Path) -> None:
    store = MerkleStateStore(directory=tmp_path)
    payload = {
        "schema_version": "wrong",
        "written_at_epoch": time.time(),
        "root": {"is_dir": True},
    }
    store._ensure_dir()
    store.current_path.write_text(
        json.dumps(payload), encoding="utf-8",
    )
    assert store.hydrate() is None


def test_store_history_append_and_trim(tmp_path: Path) -> None:
    store = MerkleStateStore(directory=tmp_path, history_cap=5)
    for i in range(20):
        store.append_history(MerkleTransitionRecord(
            ts_epoch=time.time(),
            transition_kind="full_walk",
            files_total=i,
        ))
    lines = store.history_path.read_text(
        encoding="utf-8",
    ).splitlines()
    assert len(lines) == 5


def test_store_atomic_write_no_torn_state(tmp_path: Path) -> None:
    """Multiple writes must always leave a valid JSON file."""
    store = MerkleStateStore(directory=tmp_path)
    for i in range(10):
        leaf = MerkleNode(
            relpath="x.py", is_dir=False, hash=f"hash_{i}",
        )
        root = MerkleNode(
            relpath="", is_dir=True, hash=f"root_{i}",
            children={"x.py": leaf},
        )
        store.write_current(root)
    payload = json.loads(store.current_path.read_text("utf-8"))
    assert payload["schema_version"] == SCHEMA_VERSION


# ===========================================================================
# §6 — MerkleCartographer coordinator
# ===========================================================================


def test_cartographer_hydrate_empty_returns_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(tmp_path))
    c = MerkleCartographer(repo_root=tmp_path)
    assert c.hydrate() == 0


def test_cartographer_has_changed_off_returns_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When master flag is off, has_changed always returns True so
    sensors fall through to legacy O(N) scans (no false negatives)."""
    monkeypatch.delenv(
        "JARVIS_MERKLE_CARTOGRAPHER_ENABLED", raising=False,
    )
    c = MerkleCartographer(repo_root=tmp_path)
    assert c.has_changed(["backend"]) is True


def test_cartographer_snapshot_observability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(tmp_path))
    c = MerkleCartographer(repo_root=tmp_path)
    snap = c.snapshot()
    assert snap["schema_version"] == SCHEMA_VERSION
    assert snap["leaf_count"] == 0
    assert snap["root_hash"] == ""


# ===========================================================================
# §7 — Async walker
# ===========================================================================


@pytest.mark.asyncio
async def test_walker_hashes_real_files(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    c = MerkleCartographer(repo_root=repo)
    changed = await c.update_full()
    # First walk → everything is "changed" relative to empty.
    assert "backend/foo.py" in changed
    assert "backend/bar.py" in changed
    assert "backend/core/util.py" in changed
    assert "tests/test_foo.py" in changed
    # Excluded dirs should NOT be in the leaf set.
    leaves = c.snapshot()["leaf_count"]
    assert leaves >= 4


@pytest.mark.asyncio
async def test_walker_excludes_venv_and_git(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    c = MerkleCartographer(repo_root=repo)
    await c.update_full()
    leaves = c._build_leaf_index(c._root)  # noqa: SLF001
    assert "venv/lib.py" not in leaves
    assert ".git/HEAD" not in leaves


@pytest.mark.asyncio
async def test_walker_only_scans_included_dirs(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Top-level dirs not in included list shouldn't be scanned —
    verify by adding a top-level dir and confirming it's absent."""
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    extra = repo / "spurious_top"
    extra.mkdir()
    (extra / "noise.py").write_text("X = 1\n")
    c = MerkleCartographer(repo_root=repo)
    await c.update_full()
    leaves = c._build_leaf_index(c._root)  # noqa: SLF001
    assert "spurious_top/noise.py" not in leaves


@pytest.mark.asyncio
async def test_walker_detects_change_on_second_walk(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    c = MerkleCartographer(repo_root=repo)
    await c.update_full()
    # Modify a file
    (repo / "backend" / "foo.py").write_text("def foo(): return 999\n")
    # Second walk should detect the single change
    changed = await c.update_full()
    assert "backend/foo.py" in changed
    # bar.py wasn't touched → should NOT be in changed set
    assert "backend/bar.py" not in changed


@pytest.mark.asyncio
async def test_walker_detects_deletion(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    c = MerkleCartographer(repo_root=repo)
    await c.update_full()
    # Delete a file
    (repo / "backend" / "bar.py").unlink()
    changed = await c.update_full()
    # Deletion shows up in the changed set
    assert "backend/bar.py" in changed


@pytest.mark.asyncio
async def test_walker_persists_snapshot(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    c = MerkleCartographer(repo_root=repo)
    await c.update_full()
    # Snapshot file should exist on disk
    state_file = repo / "merkle_current.json"
    assert state_file.exists()
    payload = json.loads(state_file.read_text("utf-8"))
    assert payload["schema_version"] == SCHEMA_VERSION
    assert "root" in payload


@pytest.mark.asyncio
async def test_walker_records_history_row(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    c = MerkleCartographer(repo_root=repo)
    await c.update_full()
    history_file = repo / "merkle_history.jsonl"
    assert history_file.exists()
    rows = [
        json.loads(line)
        for line in history_file.read_text("utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["transition_kind"] == "full_walk"
    assert rows[0]["files_total"] >= 4


@pytest.mark.asyncio
async def test_walker_skips_symlinks(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Symlinks could create cycles; the walker must skip them."""
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    target = repo / "backend" / "foo.py"
    link = repo / "backend" / "foo_link.py"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks not supported on this fs")
    c = MerkleCartographer(repo_root=repo)
    await c.update_full()
    leaves = c._build_leaf_index(c._root)  # noqa: SLF001
    # The symlink relpath isn't in the index
    assert "backend/foo_link.py" not in leaves


# ===========================================================================
# §8 — Boot-loop protection (the marquee correctness pin)
# ===========================================================================


@pytest.mark.asyncio
async def test_boot_protection_hydrates_persisted_state(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Process A walks + persists. Process B fresh-init reads the
    same state dir + hydrates without re-walking — instant O(1)
    queries available immediately."""
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    # Process A
    a = MerkleCartographer(repo_root=repo)
    await a.update_full()
    leaf_count_a = a.snapshot()["leaf_count"]
    root_hash_a = a.snapshot()["root_hash"]
    # Process B — fresh sentinel, same state dir
    b = MerkleCartographer(repo_root=repo)
    loaded = b.hydrate()
    assert loaded == leaf_count_a
    assert b.snapshot()["root_hash"] == root_hash_a


# ===========================================================================
# §9 — Module singleton
# ===========================================================================


def test_default_cartographer_is_singleton(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(tmp_path))
    mc.reset_default_cartographer_for_tests()
    a = mc.get_default_cartographer(repo_root=tmp_path)
    b = mc.get_default_cartographer(repo_root=tmp_path)
    assert a is b
    mc.reset_default_cartographer_for_tests()


def test_default_cartographer_hydrates_on_first_call(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(repo))
    monkeypatch.setenv("JARVIS_MERKLE_CARTOGRAPHER_ENABLED", "true")
    mc.reset_default_cartographer_for_tests()
    # Pre-populate state via direct cartographer
    pre = MerkleCartographer(repo_root=repo)
    asyncio.run(pre.update_full())
    pre_count = pre.snapshot()["leaf_count"]
    # Now singleton accessor — should hydrate from same state dir
    s = mc.get_default_cartographer(repo_root=repo)
    assert s.snapshot()["leaf_count"] == pre_count
    mc.reset_default_cartographer_for_tests()


def test_cartographer_never_raises_on_unwritable_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disk-full / permission failures must NOT take down the
    cartographer — must degrade silently."""
    bad = tmp_path / "etc" / "passwd" / "subdir"
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(bad))
    c = MerkleCartographer(repo_root=tmp_path)
    # Must not raise.
    assert c.hydrate() == 0
    snap = c.snapshot()
    assert isinstance(snap, dict)
