"""Shape-aware upstream read budget — kills the non-streaming false-502 class.

The Aegis forwarding proxy used to impose a blind 30s inter-chunk ``sock_read``
on every upstream request. That is correct for SSE (a 30s gap = a stalled
stream) but WRONG for a ``stream:false`` completion: DW generates the entire
body before writing a byte, so the socket is legitimately silent for the whole
generation (Qwen3.5-397B reasoning TTFT p50 ~66s). The proxy fired mid-
generation and synthesized a 502 ``upstream_unreachable`` before the client's
own 120s budget — the bt-2026-07-17 DreamEngine DW-RT failure class.

These tests pin the fix: shape-aware defaults, a client-declared per-request
budget honored + ceiling-clamped, the control header stripped from upstream,
and cross-module agreement on the header spelling so the two ends can't drift.
"""
from __future__ import annotations

from typing import Dict, Optional

import pytest

from backend.core.ouroboros.aegis import forwarding as F


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeRequest:
    """Minimal stand-in exposing only ``.headers`` (a case-insensitive-ish map)."""

    def __init__(self, headers: Optional[Dict[str, str]] = None, *, raise_on_access=False):
        self._headers = headers or {}
        self._raise = raise_on_access

    @property
    def headers(self):
        if self._raise:
            raise RuntimeError("header access boom")
        return self._headers


def _clear_env(monkeypatch):
    for k in (
        F._AEGIS_SOCK_READ_ENV_VAR, F._NONSTREAM_READ_ENV_VAR,
        F._READ_BUDGET_CEILING_ENV_VAR,
    ):
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------------------
# Shape defaults — the heart of the fix
# ---------------------------------------------------------------------------


def test_streaming_default_is_unchanged_30s(monkeypatch):
    _clear_env(monkeypatch)
    r = _FakeRequest()
    assert F._resolve_upstream_read_budget_s(r, is_streaming=True) == 30.0


def test_nonstreaming_default_is_generation_sized_not_30s(monkeypatch):
    _clear_env(monkeypatch)
    r = _FakeRequest()
    # The whole point: a stream:false request must NOT get the 30s inter-chunk
    # bound — it gets the generation-sized default (120s).
    assert F._resolve_upstream_read_budget_s(r, is_streaming=False) == 120.0


