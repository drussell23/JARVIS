"""Regression spine for the multi_repo.repo_signature primitive.

Pins:
  * Determinism — same resolved path always yields same signature.
  * Distinct paths -> distinct signatures.
  * Length stability — exactly 8 hex chars; AST-pinned.
  * Symbolic-link / non-existent path tolerance — signature stays
    stable even when the path doesn't exist on disk.
  * Label fallback chain: registry match -> dir basename ->
    "unknown".
  * No-arg + None call falls back to CWD signature for backward
    compat with the legacy ``get_default_index(None)`` contract.
  * AST invariant validates against current source.
"""

from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.core.ouroboros.governance.multi_repo.repo_signature import (
    compute_repo_signature,
    repo_label_for,
    register_shipped_invariants,
)
from backend.core.ouroboros.governance.multi_repo.registry import (
    RepoConfig,
    RepoRegistry,
)


class TestComputeRepoSignature:
    def test_signature_format_is_8_hex_chars(self, tmp_path: Path) -> None:
        sig = compute_repo_signature(tmp_path)
        assert isinstance(sig, str)
        assert len(sig) == 8
        assert re.fullmatch(r"[0-9a-f]{8}", sig)

    def test_deterministic_across_calls(self, tmp_path: Path) -> None:
        s1 = compute_repo_signature(tmp_path)
        s2 = compute_repo_signature(tmp_path)
        s3 = compute_repo_signature(tmp_path)
        assert s1 == s2 == s3

    def test_distinct_paths_produce_distinct_signatures(
        self, tmp_path: Path,
    ) -> None:
        a = tmp_path / "repo_a"
        b = tmp_path / "repo_b"
        a.mkdir()
        b.mkdir()
        assert compute_repo_signature(a) != compute_repo_signature(b)

    def test_path_with_dot_dot_normalizes(self, tmp_path: Path) -> None:
        target = tmp_path / "foo"
        target.mkdir()
        canonical = compute_repo_signature(target)
        with_dotdot = compute_repo_signature(
            tmp_path / "foo" / "bar" / "..",
        )
        assert canonical == with_dotdot

    def test_nonexistent_path_still_produces_signature(
        self, tmp_path: Path,
    ) -> None:
        ghost = tmp_path / "this-path-does-not-exist"
        sig = compute_repo_signature(ghost)
        assert isinstance(sig, str)
        assert len(sig) == 8
        assert sig == compute_repo_signature(ghost)

    def test_none_falls_back_to_cwd(self) -> None:
        sig_none = compute_repo_signature(None)
        sig_cwd = compute_repo_signature(Path(os.getcwd()))
        assert sig_none == sig_cwd

    def test_pure_function_no_env_dependency(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        sig_before = compute_repo_signature(tmp_path)
        monkeypatch.setenv("JARVIS_REPO_PATH", str(tmp_path / "other"))
        monkeypatch.setenv("JARVIS_PRIME_REPO_PATH", "/tmp/whatever")
        sig_after = compute_repo_signature(tmp_path)
        assert sig_before == sig_after

    def test_string_path_via_path_works(self, tmp_path: Path) -> None:
        sig_str = compute_repo_signature(Path(str(tmp_path)))
        sig_path = compute_repo_signature(tmp_path)
        assert sig_str == sig_path


class TestRepoLabelFor:
    def test_label_falls_back_to_dir_basename(self, tmp_path: Path) -> None:
        target = tmp_path / "myrepo"
        target.mkdir()
        assert repo_label_for(target) == "myrepo"

    def test_label_uses_registry_name_when_path_matches(
        self, tmp_path: Path,
    ) -> None:
        repo_dir = tmp_path / "actual-checkout-name"
        repo_dir.mkdir()
        registry = RepoRegistry((
            RepoConfig(
                name="prime",
                local_path=repo_dir,
                canary_slices=("tests/",),
            ),
        ))
        assert repo_label_for(repo_dir, registry) == "prime"

    def test_label_falls_back_to_basename_when_no_registry_match(
        self, tmp_path: Path,
    ) -> None:
        repo_dir = tmp_path / "unregistered"
        repo_dir.mkdir()
        other = tmp_path / "registered"
        other.mkdir()
        registry = RepoRegistry((
            RepoConfig(
                name="other",
                local_path=other,
                canary_slices=("tests/",),
            ),
        ))
        assert repo_label_for(repo_dir, registry) == "unregistered"

    def test_label_handles_no_registry(self, tmp_path: Path) -> None:
        target = tmp_path / "labeltest"
        target.mkdir()
        assert repo_label_for(target, registry=None) == "labeltest"

    def test_label_handles_none_path(self) -> None:
        assert repo_label_for(None) == Path(os.getcwd()).name

    def test_label_resolves_relative_paths(self, tmp_path: Path) -> None:
        target = tmp_path / "subdir"
        target.mkdir()
        cwd_orig = os.getcwd()
        try:
            os.chdir(tmp_path)
            assert repo_label_for(Path("subdir")) == "subdir"
        finally:
            os.chdir(cwd_orig)


class TestSubstrateInvariant:
    def test_invariant_holds_against_current_source(self) -> None:
        invariants = register_shipped_invariants()
        assert len(invariants) == 1
        inv = invariants[0]
        target_path = REPO_ROOT / inv.target_file
        source = target_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        violations = inv.validate(tree, source)
        assert violations == (), str(violations)

    def test_invariant_catches_missing_compute(self) -> None:
        invariants = register_shipped_invariants()
        synthetic = '''
_SIGNATURE_LEN = 8
def repo_label_for(p, r=None): return "x"
'''
        tree = ast.parse(synthetic)
        violations = invariants[0].validate(tree, synthetic)
        assert any("compute_repo_signature" in v for v in violations)

    def test_invariant_catches_changed_signature_length(self) -> None:
        invariants = register_shipped_invariants()
        synthetic = '''
_SIGNATURE_LEN = 16
def compute_repo_signature(p): return "x" * 16
def repo_label_for(p, r=None): return "x"
'''
        tree = ast.parse(synthetic)
        violations = invariants[0].validate(tree, synthetic)
        assert any("_SIGNATURE_LEN MUST stay 8" in v for v in violations)

    def test_invariant_catches_determinism_break_via_time(self) -> None:
        invariants = register_shipped_invariants()
        synthetic = '''
import time
_SIGNATURE_LEN = 8
def compute_repo_signature(p):
    t = time.time()
    return "x"
def repo_label_for(p, r=None): return "x"
'''
        tree = ast.parse(synthetic)
        violations = invariants[0].validate(tree, synthetic)
        assert any(
            "deterministic" in v and "time" in v
            for v in violations
        )

    def test_invariant_catches_determinism_break_via_random(self) -> None:
        invariants = register_shipped_invariants()
        synthetic = '''
import random
_SIGNATURE_LEN = 8
def compute_repo_signature(p):
    return random.choice(["a", "b"])
def repo_label_for(p, r=None): return "x"
'''
        tree = ast.parse(synthetic)
        violations = invariants[0].validate(tree, synthetic)
        assert any(
            "deterministic" in v and "random" in v
            for v in violations
        )
