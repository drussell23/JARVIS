"""tests/governance/autonomy/test_snapshot_manager.py

TDD tests for SnapshotManager — pre-operation snapshot utility for L3 SafetyNet.

Covers:
- FileSnapshot: to_dict excludes content, content_hash is valid SHA-256
- RestorePoint: file_count property, get_snapshot lookup, to_dict structure
- SnapshotManager: create/get/list snapshots and restore points, pruning,
  content retrieval, git_ref preservation, hash integrity, to_dict summary
"""
from __future__ import annotations

import hashlib

import pytest

from backend.core.ouroboros.governance.autonomy.snapshot_manager import (
    FileSnapshot,
    RestorePoint,
    SnapshotManager,
)


# ---------------------------------------------------------------------------
# FileSnapshot tests
# ---------------------------------------------------------------------------


class TestFileSnapshot:
    def test_to_dict_excludes_content(self):
        """to_dict serialization must NOT include the 'content' key."""
        snap = FileSnapshot(
            snapshot_id="snap-001",
            file_path="/tmp/foo.py",
            content_hash="abc123",
            content="print('hello')",
        )
        d = snap.to_dict()
        assert "content" not in d
        assert d["snapshot_id"] == "snap-001"
        assert d["file_path"] == "/tmp/foo.py"
        assert d["content_hash"] == "abc123"

    def test_snapshot_has_hash(self):
        """content_hash must be a valid 64-character lowercase hex SHA-256."""
        content = "def greet(): pass"
        expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        mgr = SnapshotManager()
        snap = mgr.create_snapshot("/tmp/greet.py", content)
        assert snap.content_hash == expected_hash
        assert len(snap.content_hash) == 64
        # Verify it is valid hex
        int(snap.content_hash, 16)


# ---------------------------------------------------------------------------
# RestorePoint tests
# ---------------------------------------------------------------------------


class TestRestorePoint:
    def test_file_count_property(self):
        """file_count should equal the number of snapshots."""
        snaps = [
            FileSnapshot(
                snapshot_id=f"s{i}",
                file_path=f"/tmp/f{i}.py",
                content_hash="h",
                content="c",
            )
            for i in range(3)
        ]
        rp = RestorePoint(
            restore_id="rp-1",
            name="test",
            snapshots=snaps,
        )
        assert rp.file_count == 3

    def test_get_snapshot_found(self):
        """get_snapshot returns the snapshot matching a given file path."""
        snap = FileSnapshot(
            snapshot_id="s1",
            file_path="/tmp/target.py",
            content_hash="h",
            content="c",
        )
        rp = RestorePoint(
            restore_id="rp-1",
            name="test",
            snapshots=[snap],
        )
        result = rp.get_snapshot("/tmp/target.py")
        assert result is snap

    def test_get_snapshot_not_found(self):
        """get_snapshot returns None when path does not match."""
        snap = FileSnapshot(
            snapshot_id="s1",
            file_path="/tmp/a.py",
            content_hash="h",
            content="c",
        )
        rp = RestorePoint(
            restore_id="rp-1",
            name="test",
            snapshots=[snap],
        )
        assert rp.get_snapshot("/tmp/b.py") is None

    def test_to_dict_structure(self):
        """to_dict must include restore_id, name, and file_count keys."""
        rp = RestorePoint(
            restore_id="rp-42",
            name="before-refactor",
        )
        d = rp.to_dict()
        assert d["restore_id"] == "rp-42"
        assert d["name"] == "before-refactor"
        assert "file_count" in d
        assert d["file_count"] == 0


# ---------------------------------------------------------------------------
# SnapshotManager tests
# ---------------------------------------------------------------------------


