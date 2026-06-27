from __future__ import annotations
import backend.core.ouroboros.battle_test.harness as H


def test_adaptive_interval_inversely_proportional(monkeypatch):
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_ADAPTIVE_POLLING_ENABLED", "true")
    assert H.rg_poll_interval_for("ok")       == 10.0
    assert H.rg_poll_interval_for("warn")     == 3.0
    assert H.rg_poll_interval_for("high")     == 0.5
    assert H.rg_poll_interval_for("critical") == 0.2
    # unknown level -> OK interval
    assert H.rg_poll_interval_for("???")      == 10.0


def test_backstop_interval_has_fixed_floor(monkeypatch):
    monkeypatch.delenv("JARVIS_RESOURCE_GOVERNOR_BACKSTOP_INTERVAL_S", raising=False)
    assert H.rg_backstop_interval_s() == 1.0
