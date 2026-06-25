"""Tests for gcp_compute_rest.py -- native async GCP Compute REST client.

ZERO real GCP / network: the metadata server AND the Compute API HTTP are
mocked at the _http_request boundary. Covers:
  * metadata client extracts token / scopes / zone (strip path) / project
  * verify_compute_scopes -> OK with cloud-platform / compute; IAM_PERMISSION_DENIED
    without; metadata-unreachable -> graceful IAM_PERMISSION_DENIED (no raise)
  * create_instance builds the correct payload (sourceImage family, machineType
    with the detected zone, Spot+DELETE, startup-script) + posts to the
    zone-correct URL
  * Spot-insert-fails -> on-demand fallback
  * await_running_ip polls until RUNNING + extracts networkIP; never-RUNNING ->
    bounded-timeout None (fail-soft)
  * delete hits the right URL; 404 idempotent-OK
  * metadata-unreachable -> fail-soft (no crash)
  * no hardcoded zone/project/IP -- all come from metadata
"""
from __future__ import annotations

import json

import pytest

import backend.core.ouroboros.governance.gcp_compute_rest as gr
from backend.core.ouroboros.governance.gcp_compute_rest import GCPComputeRest

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# A mock _http_request that routes metadata + compute API by URL.
# ---------------------------------------------------------------------------

class FakeHTTP:
    """Records requests; returns scripted (status, text) per URL substring."""

    def __init__(self) -> None:
        self.calls = []
        # Default metadata fixtures (dynamic identity -- nothing hardcoded in
        # the client; the values live ONLY here in the fake).
        self.token = json.dumps({"access_token": "ya29.FAKE", "expires_in": 3599})
        self.scopes_text = "https://www.googleapis.com/auth/cloud-platform"
        self.zone_text = "projects/123456789/zones/us-west2-b"
        self.project_text = "my-test-project"
        # Compute-API scripted responses (callable or (status, text)).
        self.insert_responses = [(200, json.dumps({"name": "op-insert"}))]
        self._insert_idx = 0
        self.get_responses = [(200, json.dumps({
            "status": "RUNNING",
            "networkInterfaces": [{"networkIP": "10.128.0.42"}],
        }))]
        self._get_idx = 0
        self.delete_response = (200, json.dumps({"name": "op-delete"}))

    async def __call__(self, url, *, method="GET", headers=None, body=None, timeout_s=10.0):
        self.calls.append({"url": url, "method": method, "headers": headers, "body": body})
        # Metadata routes.
        if "metadata.google.internal" in url:
            if url.endswith("/token"):
                return (200, self.token)
            if url.endswith("/scopes"):
                return (200, self.scopes_text)
            if url.endswith("instance/zone"):
                return (200, self.zone_text)
            if url.endswith("project/project-id"):
                return (200, self.project_text)
            return (404, "")
        # Compute API routes.
        if "compute.googleapis.com" in url:
            if method == "POST":
                resp = self.insert_responses[min(self._insert_idx, len(self.insert_responses) - 1)]
                self._insert_idx += 1
                return resp
            if method == "GET":
                resp = self.get_responses[min(self._get_idx, len(self.get_responses) - 1)]
                self._get_idx += 1
                return resp
            if method == "DELETE":
                return self.delete_response
        return (0, "[unrouted]")


@pytest.fixture
def http(monkeypatch):
    fake = FakeHTTP()
    monkeypatch.setattr(gr, "_http_request", fake)
    # Clear any env identity overrides so identity comes from metadata only.
    for var in ("GCP_PROJECT_ID", "GOOGLE_CLOUD_PROJECT", "GCP_ZONE"):
        monkeypatch.delenv(var, raising=False)
    # Tight poll so the never-RUNNING timeout test is fast.
    monkeypatch.setenv("JARVIS_FAILOVER_RUNNING_POLL_S", "0.1")
    return fake


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------

async def test_metadata_extracts_token_scopes_zone_project(http):
    c = GCPComputeRest()
    assert await c.access_token() == "ya29.FAKE"
    assert await c.scopes() == ["https://www.googleapis.com/auth/cloud-platform"]
    # Zone is stripped to the LAST path component (no hardcoding).
    assert await c.zone() == "us-west2-b"
    assert await c.project() == "my-test-project"


