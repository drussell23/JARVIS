"""ClusterIntelligence-CrossSession Slice 1 -- regression spine.

Pins the representative_paths build-time enrichment on
:class:`ClusterInfo` plus the additive ``commit_hash`` field on
:class:`CorpusItem`. Backward-compat: existing
``test_semantic_index.py`` (134 tests) stay green untouched.

Coverage:
  * Sub-flag asymmetric env semantics (default false until Slice 5)
  * Env-knob clamping (top-K floor/ceiling, timeout floor/ceiling)
  * Backward-compat: ClusterInfo / CorpusItem defaults preserved
  * _load_commit_paths parser: well-formed git output, blank
    separators, malformed lines, missing prefix, absolute-path
    skip, nul-injection skip, trailing-block flush
  * _load_commit_paths defensive degradation: git missing /
    timeout / non-zero return / OSError -> empty dict
  * _attach_paths_to_clusters aggregation: top-K by frequency,
    lexicographic tie-break, empty member buckets, length
    mismatch defense, dataclass replace round-trip
  * Git pretty-format swap: %ct|%s when flag off,
    %ct|%H|%s when flag on; subject-with-pipe parses correctly
"""
from __future__ import annotations

import dataclasses as _dc
import subprocess
from pathlib import Path
from typing import Dict, List, Tuple
from unittest import mock

import pytest

from backend.core.ouroboros.governance.semantic_index import (
    CLUSTER_KIND_GOAL,
    SOURCE_GIT_COMMIT,
    SOURCE_GOAL,
    ClusterInfo,
    CorpusItem,
    _attach_paths_to_clusters,
    _cluster_representative_path_k,
    _COMMIT_PATH_BLOCK_PREFIX,
    _git_path_scan_timeout_s,
    _load_commit_paths,
    _representative_paths_enabled,
)


# ---------------------------------------------------------------------------
# Sub-flag + env-knob semantics
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for var in (
        "JARVIS_SEMANTIC_INDEX_REPRESENTATIVE_PATHS_ENABLED",
        "JARVIS_CLUSTER_REPRESENTATIVE_PATH_K",
        "JARVIS_SEMANTIC_GIT_PATH_SCAN_TIMEOUT_S",
    ):
        monkeypatch.delenv(var, raising=False)


class TestSubFlag:
    def test_default_true_post_graduation(self, monkeypatch):
        # Post-graduation default is true; explicit "false" is the
        # operator escape hatch.
        monkeypatch.setenv(
            "JARVIS_SEMANTIC_INDEX_REPRESENTATIVE_PATHS_ENABLED",
            "false",
        )
        assert _representative_paths_enabled() is True

    def test_empty_is_default(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_SEMANTIC_INDEX_REPRESENTATIVE_PATHS_ENABLED", "",
        )
        assert _representative_paths_enabled() is True

    @pytest.mark.parametrize("raw", ["1", "true", "yes", "On"])
    def test_truthy(self, monkeypatch, raw):
        monkeypatch.setenv(
            "JARVIS_SEMANTIC_INDEX_REPRESENTATIVE_PATHS_ENABLED",
            raw,
        )
        assert _representative_paths_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "garbage"])
    def test_falsy(self, monkeypatch, raw):
        monkeypatch.setenv(
            "JARVIS_SEMANTIC_INDEX_REPRESENTATIVE_PATHS_ENABLED",
            raw,
        )
        assert _representative_paths_enabled() is False


class TestKnobClamping:
    def test_k_default(self):
        assert _cluster_representative_path_k() == 8

    def test_k_floor(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CLUSTER_REPRESENTATIVE_PATH_K", "0")
        assert _cluster_representative_path_k() == 1

    def test_k_ceiling(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CLUSTER_REPRESENTATIVE_PATH_K", "9999",
        )
        assert _cluster_representative_path_k() == 64

    def test_k_garbage(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CLUSTER_REPRESENTATIVE_PATH_K", "abc",
        )
        assert _cluster_representative_path_k() == 8

    def test_timeout_default(self):
        assert _git_path_scan_timeout_s() == 8.0

    def test_timeout_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_SEMANTIC_GIT_PATH_SCAN_TIMEOUT_S", "0.0",
        )
        assert _git_path_scan_timeout_s() == 1.0

    def test_timeout_ceiling(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_SEMANTIC_GIT_PATH_SCAN_TIMEOUT_S", "100",
        )
        assert _git_path_scan_timeout_s() == 30.0


