"""test_a1_gcp_preflight -- TDD spine for scripts/a1_gcp_preflight.py.

All checks are $0: NO network calls, NO node creation, NO real GCPComputeRest.
A fake rest object is injected via the ``rest=`` parameter.

asyncio_mode = auto (pytest.ini), so no @pytest.mark.asyncio needed.
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest  # noqa: E402

from scripts.a1_gcp_preflight import preflight_gcp_ready  # noqa: E402


# ---------------------------------------------------------------------------
# Fake rest object -- mirrors the real GCPComputeRest async interface.
# ---------------------------------------------------------------------------

class _FakeRest:
    """Minimal injectable double for GCPComputeRest."""

    def __init__(
        self,
        *,
        auth_mode: str = "adc",
        scope_ok: bool = True,
        project_val: str | None = "jarvis-473803",
        zone_val: str | None = "us-central1-a",
        project_raises: bool = False,
        zone_raises: bool = False,
    ) -> None:
        self._auth_mode = auth_mode
        self._scope_ok = scope_ok
        self._project_val = project_val
        self._zone_val = zone_val
        self._project_raises = project_raises
        self._zone_raises = zone_raises

    async def verify_compute_scopes(self):
        return (self._scope_ok, "fake:scope_result")

    async def access_token(self):
        return "fake-token" if self._scope_ok else None

    async def project(self):
        if self._project_raises:
            raise RuntimeError("injected project() failure")
        return self._project_val

    async def zone(self):
        if self._zone_raises:
            raise RuntimeError("injected zone() failure")
        return self._zone_val


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_all_present_returns_ok():
    """Happy path: valid auth + project + zone -> (True, [])."""
    ok, problems = await preflight_gcp_ready(
        rest=_FakeRest(auth_mode="adc", scope_ok=True,
                       project_val="jarvis-473803", zone_val="us-central1-a")
    )
    assert ok is True
    assert problems == []


async def test_missing_zone_reports_export():
    """Empty zone -> ok=False, problem contains export GCP_ZONE=."""
    ok, problems = await preflight_gcp_ready(
        rest=_FakeRest(auth_mode="adc", scope_ok=True,
                       project_val="jarvis-473803", zone_val="")
    )
    assert ok is False
    assert any("export GCP_ZONE=" in p for p in problems), problems


async def test_missing_zone_none_reports_export():
    """None zone -> same export GCP_ZONE= message."""
    ok, problems = await preflight_gcp_ready(
        rest=_FakeRest(auth_mode="adc", scope_ok=True,
                       project_val="jarvis-473803", zone_val=None)
    )
    assert ok is False
    assert any("export GCP_ZONE=" in p for p in problems), problems


async def test_missing_project_reports_export():
    """None project -> ok=False, problem contains export GCP_PROJECT_ID=."""
    ok, problems = await preflight_gcp_ready(
        rest=_FakeRest(auth_mode="adc", scope_ok=True,
                       project_val=None, zone_val="us-central1-a")
    )
    assert ok is False
    assert any("export GCP_PROJECT_ID=" in p for p in problems), problems


async def test_no_auth_reports_both_options():
    """_auth_mode=='metadata' -> problem mentions GOOGLE_APPLICATION_CREDENTIALS
    AND gcloud auth application-default login."""
    ok, problems = await preflight_gcp_ready(
        rest=_FakeRest(auth_mode="metadata", scope_ok=False,
                       project_val="jarvis-473803", zone_val="us-central1-a")
    )
    assert ok is False
    combined = " ".join(problems)
    assert "GOOGLE_APPLICATION_CREDENTIALS" in combined, combined
    assert "gcloud auth application-default login" in combined, combined


async def test_no_auth_via_verify_scope_failure():
    """verify_compute_scopes returns False even with sa mode -> auth problem."""
    ok, problems = await preflight_gcp_ready(
        rest=_FakeRest(auth_mode="sa", scope_ok=False,
                       project_val="jarvis-473803", zone_val="us-central1-a")
    )
    assert ok is False
    assert any("GOOGLE_APPLICATION_CREDENTIALS" in p for p in problems), problems


async def test_never_raises():
    """A fake whose project() raises must not propagate -- returns (False, [...])."""
    ok, problems = await preflight_gcp_ready(
        rest=_FakeRest(auth_mode="adc", scope_ok=True,
                       project_val=None, project_raises=True,
                       zone_val="us-central1-a")
    )
    assert ok is False
    # Must have at least a project problem (or a generic error entry)
    assert len(problems) >= 1


async def test_require_zone_false_skips_zone_check():
    """require_zone=False: missing zone is NOT a problem."""
    ok, problems = await preflight_gcp_ready(
        require_zone=False,
        rest=_FakeRest(auth_mode="adc", scope_ok=True,
                       project_val="jarvis-473803", zone_val=None)
    )
    assert ok is True
    assert not any("GCP_ZONE" in p for p in problems)


async def test_multiple_problems_all_reported():
    """Missing project AND zone both appear in problems list."""
    ok, problems = await preflight_gcp_ready(
        rest=_FakeRest(auth_mode="adc", scope_ok=True,
                       project_val=None, zone_val=None)
    )
    assert ok is False
    assert any("GCP_PROJECT_ID" in p for p in problems)
    assert any("GCP_ZONE" in p for p in problems)