class TestSnapshotManager:
    def test_create_snapshot(self):
        """create_snapshot returns a FileSnapshot with correct SHA-256 hash."""
        mgr = SnapshotManager()
        content = "x = 42"
        snap = mgr.create_snapshot("/tmp/x.py", content)
        expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
        assert snap.content_hash == expected
        assert snap.file_path == "/tmp/x.py"
        assert snap.content == content
        assert snap.snapshot_id  # non-empty

    def test_create_restore_point(self):
        """create_restore_point returns a RestorePoint with correct file count."""
        mgr = SnapshotManager()
        files = {
            "/tmp/a.py": "a = 1",
            "/tmp/b.py": "b = 2",
            "/tmp/c.py": "c = 3",
        }
        rp = mgr.create_restore_point("test-rp", files)
        assert rp.file_count == 3
        assert rp.name == "test-rp"
        assert rp.restore_id  # non-empty

    def test_get_restore_point(self):
        """create then get returns the same RestorePoint object."""
        mgr = SnapshotManager()
        rp = mgr.create_restore_point("rp-1", {"/tmp/f.py": "content"})
        retrieved = mgr.get_restore_point(rp.restore_id)
        assert retrieved is rp

    def test_get_restore_point_not_found(self):
        """get_restore_point with a bogus ID returns None."""
        mgr = SnapshotManager()
        assert mgr.get_restore_point("nonexistent-id") is None

    def test_get_file_content(self):
        """Content stored in a restore point can be retrieved by file path."""
        mgr = SnapshotManager()
        rp = mgr.create_restore_point(
            "rp-content",
            {"/tmp/hello.py": "print('hello')"},
        )
        content = mgr.get_file_content(rp.restore_id, "/tmp/hello.py")
        assert content == "print('hello')"

    def test_get_file_content_missing_file(self):
        """get_file_content returns None for a file not in the restore point."""
        mgr = SnapshotManager()
        rp = mgr.create_restore_point("rp", {"/tmp/a.py": "a"})
        assert mgr.get_file_content(rp.restore_id, "/tmp/missing.py") is None

    def test_get_file_content_missing_restore_point(self):
        """get_file_content returns None for a nonexistent restore point."""
        mgr = SnapshotManager()
        assert mgr.get_file_content("bad-id", "/tmp/a.py") is None

    def test_list_restore_points(self):
        """list_restore_points returns serialized dicts for all stored points."""
        mgr = SnapshotManager()
        mgr.create_restore_point("rp-1", {"/tmp/a.py": "a"})
        mgr.create_restore_point("rp-2", {"/tmp/b.py": "b"})
        mgr.create_restore_point("rp-3", {"/tmp/c.py": "c"})
        listing = mgr.list_restore_points()
        assert len(listing) == 3
        # Each entry is a dict (from to_dict), not a RestorePoint
        assert all(isinstance(d, dict) for d in listing)
        names = {d["name"] for d in listing}
        assert names == {"rp-1", "rp-2", "rp-3"}

    def test_prune_oldest(self):
        """When capacity is exceeded, oldest restore points are pruned."""
        max_rp = 5
        mgr = SnapshotManager(max_restore_points=max_rp)
        ids = []
        for i in range(max_rp + 5):
            rp = mgr.create_restore_point(f"rp-{i}", {f"/tmp/{i}.py": str(i)})
            ids.append(rp.restore_id)
        # Only max_rp should remain
        listing = mgr.list_restore_points()
        assert len(listing) == max_rp
        # The oldest 5 should be gone
        for old_id in ids[:5]:
            assert mgr.get_restore_point(old_id) is None
        # The newest max_rp should still be present
        for new_id in ids[5:]:
            assert mgr.get_restore_point(new_id) is not None

    def test_to_dict_summary(self):
        """to_dict returns a summary with total_restore_points and total_snapshots."""
        mgr = SnapshotManager()
        mgr.create_restore_point("rp-1", {"/tmp/a.py": "a", "/tmp/b.py": "b"})
        mgr.create_restore_point("rp-2", {"/tmp/c.py": "c"})
        d = mgr.to_dict()
        assert d["total_restore_points"] == 2
        assert d["total_snapshots"] == 3

    def test_create_restore_point_with_git_ref(self):
        """git_ref is preserved on the restore point."""
        mgr = SnapshotManager()
        rp = mgr.create_restore_point(
            "rp-git",
            {"/tmp/g.py": "g"},
            git_ref="abc123def456",
        )
        assert rp.git_ref == "abc123def456"
        # Also appears in to_dict
        d = rp.to_dict()
        assert d["git_ref"] == "abc123def456"

    def test_content_hash_integrity(self):
        """The stored hash matches a fresh hashlib.sha256 computation."""
        mgr = SnapshotManager()
        content = "class Foo:\n    pass\n"
        rp = mgr.create_restore_point("rp-hash", {"/tmp/foo.py": content})
        snap = rp.get_snapshot("/tmp/foo.py")
        assert snap is not None
        expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
        assert snap.content_hash == expected

    def test_create_snapshot_with_metadata(self):
        """Metadata is stored and appears in to_dict."""
        mgr = SnapshotManager()
        meta = {"op_id": "op-42", "brain_id": "qwen_coder"}
        snap = mgr.create_snapshot("/tmp/m.py", "x", metadata=meta)
        assert snap.metadata == meta
        assert snap.to_dict()["metadata"] == meta

    def test_create_restore_point_with_metadata(self):
        """Metadata on a restore point is preserved."""
        mgr = SnapshotManager()
        meta = {"phase": "VALIDATE"}
        rp = mgr.create_restore_point("rp-meta", {"/tmp/m.py": "m"}, metadata=meta)
        assert rp.metadata == meta

    def test_max_snapshots_per_point_enforced(self):
        """Exceeding max_snapshots_per_point raises ValueError."""
        mgr = SnapshotManager(max_snapshots_per_point=2)
        files = {f"/tmp/{i}.py": str(i) for i in range(5)}
        with pytest.raises(ValueError, match="exceeds.*max"):
            mgr.create_restore_point("too-many", files)

    def test_snapshot_timestamp_is_monotonic_ns(self):
        """Timestamps use monotonic_ns (positive integer)."""
        mgr = SnapshotManager()
        snap = mgr.create_snapshot("/tmp/ts.py", "t")
        assert isinstance(snap.timestamp_ns, int)
        assert snap.timestamp_ns > 0
