"""Tests for AgentRegistry._route_to_canary() and _check_graduation()."""
import os
import threading
import time
import pytest

from backend.neural_mesh.registry.agent_registry import AgentRegistry


@pytest.fixture(autouse=True)
def reset_registry():
    AgentRegistry._instance = None
    yield
    AgentRegistry._instance = None


def test_route_to_canary_deterministic():
    """Same (domain_id, das_canary_key) always returns same routing decision."""
    registry = AgentRegistry()
    domain_id = "vision_action:xcode"
    key = "abc123canary"
    result1 = registry._route_to_canary(domain_id, key)
    result2 = registry._route_to_canary(domain_id, key)
    assert result1 == result2


def test_route_to_canary_zero_pct(monkeypatch):
    """DAS_CANARY_TRAFFIC_PCT=0 means no traffic goes to canary."""
    monkeypatch.setenv("DAS_CANARY_TRAFFIC_PCT", "0")
    registry = AgentRegistry()
    # No domain+key combination should route to canary at 0%
    assert not registry._route_to_canary("vision_action:xcode", "anykey")
    assert not registry._route_to_canary("browser_navigation:chrome", "anotherkey")


def test_route_to_canary_hundred_pct(monkeypatch):
    """DAS_CANARY_TRAFFIC_PCT=100 means all traffic goes to canary."""
    monkeypatch.setenv("DAS_CANARY_TRAFFIC_PCT", "100")
    registry = AgentRegistry()
    assert registry._route_to_canary("vision_action:xcode", "anykey")
    assert registry._route_to_canary("browser_navigation:chrome", "anotherkey")


def test_check_graduation_no_stats():
    """_check_graduation returns False when no canary stats exist for domain."""
    registry = AgentRegistry()
    assert not registry._check_graduation("some_domain:some_app")


def test_check_graduation_insufficient_requests(monkeypatch):
    """_check_graduation returns False when request count is below minimum."""
    monkeypatch.setenv("DAS_CANARY_MIN_REQUESTS", "10")
    registry = AgentRegistry()
    registry._canary_stats = {
        "vision_action:xcode": {
            "requests": 5,
            "errors": 0,
            "start_ts": time.time() - 10,
            "distinct_sessions": {"s1"},
        }
    }
    assert not registry._check_graduation("vision_action:xcode")


def test_check_graduation_passes_with_sufficient_data(monkeypatch):
    """_check_graduation returns True when all gates are satisfied."""
    monkeypatch.setenv("DAS_CANARY_MIN_REQUESTS", "10")
    monkeypatch.setenv("DAS_CANARY_MAX_ERROR_RATE", "0.01")
    registry = AgentRegistry()
    registry._canary_stats = {
        "vision_action:xcode": {
            "requests": 15,
            "errors": 0,
            "start_ts": time.time() - 400,
            "distinct_sessions": {"s1", "s2", "s3", "s4"},
        }
    }
    assert registry._check_graduation("vision_action:xcode")


def test_check_graduation_fails_high_error_rate(monkeypatch):
    """_check_graduation returns False when error rate exceeds threshold."""
    monkeypatch.setenv("DAS_CANARY_MIN_REQUESTS", "5")
    monkeypatch.setenv("DAS_CANARY_MAX_ERROR_RATE", "0.01")
    registry = AgentRegistry()
    registry._canary_stats = {
        "vision_action:xcode": {
            "requests": 10,
            "errors": 3,  # 30% error rate
            "start_ts": time.time() - 400,
            "distinct_sessions": {"s1", "s2", "s3"},
        }
    }
    assert not registry._check_graduation("vision_action:xcode")
