"""Drift-kill regression: the baker's image_family/model are DICTATED by the single
runtime source of truth (``failover_tier.quality_tier()``), so a baked image can
NEVER diverge from what the failover provisioner will request. Three hardcode sites
(baker default, CLI default, packer default) collapse into one dictator. Explicit
caller overrides (e.g. a small-model validation bake) still win.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.cloud_build_baker import CloudBuildBaker
from backend.core.ouroboros.governance import cloud_build_baker as cbb


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # Quality-tier specs resolve from defaults unless a test overrides.
    for k in ("JARVIS_FAILOVER_QUALITY_IMAGE", "JARVIS_FAILOVER_QUALITY_MODEL"):
        monkeypatch.delenv(k, raising=False)
    yield


def test_baker_derives_family_and_model_from_source_of_truth():
    b = CloudBuildBaker(project="p")
    assert b.image_family == "jarvis-prime-coder-32b"
    assert b.model == "qwen2.5-coder:32b"


def test_env_override_flows_through_to_baker(monkeypatch):
    # The dictator (quality_tier) reads env -> a single override moves the baker too.
    monkeypatch.setenv("JARVIS_FAILOVER_QUALITY_IMAGE", "fam-z")
    monkeypatch.setenv("JARVIS_FAILOVER_QUALITY_MODEL", "qwen2.5-coder:14b")
    b = CloudBuildBaker(project="p")
    assert b.image_family == "fam-z"
    assert b.model == "qwen2.5-coder:14b"


def test_explicit_args_win_over_source_of_truth():
    # A validation bake (0.5B, throwaway family) must be able to override.
    b = CloudBuildBaker(project="p", image_family="jarvis-prime-coder-validate", model="qwen2.5-coder:0.5b")
    assert b.image_family == "jarvis-prime-coder-validate"
    assert b.model == "qwen2.5-coder:0.5b"


def test_build_config_now_pins_model_label_var(tmp_path):
    # Because the baker always carries a resolved model, build_config emits an
    # explicit -var=model_label -> the bake can't silently fall back to the packer
    # template's own default (drift-kill at the wire).
    spec = tmp_path / "x.pkr.hcl"
    spec.write_text('source "googlecompute" "x" {}\nbuild { sources = ["x"] }\n')
    b = CloudBuildBaker(project="p", spec_path=str(spec))
    cfg = b.build_config("proj")
    joined = " ".join(" ".join(s.get("args", [])) for s in cfg["steps"])
    assert "model_label=qwen2.5-coder:32b" in joined
    assert "image_family=jarvis-prime-coder-32b" in joined


def test_fail_soft_when_source_of_truth_raises(monkeypatch):
    # A metadata lookup must NEVER crash a bake -> fall back to legacy literals.
    import backend.core.ouroboros.governance.failover_tier as ft

    def _boom():
        raise RuntimeError("source of truth unavailable")

    monkeypatch.setattr(ft, "quality_tier", _boom)
    fam, model = cbb._quality_tier_defaults()
    assert fam == "jarvis-prime-coder-32b"
    assert model == "qwen2.5-coder:32b"