async def test_zone_override_from_env_is_stripped(monkeypatch, http):
    monkeypatch.setenv("GCP_ZONE", "europe-west1-d")
    c = GCPComputeRest()
    assert await c.zone() == "europe-west1-d"


# ---------------------------------------------------------------------------
# IAM scope self-verification
# ---------------------------------------------------------------------------

async def test_verify_scopes_ok_with_cloud_platform(http):
    c = GCPComputeRest()
    ok, detail = await c.verify_compute_scopes()
    assert ok is True
    assert "compute_scope_present" in detail


async def test_verify_scopes_ok_with_compute_scope(http):
    http.scopes_text = "https://www.googleapis.com/auth/compute"
    c = GCPComputeRest()
    ok, _ = await c.verify_compute_scopes()
    assert ok is True


async def test_verify_scopes_denied_without_compute(http):
    http.scopes_text = (
        "https://www.googleapis.com/auth/devstorage.read_only\n"
        "https://www.googleapis.com/auth/logging.write"
    )
    c = GCPComputeRest()
    ok, detail = await c.verify_compute_scopes()
    assert ok is False
    assert detail.startswith("IAM_PERMISSION_DENIED:missing_compute_scope:")


async def test_verify_scopes_metadata_unreachable_is_graceful(monkeypatch, http):
    # Simulate off-GCE: metadata returns nothing for scopes.
    async def _unreachable(url, **kw):
        return (0, "[urllib error]")
    monkeypatch.setattr(gr, "_http_request", _unreachable)
    c = GCPComputeRest()
    ok, detail = await c.verify_compute_scopes()
    assert ok is False
    assert detail == "IAM_PERMISSION_DENIED:metadata_unreachable"


# ---------------------------------------------------------------------------
# create_instance payload + URL + Spot-first
# ---------------------------------------------------------------------------

async def test_create_instance_builds_correct_payload_and_url(http, monkeypatch):
    monkeypatch.setenv("JPRIME_IMAGE_FAMILY", "jarvis-prime-coder")
    monkeypatch.setenv("JARVIS_FAILOVER_MACHINE_TYPE", "e2-highmem-2")
    monkeypatch.setenv("JARVIS_FAILOVER_NODE_NAME", "jarvis-prime-failover")
    c = GCPComputeRest()
    ok, detail = await c.create_instance(startup_script="#!/bin/bash\necho hi")
    assert ok is True
    assert detail.startswith("created:SPOT")

    post = [call for call in http.calls if call["method"] == "POST"][0]
    # Zone-correct URL -- zone came from metadata (us-west2-b), NOT a literal.
    assert post["url"] == (
        "https://compute.googleapis.com/compute/v1/projects/my-test-project"
        "/zones/us-west2-b/instances"
    )
    payload = json.loads(post["body"].decode("utf-8"))
    assert payload["name"] == "jarvis-prime-failover"
    # machineType embeds the detected zone (no hardcoding).
    assert payload["machineType"] == "zones/us-west2-b/machineTypes/e2-highmem-2"
    # sourceImage is the golden-image FAMILY, project from metadata.
    assert payload["disks"][0]["initializeParams"]["sourceImage"] == (
        "projects/my-test-project/global/images/family/jarvis-prime-coder"
    )
    # Spot scheduling: provisioningModel SPOT + terminationAction DELETE.
    assert payload["scheduling"]["provisioningModel"] == "SPOT"
    assert payload["scheduling"]["instanceTerminationAction"] == "DELETE"
    # startup-script is in the metadata items.
    items = payload["metadata"]["items"]
    assert any(it["key"] == "startup-script" and "echo hi" in it["value"] for it in items)


async def test_create_instance_spot_fails_falls_back_to_on_demand(http):
    # Spot POST 409 -> on-demand POST 200.
    http.insert_responses = [
        (409, "spot capacity unavailable"),
        (200, json.dumps({"name": "op-ondemand"})),
    ]
    c = GCPComputeRest()
    ok, detail = await c.create_instance(startup_script="x")
    assert ok is True
    assert detail.startswith("created:on-demand")
    posts = [call for call in http.calls if call["method"] == "POST"]
    assert len(posts) == 2
    # First was Spot, second on-demand (no scheduling block).
    p1 = json.loads(posts[0]["body"].decode())
    p2 = json.loads(posts[1]["body"].decode())
    assert "scheduling" in p1
    assert "scheduling" not in p2