# ---------------------------------------------------------------------------
# Backward-compat: dataclass defaults preserved
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    def test_corpus_item_commit_hash_defaults_empty(self):
        item = CorpusItem(text="hi", source=SOURCE_GIT_COMMIT, ts=0.0)
        assert item.commit_hash == ""

    def test_cluster_info_representative_paths_defaults_empty(self):
        cluster = ClusterInfo(
            cluster_id=0, size=1, kind=CLUSTER_KIND_GOAL,
            centroid=(0.0,), centroid_hash8="abc12345",
            nearest_item_text="x",
            nearest_item_source=SOURCE_GIT_COMMIT,
            source_composition=((SOURCE_GIT_COMMIT, 1),),
        )
        assert cluster.representative_paths == ()
        assert isinstance(cluster.representative_paths, tuple)

    def test_cluster_info_frozen_still_frozen(self):
        cluster = ClusterInfo(
            cluster_id=0, size=1, kind=CLUSTER_KIND_GOAL,
            centroid=(0.0,), centroid_hash8="abc12345",
            nearest_item_text="x",
            nearest_item_source=SOURCE_GIT_COMMIT,
            source_composition=((SOURCE_GIT_COMMIT, 1),),
        )
        with pytest.raises(_dc.FrozenInstanceError):
            cluster.representative_paths = ("x",)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# _load_commit_paths parser
# ---------------------------------------------------------------------------


def _stub_subprocess_run(stdout: str, returncode: int = 0):
    """Build a CompletedProcess-like return for subprocess.run."""
    out = subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr="",
    )
    return mock.MagicMock(return_value=out)


class TestLoadCommitPathsParser:
    def test_well_formed_blocks(self, tmp_path):
        stdout = (
            ">>>aaa111\n"
            "src/a.py\n"
            "src/b.py\n"
            "\n"
            ">>>bbb222\n"
            "src/c.py\n"
        )
        with mock.patch(
            "subprocess.run", _stub_subprocess_run(stdout),
        ):
            out = _load_commit_paths(
                tmp_path, git_limit=10, timeout_s=5.0,
            )
        assert out == {
            "aaa111": ("src/a.py", "src/b.py"),
            "bbb222": ("src/c.py",),
        }

    def test_trailing_block_no_blank_flushes(self, tmp_path):
        # Last commit has no trailing blank line.
        stdout = (
            ">>>aaa111\n"
            "src/a.py\n"
        )
        with mock.patch(
            "subprocess.run", _stub_subprocess_run(stdout),
        ):
            out = _load_commit_paths(
                tmp_path, git_limit=1, timeout_s=5.0,
            )
        assert out == {"aaa111": ("src/a.py",)}

    def test_empty_stdout(self, tmp_path):
        with mock.patch(
            "subprocess.run", _stub_subprocess_run(""),
        ):
            out = _load_commit_paths(
                tmp_path, git_limit=10, timeout_s=5.0,
            )
        assert out == {}

    def test_only_prefix_no_paths(self, tmp_path):
        # Commit with no file changes (e.g., merge commit).
        stdout = ">>>aaa111\n\n>>>bbb222\nsrc/x.py\n"
        with mock.patch(
            "subprocess.run", _stub_subprocess_run(stdout),
        ):
            out = _load_commit_paths(
                tmp_path, git_limit=2, timeout_s=5.0,
            )
        # Empty-paths commit still recorded with empty tuple.
        assert "aaa111" in out
        assert out["aaa111"] == ()
        assert out["bbb222"] == ("src/x.py",)

    def test_path_without_active_commit_skipped(self, tmp_path):
        # Stray path before any prefix -- ignored.
        stdout = "lonely_path.py\n>>>aaa111\nreal.py\n"
        with mock.patch(
            "subprocess.run", _stub_subprocess_run(stdout),
        ):
            out = _load_commit_paths(
                tmp_path, git_limit=1, timeout_s=5.0,
            )
        assert out == {"aaa111": ("real.py",)}

    def test_absolute_path_skipped(self, tmp_path):
        stdout = ">>>aaa111\n/etc/passwd\nsrc/ok.py\n"
        with mock.patch(
            "subprocess.run", _stub_subprocess_run(stdout),
        ):
            out = _load_commit_paths(
                tmp_path, git_limit=1, timeout_s=5.0,
            )
        assert out == {"aaa111": ("src/ok.py",)}

    def test_nul_byte_path_skipped(self, tmp_path):
        stdout = ">>>aaa111\nsrc/\x00bad.py\nsrc/ok.py\n"
        with mock.patch(
            "subprocess.run", _stub_subprocess_run(stdout),
        ):
            out = _load_commit_paths(
                tmp_path, git_limit=1, timeout_s=5.0,
            )
        assert out == {"aaa111": ("src/ok.py",)}

    def test_empty_hash_after_prefix_resets(self, tmp_path):
        stdout = ">>>\nsrc/lost.py\n>>>aaa111\nsrc/found.py\n"
        with mock.patch(
            "subprocess.run", _stub_subprocess_run(stdout),
        ):
            out = _load_commit_paths(
                tmp_path, git_limit=2, timeout_s=5.0,
            )
        # The empty-hash block is dropped; the named one captured.
        assert out == {"aaa111": ("src/found.py",)}

    def test_blocked_prefix_constant(self):
        # Future-proof: pin the prefix so observers / consumers
        # don't drift independently.
        assert _COMMIT_PATH_BLOCK_PREFIX == ">>>"