def test_nonstreaming_default_env_tunable(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv(F._NONSTREAM_READ_ENV_VAR, "180")
    r = _FakeRequest()
    assert F._resolve_upstream_read_budget_s(r, is_streaming=False) == 180.0


def test_nonstreaming_invalid_env_falls_back(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv(F._NONSTREAM_READ_ENV_VAR, "not-a-number")
    assert F._nonstream_read_default_s() == 120.0
    monkeypatch.setenv(F._NONSTREAM_READ_ENV_VAR, "-5")
    assert F._nonstream_read_default_s() == 120.0


# ---------------------------------------------------------------------------
# Client-declared budget honored + clamped (authority stays server-side)
# ---------------------------------------------------------------------------


def test_client_hint_honored(monkeypatch):
    _clear_env(monkeypatch)
    r = _FakeRequest({F._UPSTREAM_READ_BUDGET_HEADER: "150"})
    # Non-streaming shape but the client asked for 150 → honored (below ceiling).
    assert F._resolve_upstream_read_budget_s(r, is_streaming=False) == 150.0
    # Even a streaming request honors an explicit client hint (a slow-TTFT
    # reasoning stream declaring its 360s window).
    r2 = _FakeRequest({F._UPSTREAM_READ_BUDGET_HEADER: "360"})
    assert F._resolve_upstream_read_budget_s(r2, is_streaming=True) == 360.0


def test_client_hint_clamped_to_ceiling(monkeypatch):
    _clear_env(monkeypatch)
    # A buggy/compromised client asking for 99999s is clamped — Aegis stays the
    # authority on the maximum upstream read.
    r = _FakeRequest({F._UPSTREAM_READ_BUDGET_HEADER: "99999"})
    assert F._resolve_upstream_read_budget_s(r, is_streaming=False) == 600.0


def test_client_hint_clamped_to_floor(monkeypatch):
    _clear_env(monkeypatch)
    r = _FakeRequest({F._UPSTREAM_READ_BUDGET_HEADER: "0.5"})
    assert F._resolve_upstream_read_budget_s(r, is_streaming=False) == F._READ_BUDGET_FLOOR_S


def test_ceiling_env_tunable(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv(F._READ_BUDGET_CEILING_ENV_VAR, "300")
    r = _FakeRequest({F._UPSTREAM_READ_BUDGET_HEADER: "500"})
    assert F._resolve_upstream_read_budget_s(r, is_streaming=False) == 300.0


def test_ceiling_also_bounds_a_misset_shape_default(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv(F._NONSTREAM_READ_ENV_VAR, "900")   # operator over-set
    monkeypatch.setenv(F._READ_BUDGET_CEILING_ENV_VAR, "600")
    r = _FakeRequest()
    assert F._resolve_upstream_read_budget_s(r, is_streaming=False) == 600.0


# ---------------------------------------------------------------------------
# Robustness — never raises, always degrades to a shape default
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "  ", "abc", "0", "-1", "nan", "inf"])
def test_invalid_hint_degrades_to_shape_default(monkeypatch, bad):
    _clear_env(monkeypatch)
    r = _FakeRequest({F._UPSTREAM_READ_BUDGET_HEADER: bad})
    val = F._resolve_upstream_read_budget_s(r, is_streaming=False)
    # 'inf' parses to float('inf') > 0 → clamped to ceiling; everything else
    # falls through to the non-streaming shape default. Both are bounded + sane.
    assert val in (120.0, 600.0)
    assert F._READ_BUDGET_FLOOR_S <= val <= 600.0


def test_header_access_failure_never_raises(monkeypatch):
    _clear_env(monkeypatch)
    r = _FakeRequest(raise_on_access=True)
    # A broken request object must not break forwarding — degrade to default.
    assert F._resolve_upstream_read_budget_s(r, is_streaming=False) == 120.0


# ---------------------------------------------------------------------------
# Control header is stripped from the outbound (never leaks to upstream)
# ---------------------------------------------------------------------------


def test_control_header_in_forwarding_strip_set():
    # The forwarding handler strips a fixed set of JARVIS↔Aegis control headers
    # before composing the outbound request. Assert our header is lowercased
    # into that discipline (the actual strip happens inline in forward_request;
    # this pins the invariant that the name is treated as strip-eligible).
    import inspect
    src = inspect.getsource(F.forward_request)
    # The outbound-header strip loop drops this control header by its lowercased
    # name so it never reaches upstream (DW/Anthropic).
    assert "_UPSTREAM_READ_BUDGET_HEADER.lower()" in src


# ---------------------------------------------------------------------------
# Cross-module contract — the two ends can never drift on the spelling
# ---------------------------------------------------------------------------


def test_header_name_agrees_across_client_and_daemon():
    from backend.core.ouroboros.governance.aegis_provider_bridge import (
        UPSTREAM_READ_BUDGET_HEADER_NAME as client_name,
    )
    from backend.core.ouroboros.governance.doubleword_provider import (
        _AEGIS_READ_BUDGET_HEADER as provider_alias,
    )
    assert F._UPSTREAM_READ_BUDGET_HEADER == client_name == provider_alias
    assert client_name == "X-JARVIS-Upstream-Read-Budget-S"


# ---------------------------------------------------------------------------
# Client-side helper — stamps a valid budget, omits an invalid one
# ---------------------------------------------------------------------------


def test_client_helper_stamps_and_omits():
    from backend.core.ouroboros.governance.doubleword_provider import (
        _dw_declare_read_budget, _AEGIS_READ_BUDGET_HEADER,
    )
    assert _dw_declare_read_budget({}, 150.0) == {_AEGIS_READ_BUDGET_HEADER: "150.000"}
    # Non-positive / invalid → header omitted (proxy uses its shape default).
    assert _dw_declare_read_budget({}, 0) == {}
    assert _dw_declare_read_budget({}, -3) == {}
    assert _dw_declare_read_budget({}, float("nan")) == {}
    # A declared budget round-trips through the resolver to the same clamp band.
    hdrs = _dw_declare_read_budget({}, 150.0)
    r = _FakeRequest(dict(hdrs))
    assert F._resolve_upstream_read_budget_s(r, is_streaming=False) == 150.0
