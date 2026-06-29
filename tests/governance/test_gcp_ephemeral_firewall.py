"""REST-native ephemeral firewall micro-perimeter + dynamic public-IP discovery.

The hybrid local->cloud handoff is blocked because :11434 is firewalled to the
VPC. The fix injects a /32-scoped ephemeral firewall rule (the orchestrator's
OWN detected public egress IP -> tcp:11434) at AWAKENING via the SAME native
Compute REST bridge used for provisioning -- and tears it down alongside the
node on every exit path. ZERO hardcoded IPs, zero gcloud, zero orphan holes.

TDD with the HTTP boundary monkeypatched -- ZERO real network.
"""
from __future__ import annotations

import json

import pytest

import backend.core.ouroboros.governance.gcp_compute_rest as gcr


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT_ID", "jarvis-473803")
    monkeypatch.setenv("GCP_ZONE", "us-central1-b")
    monkeypatch.setenv("JARVIS_FAILOVER_USE_ADC", "false")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    # A token is present (auth already proven elsewhere); these tests exercise
    # the firewall REST contract, not auth.
    async def _fake_token(self):  # noqa: ANN001
        return "fake-token"
    monkeypatch.setattr(gcr.GCPComputeRest, "access_token", _fake_token)
    yield


# ---------------------------------------------------------------------------
# Dynamic public-IP self-discovery
# ---------------------------------------------------------------------------

async def test_resolve_public_ip_returns_validated_ipv4(monkeypatch):
    monkeypatch.setattr(gcr, "_fetch_public_ip", lambda: "203.0.113.7")
    assert await gcr.resolve_local_public_ip() == "203.0.113.7"


async def test_resolve_public_ip_strips_whitespace(monkeypatch):
    monkeypatch.setattr(gcr, "_fetch_public_ip", lambda: "  203.0.113.7\n")
    assert await gcr.resolve_local_public_ip() == "203.0.113.7"


async def test_resolve_public_ip_rejects_garbage(monkeypatch):
    monkeypatch.setattr(gcr, "_fetch_public_ip", lambda: "not-an-ip")
    assert await gcr.resolve_local_public_ip() is None


async def test_resolve_public_ip_failsoft(monkeypatch):
    def _boom():
        raise RuntimeError("no network")
    monkeypatch.setattr(gcr, "_fetch_public_ip", _boom)
    assert await gcr.resolve_local_public_ip() is None


# ---------------------------------------------------------------------------
# REST firewall create -- /32 scoped, tcp:11434 only
# ---------------------------------------------------------------------------

async def test_create_firewall_rule_payload(monkeypatch):
    seen = {}

    async def fake_http(url, *, method, headers=None, body=None, timeout_s=30.0):
        seen["url"] = url
        seen["method"] = method
        seen["payload"] = json.loads(body.decode()) if body else None
        return (200, '{"status":"PENDING"}')

    monkeypatch.setattr(gcr, "_http_request", fake_http)

    c = gcr.GCPComputeRest()
    ok, detail = await c.create_firewall_rule(
        name="jarvis-ephemeral-failover-allow", source_ip="203.0.113.7", port=11434,
    )
    assert ok is True
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/global/firewalls")
    p = seen["payload"]
    assert p["name"] == "jarvis-ephemeral-failover-allow"
    assert p["sourceRanges"] == ["203.0.113.7/32"]      # /32 -- single host
    allowed = p["allowed"][0]
    assert allowed["IPProtocol"] == "tcp"
    assert "11434" in [str(x) for x in allowed["ports"]]  # only the prime port


async def test_create_firewall_failsoft_on_http_error(monkeypatch):
    async def fake_http(url, *, method, headers=None, body=None, timeout_s=30.0):
        return (403, "forbidden")
    monkeypatch.setattr(gcr, "_http_request", fake_http)
    c = gcr.GCPComputeRest()
    ok, detail = await c.create_firewall_rule(
        name="x", source_ip="203.0.113.7", port=11434,
    )
    assert ok is False
    assert "403" in detail or "forbidden" in detail.lower()


async def test_create_firewall_requires_source_ip(monkeypatch):
    """No detected IP -> NO 0.0.0.0/0 fallback (never open to the internet)."""
    c = gcr.GCPComputeRest()
    ok, detail = await c.create_firewall_rule(name="x", source_ip="", port=11434)
    assert ok is False
    assert "no_source_ip" in detail or "source" in detail.lower()


