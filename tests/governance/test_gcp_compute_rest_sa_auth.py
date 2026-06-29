"""Tests for the Dynamic IAM Credential Bridge (Hybrid Execution Mesh, 2026-06-28).

The native ``GCPComputeRest`` orchestrator authenticated ONLY via the GCE
metadata server -- so it could provision J-Prime only when the orchestrator was
ITSELF running on a GCE node. The Hybrid Mesh runs the orchestrator on a local
Mac and bridges to the real GCP J-Prime golden image. This requires an adaptive
auth resolver: when ``GOOGLE_APPLICATION_CREDENTIALS`` (a Service Account JSON)
is present, mint the Compute OAuth token via the native ``google-auth`` SDK and
issue the Compute REST calls directly -- ZERO gcloud CLI, ZERO metadata server.

TDD with an injected token minter -- ZERO real GCP / network.
"""
from __future__ import annotations

import pytest

import backend.core.ouroboros.governance.gcp_compute_rest as gcr


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    # Default: no SA creds, no project/zone override (legacy metadata mode).
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GCP_ZONE", raising=False)
    # Determinism: pin ADC OFF by default so a dev machine's real gcloud ADC
    # never leaks into the metadata/on-VPC tests. ADC tests override this.
    monkeypatch.setenv("JARVIS_FAILOVER_USE_ADC", "false")
    yield


# ---------------------------------------------------------------------------
# SA-credentials path detection
# ---------------------------------------------------------------------------

def test_sa_path_detected_from_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/secrets/sa.json")
    assert gcr._sa_credentials_path() == "/secrets/sa.json"


def test_sa_path_empty_when_unset():
    assert gcr._sa_credentials_path() == ""


# ---------------------------------------------------------------------------
# access_token() uses the SA minter when creds are present
# ---------------------------------------------------------------------------

async def test_access_token_uses_sa_minter_when_creds_present(monkeypatch):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/secrets/sa.json")
    monkeypatch.setattr(
        gcr, "mint_sa_access_token", lambda path: ("tok-abc", "proj-from-sa")
    )
    # If access_token reached metadata it would explode -- assert it never does.
    async def _boom(path):  # noqa: ANN001
        raise AssertionError("metadata must NOT be consulted under SA auth")
    monkeypatch.setattr(gcr.GCPComputeRest, "_metadata", _boom)

    client = gcr.GCPComputeRest()
    assert await client.access_token() == "tok-abc"


async def test_project_from_sa_when_no_override(monkeypatch):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/secrets/sa.json")
    monkeypatch.setattr(
        gcr, "mint_sa_access_token", lambda path: ("tok-abc", "proj-from-sa")
    )
    client = gcr.GCPComputeRest()
    assert await client.project() == "proj-from-sa"


async def test_env_project_override_wins_over_sa(monkeypatch):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/secrets/sa.json")
    monkeypatch.setenv("GCP_PROJECT_ID", "proj-env")
    monkeypatch.setattr(
        gcr, "mint_sa_access_token", lambda path: ("tok-abc", "proj-from-sa")
    )
    client = gcr.GCPComputeRest()
    assert await client.project() == "proj-env"


async def test_zone_from_env_under_sa(monkeypatch):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/secrets/sa.json")
    monkeypatch.setenv("GCP_ZONE", "us-central1-a")
    client = gcr.GCPComputeRest()
    assert await client.zone() == "us-central1-a"


# ---------------------------------------------------------------------------
# verify_compute_scopes() under SA auth
# ---------------------------------------------------------------------------

async def test_verify_compute_scopes_passes_with_sa(monkeypatch):
    """SA auth requests cloud-platform when minting -> scope-verify passes; the
    real IAM role is enforced server-side at instances.insert (a 403 surfaces
    there as a real create failure -- fail-CLOSED is preserved)."""
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/secrets/sa.json")
    monkeypatch.setattr(
        gcr, "mint_sa_access_token", lambda path: ("tok-abc", "proj-from-sa")
    )
    client = gcr.GCPComputeRest()
    ok, detail = await client.verify_compute_scopes()
    assert ok is True
    assert "sa" in detail.lower()


