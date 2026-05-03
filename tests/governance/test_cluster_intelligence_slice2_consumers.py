"""ClusterIntelligence-CrossSession Slice 2 -- regression spine.

Pins the consumer surfaces for Slice 1's representative_paths
field: codebase_character projection + to_prompt_section + the
ProactiveExploration sensor's envelope target_files routing.

Coverage:
  * ClusterCharacter additive field default + to_dict round-trip
  * compute_codebase_character defensive getattr projection
    (older ClusterInfo without the field still projects safely)
  * Garbage path entries in source ClusterInfo dropped on
    projection (None / non-string / empty)
  * to_prompt_section "Files: ..." line appears only when paths
    non-empty (backward-compat: empty -> line absent)
  * Sub-flag asymmetric env semantics (default false until
    Slice 5)
  * Envelope cap clamping (floor/ceiling/garbage)
  * ProactiveExploration sensor envelope wiring:
    - sub-flag off -> sentinel ``(".",)`` regardless of paths
    - sub-flag on + paths empty -> sentinel ``(".",)`` (Slice 1
      master off path)
    - sub-flag on + paths populated -> target_files == paths
      (truncated to env cap)
    - description hint reflects path-aware vs sentinel routing
    - evidence carries target_files_source +
      representative_paths_count for downstream observability
"""
from __future__ import annotations

import os
import sys
from typing import List, Tuple
from unittest import mock

import pytest

