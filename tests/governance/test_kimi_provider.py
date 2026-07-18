"""KimiProvider — Kimi K3 (Moonshot) third-tier adapter (GROUNDWORK, inert).

Pins the two hard-constraint fixes and the routing gate:
  * locked sampling params (temperature/top_p/penalties) are stripped
    DECLARATIVELY at egress (root-cause, not try/except-on-400);
  * Semantic Budget Routing gates the lane to COMPLEX planning or DW-RT-denial
    failover — an IMMEDIATE task DEFINITIVELY bypasses Kimi;
  * credential-gated availability + provider_availability parity.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.dw_egress_interceptor import sanitize_egress_body
from backend.core.ouroboros.governance import kimi_provider as kp
from backend.core.ouroboros.governance.provider_availability import (
    collect_provider_availability,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in ("JARVIS_KIMI_PROVIDER_ENABLED", "MOONSHOT_API_KEY",
                "KIMI_BASE_URL", "JARVIS_KIMI_MODEL",
                "JARVIS_DW_EGRESS_SANITIZE_RULES"):
        monkeypatch.delenv(var, raising=False)
    yield


# ===========================================================================
# A. Declarative locked-param sanitization (mandate 1 — root-cause)
# ===========================================================================


LOCKED = {"temperature": 0.7, "top_p": 0.9, "presence_penalty": 0.1,
          "frequency_penalty": 0.2, "n": 2}


@pytest.mark.parametrize("model", ["kimi-k3", "Kimi-K3-Turbo", "moonshot-v1-128k"])
def test_egress_strips_locked_params_for_kimi_models(model):
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 100, **LOCKED}
    out = sanitize_egress_body(body, model)
    for p in LOCKED:
        assert p not in out, f"{p} must be stripped for {model}"
    # ...but legitimate fields survive.
    assert out["messages"] == body["messages"]
    assert out["max_tokens"] == 100


def test_egress_leaves_non_kimi_models_untouched():
    body = {"model": "claude-sonnet-4-6", "messages": [], "temperature": 0.7}
    out = sanitize_egress_body(body, "claude-sonnet-4-6")
    assert out.get("temperature") == 0.7      # not a kimi/moonshot model → unchanged


# ===========================================================================
# B. KimiProvider.build_request_body — native sanitization + locked reasoning
# ===========================================================================


def test_build_body_strips_injected_locked_params():
    """Even if a caller injects a locked param via `extra`, it never reaches the
    wire — the adapter sanitizes natively via the shared egress interceptor."""
    prov = kp.KimiProvider(api_key="k", model="kimi-k3")
    body = prov.build_request_body(
        [{"role": "user", "content": "plan this"}],
        extra={"temperature": 0.5, "top_p": 0.8},
    )
    assert "temperature" not in body and "top_p" not in body
    assert body["reasoning_effort"] == "max"     # locked value stamped
    assert body["model"] == "kimi-k3"
    assert body["messages"][0]["content"] == "plan this"


# ===========================================================================
# C. Credential-gated availability
# ===========================================================================


def test_unavailable_without_credential(monkeypatch):
    monkeypatch.setenv("JARVIS_KIMI_PROVIDER_ENABLED", "true")   # enabled but no key
    assert kp.KimiProvider(api_key="").is_available() is False


def test_unavailable_when_disabled_even_with_credential(monkeypatch):
    monkeypatch.delenv("JARVIS_KIMI_PROVIDER_ENABLED", raising=False)  # default off
    assert kp.KimiProvider(api_key="present").is_available() is False


def test_available_only_when_enabled_and_credentialed(monkeypatch):
    monkeypatch.setenv("JARVIS_KIMI_PROVIDER_ENABLED", "true")
    assert kp.KimiProvider(api_key="present").is_available() is True


def test_resolve_availability_reasons(monkeypatch):
    monkeypatch.delenv("JARVIS_KIMI_PROVIDER_ENABLED", raising=False)
    assert kp.resolve_kimi_availability() == (False, "disabled")
    monkeypatch.setenv("JARVIS_KIMI_PROVIDER_ENABLED", "true")
    assert kp.resolve_kimi_availability() == (False, "no_credential")
    monkeypatch.setenv("MOONSHOT_API_KEY", "secret")
    assert kp.resolve_kimi_availability() == (True, "available")


# ===========================================================================
# D. Semantic Budget Routing — the lane gate (mandate 2)
# ===========================================================================


def test_immediate_definitively_bypasses_kimi():
    assert kp.kimi_route_eligible("immediate") is False
    assert kp.kimi_route_eligible("immediate", dw_rt_denied=True) is False  # even on failover


@pytest.mark.parametrize("route", ["background", "speculative"])
def test_bulk_routes_bypass_kimi(route):
    assert kp.kimi_route_eligible(route, dw_rt_denied=True) is False


def test_complex_is_the_kimi_home_lane():
    assert kp.kimi_route_eligible("complex") is True


def test_standard_only_as_dw_rt_failover():
    assert kp.kimi_route_eligible("standard") is False              # DW healthy → no
    assert kp.kimi_route_eligible("standard", dw_rt_denied=True) is True   # failover → yes


def test_route_gate_never_raises_on_garbage():
    assert kp.kimi_route_eligible(None) is False  # type: ignore[arg-type]


# ===========================================================================
# E. provider_availability parity (mandate 3)
# ===========================================================================


def test_snapshot_carries_kimi_fields_default_conservative():
    snap = collect_provider_availability()
    # default (adapter off) → not available, forensic reason present
    assert snap.kimi_available is False
    assert isinstance(snap.kimi_reason, str) and snap.kimi_reason


def test_snapshot_reflects_enabled_credentialed_kimi(monkeypatch):
    monkeypatch.setenv("JARVIS_KIMI_PROVIDER_ENABLED", "true")
    monkeypatch.setenv("MOONSHOT_API_KEY", "secret")
    snap = collect_provider_availability()
    assert snap.kimi_available is True
    assert snap.kimi_reason == "available"
    # ...and parity: the claude_/dw_ fields still populate as before.
    assert hasattr(snap, "claude_available") and hasattr(snap, "dw_healthy")
