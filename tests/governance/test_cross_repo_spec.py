"""Cross-Repository IaC Bridge + Verification Lock.

jarvis-prime is the sovereign owner of the GPU Packer spec; JARVIS ingests it
cross-repo. The baker resolves the spec from the jarvis-prime repo, and FAILS FAST
with CrossRepoDependencyError if the sovereign spec is missing/unreadable/empty --
never silently bakes a stale forked copy.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.cloud_build_baker import (
    CloudBuildBaker,
    CrossRepoDependencyError,
    resolve_jprime_spec_path,
    verify_cross_repo_spec,
)


def test_resolve_uses_jprime_path_env(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_PRIME_PATH", str(tmp_path))
    p = resolve_jprime_spec_path()
    assert str(p).startswith(str(tmp_path))
    assert p.name.endswith(".pkr.hcl")
    assert "packer" in str(p)


def test_explicit_path_wins(tmp_path):
    explicit = tmp_path / "custom.pkr.hcl"
    assert resolve_jprime_spec_path(str(explicit)) == explicit


def test_verify_missing_raises_crossrepo(tmp_path):
    with pytest.raises(CrossRepoDependencyError) as e:
        verify_cross_repo_spec(tmp_path / "nope.pkr.hcl")
    assert "jarvis-prime" in str(e.value).lower()


def test_verify_empty_raises(tmp_path):
    f = tmp_path / "empty.pkr.hcl"
    f.write_text("")
    with pytest.raises(CrossRepoDependencyError):
        verify_cross_repo_spec(f)


def test_verify_ok_returns_path(tmp_path):
    f = tmp_path / "ok.pkr.hcl"
    f.write_text('source "googlecompute" "x" {}\n')
    assert verify_cross_repo_spec(f) == f


def test_baker_defaults_to_jprime_spec(monkeypatch, tmp_path):
    # Lay a fake jarvis-prime repo with the spec at the canonical relative path.
    spec = tmp_path / "infra" / "packer" / "jprime_gpu_golden_image.pkr.hcl"
    spec.parent.mkdir(parents=True)
    spec.write_text('source "googlecompute" "x" {}\n')
    monkeypatch.setenv("JARVIS_PRIME_PATH", str(tmp_path))
    b = CloudBuildBaker(project="p", image_family="f")   # NO spec_path -> jarvis-prime
    cfg = b.build_config("p")                            # reads + base64-inlines the sovereign spec
    assert cfg["steps"]                                  # built successfully from jarvis-prime


def test_baker_fails_fast_when_sovereign_spec_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_PRIME_PATH", str(tmp_path))   # empty repo -> no spec
    b = CloudBuildBaker(project="p", image_family="f")
    with pytest.raises(CrossRepoDependencyError):
        b.build_config("p")