from backend.core.ouroboros.governance.codebase_character import (
    ClusterCharacter,
    CodebaseCharacterSnapshot,
    DigestOutcome,
    compute_codebase_character,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for var in (
        "JARVIS_PROACTIVE_EXPLORATION_USE_REPRESENTATIVE_PATHS",
        "JARVIS_EXPLORATION_REPRESENTATIVE_PATH_CAP",
        "JARVIS_EXPLORATION_CLUSTER_EMIT_PER_SCAN",
        "JARVIS_CODEBASE_CHARACTER_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)
    # Force codebase_character on so projection runs in tests.
    monkeypatch.setenv("JARVIS_CODEBASE_CHARACTER_ENABLED", "true")
    # Lower min_clusters threshold so single-cluster fixtures
    # produce READY snapshots (default is 2).
    monkeypatch.setenv("JARVIS_CODEBASE_CHARACTER_MIN_CLUSTERS", "1")


class _FakeCluster:
    """Duck-typed _ClusterLike stand-in for projection tests."""
    def __init__(
        self, *,
        cluster_id: int = 0,
        kind: str = "goal",
        size: int = 5,
        nearest_item_text: str = "voice biometric authentication primitive",
        nearest_item_source: str = "git_commit",
        source_composition: Tuple[Tuple[str, int], ...] = (
            ("git_commit", 5),
        ),
        centroid_hash8: str = "abc12345",
        representative_paths=None,
    ):
        self.cluster_id = cluster_id
        self.kind = kind
        self.size = size
        self.nearest_item_text = nearest_item_text
        self.nearest_item_source = nearest_item_source
        self.source_composition = source_composition
        self.centroid_hash8 = centroid_hash8
        if representative_paths is not None:
            self.representative_paths = representative_paths
        # else: simulate older ClusterInfo without the field at all


# ---------------------------------------------------------------------------
# ClusterCharacter additive field
# ---------------------------------------------------------------------------


class TestClusterCharacterAdditive:
    def test_default_empty_tuple(self):
        cc = ClusterCharacter(
            cluster_id=0, kind="goal", size=1,
            theme_label="x", nearest_item_excerpt="x",
            nearest_item_source="git_commit",
            source_composition=(),
            centroid_hash8="aaaaaaaa",
        )
        assert cc.representative_paths == ()
        assert isinstance(cc.representative_paths, tuple)

    def test_to_dict_serializes_paths(self):
        cc = ClusterCharacter(
            cluster_id=0, kind="goal", size=1,
            theme_label="x", nearest_item_excerpt="x",
            nearest_item_source="git_commit",
            source_composition=(),
            centroid_hash8="aaaaaaaa",
            representative_paths=("a.py", "b.py"),
        )
        d = cc.to_dict()
        assert d["representative_paths"] == ["a.py", "b.py"]

    def test_to_dict_defaults_to_empty_list(self):
        cc = ClusterCharacter(
            cluster_id=0, kind="goal", size=1,
            theme_label="x", nearest_item_excerpt="x",
            nearest_item_source="git_commit",
            source_composition=(),
            centroid_hash8="aaaaaaaa",
        )
        assert cc.to_dict()["representative_paths"] == []


# ---------------------------------------------------------------------------
# compute_codebase_character projection (defensive getattr)
# ---------------------------------------------------------------------------


class TestProjection:
    def test_paths_threaded_through(self):
        cluster = _FakeCluster(
            representative_paths=("voice/auth.py", "voice/util.py"),
        )
        snap = compute_codebase_character(
            enabled=True, clusters=[cluster],
            cluster_mode="kmeans",
            total_corpus_items=10,
            built_at_ts=1700000000.0,
            generated_at_ts=1700000100.0,
        )
        assert snap.outcome is DigestOutcome.READY
        assert snap.clusters[0].representative_paths == (
            "voice/auth.py", "voice/util.py",
        )

    def test_missing_attribute_defaults_empty(self):
        # Older ClusterInfo without representative_paths attribute.
        cluster = _FakeCluster()
        # Verify the simulated absence
        assert not hasattr(cluster, "representative_paths")
        snap = compute_codebase_character(
            enabled=True, clusters=[cluster],
            cluster_mode="kmeans",
            total_corpus_items=10,
            built_at_ts=1700000000.0,
            generated_at_ts=1700000100.0,
        )
        assert snap.outcome is DigestOutcome.READY
        assert snap.clusters[0].representative_paths == ()

    def test_garbage_entries_dropped(self):
        # Mixed valid + garbage (None, non-string, empty).
        cluster = _FakeCluster(
            representative_paths=(
                "ok.py", None, 42, "", "another.py",
            ),
        )
        snap = compute_codebase_character(
            enabled=True, clusters=[cluster],
            cluster_mode="kmeans",
            total_corpus_items=10,
            built_at_ts=1700000000.0,
            generated_at_ts=1700000100.0,
        )
        assert snap.clusters[0].representative_paths == (
            "ok.py", "another.py",
        )

    def test_empty_tuple_preserved(self):
        cluster = _FakeCluster(representative_paths=())
        snap = compute_codebase_character(
            enabled=True, clusters=[cluster],
            cluster_mode="kmeans",
            total_corpus_items=10,
            built_at_ts=1700000000.0,
            generated_at_ts=1700000100.0,
        )
        assert snap.clusters[0].representative_paths == ()


# ---------------------------------------------------------------------------
# to_prompt_section — Files: ... line conditional rendering
# ---------------------------------------------------------------------------


class TestRenderPromptBlock:
    def _snap(self, *, paths=()) -> CodebaseCharacterSnapshot:
        cluster = _FakeCluster(representative_paths=paths)
        return compute_codebase_character(
            enabled=True, clusters=[cluster],
            cluster_mode="kmeans",
            total_corpus_items=10,
            built_at_ts=1700000000.0,
            generated_at_ts=1700000100.0,
        )

    def test_files_line_appears_when_paths_present(self):
        snap = self._snap(paths=("voice/auth.py", "voice/util.py"))
        block = snap.to_prompt_section()
        assert "Files: voice/auth.py, voice/util.py" in block

    def test_files_line_absent_when_paths_empty(self):
        snap = self._snap(paths=())
        block = snap.to_prompt_section()
        assert "Files:" not in block

    def test_signature_line_still_present_with_paths(self):
        snap = self._snap(paths=("a.py",))
        block = snap.to_prompt_section()
        assert "(signature: abc12345)" in block

    def test_files_line_after_excerpt(self):
        snap = self._snap(paths=("a.py",))
        block = snap.to_prompt_section()
        # Order matters: representative excerpt should precede Files
        excerpt_idx = block.find("Representative:")
        files_idx = block.find("Files:")
        assert excerpt_idx >= 0 and files_idx >= 0
        assert excerpt_idx < files_idx

    def test_files_line_ascii_safe(self):
        snap = self._snap(paths=("a.py", "b.py"))
        block = snap.to_prompt_section()
        # Slice 2 added one new line ("Files: ..."). Pin THAT line
        # is ASCII-encodable (Iron Gate compat for the additive
        # surface). The pre-existing block has its own glyphs and
        # is not Slice 2's responsibility to police.
        for line in block.splitlines():
            if line.startswith("Files:"):
                line.encode("ascii")  # raises if Slice 2 broke it


# ---------------------------------------------------------------------------
# Sensor sub-flag + cap env knobs
# ---------------------------------------------------------------------------


from backend.core.ouroboros.governance.intake.sensors.proactive_exploration_sensor import (  # noqa: E501
    _representative_path_envelope_cap,
    _use_representative_paths_enabled,
)


class TestSensorSubFlag:
    def test_default_true_post_graduation(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_PROACTIVE_EXPLORATION_USE_REPRESENTATIVE_PATHS",
            raising=False,
        )
        assert _use_representative_paths_enabled() is True

    def test_empty_is_default(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PROACTIVE_EXPLORATION_USE_REPRESENTATIVE_PATHS",
            "",
        )
        assert _use_representative_paths_enabled() is True

    @pytest.mark.parametrize("raw", ["1", "true", "yes", "On"])
    def test_truthy(self, monkeypatch, raw):
        monkeypatch.setenv(
            "JARVIS_PROACTIVE_EXPLORATION_USE_REPRESENTATIVE_PATHS",
            raw,
        )
        assert _use_representative_paths_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "garbage"])
    def test_falsy(self, monkeypatch, raw):
        monkeypatch.setenv(
            "JARVIS_PROACTIVE_EXPLORATION_USE_REPRESENTATIVE_PATHS",
            raw,
        )
        assert _use_representative_paths_enabled() is False