async def test_verify_compute_scopes_failsoft_when_sa_mint_fails(monkeypatch):
    """A broken/space-less SA JSON -> mint returns (None, None) -> fail-CLOSED
    (the loop is NOT crashed; awaken aborts cleanly, op stays sealed)."""
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/secrets/sa.json")
    monkeypatch.setattr(gcr, "mint_sa_access_token", lambda path: (None, None))
    client = gcr.GCPComputeRest()
    ok, detail = await client.verify_compute_scopes()
    assert ok is False
    assert "IAM_PERMISSION_DENIED" in detail


async def test_sa_mint_exception_is_failsoft(monkeypatch):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/secrets/sa.json")

    def _boom(path):  # noqa: ANN001
        raise RuntimeError("corrupt SA json")

    monkeypatch.setattr(gcr, "mint_sa_access_token", _boom)
    client = gcr.GCPComputeRest()
    # Must NOT raise -- fail-soft to None.
    assert await client.access_token() is None


# ---------------------------------------------------------------------------
# No SA creds -> byte-identical legacy metadata path
# ---------------------------------------------------------------------------

async def test_ensure_token_concurrent_mints_exactly_once(monkeypatch):
    """The parallel teardown gather (delete_instance + delete_firewall_rule on
    ONE client) raced _ensure_token: it set _cred_minted=True BEFORE the await
    completed, so the 2nd coroutine read the still-None cached token -> a bogus
    AUTH_OR_PROJECT_UNRESOLVED (an orphan firewall hole). An asyncio.Lock must
    serialize the mint so it runs EXACTLY once and every concurrent caller gets
    the real token."""
    import asyncio
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/secrets/sa.json")
    calls = {"n": 0}

    def _slow_mint(path):
        import time
        calls["n"] += 1
        time.sleep(0.05)  # widen the race window
        return ("tok-once", "proj-once")

    monkeypatch.setattr(gcr, "mint_sa_access_token", _slow_mint)
    client = gcr.GCPComputeRest()
    results = await asyncio.gather(*[client.access_token() for _ in range(6)])
    assert calls["n"] == 1                        # minted EXACTLY once (locked)
    assert all(r == "tok-once" for r in results)   # every caller got the token


async def test_no_sa_falls_back_to_metadata(monkeypatch):
    """Unset GOOGLE_APPLICATION_CREDENTIALS -> the metadata path is used exactly
    as before (the SA bridge composes ON TOP, never replaces)."""
    async def _fake_meta(self, path):  # noqa: ANN001
        if "token" in path:
            return '{"access_token": "meta-token"}'
        return None

    monkeypatch.setattr(gcr.GCPComputeRest, "_metadata", _fake_meta)
    # The minter must NOT be consulted with no creds present.
    monkeypatch.setattr(
        gcr, "mint_sa_access_token",
        lambda path: (_ for _ in ()).throw(AssertionError("SA minter called w/o creds")),
    )
    client = gcr.GCPComputeRest()
    assert await client.access_token() == "meta-token"


# ---------------------------------------------------------------------------
# Adaptive auth: gcloud Application Default Credentials (authorized_user) --
# the operator has ADC, not a SA JSON. The bridge must use it too.
# ---------------------------------------------------------------------------

async def test_adc_used_when_no_sa_but_adc_available(monkeypatch):
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setattr(gcr, "_adc_available", lambda: True)
    monkeypatch.setattr(gcr, "mint_adc_access_token", lambda: ("adc-tok", "jarvis-473803"))
    # metadata must NOT be consulted under ADC.
    async def _boom(self, path):  # noqa: ANN001
        raise AssertionError("metadata must NOT be consulted under ADC")
    monkeypatch.setattr(gcr.GCPComputeRest, "_metadata", _boom)

    client = gcr.GCPComputeRest()
    assert await client.access_token() == "adc-tok"
    assert await client.project() == "jarvis-473803"