class TestLoadCommitPathsDefensive:
    def test_git_missing_returns_empty(self, tmp_path):
        with mock.patch(
            "subprocess.run", side_effect=FileNotFoundError,
        ):
            assert _load_commit_paths(
                tmp_path, git_limit=10, timeout_s=5.0,
            ) == {}

    def test_timeout_returns_empty(self, tmp_path):
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=5),
        ):
            assert _load_commit_paths(
                tmp_path, git_limit=10, timeout_s=5.0,
            ) == {}

    def test_non_zero_return_returns_empty(self, tmp_path):
        with mock.patch(
            "subprocess.run", _stub_subprocess_run(
                ">>>aaa111\nfile.py", returncode=1,
            ),
        ):
            assert _load_commit_paths(
                tmp_path, git_limit=10, timeout_s=5.0,
            ) == {}

    def test_oserror_returns_empty(self, tmp_path):
        with mock.patch(
            "subprocess.run", side_effect=OSError("disk gone"),
        ):
            assert _load_commit_paths(
                tmp_path, git_limit=10, timeout_s=5.0,
            ) == {}


# ---------------------------------------------------------------------------
# _attach_paths_to_clusters aggregation
# ---------------------------------------------------------------------------


def _make_cluster(cid: int, *, hash8: str = "h0000000") -> ClusterInfo:
    return ClusterInfo(
        cluster_id=cid, size=3, kind=CLUSTER_KIND_GOAL,
        centroid=(0.0,), centroid_hash8=hash8,
        nearest_item_text="x", nearest_item_source=SOURCE_GIT_COMMIT,
        source_composition=((SOURCE_GIT_COMMIT, 3),),
    )


def _make_commit_item(commit_hash: str) -> CorpusItem:
    return CorpusItem(
        text="commit subject",
        source=SOURCE_GIT_COMMIT,
        ts=1700000000.0,
        commit_hash=commit_hash,
    )


def _make_non_commit_item() -> CorpusItem:
    return CorpusItem(
        text="goal entry",
        source=SOURCE_GOAL,
        ts=1700000000.0,
    )