class TestEnvelopeCap:
    def test_default(self):
        assert _representative_path_envelope_cap() == 8

    def test_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_EXPLORATION_REPRESENTATIVE_PATH_CAP", "0",
        )
        assert _representative_path_envelope_cap() == 1

    def test_ceiling(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_EXPLORATION_REPRESENTATIVE_PATH_CAP", "9999",
        )
        assert _representative_path_envelope_cap() == 32

    def test_garbage(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_EXPLORATION_REPRESENTATIVE_PATH_CAP", "abc",
        )
        assert _representative_path_envelope_cap() == 8


# ---------------------------------------------------------------------------
# Sensor envelope wiring (the load-bearing test for routing)
# ---------------------------------------------------------------------------


class _StubRouter:
    """Captures every envelope ingested by the sensor."""
    def __init__(self):
        self.envelopes = []

    async def ingest(self, envelope):
        self.envelopes.append(envelope)
        return None


def _stub_snapshot(paths: Tuple[str, ...] = (), *, ready: bool = True):
    """Build a CodebaseCharacterSnapshot stub with one cluster."""
    cluster = ClusterCharacter(
        cluster_id=0, kind="goal", size=5,
        theme_label="voice biometric auth",
        nearest_item_excerpt="voice biometric authentication primitive",
        nearest_item_source="git_commit",
        source_composition=(("git_commit", 5),),
        centroid_hash8="cluster1",
        representative_paths=paths,
    )
    if ready:
        return CodebaseCharacterSnapshot(
            outcome=DigestOutcome.READY,
            clusters=(cluster,),
            generated_at_ts=1700000100.0,
            total_corpus_items=10,
            cluster_mode="kmeans",
            built_at_ts=1700000000.0,
            truncated_count=0,
        )
    return CodebaseCharacterSnapshot(
        outcome=DigestOutcome.INSUFFICIENT_CLUSTERS,
        clusters=(),
        generated_at_ts=1700000100.0,
        total_corpus_items=10,
        cluster_mode="kmeans",
        built_at_ts=1700000000.0,
        truncated_count=0,
    )


def _build_sensor():
    """Build a ProactiveExplorationSensor wired to a stub router."""
    from backend.core.ouroboros.governance.intake.sensors.proactive_exploration_sensor import (  # noqa: E501
        ProactiveExplorationSensor,
    )
    router = _StubRouter()
    sensor = ProactiveExplorationSensor(
        repo="test", router=router,
    )
    return sensor, router


