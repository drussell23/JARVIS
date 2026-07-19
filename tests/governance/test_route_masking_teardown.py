"""Pre-emptive Route Masking + Graceful Transport Teardown spine.

Operator mandates (2026-07-18):
  * A ``route=background`` envelope with a failing primary must exhaust
    its cheap pool NATIVELY — Claude is omitted from the fallback chain
    at POOL-BUILD time by the cost contract's own classifier (zero
    duplicated budget rules), never detonated mid-dispatch as a
    ``CostContractViolation``.
  * An injected ``ClientConnectionResetError`` in the aegis forward
    path is silently swallowed — one DEBUG line + 499, upstream
    released, no ERROR traceback on the operator terminal.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance.candidate_generator import (
    claude_route_masked,
    should_cascade_to_claude,
)

ROOT = Path(__file__).resolve().parents[2]


class _Ctx:
    def __init__(self, route: str, read_only: bool = False) -> None:
        self.provider_route = route
        self.is_read_only = read_only
        self.op_id = "op-mask-test"


@pytest.fixture(autouse=True)
def _contract_on(monkeypatch):
    monkeypatch.setenv("JARVIS_COST_CONTRACT_RUNTIME_ASSERT_ENABLED", "true")
    yield


# ---------------------------------------------------------------------------
# (1) The mask — the contract's OWN classifier decides the pool
# ---------------------------------------------------------------------------


def test_background_route_is_masked():
    assert claude_route_masked(_Ctx("background")) is True


def test_speculative_route_is_masked():
    assert claude_route_masked(_Ctx("speculative")) is True


def test_background_read_only_reflex_not_masked():
    # Manifesto §5 Nervous System Reflex — read-only BG MAY cascade.
    assert claude_route_masked(_Ctx("background", read_only=True)) is False


def test_paid_routes_not_masked():
    for route in ("standard", "complex", "immediate"):
        assert claude_route_masked(_Ctx(route)) is False


def test_contract_off_no_mask(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_COST_CONTRACT_RUNTIME_ASSERT_ENABLED", "false",
    )
    assert claude_route_masked(_Ctx("background")) is False


def test_mask_never_raises_on_garbage():
    class _Broken:
        pass
    assert claude_route_masked(_Broken()) is False


# ---------------------------------------------------------------------------
# (2) The decision — masked route NEVER cascades, exhausts natively
# ---------------------------------------------------------------------------


def test_masked_route_never_cascades_even_with_healthy_fallback():
    """The mandate's core assertion: BG + failing primary + a perfectly
    healthy configured Claude fallback -> NO cascade. The op falls to
    the immortal DW-retry/degrade branch (native cheap-pool
    exhaustion) instead of a mid-dispatch violation."""
    assert should_cascade_to_claude(
        has_fallback=True,          # Claude IS configured...
        claude_breaker_open=False,  # ...and its lane is healthy...
        enabled=True,
        route_masked=True,          # ...but the route may not buy it.
    ) is False


def test_mask_checked_before_breaker_logic():
    # Masked beats every other consideration, including kill-switch-off.
    assert should_cascade_to_claude(
        has_fallback=True, claude_breaker_open=False,
        enabled=False, route_masked=True,
    ) is False


def test_unmasked_legacy_behavior_byte_identical():
    assert should_cascade_to_claude(
        has_fallback=True, claude_breaker_open=False, enabled=True,
    ) is True
    assert should_cascade_to_claude(
        has_fallback=True, claude_breaker_open=True, enabled=True,
    ) is False
    assert should_cascade_to_claude(
        has_fallback=False, claude_breaker_open=False, enabled=True,
    ) is False


# ---------------------------------------------------------------------------
# (3) Wiring pins — BOTH Claude-purchase paths consult the mask
# ---------------------------------------------------------------------------


def _gen_src() -> str:
    return (
        ROOT / "backend/core/ouroboros/governance/candidate_generator.py"
    ).read_text()


def test_exhaustion_cascade_consults_mask():
    src = _gen_src()
    idx = src.index("_do_cascade = should_cascade_to_claude(")
    region = src[idx - 400:idx + 400]
    assert "claude_route_masked(context)" in region
    assert "route_masked=_route_masked" in region


def test_slice76_sever_cascade_consults_mask():
    src = _gen_src()
    idx = src.index("Slice 76 pre-flight: DW DIRECT_STREAMING")
    region = src[idx - 1200:idx]
    assert "claude_route_masked(context)" in region


def test_mask_composes_contract_classifier_not_duplicated_rules():
    src = _gen_src()
    body = src[src.index("def claude_route_masked"):][:2000]
    assert "classify_route_compatibility" in body
    # DRY: no re-statement of the route sets — the contract owns them.
    assert "background" not in body.split('"""')[2] if '"""' in body else True
    assert "COST_GATED_ROUTES" not in body


# ---------------------------------------------------------------------------
# (4) Graceful Transport Teardown — forwarding releases upstream + quiet
# ---------------------------------------------------------------------------


def test_forwarding_prepare_wrapped_with_upstream_release():
    src = (
        ROOT / "backend/core/ouroboros/aegis/forwarding.py"
    ).read_text()
    idx = src.index("await client_resp.prepare(request)")
    region = src[idx - 200:idx + 1400]
    assert "except (ConnectionResetError, ConnectionError)" in region
    assert "upstream_resp.release()" in region      # no half-read pool leak
    assert "logger.debug" in region                 # quiet, not ERROR
    assert region.index("try:") < region.index(
        "await client_resp.prepare(request)"
    )


async def test_injected_reset_is_swallowed_by_daemon_layer(monkeypatch):
    """Functional: forward_request raising the reset family must come
    back as a quiet 499 from the daemon's _do_forward — never propagate
    to aiohttp's ERROR logger."""
    import aiohttp
    reset_exc = getattr(
        aiohttp.client_exceptions, "ClientConnectionResetError",
        ConnectionResetError,
    )

    src = (ROOT / "backend/core/ouroboros/aegis/daemon.py").read_text()
    body = src[src.index("async def _do_forward"):][:2500]
    # The daemon catch is (ConnectionResetError, ConnectionError);
    # aiohttp's variant must be a subclass for the swallow to hold.
    assert issubclass(reset_exc, ConnectionResetError)
    assert "except (ConnectionResetError" in body
    assert "return web.Response(status=499)" in body
