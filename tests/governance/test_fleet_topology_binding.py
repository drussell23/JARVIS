# tests/governance/test_fleet_topology_binding.py
from __future__ import annotations


def test_dw_models_for_route_guard_present_and_gated():
    import backend.core.ouroboros.governance.provider_topology as pt
    src = open(pt.__file__).read()
    assert "_fleet_guarded" in src
    assert "fleet_authoritative_enabled" in src
    assert "fleet_apply_rerank" in src
    # all three model-returning paths funnel through the guard
    assert src.count("_fleet_guarded(route,") >= 3


def test_rerank_applied_when_authoritative(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_FLEET_EVALUATOR_AUTHORITATIVE", "true")
    monkeypatch.setenv("JARVIS_FLEET_CALIBRATION_PATH", str(tmp_path / "c.json"))
    from backend.core.ouroboros.governance import fleet_calibration_store as s
    st = s.FleetCalibrationStore()
    st.record_probe("good", kind="code", code_pass=True, ttft_ms=200, tok_per_s=90, now=1.0)
    st.record_probe("good", kind="code", code_pass=True, ttft_ms=200, tok_per_s=90, now=2.0)
    st.record_probe("bad", kind="code", code_pass=False, ttft_ms=100, tok_per_s=120, now=1.0)
    st.record_probe("bad", kind="code", code_pass=False, ttft_ms=100, tok_per_s=120, now=2.0)
    st.save()
    out = s.fleet_apply_rerank("standard", ("bad", "good"))
    assert out[0] == "good"     # measured-valid coder rises above measured-invalid


def test_guard_off_is_identity(monkeypatch, tmp_path):
    monkeypatch.delenv("JARVIS_FLEET_EVALUATOR_AUTHORITATIVE", raising=False)
    from backend.core.ouroboros.governance import provider_topology as pt
    # _fleet_guarded must be a pass-through when the gate is off
    assert pt._fleet_guarded("standard", ("a", "b", "c")) == ("a", "b", "c")


def test_fleet_flags_registered():
    from backend.core.ouroboros.governance import flag_registry as fr
    from backend.core.ouroboros.governance import flag_registry_seed as seed

    reg = fr.FlagRegistry()
    seed.seed_default_registry(reg)

    expected = {
        "JARVIS_FLEET_EVALUATOR_ENABLED",
        "JARVIS_FLEET_EVALUATOR_AUTHORITATIVE",
        "JARVIS_FLEET_EWMA_ALPHA",
        "JARVIS_FLEET_PROBE_MAX_TOKENS",
        "JARVIS_FLEET_PROBE_TIMEOUT_S",
        "JARVIS_FLEET_MAX_MODELS_PER_CYCLE",
        "JARVIS_FLEET_DAILY_USD_CAP",
        "JARVIS_FLEET_GRAD_MIN_SAMPLES",
        "JARVIS_FLEET_GRAD_MIN_AST",
        "JARVIS_FLEET_GRAD_MARGIN",
        "JARVIS_FLEET_GRAD_STABLE_CYCLES",
        "JARVIS_FLEET_CALIBRATION_PATH",
    }
    for name in expected:
        assert reg.get_spec(name) is not None, f"{name} not registered"