async def test_create_instance_no_token_fails_closed(monkeypatch, http):
    async def _no_token(url, **kw):
        if url.endswith("/token"):
            return (0, "[urllib error]")
        return await FakeHTTP().__call__(url, **kw)
    monkeypatch.setattr(gr, "_http_request", _no_token)
    c = GCPComputeRest()
    ok, detail = await c.create_instance(startup_script="x")
    assert ok is False
    assert detail.startswith("AUTH_TOKEN_UNAVAILABLE")


# ---------------------------------------------------------------------------
# await_running_ip
# ---------------------------------------------------------------------------

async def test_await_running_ip_polls_until_running(http):
    # First GET -> PROVISIONING, second -> RUNNING with the internal IP.
    http.get_responses = [
        (200, json.dumps({"status": "PROVISIONING", "networkInterfaces": []})),
        (200, json.dumps({
            "status": "RUNNING",
            "networkInterfaces": [{"networkIP": "10.128.0.99"}],
        })),
    ]
    c = GCPComputeRest()
    ip = await c.await_running_ip(timeout_s=5.0, poll_s=0.01)
    assert ip == "10.128.0.99"
    gets = [
        call for call in http.calls
        if call["method"] == "GET" and "compute.googleapis.com" in call["url"]
    ]
    # Zone-correct GET URL (dynamic zone/project, dynamic instance name).
    assert gets[0]["url"] == (
        "https://compute.googleapis.com/compute/v1/projects/my-test-project"
        "/zones/us-west2-b/instances/jarvis-prime-failover"
    )


async def test_await_running_ip_never_running_times_out_failsoft(http):
    http.get_responses = [
        (200, json.dumps({"status": "PROVISIONING", "networkInterfaces": []})),
    ]
    c = GCPComputeRest()
    ip = await c.await_running_ip(timeout_s=0.05, poll_s=0.01)
    assert ip is None  # bounded timeout, no raise


async def test_await_running_ip_extracts_internal_not_external(http):
    # Internal IP is networkIP -- not a hardcoded value; comes from the response.
    http.get_responses = [
        (200, json.dumps({
            "status": "RUNNING",
            "networkInterfaces": [{
                "networkIP": "10.10.10.10",
                "accessConfigs": [{"natIP": "203.0.113.1"}],
            }],
        })),
    ]
    c = GCPComputeRest()
    ip = await c.await_running_ip(timeout_s=2.0, poll_s=0.01)
    assert ip == "10.10.10.10"


# ---------------------------------------------------------------------------
# delete_instance
# ---------------------------------------------------------------------------

async def test_delete_instance_hits_correct_url(http):
    c = GCPComputeRest()
    ok, detail = await c.delete_instance()
    assert ok is True
    delete = [call for call in http.calls if call["method"] == "DELETE"][0]
    assert delete["url"] == (
        "https://compute.googleapis.com/compute/v1/projects/my-test-project"
        "/zones/us-west2-b/instances/jarvis-prime-failover"
    )
    # Bearer token from metadata is attached.
    assert delete["headers"]["Authorization"] == "Bearer ya29.FAKE"


async def test_delete_instance_404_is_idempotent_ok(http):
    http.delete_response = (404, "not found")
    c = GCPComputeRest()
    ok, detail = await c.delete_instance()
    assert ok is True
    assert "404" in detail


async def test_delete_instance_no_token_fails_closed(monkeypatch, http):
    async def _no_token(url, **kw):
        if url.endswith("/token"):
            return (0, "")
        return (200, "")
    monkeypatch.setattr(gr, "_http_request", _no_token)
    c = GCPComputeRest()
    ok, detail = await c.delete_instance()
    assert ok is False
    assert detail.startswith("AUTH_TOKEN_UNAVAILABLE")


# ---------------------------------------------------------------------------
# No hardcoding guard: identity is fully metadata-driven.
# ---------------------------------------------------------------------------

async def test_identity_is_fully_dynamic_from_metadata(http):
    # Change the metadata fixtures -> the client must follow (no literals).
    http.zone_text = "projects/9/zones/asia-northeast1-c"
    http.project_text = "another-project"
    c = GCPComputeRest()
    await c.create_instance(startup_script="x")
    post = [call for call in http.calls if call["method"] == "POST"][0]
    assert "asia-northeast1-c" in post["url"]
    assert "another-project" in post["url"]