# ---------------------------------------------------------------------------
# REST firewall delete -- the IaC teardown half
# ---------------------------------------------------------------------------

async def test_delete_firewall_rule(monkeypatch):
    seen = {}

    async def fake_http(url, *, method, headers=None, body=None, timeout_s=30.0):
        seen["url"] = url
        seen["method"] = method
        return (200, '{"status":"PENDING"}')

    monkeypatch.setattr(gcr, "_http_request", fake_http)
    c = gcr.GCPComputeRest()
    ok, detail = await c.delete_firewall_rule("jarvis-ephemeral-failover-allow")
    assert ok is True
    assert seen["method"] == "DELETE"
    assert seen["url"].endswith("/global/firewalls/jarvis-ephemeral-failover-allow")


async def test_delete_firewall_idempotent_on_404(monkeypatch):
    """A 404 (already gone) is treated as success -- no orphan-hole anxiety."""
    async def fake_http(url, *, method, headers=None, body=None, timeout_s=30.0):
        return (404, "not found")
    monkeypatch.setattr(gcr, "_http_request", fake_http)
    c = gcr.GCPComputeRest()
    ok, detail = await c.delete_firewall_rule("x")
    assert ok is True  # already absent == the desired end state


# ---------------------------------------------------------------------------
# Controller wiring: AWAKEN opens the perimeter; teardown closes it.
# ---------------------------------------------------------------------------

class _FakeRest:
    def __init__(self):
        self.created = []
        self.deleted = []

    async def create_firewall_rule(self, *, name, source_ip, port=11434):
        self.created.append((name, source_ip, port))
        return (True, "created:200")

    async def delete_firewall_rule(self, name):
        self.deleted.append(name)
        return (True, "deleted:200")


async def test_open_perimeter_resolves_ip_and_creates_rule(monkeypatch):
    import backend.core.ouroboros.governance.failover_lifecycle as fl
    monkeypatch.setenv("JARVIS_FAILOVER_EPHEMERAL_FW_ENABLED", "true")
    fake = _FakeRest()
    monkeypatch.setattr(gcr, "get_compute_rest", lambda: fake)
    monkeypatch.setattr(gcr, "resolve_local_public_ip",
                        lambda fetch_fn=None: _async("203.0.113.7"))

    ctrl = fl.FailoverLifecycleController(
        vm_awaken_fn=lambda *, startup_script: True, vm_delete_fn=lambda: True,
        node_ready_fn=lambda e: True, clock_fn=lambda: 1.0,
    )
    await ctrl._open_ephemeral_perimeter()
    assert fake.created and fake.created[0][1] == "203.0.113.7"
    assert ctrl._ephemeral_fw_rule == "jarvis-ephemeral-failover-allow"


async def test_open_perimeter_gate_off_no_rule(monkeypatch):
    import backend.core.ouroboros.governance.failover_lifecycle as fl
    monkeypatch.setenv("JARVIS_FAILOVER_EPHEMERAL_FW_ENABLED", "false")
    fake = _FakeRest()
    monkeypatch.setattr(gcr, "get_compute_rest", lambda: fake)
    ctrl = fl.FailoverLifecycleController(
        vm_awaken_fn=lambda *, startup_script: True, vm_delete_fn=lambda: True,
        node_ready_fn=lambda e: True, clock_fn=lambda: 1.0,
    )
    await ctrl._open_ephemeral_perimeter()
    assert fake.created == []
    assert ctrl._ephemeral_fw_rule is None


async def test_close_perimeter_deletes_and_clears(monkeypatch):
    import backend.core.ouroboros.governance.failover_lifecycle as fl
    fake = _FakeRest()
    monkeypatch.setattr(gcr, "get_compute_rest", lambda: fake)
    ctrl = fl.FailoverLifecycleController(
        vm_awaken_fn=lambda *, startup_script: True, vm_delete_fn=lambda: True,
        node_ready_fn=lambda e: True, clock_fn=lambda: 1.0,
    )
    ctrl._ephemeral_fw_rule = "jarvis-ephemeral-failover-allow"
    await ctrl._close_ephemeral_perimeter()
    assert fake.deleted == ["jarvis-ephemeral-failover-allow"]
    assert ctrl._ephemeral_fw_rule is None  # cleared -> no orphan hole


async def _async(v):
    return v
