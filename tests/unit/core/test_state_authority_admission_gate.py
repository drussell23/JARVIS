import os

from backend.core import state_authority as sa


def test_publish_heavy_spawn_admission_closes_on_sequential_mode(monkeypatch):
    monkeypatch.setenv("JARVIS_STARTUP_EFFECTIVE_MODE", "sequential")
    monkeypatch.delenv("JARVIS_BACKEND_MINIMAL", raising=False)

    result = sa.publish_heavy_spawn_admission(
        context="unit_test",
        min_available_gb=1.5,
        mode="sequential",
        available_gb=8.0,
    )

    assert result.consistent is False
    assert result.authoritative_value == "false"
    assert os.environ["JARVIS_CAN_SPAWN_HEAVY"] == "false"
    assert os.environ["JARVIS_STARTUP_EFFECTIVE_MODE"] == "sequential"
    assert "mode=sequential" in os.environ["JARVIS_HEAVY_ADMISSION_REASON"]


def test_can_spawn_heavy_allows_local_full_with_headroom(monkeypatch):
    monkeypatch.setenv("JARVIS_STARTUP_EFFECTIVE_MODE", "local_full")
    monkeypatch.delenv("JARVIS_BACKEND_MINIMAL", raising=False)

    result = sa.can_spawn_heavy(
        min_available_gb=1.5,
        mode="local_full",
        available_gb=6.5,
    )

    assert result.consistent is True
    assert result.authoritative_value == "true"


def test_validate_heavy_spawn_admission_detects_mode_gate_mismatch(monkeypatch):
    monkeypatch.setenv("JARVIS_STARTUP_EFFECTIVE_MODE", "sequential")
    monkeypatch.setenv("JARVIS_CAN_SPAWN_HEAVY", "true")

    results = sa.validate_consistency(concept="heavy_spawn_admission")
    assert len(results) == 1
    assert results[0].consistent is False
    assert any("effective_mode=sequential" in d for d in results[0].divergences)
