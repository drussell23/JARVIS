"""Sovereign Transport Profiler Matrix tests (2026-06-20).

Learn-then-detach immortal batch-only profile: 1-strike record, persisted across
forks, optional TTL re-probe, gated + fail-soft."""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.dw_transport_profile import (
    TransportProfile,
    transport_profile_enabled,
)


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_TRANSPORT_PROFILE_ENABLED", "true")
    monkeypatch.delenv("JARVIS_DW_TRANSPORT_PROFILE_TTL_S", raising=False)


def _prof():
    return TransportProfile(path=Path(tempfile.mktemp()))


def test_record_then_is_batch_only():
    p = _prof()
    assert p.is_batch_only("Qwen/Qwen3.5-397B-A17B-FP8-dottxt") is False
    p.record_batch_only("Qwen/Qwen3.5-397B-A17B-FP8-dottxt")
    assert p.is_batch_only("Qwen/Qwen3.5-397B-A17B-FP8-dottxt") is True


def test_unknown_model_not_batch_only():
    assert _prof().is_batch_only("openai/gpt-oss-120b") is False


def test_persists_to_disk():
    path = Path(tempfile.mktemp())
    TransportProfile(path=path).record_batch_only("m-dottxt")
    payload = json.loads(path.read_text())
    assert "m-dottxt" in payload.get("batch_only", {})
    assert payload["schema_version"] == "transport_profile.1"


def test_survives_fork_rehydration():
    path = Path(tempfile.mktemp())
    TransportProfile(path=path).record_batch_only("m-dottxt")
    # Fresh instance = the forked subprocess rehydrating from disk.
    assert TransportProfile(path=path).is_batch_only("m-dottxt") is True


def test_immortal_by_default_no_decay():
    p = _prof()
    p.record_batch_only("m")
    # Backdate 100 days — with default TTL=0 (immortal) it must NOT decay.
    p._batch_only["m"] = time.time() - (100 * 24 * 3600)
    assert p.is_batch_only("m") is True


def test_ttl_decay_reopens_rt_probe(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_TRANSPORT_PROFILE_TTL_S", "3600")  # 1h
    p = _prof()
    p.record_batch_only("m")
    assert p.is_batch_only("m") is True
    # Backdate beyond the TTL → decays on read → re-opens RT re-probe.
    p._batch_only["m"] = time.time() - (2 * 3600)
    assert p.is_batch_only("m") is False
    assert "m" not in p._batch_only


def test_disabled_never_tags(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_TRANSPORT_PROFILE_ENABLED", "0")
    p = _prof()
    p.record_batch_only("m")
    assert p.is_batch_only("m") is False


def test_default_enabled(monkeypatch):
    monkeypatch.delenv("JARVIS_DW_TRANSPORT_PROFILE_ENABLED", raising=False)
    assert transport_profile_enabled() is True


def test_clear_drops_tag():
    p = _prof()
    p.record_batch_only("m")
    p.clear("m")
    assert p.is_batch_only("m") is False


def test_ttl_clamped_to_30_days(monkeypatch):
    from backend.core.ouroboros.governance.dw_transport_profile import _profile_ttl_s
    monkeypatch.setenv("JARVIS_DW_TRANSPORT_PROFILE_TTL_S", "999999999")
    assert _profile_ttl_s() == 30 * 24 * 3600.0
    monkeypatch.setenv("JARVIS_DW_TRANSPORT_PROFILE_TTL_S", "-5")
    assert _profile_ttl_s() == 0.0


def test_record_never_raises():
    p = _prof()
    p.record_batch_only("")       # empty
    p.record_batch_only(None)     # type: ignore
    assert p.is_batch_only("") is False


def test_idempotent_refresh():
    p = _prof()
    p.record_batch_only("m")
    t1 = p._batch_only["m"]
    time.sleep(0.01)
    p.record_batch_only("m")
    assert p._batch_only["m"] >= t1  # refreshed, single entry
    assert list(p._batch_only.keys()) == ["m"]
