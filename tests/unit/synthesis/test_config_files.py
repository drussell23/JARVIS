import yaml
from pathlib import Path

_SYNTH_DIR = Path("backend/neural_mesh/synthesis")


def test_sandbox_allowlist_has_allowed_imports():
    data = yaml.safe_load((_SYNTH_DIR / "sandbox_allowlist.yaml").read_text())
    assert "allowed_imports" in data
    assert "asyncio" in data["allowed_imports"]


def test_gap_resolution_policy_has_defaults():
    data = yaml.safe_load((_SYNTH_DIR / "gap_resolution_policy.yaml").read_text())
    assert "version" in data
    assert "defaults" in data
    d = data["defaults"]
    assert "risk_class" in d
    assert "idempotent" in d
    assert "slo_p99_ms" in d


def test_gap_resolution_policy_has_domain_overrides():
    data = yaml.safe_load((_SYNTH_DIR / "gap_resolution_policy.yaml").read_text())
    assert "domain_overrides" in data
    overrides = data["domain_overrides"]
    assert "file_edit:any" in overrides
    assert overrides["file_edit:any"]["risk_class"] == "high"
