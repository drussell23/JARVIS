"""Tests for the REAL air-gapped 3-repo Trinity compose generator (G2).

NO real Docker. Pure generation + static sinkhole assertion. Roots are passed
in and MUST appear verbatim (no hardcoded paths).
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.saga.trinity_compose_generator import (
    TRINITY_NETWORK,
    assert_sinkhole,
    generate_trinity_compose,
    serialize_compose,
)
from backend.core.ouroboros.governance.saga.trinity_integration_gate import (
    EGRESS_MOCK_SERVICE,
    LIVE_PROVIDER_HOSTS,
)

yaml = pytest.importorskip("yaml")

_JARVIS = "/repos/jarvis"
_PRIME = "/repos/J-Prime"
_REACTOR = "/repos/reactor-core"


def _gen():
    return generate_trinity_compose(
        jarvis_root=_JARVIS,
        prime_root=_PRIME,
        reactor_root=_REACTOR,
        mock_port=9900,
    )


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #
def test_generates_three_services_plus_egress_mock():
    compose = _gen()
    services = compose["services"]
    assert {"jarvis", "prime", "reactor", EGRESS_MOCK_SERVICE} == set(services)


def test_roots_are_the_passed_args_no_hardcoded_paths():
    compose = _gen()
    services = compose["services"]
    assert services["jarvis"]["volumes"] == ["%s:/app:ro" % _JARVIS]
    assert services["prime"]["volumes"] == ["%s:/app:ro" % _PRIME]
    assert services["reactor"]["volumes"] == ["%s:/app:ro" % _REACTOR]
    # Mounts are read-only -> no cross-repo write through the sandbox.
    for svc in ("jarvis", "prime", "reactor"):
        assert services[svc]["volumes"][0].endswith(":ro")


def test_different_roots_produce_different_compose():
    a = generate_trinity_compose(jarvis_root="/a", prime_root="/b", reactor_root="/c")
    b = generate_trinity_compose(jarvis_root="/x", prime_root="/y", reactor_root="/z")
    assert a["services"]["prime"]["volumes"] != b["services"]["prime"]["volumes"]


def test_deterministic_same_args_same_output():
    assert serialize_compose(_gen()) == serialize_compose(_gen())


def test_internal_network_declared():
    compose = _gen()
    nets = compose["networks"]
    assert set(nets) == {TRINITY_NETWORK}
    assert nets[TRINITY_NETWORK]["internal"] is True


def test_jarvis_depends_on_prime_and_reactor_service_healthy():
    compose = _gen()
    deps = compose["services"]["jarvis"]["depends_on"]
    assert deps["prime"]["condition"] == "service_healthy"
    assert deps["reactor"]["condition"] == "service_healthy"


def test_prime_and_reactor_have_healthchecks_jarvis_does_not():
    compose = _gen()
    services = compose["services"]
    assert "healthcheck" in services["prime"]
    assert "healthcheck" in services["reactor"]
    # jarvis is the driver (no one depends on it) -> no healthcheck needed.
    assert "healthcheck" not in services["jarvis"]
    # healthcheck hits /health (verified ground-truth endpoints).
    hc = services["prime"]["healthcheck"]["test"]
    assert any("/health" in part for part in hc)


def test_prime_and_reactor_run_their_own_servers():
    compose = _gen()
    assert "run_server.py" in " ".join(compose["services"]["prime"]["command"])
    assert "run_reactor.py" in " ".join(compose["services"]["reactor"]["command"])


def test_every_service_provider_urls_point_at_egress_mock():
    compose = _gen()
    for name in ("jarvis", "prime", "reactor"):
        env = compose["services"][name]["environment"]
        assert env["DOUBLEWORD_BASE_URL"].startswith("http://%s:" % EGRESS_MOCK_SERVICE)
        assert env["ANTHROPIC_BASE_URL"].startswith("http://%s:" % EGRESS_MOCK_SERVICE)


def test_health_knobs_env_tunable(monkeypatch):
    monkeypatch.setenv("JARVIS_TRINITY_HEALTH_INTERVAL_S", "9")
    monkeypatch.setenv("JARVIS_TRINITY_HEALTH_RETRIES", "3")
    compose = _gen()
    hc = compose["services"]["reactor"]["healthcheck"]
    assert hc["interval"] == "9s"
    assert hc["retries"] == 3


def test_base_image_env_tunable(monkeypatch):
    monkeypatch.setenv("JARVIS_TRINITY_BASE_IMAGE", "python:3.12-slim")
    compose = _gen()
    assert compose["services"]["prime"]["image"] == "python:3.12-slim"


# --------------------------------------------------------------------------- #
# assert_sinkhole — fail-CLOSED static guarantee
# --------------------------------------------------------------------------- #
def test_assert_sinkhole_true_for_generated_compose():
    ok, reason = assert_sinkhole(_gen())
    assert ok, reason


def test_assert_sinkhole_false_if_network_not_internal():
    compose = _gen()
    compose["networks"][TRINITY_NETWORK]["internal"] = False
    ok, reason = assert_sinkhole(compose)
    assert not ok and "internal" in reason


def test_assert_sinkhole_false_if_live_provider_host_leaks():
    compose = _gen()
    compose["services"]["prime"]["environment"]["DOUBLEWORD_BASE_URL"] = (
        "https://%s" % LIVE_PROVIDER_HOSTS[1]
    )
    ok, reason = assert_sinkhole(compose)
    assert not ok and "live_provider_host_leaked" in reason


def test_assert_sinkhole_false_if_service_publishes_host_port():
    compose = _gen()
    compose["services"]["reactor"]["ports"] = ["8090:8090"]
    ok, reason = assert_sinkhole(compose)
    assert not ok and "publishes_host_port" in reason


def test_assert_sinkhole_false_if_service_on_extra_network():
    compose = _gen()
    compose["services"]["jarvis"]["networks"] = [TRINITY_NETWORK, "host_net"]
    ok, reason = assert_sinkhole(compose)
    assert not ok and "internal_network" in reason


def test_assert_sinkhole_false_for_non_mock_url():
    compose = _gen()
    compose["services"]["jarvis"]["environment"]["SOME_URL"] = "http://evil.example.com"
    ok, reason = assert_sinkhole(compose)
    assert not ok and "non_mock_url_in_env" in reason


def test_serialize_round_trips_through_yaml():
    compose = _gen()
    text = serialize_compose(compose)
    reparsed = yaml.safe_load(text)
    ok, reason = assert_sinkhole(reparsed)
    assert ok, reason