class TestAttachPaths:
    def test_top_k_by_frequency(self):
        cluster = _make_cluster(0)
        members = [
            _make_commit_item("c1"),
            _make_commit_item("c2"),
            _make_commit_item("c3"),
        ]
        labels = [0, 0, 0]
        commit_paths = {
            "c1": ("a.py", "b.py"),
            "c2": ("a.py",),
            "c3": ("a.py", "c.py"),
        }
        out = _attach_paths_to_clusters(
            [cluster], members, labels, commit_paths, top_k=2,
        )
        # a.py touched 3x (top), b.py + c.py each 1x (lex tie-break).
        assert out[0].representative_paths == ("a.py", "b.py")

    def test_lexicographic_tie_break(self):
        cluster = _make_cluster(0)
        members = [
            _make_commit_item("c1"),
            _make_commit_item("c2"),
        ]
        labels = [0, 0]
        commit_paths = {
            "c1": ("zebra.py", "apple.py"),
            "c2": ("middle.py",),
        }
        out = _attach_paths_to_clusters(
            [cluster], members, labels, commit_paths, top_k=3,
        )
        # All have count=1 -> lexicographic order
        assert out[0].representative_paths == (
            "apple.py", "middle.py", "zebra.py",
        )

    def test_top_k_truncates(self):
        cluster = _make_cluster(0)
        members = [_make_commit_item("c1")]
        labels = [0]
        commit_paths = {
            "c1": tuple(f"f{i}.py" for i in range(20)),
        }
        out = _attach_paths_to_clusters(
            [cluster], members, labels, commit_paths, top_k=5,
        )
        assert len(out[0].representative_paths) == 5

    def test_non_commit_members_skipped(self):
        cluster = _make_cluster(0)
        members = [
            _make_commit_item("c1"),
            _make_non_commit_item(),  # commit_hash empty -> skipped
        ]
        labels = [0, 0]
        commit_paths = {"c1": ("a.py",)}
        out = _attach_paths_to_clusters(
            [cluster], members, labels, commit_paths, top_k=5,
        )
        assert out[0].representative_paths == ("a.py",)

    def test_empty_cluster_unchanged(self):
        cluster = _make_cluster(0)
        # Cluster has no member commits with hashes.
        out = _attach_paths_to_clusters(
            [cluster], [], [], {}, top_k=5,
        )
        assert out[0].representative_paths == ()

    def test_unknown_commit_hash_silently_dropped(self):
        cluster = _make_cluster(0)
        members = [_make_commit_item("c_missing")]
        labels = [0]
        # commit_paths_by_hash doesn't contain c_missing.
        out = _attach_paths_to_clusters(
            [cluster], members, labels, {"c_other": ("x.py",)},
            top_k=5,
        )
        assert out[0].representative_paths == ()

    def test_length_mismatch_defensive_unchanged(self):
        cluster = _make_cluster(0)
        members = [_make_commit_item("c1"), _make_commit_item("c2")]
        labels = [0]  # MISMATCH: 1 label vs 2 members
        out = _attach_paths_to_clusters(
            [cluster], members, labels,
            {"c1": ("a.py",)}, top_k=5,
        )
        # Defensive return preserves the input cluster unchanged.
        assert out[0].representative_paths == ()

    def test_top_k_zero_unchanged(self):
        cluster = _make_cluster(0)
        members = [_make_commit_item("c1")]
        labels = [0]
        commit_paths = {"c1": ("a.py",)}
        out = _attach_paths_to_clusters(
            [cluster], members, labels, commit_paths, top_k=0,
        )
        assert out[0].representative_paths == ()

    def test_empty_clusters_returns_empty(self):
        out = _attach_paths_to_clusters([], [], [], {}, top_k=5)
        assert out == []

    def test_multiple_clusters_independent(self):
        c0 = _make_cluster(0, hash8="hash00aa")
        c1 = _make_cluster(1, hash8="hash11bb")
        members = [
            _make_commit_item("ca"),
            _make_commit_item("cb"),
            _make_commit_item("cc"),
            _make_commit_item("cd"),
        ]
        labels = [0, 0, 1, 1]
        commit_paths = {
            "ca": ("foo.py",),
            "cb": ("foo.py", "bar.py"),
            "cc": ("baz.py",),
            "cd": ("baz.py", "qux.py"),
        }
        out = _attach_paths_to_clusters(
            [c0, c1], members, labels, commit_paths, top_k=3,
        )
        # Cluster 0: foo.py (2x), bar.py (1x)
        assert out[0].representative_paths == ("foo.py", "bar.py")
        # Cluster 1: baz.py (2x), qux.py (1x)
        assert out[1].representative_paths == ("baz.py", "qux.py")

    def test_garbage_path_entries_skipped(self):
        cluster = _make_cluster(0)
        members = [_make_commit_item("c1")]
        labels = [0]
        # Mixed garbage in the paths tuple.
        commit_paths: Dict[str, Tuple[str, ...]] = {
            "c1": ("ok.py", "", None, 42),  # type: ignore[dict-item]
        }
        out = _attach_paths_to_clusters(
            [cluster], members, labels, commit_paths, top_k=5,
        )
        assert out[0].representative_paths == ("ok.py",)

    def test_returns_new_clusterinfo_not_mutating_input(self):
        cluster = _make_cluster(0)
        members = [_make_commit_item("c1")]
        labels = [0]
        commit_paths = {"c1": ("a.py",)}
        out = _attach_paths_to_clusters(
            [cluster], members, labels, commit_paths, top_k=5,
        )
        # Input cluster's representative_paths still empty.
        assert cluster.representative_paths == ()
        # Output is a different ClusterInfo instance.
        assert out[0] is not cluster
        # All other fields preserved by dataclass.replace.
        assert out[0].cluster_id == cluster.cluster_id
        assert out[0].centroid_hash8 == cluster.centroid_hash8
        assert out[0].kind == cluster.kind


