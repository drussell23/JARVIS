"""Autonomous capability scout — reasoning-effort compliance dimension.

The Phase-12 discovery mesh (periodic /v1/models scan → minimal-token entitlement
probe → transport-profile / EntitlementCatalogCache injection) already exists and
is driven by the GovernedLoopService "Autonomic Pacemaker" + 30-min refresh loop.
It mapped two of the three capability dimensions — RBAC entitlement (403) and
batch-only — but left the THIRD blank: whether a model honors
``reasoning_effort="none"`` or floors it (the gpt-oss-120b class that returns
empty content when told not to think), which was only ever learned reactively by
burning a real op.

``probe_reasoning_compliance`` completes the mesh: it rides the SAME discovery
cycle, composes the provider's own ``_reasoning_request_params`` (DRY), and
records floors into the EXISTING ReasoningProfile. These tests pin both mandated
scenarios — an unentitled model → cached 403 state, and a new reasoning model →
floor recorded — plus the asymmetric never-mis-learn discipline.
"""
from __future__ import annotations

import asyncio
import pathlib
import tempfile

import pytest

from backend.core.ouroboros.governance import dw_discovery_runner as D
from backend.core.ouroboros.governance import dw_reasoning_profile as RP
from backend.core.ouroboros.governance import dw_transport_profile as TP


# ---------------------------------------------------------------------------
# Fakes — a mocked DW endpoint keyed by model id
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status, body=""):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return self._body

    async def read(self):
        return b""


class _FakeSession:
    """Routes each POST to a scripted response by the payload's model id."""

    def __init__(self, script):
        self._script = script          # model_id -> (status, body)
        self.calls = []                # observed payloads

    def post(self, url, *, headers=None, json=None, timeout=None):
        model = json["model"]
        self.calls.append(json)
        status, body = self._script.get(model, (500, "unmapped"))
        return _Resp(status, body)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture()
def fresh_reasoning_profile(monkeypatch):
    prof = RP.ReasoningProfile(
        path=pathlib.Path(tempfile.mktemp(prefix="rp_")), autosave=False,
    )
    monkeypatch.setattr(RP, "_DEFAULT_PROFILE", prof)
    monkeypatch.setenv("JARVIS_DW_REASONING_PROFILE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DW_REASONING_COMPLIANCE_PROBE_ENABLED", "true")
    return prof


# ---------------------------------------------------------------------------
# Scenario 2 (mandate) — a NEW reasoning model → floor recorded
# ---------------------------------------------------------------------------


def test_new_reasoning_model_rejecting_none_records_floor(fresh_reasoning_profile):
    prof = fresh_reasoning_profile
    session = _FakeSession({
        # A reasoning model that CANNOT disable thinking → 400 rejection.
        "inkling": (400, "reasoning_effort cannot be disabled for this model"),
        # A reasoning-free-capable model honors effort=none → 200.
        "qwen-397b": (200, "ok"),
    })
    floored, clean = _run(D.probe_reasoning_compliance(
        session=session, base_url="http://dw", api_key="k",
        model_ids=["inkling", "qwen-397b"],
    ))
    assert floored == ["inkling"]
    assert clean == ["qwen-397b"]
    # The floor is persisted in the EXISTING reasoning profile (election-time
    # call params now know to raise effort for this model).
    assert prof.learned_min_effort("inkling") is not None
    assert prof.learned_min_effort("qwen-397b") is None


def test_probe_uses_reasoning_request_params_wire_schema(fresh_reasoning_profile):
    # DRY: the probe composes the provider's own _reasoning_request_params, so
    # the payload carries the compliant reasoning_effort knob.
    session = _FakeSession({"m": (200, "ok")})
    _run(D.probe_reasoning_compliance(
        session=session, base_url="http://dw", api_key="k", model_ids=["m"],
    ))
    assert len(session.calls) == 1
    assert session.calls[0].get("reasoning_effort") == "none"
    assert session.calls[0]["max_tokens"] == 1        # minimal token generation


# ---------------------------------------------------------------------------
# Asymmetric discipline — never mis-learn from ambiguous evidence
# ---------------------------------------------------------------------------