async def test_adc_marks_hybrid_mesh(monkeypatch):
    """ADC implies the orchestrator is OFF-GCE -> external IP for the handoff."""
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setattr(gcr, "_adc_available", lambda: True)
    client = gcr.GCPComputeRest()
    assert client._select_reachable_ip(_DOC_BOTH_IPS) == "34.72.10.20"


async def test_adc_verify_scopes_passes(monkeypatch):
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setattr(gcr, "_adc_available", lambda: True)
    monkeypatch.setattr(gcr, "mint_adc_access_token", lambda: ("adc-tok", "p"))
    client = gcr.GCPComputeRest()
    ok, detail = await client.verify_compute_scopes()
    assert ok is True
    assert "adc" in detail.lower() or "credential" in detail.lower()


async def test_sa_wins_over_adc(monkeypatch):
    """When BOTH a SA JSON and ADC exist, the explicit SA JSON wins."""
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/secrets/sa.json")
    monkeypatch.setattr(gcr, "_adc_available", lambda: True)
    monkeypatch.setattr(gcr, "mint_sa_access_token", lambda path: ("sa-tok", "sa-proj"))
    monkeypatch.setattr(
        gcr, "mint_adc_access_token",
        lambda: (_ for _ in ()).throw(AssertionError("ADC must not be used when SA present")),
    )
    client = gcr.GCPComputeRest()
    assert await client.access_token() == "sa-tok"


# ---------------------------------------------------------------------------
# Hybrid data-plane handoff: a LOCAL orchestrator must reach the node's
# EXTERNAL ephemeral IP (natIP), not the in-VPC internal IP.
# ---------------------------------------------------------------------------

_DOC_BOTH_IPS = {
    "status": "RUNNING",
    "networkInterfaces": [
        {
            "networkIP": "10.128.0.5",
            "accessConfigs": [{"type": "ONE_TO_ONE_NAT", "natIP": "34.72.10.20"}],
        }
    ],
}


def test_extract_external_ip_from_doc():
    assert gcr.GCPComputeRest._extract_external_ip(_DOC_BOTH_IPS) == "34.72.10.20"


def test_extract_external_ip_none_when_no_access_config():
    doc = {"networkInterfaces": [{"networkIP": "10.128.0.5"}]}
    assert gcr.GCPComputeRest._extract_external_ip(doc) is None


def test_hybrid_mode_selects_external_ip(monkeypatch):
    """Off-GCE (SA creds present) -> the reachable IP is the EXTERNAL natIP."""
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/secrets/sa.json")
    client = gcr.GCPComputeRest()
    assert client._select_reachable_ip(_DOC_BOTH_IPS) == "34.72.10.20"


def test_onvpc_mode_selects_internal_ip():
    """On-GCE (no SA creds, no hybrid flag) -> internal IP, byte-identical."""
    client = gcr.GCPComputeRest()
    assert client._select_reachable_ip(_DOC_BOTH_IPS) == "10.128.0.5"


def test_hybrid_flag_forces_external_even_without_sa(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_HYBRID_MESH", "true")
    client = gcr.GCPComputeRest()
    assert client._select_reachable_ip(_DOC_BOTH_IPS) == "34.72.10.20"


def test_hybrid_falls_back_to_internal_when_no_external(monkeypatch):
    """Hybrid mode but the node has no external IP yet -> fall back to internal
    (fail-soft -- never return None when an internal IP exists)."""
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/secrets/sa.json")
    doc = {"networkInterfaces": [{"networkIP": "10.128.0.5"}]}
    client = gcr.GCPComputeRest()
    assert client._select_reachable_ip(doc) == "10.128.0.5"