class TestSensorEnvelopeRouting:
    @pytest.mark.asyncio
    async def test_subflag_off_uses_sentinel(self, monkeypatch):
        # Post-graduation default is true; explicit "false" is the
        # operator escape hatch.
        monkeypatch.setenv(
            "JARVIS_PROACTIVE_EXPLORATION_USE_REPRESENTATIVE_PATHS",
            "false",
        )
        sensor, router = _build_sensor()
        snapshot = _stub_snapshot(
            paths=("voice/auth.py", "voice/util.py"),
        )
        with mock.patch(
            "backend.core.ouroboros.governance."
            "codebase_character.compute_codebase_character",
            return_value=snapshot,
        ), mock.patch(
            "backend.core.ouroboros.governance."
            "codebase_character.codebase_character_enabled",
            return_value=True,
        ), mock.patch(
            "backend.core.ouroboros.governance."
            "semantic_index.get_default_index",
            return_value=mock.MagicMock(
                stats=mock.MagicMock(return_value=mock.MagicMock(
                    cluster_mode="kmeans", corpus_n=10, built_at=0.0,
                )),
                clusters=[],
            ),
        ):
            await sensor._emit_cluster_coverage_signals()
        assert len(router.envelopes) == 1
        env = router.envelopes[0]
        assert env.target_files == (".",)
        assert env.evidence["target_files_source"] == (
            "project_root_sentinel"
        )
        assert env.evidence["representative_paths_count"] == 2

    @pytest.mark.asyncio
    async def test_subflag_on_paths_empty_uses_sentinel(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_PROACTIVE_EXPLORATION_USE_REPRESENTATIVE_PATHS",
            "true",
        )
        sensor, router = _build_sensor()
        snapshot = _stub_snapshot(paths=())  # empty
        with mock.patch(
            "backend.core.ouroboros.governance."
            "codebase_character.compute_codebase_character",
            return_value=snapshot,
        ), mock.patch(
            "backend.core.ouroboros.governance."
            "codebase_character.codebase_character_enabled",
            return_value=True,
        ), mock.patch(
            "backend.core.ouroboros.governance."
            "semantic_index.get_default_index",
            return_value=mock.MagicMock(
                stats=mock.MagicMock(return_value=mock.MagicMock(
                    cluster_mode="kmeans", corpus_n=10, built_at=0.0,
                )),
                clusters=[],
            ),
        ):
            await sensor._emit_cluster_coverage_signals()
        env = router.envelopes[0]
        assert env.target_files == (".",)
        assert env.evidence["target_files_source"] == (
            "project_root_sentinel"
        )
        assert env.evidence["representative_paths_count"] == 0

    @pytest.mark.asyncio
    async def test_subflag_on_paths_populated_uses_paths(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_PROACTIVE_EXPLORATION_USE_REPRESENTATIVE_PATHS",
            "true",
        )
        sensor, router = _build_sensor()
        snapshot = _stub_snapshot(
            paths=("voice/auth.py", "voice/util.py", "voice/test.py"),
        )
        with mock.patch(
            "backend.core.ouroboros.governance."
            "codebase_character.compute_codebase_character",
            return_value=snapshot,
        ), mock.patch(
            "backend.core.ouroboros.governance."
            "codebase_character.codebase_character_enabled",
            return_value=True,
        ), mock.patch(
            "backend.core.ouroboros.governance."
            "semantic_index.get_default_index",
            return_value=mock.MagicMock(
                stats=mock.MagicMock(return_value=mock.MagicMock(
                    cluster_mode="kmeans", corpus_n=10, built_at=0.0,
                )),
                clusters=[],
            ),
        ):
            await sensor._emit_cluster_coverage_signals()
        env = router.envelopes[0]
        assert env.target_files == (
            "voice/auth.py", "voice/util.py", "voice/test.py",
        )
        assert env.evidence["target_files_source"] == (
            "representative_paths"
        )
        assert env.evidence["representative_paths_count"] == 3
        # Description hint reflects path-aware routing.
        assert "Representative files: voice/auth.py" in env.description

    @pytest.mark.asyncio
    async def test_subflag_on_paths_truncated_to_cap(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PROACTIVE_EXPLORATION_USE_REPRESENTATIVE_PATHS",
            "true",
        )
        monkeypatch.setenv(
            "JARVIS_EXPLORATION_REPRESENTATIVE_PATH_CAP", "2",
        )
        sensor, router = _build_sensor()
        snapshot = _stub_snapshot(
            paths=tuple(f"f{i}.py" for i in range(20)),
        )
        with mock.patch(
            "backend.core.ouroboros.governance."
            "codebase_character.compute_codebase_character",
            return_value=snapshot,
        ), mock.patch(
            "backend.core.ouroboros.governance."
            "codebase_character.codebase_character_enabled",
            return_value=True,
        ), mock.patch(
            "backend.core.ouroboros.governance."
            "semantic_index.get_default_index",
            return_value=mock.MagicMock(
                stats=mock.MagicMock(return_value=mock.MagicMock(
                    cluster_mode="kmeans", corpus_n=10, built_at=0.0,
                )),
                clusters=[],
            ),
        ):
            await sensor._emit_cluster_coverage_signals()
        env = router.envelopes[0]
        # Truncated to envelope cap, ORIGINAL count preserved in
        # evidence (so cascade observers see how many paths existed).
        assert len(env.target_files) == 2
        assert env.evidence["representative_paths_count"] == 20

    @pytest.mark.asyncio
    async def test_description_hint_sentinel_when_no_paths(
        self, monkeypatch,
    ):
        # Post-graduation default is true; explicit "false" is the
        # operator escape hatch.
        monkeypatch.setenv(
            "JARVIS_PROACTIVE_EXPLORATION_USE_REPRESENTATIVE_PATHS",
            "false",
        )
        sensor, router = _build_sensor()
        snapshot = _stub_snapshot(paths=("a.py",))  # populated but flag off
        with mock.patch(
            "backend.core.ouroboros.governance."
            "codebase_character.compute_codebase_character",
            return_value=snapshot,
        ), mock.patch(
            "backend.core.ouroboros.governance."
            "codebase_character.codebase_character_enabled",
            return_value=True,
        ), mock.patch(
            "backend.core.ouroboros.governance."
            "semantic_index.get_default_index",
            return_value=mock.MagicMock(
                stats=mock.MagicMock(return_value=mock.MagicMock(
                    cluster_mode="kmeans", corpus_n=10, built_at=0.0,
                )),
                clusters=[],
            ),
        ):
            await sensor._emit_cluster_coverage_signals()
        env = router.envelopes[0]
        assert "search_code / read_file" in env.description
        assert "Representative files:" not in env.description