def test_403_is_skipped_by_reasoning_probe(fresh_reasoning_profile):
    prof = fresh_reasoning_profile
    session = _FakeSession({"blocked": (403, "blocked by a routing rule")})
    floored, clean = _run(D.probe_reasoning_compliance(
        session=session, base_url="http://dw", api_key="k",
        model_ids=["blocked"],
    ))
    # 403 is the entitlement probe's dimension — reasoning probe records NOTHING.
    assert floored == [] and clean == []
    assert prof.learned_min_effort("blocked") is None


def test_transient_5xx_records_nothing(fresh_reasoning_profile):
    prof = fresh_reasoning_profile
    session = _FakeSession({"m": (503, "service unavailable")})
    floored, clean = _run(D.probe_reasoning_compliance(
        session=session, base_url="http://dw", api_key="k", model_ids=["m"],
    ))
    assert floored == [] and clean == []
    assert prof.learned_min_effort("m") is None


def test_non_reasoning_400_records_nothing(fresh_reasoning_profile):
    # A 400 that is NOT a reasoning-rejection (e.g. a schema error) must not be
    # mistaken for a reasoning floor.
    prof = fresh_reasoning_profile
    session = _FakeSession({"m": (400, "invalid 'foo' parameter")})
    floored, _ = _run(D.probe_reasoning_compliance(
        session=session, base_url="http://dw", api_key="k", model_ids=["m"],
    ))
    assert floored == []
    assert prof.learned_min_effort("m") is None


def test_already_profiled_model_is_not_reprobed(fresh_reasoning_profile):
    prof = fresh_reasoning_profile
    prof.record_reasoning_floor("known", "low")       # already learned
    session = _FakeSession({"known": (400, "reasoning cannot be disabled")})
    floored, clean = _run(D.probe_reasoning_compliance(
        session=session, base_url="http://dw", api_key="k", model_ids=["known"],
    ))
    # Skipped — the reasoning profile persists, so this is a one-time cost.
    assert session.calls == []
    assert floored == [] and clean == []


def test_master_off_is_noop(fresh_reasoning_profile, monkeypatch):
    monkeypatch.setenv("JARVIS_DW_REASONING_COMPLIANCE_PROBE_ENABLED", "false")
    session = _FakeSession({"m": (400, "reasoning cannot be disabled")})
    floored, clean = _run(D.probe_reasoning_compliance(
        session=session, base_url="http://dw", api_key="k", model_ids=["m"],
    ))
    assert session.calls == [] and floored == [] and clean == []


def test_no_api_key_is_noop(fresh_reasoning_profile):
    session = _FakeSession({"m": (200, "ok")})
    floored, clean = _run(D.probe_reasoning_compliance(
        session=session, base_url="http://dw", api_key="", model_ids=["m"],
    ))
    assert session.calls == [] and floored == [] and clean == []


def test_probe_never_raises_on_session_fault(fresh_reasoning_profile):
    class _BoomSession:
        def post(self, *a, **k):
            raise RuntimeError("network exploded")
    # Must degrade to (no floor) rather than propagate.
    floored, clean = _run(D.probe_reasoning_compliance(
        session=_BoomSession(), base_url="http://dw", api_key="k",
        model_ids=["m"],
    ))
    assert floored == [] and clean == []


# ---------------------------------------------------------------------------
# Scenario 1 (mandate) — an UNENTITLED model → cached 403 state
# (existing entitlement probe; pinned here so the two dimensions compose)
# ---------------------------------------------------------------------------


def test_unentitled_model_yields_cached_403_state(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_ENTITLEMENT_PROBE_ENABLED", "true")
    prof = TP.get_transport_profile()
    prof.clear("unentitled-xyz")
    session = _FakeSession({
        "unentitled-xyz": (403, "blocked by a routing rule"),
        "entitled-397b": (200, "ok"),
    })
    denied, cleared = _run(D.probe_rt_entitlement(
        session=session, base_url="http://dw", api_key="k",
        model_ids=["unentitled-xyz", "entitled-397b"],
    ))
    assert "unentitled-xyz" in denied
    assert "entitled-397b" in cleared
    # The 403 is now cached in the election matrix (transport profile): the RT
    # ladder skips it without re-burning a probe within the TTL window.
    assert prof.is_unavailable("unentitled-xyz", TP.TRANSPORT_REALTIME) is True