# ---------------------------------------------------------------------------
# Git pretty-format swap (assemble_corpus path)
# ---------------------------------------------------------------------------


class TestGitPrettyFormatSwap:
    def test_format_unchanged_when_flag_off(self, tmp_path, monkeypatch):
        # Post-graduation default is true; explicit "false" is the
        # operator escape hatch.
        monkeypatch.setenv(
            "JARVIS_SEMANTIC_INDEX_REPRESENTATIVE_PATHS_ENABLED",
            "false",
        )
        from backend.core.ouroboros.governance import (
            semantic_index as si,
        )
        captured_args: List[List[str]] = []

        def _capture(args, **kwargs):
            captured_args.append(list(args))
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="", stderr="",
            )

        with mock.patch.object(si.subprocess, "run", _capture):
            si._assemble_corpus(tmp_path, git_limit=5, max_items=100)

        # Pre-Slice-1 format preserved.
        git_calls = [a for a in captured_args if a[0] == "git"]
        assert len(git_calls) == 1
        assert "--pretty=format:%ct|%s" in git_calls[0]

    def test_format_swapped_when_flag_on(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_SEMANTIC_INDEX_REPRESENTATIVE_PATHS_ENABLED",
            "true",
        )
        from backend.core.ouroboros.governance import (
            semantic_index as si,
        )
        captured_args: List[List[str]] = []

        def _capture(args, **kwargs):
            captured_args.append(list(args))
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="", stderr="",
            )

        with mock.patch.object(si.subprocess, "run", _capture):
            si._assemble_corpus(tmp_path, git_limit=5, max_items=100)

        git_calls = [a for a in captured_args if a[0] == "git"]
        assert len(git_calls) == 1
        assert "--pretty=format:%ct|%H|%s" in git_calls[0]

    def test_subject_with_pipe_parses_correctly(self, tmp_path, monkeypatch):
        """Subject lines may contain ``|``. The 3-way split must
        only consume the first 2 separators."""
        monkeypatch.setenv(
            "JARVIS_SEMANTIC_INDEX_REPRESENTATIVE_PATHS_ENABLED",
            "true",
        )
        from backend.core.ouroboros.governance import (
            semantic_index as si,
        )
        # Subject contains pipes -- common in commit messages
        # like "feat(foo): bar | baz"
        stdout = (
            "1700000000|abc123def456|feat(foo): bar | baz | qux\n"
            "1700000100|deadbeef0000|simple subject\n"
        )

        def _stub(args, **kwargs):
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=stdout, stderr="",
            )

        with mock.patch.object(si.subprocess, "run", _stub):
            items = si._assemble_corpus(
                tmp_path, git_limit=5, max_items=100,
            )

        commit_items = [
            it for it in items if it.source == SOURCE_GIT_COMMIT
        ]
        assert len(commit_items) == 2
        # First item: subject preserved with its pipes
        first = next(
            i for i in commit_items if i.commit_hash == "abc123def456"
        )
        assert "bar | baz | qux" in first.text
        # Second item: simple subject
        second = next(
            i for i in commit_items if i.commit_hash == "deadbeef0000"
        )
        assert second.text == "simple subject"

    def test_commit_hash_empty_when_flag_off(self, tmp_path, monkeypatch):
        # Post-graduation default is true; explicit "false" is the
        # operator escape hatch.
        monkeypatch.setenv(
            "JARVIS_SEMANTIC_INDEX_REPRESENTATIVE_PATHS_ENABLED",
            "false",
        )
        from backend.core.ouroboros.governance import (
            semantic_index as si,
        )
        stdout = "1700000000|simple subject\n"

        def _stub(args, **kwargs):
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=stdout, stderr="",
            )

        with mock.patch.object(si.subprocess, "run", _stub):
            items = si._assemble_corpus(
                tmp_path, git_limit=5, max_items=100,
            )

        commit_items = [
            it for it in items if it.source == SOURCE_GIT_COMMIT
        ]
        assert len(commit_items) == 1
        # Hash field stays empty when the flag is off.
        assert commit_items[0].commit_hash == ""
