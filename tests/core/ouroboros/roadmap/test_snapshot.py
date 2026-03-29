"""Tests for RoadmapSnapshot and SnapshotFragment."""
from __future__ import annotations

import hashlib
import time

import pytest

from backend.core.ouroboros.roadmap.snapshot import (
    RoadmapSnapshot,
    SnapshotFragment,
    compute_snapshot_hash,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _frag(
    source_id: str = "spec:test",
    content_hash: str = "abc123",
    tier: int = 0,
    fragment_type: str = "spec",
) -> SnapshotFragment:
    return SnapshotFragment(
        source_id=source_id,
        uri=f"docs/{source_id}.md",
        tier=tier,
        content_hash=content_hash,
        fetched_at=1_700_000_000.0,
        mtime=1_699_999_000.0,
        title="Test Fragment",
        summary="First 500 chars of content.",
        fragment_type=fragment_type,
    )


# ---------------------------------------------------------------------------
# SnapshotFragment
# ---------------------------------------------------------------------------

class TestSnapshotFragment:
    def test_fragment_is_frozen(self):
        sf = _frag()
        with pytest.raises((AttributeError, dataclasses_frozen_error := AttributeError)):
            sf.source_id = "other"  # type: ignore[misc]

    def test_fragment_stores_fields(self):
        sf = _frag(source_id="git:jarvis:bounded", content_hash="deadbeef", tier=1)
        assert sf.source_id == "git:jarvis:bounded"
        assert sf.content_hash == "deadbeef"
        assert sf.tier == 1

    def test_invalid_tier_raises(self):
        with pytest.raises(ValueError, match="tier"):
            _frag(tier=99)

    def test_invalid_fragment_type_raises(self):
        with pytest.raises(ValueError, match="fragment_type"):
            SnapshotFragment(
                source_id="x",
                uri="x.md",
                tier=0,
                content_hash="aaa",
                fetched_at=0.0,
                mtime=0.0,
                title="X",
                summary="",
                fragment_type="unknown_type",
            )

    def test_empty_source_id_raises(self):
        with pytest.raises(ValueError, match="source_id"):
            SnapshotFragment(
                source_id="",
                uri="x.md",
                tier=0,
                content_hash="aaa",
                fetched_at=0.0,
                mtime=0.0,
                title="X",
                summary="",
                fragment_type="spec",
            )


# ---------------------------------------------------------------------------
# compute_snapshot_hash
# ---------------------------------------------------------------------------

class TestComputeSnapshotHash:
    def test_snapshot_hash_is_deterministic(self):
        frags = (_frag("a", "hash_a"), _frag("b", "hash_b"))
        h1 = compute_snapshot_hash(frags)
        h2 = compute_snapshot_hash(frags)
        assert h1 == h2

    def test_snapshot_hash_changes_with_content(self):
        frags_v1 = (_frag("a", "hash_a"),)
        frags_v2 = (_frag("a", "hash_b"),)
        assert compute_snapshot_hash(frags_v1) != compute_snapshot_hash(frags_v2)

    def test_snapshot_hash_order_independent(self):
        f1 = _frag("alpha", "hash_alpha")
        f2 = _frag("beta", "hash_beta")
        assert compute_snapshot_hash((f1, f2)) == compute_snapshot_hash((f2, f1))

    def test_snapshot_hash_canonical_format(self):
        """Verify the exact SHA-256 formula matches the spec."""
        f1 = _frag("spec:test", "deadbeef")
        f2 = _frag("plan:roadmap", "cafebabe")
        frags = (f1, f2)

        # Manually compute expected hash per spec formula
        lines = sorted(
            f"{sf.source_id}\t{sf.content_hash}"
            for sf in frags
        )
        payload = "\n".join(lines)
        expected = hashlib.sha256(payload.encode()).hexdigest()

        assert compute_snapshot_hash(frags) == expected

    def test_snapshot_hash_empty_fragments(self):
        """Empty fragment tuple should produce a deterministic hash."""
        h = compute_snapshot_hash(())
        assert isinstance(h, str)
        assert len(h) == 64  # hex SHA-256

    def test_snapshot_hash_tab_separator_prevents_collision(self):
        """'ab' + 'c' vs 'a' + 'bc' — tab separator must prevent same hash."""
        f_ab_c = SnapshotFragment(
            source_id="ab",
            uri="x",
            tier=0,
            content_hash="c",
            fetched_at=0.0,
            mtime=0.0,
            title="",
            summary="",
            fragment_type="spec",
        )
        f_a_bc = SnapshotFragment(
            source_id="a",
            uri="x",
            tier=0,
            content_hash="bc",
            fetched_at=0.0,
            mtime=0.0,
            title="",
            summary="",
            fragment_type="spec",
        )
        assert compute_snapshot_hash((f_ab_c,)) != compute_snapshot_hash((f_a_bc,))


# ---------------------------------------------------------------------------
# RoadmapSnapshot
# ---------------------------------------------------------------------------

class TestRoadmapSnapshot:
    def test_snapshot_version_increments(self):
        frags = (_frag("a", "hash_a"),)
        snap1 = RoadmapSnapshot.create(frags, previous_version=0, previous_hash=None)
        assert snap1.version == 1

        # New content → version increments again
        frags2 = (_frag("a", "hash_b"),)
        snap2 = RoadmapSnapshot.create(frags2, previous_version=snap1.version, previous_hash=snap1.content_hash)
        assert snap2.version == 2

    def test_snapshot_version_unchanged_if_same_hash(self):
        frags = (_frag("a", "hash_a"),)
        snap1 = RoadmapSnapshot.create(frags, previous_version=3, previous_hash=None)

        # Same content → version must NOT change
        snap2 = RoadmapSnapshot.create(frags, previous_version=snap1.version, previous_hash=snap1.content_hash)
        assert snap2.version == snap1.version

    def test_snapshot_tier_counts(self):
        frags = (
            _frag("a", "h1", tier=0),
            _frag("b", "h2", tier=0),
            _frag("c", "h3", tier=1),
            _frag("d", "h4", tier=3),
        )
        snap = RoadmapSnapshot.create(frags)
        assert snap.tier_counts[0] == 2
        assert snap.tier_counts[1] == 1
        assert snap.tier_counts[3] == 1
        assert 2 not in snap.tier_counts

    def test_snapshot_content_hash_matches_formula(self):
        frags = (_frag("spec:x", "abc"), _frag("plan:y", "def"))
        snap = RoadmapSnapshot.create(frags)
        assert snap.content_hash == compute_snapshot_hash(frags)

    def test_snapshot_created_at_is_recent(self):
        before = time.time()
        snap = RoadmapSnapshot.create((_frag(),))
        after = time.time()
        assert before <= snap.created_at <= after

    def test_snapshot_fragments_are_preserved(self):
        frags = (_frag("a", "h1"), _frag("b", "h2"))
        snap = RoadmapSnapshot.create(frags)
        assert set(snap.fragments) == set(frags)

    def test_snapshot_first_creation_version_is_one(self):
        snap = RoadmapSnapshot.create((_frag(),), previous_version=0, previous_hash=None)
        assert snap.version == 1

    def test_snapshot_version_starts_at_previous_when_no_change(self):
        frags = (_frag(),)
        snap1 = RoadmapSnapshot.create(frags, previous_version=7, previous_hash=None)
        snap2 = RoadmapSnapshot.create(frags, previous_version=snap1.version, previous_hash=snap1.content_hash)
        assert snap2.version == snap1.version == 8
