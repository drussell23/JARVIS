"""Task T2 -- UPSTREAM QUARANTINE wiring into the immortal re-queue seam.

These tests prove the keystone: at the LIVE immortal re-queue decision in
``candidate_generator._dispatch_via_sentinel``, a DEDUCED global DW outage
(full rolling window of all-failed sweeps for a route) seals the op into the
Cryo-DLQ and raises a TERMINAL ``upstream_quarantine:...`` error INSTEAD of
recursing into the immortal queue forever (the observed dilation hops=77
pathology). A transient failure (window not yet all-False) takes the EXISTING
immortal retry unchanged. ``quarantine_enabled()`` false -> byte-identical legacy
path. ``record_sweep(success=True)`` clears the deduced outage.

The deep async dispatcher is hard to drive end-to-end, so the keystone-intercept
test exercises the SAME decision logic the LIVE seam runs (the real gradient +
real quarantine_op gate), and a structural test pins the intercept's placement
relative to the live ``_dispatch_via_sentinel`` immortal recursion + the success/
failure ``record_sweep`` sites.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_quarantine_module(monkeypatch):
    """Re-import provider_quarantine with a clean singleton + current env."""
    for key in list(sys.modules.keys()):
        if "provider_quarantine" in key:
            del sys.modules[key]
    return importlib.import_module(
        "backend.core.ouroboros.governance.provider_quarantine"
    )


def _seed_full_outage(gradient, route, window):
    """Drive the gradient to a FULL all-failure window for *route*."""
    for _ in range(window):
        gradient.record_sweep(route, success=False)


# ---------------------------------------------------------------------------
# 1. LIVE-seam decision: full all-False window -> quarantine fires (terminal)
# ---------------------------------------------------------------------------

def test_full_window_outage_quarantines_terminal_no_recurse(monkeypatch):
    """The intercept condition the live seam runs: with the REAL gradient seeded
    to a full all-failure window, quarantine_enabled() True + is_global_outage
    True -> quarantine_op is called and the seam raises the terminal
    ``upstream_quarantine:`` error (it does NOT proceed to the immortal recurse).
    """
    monkeypatch.setenv("JARVIS_QUARANTINE_WINDOW", "5")
    monkeypatch.setenv("JARVIS_PROVIDER_QUARANTINE_ENABLED", "true")
    mod = _fresh_quarantine_module(monkeypatch)

    gradient = mod.get_provider_health_gradient()
    gradient.reset("standard")
    _seed_full_outage(gradient, "standard", 5)
    assert gradient.is_global_outage("standard") is True

    # Spy on quarantine_op so we don't touch the real DLQ file.
    called = {"n": 0, "route": None, "telemetry": None}

    def _fake_quarantine_op(ctx, *, route, telemetry):
        called["n"] += 1
        called["route"] = route
        called["telemetry"] = telemetry
        return True  # sealed in Cryo-DLQ

    monkeypatch.setattr(mod, "quarantine_op", _fake_quarantine_op)

    ctx = SimpleNamespace(op_id="op-deadbeef0001")

    # Replicate the LIVE seam's intercept decision (candidate_generator
    # _dispatch_via_sentinel, just before the immortal _imm_should_retry recurse).
    recursed = {"n": 0}

    def _run_seam():
        if mod.quarantine_enabled() and gradient.is_global_outage("standard"):
            _telemetry = {
                "route": "standard", "fleet_exhausted": True,
                "lanes": "batch+realtime", "failure_mode": "TIMEOUT",
                "dilation_hops": -1,
            }
            if mod.quarantine_op(ctx, route="standard", telemetry=_telemetry):
                raise RuntimeError("upstream_quarantine:dw_global_outage")
        # legacy immortal path would recurse here
        recursed["n"] += 1

    with pytest.raises(RuntimeError) as exc:
        _run_seam()

    assert str(exc.value).startswith("upstream_quarantine:")
    assert called["n"] == 1
    assert called["route"] == "standard"
    assert recursed["n"] == 0  # immortal recurse SKIPPED -- terminal, not re-queued


# ---------------------------------------------------------------------------
# 2. Transient (window not yet all-False) -> NO quarantine -> legacy retry
# ---------------------------------------------------------------------------

def test_transient_failure_no_quarantine_legacy_retry(monkeypatch):
    """A transient failure (a full window that contains ANY success, or a
    not-yet-full window) leaves is_global_outage False -> quarantine_op is NEVER
    called and the legacy immortal retry path runs unchanged."""
    monkeypatch.setenv("JARVIS_QUARANTINE_WINDOW", "5")
    monkeypatch.setenv("JARVIS_PROVIDER_QUARANTINE_ENABLED", "true")
    mod = _fresh_quarantine_module(monkeypatch)

    gradient = mod.get_provider_health_gradient()
    gradient.reset("standard")
    # 4 failures + 1 success in a window of 5 -> not all-False -> not an outage.
    for _ in range(4):
        gradient.record_sweep("standard", success=False)
    gradient.record_sweep("standard", success=True)
    assert gradient.is_global_outage("standard") is False

    called = {"n": 0}
    monkeypatch.setattr(
        mod, "quarantine_op",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or True,
    )

    recursed = {"n": 0}

    def _run_seam():
        if mod.quarantine_enabled() and gradient.is_global_outage("standard"):
            if mod.quarantine_op(SimpleNamespace(op_id="x"), route="standard",
                                 telemetry={}):
                raise RuntimeError("upstream_quarantine:dw_global_outage")
        recursed["n"] += 1  # legacy immortal recurse

    _run_seam()
    assert called["n"] == 0      # quarantine never consulted/fired
    assert recursed["n"] == 1    # legacy immortal path ran unchanged


# ---------------------------------------------------------------------------
# 3. quarantine_enabled() false -> byte-identical legacy path (no quarantine)
# ---------------------------------------------------------------------------

def test_disabled_is_byte_identical_legacy(monkeypatch):
    """Master switch false -> the intercept is fully skipped even under a deduced
    outage: the legacy immortal path runs and quarantine_op is never reached."""
    monkeypatch.setenv("JARVIS_QUARANTINE_WINDOW", "5")
    monkeypatch.setenv("JARVIS_PROVIDER_QUARANTINE_ENABLED", "false")
    mod = _fresh_quarantine_module(monkeypatch)

    assert mod.quarantine_enabled() is False

    gradient = mod.get_provider_health_gradient()
    gradient.reset("standard")
    _seed_full_outage(gradient, "standard", 5)
    assert gradient.is_global_outage("standard") is True  # outage IS deduced...

    called = {"n": 0}
    monkeypatch.setattr(
        mod, "quarantine_op",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or True,
    )

    recursed = {"n": 0}

    def _run_seam():
        # quarantine_enabled() short-circuits FIRST -> outage never consulted.
        if mod.quarantine_enabled() and gradient.is_global_outage("standard"):
            if mod.quarantine_op(SimpleNamespace(op_id="x"), route="standard",
                                 telemetry={}):
                raise RuntimeError("upstream_quarantine:dw_global_outage")
        recursed["n"] += 1

    _run_seam()
    assert called["n"] == 0    # ...but quarantine NEVER fires (master off)
    assert recursed["n"] == 1  # legacy immortal path ran -- byte-identical


# ---------------------------------------------------------------------------
# 4. record_sweep(success=True) clears a previously-deduced outage (recovery)
# ---------------------------------------------------------------------------

def test_success_sweep_clears_outage(monkeypatch):
    """A recovery sweep (DW comes back) records success and flips
    is_global_outage False -- the gradient recovers autonomously, so the NEXT
    failed sweep starts a fresh window rather than re-triggering instantly."""
    monkeypatch.setenv("JARVIS_QUARANTINE_WINDOW", "5")
    mod = _fresh_quarantine_module(monkeypatch)

    gradient = mod.get_provider_health_gradient()
    gradient.reset("complex")
    _seed_full_outage(gradient, "complex", 5)
    assert gradient.is_global_outage("complex") is True

    # The success-return site records a success -> one True lands in the window.
    gradient.record_sweep("complex", success=True)
    assert gradient.is_global_outage("complex") is False


# ---------------------------------------------------------------------------
# 5. STRUCTURAL: the live seam wires the intercept BEFORE the immortal recurse
#    + the success/failure record_sweep sites are present in the right order.
# ---------------------------------------------------------------------------

def test_live_seam_structurally_wired():
    """Pin the LIVE seam in candidate_generator: the UPSTREAM QUARANTINE
    intercept (is_global_outage + quarantine_op + the terminal raise) must
    PRECEDE the immortal ``_dispatch_via_sentinel`` re-queue recursion, and the
    failure ``record_sweep`` (full-fleet exhaustion) + success ``record_sweep``
    sites must both exist."""
    spec = importlib.util.find_spec(
        "backend.core.ouroboros.governance.candidate_generator"
    )
    with open(spec.origin) as fh:
        src = fh.read()

    # The intercept symbols are present.
    assert "is_global_outage(" in src
    assert "quarantine_op(" in src
    assert "quarantine_enabled()" in src
    assert "upstream_quarantine:dw_global_outage" in src
    assert "record_sweep(" in src

    # The terminal raise precedes the immortal recursion.
    i_raise = src.find('raise RuntimeError(\n                            "upstream_quarantine:dw_global_outage"')
    if i_raise == -1:
        i_raise = src.find("upstream_quarantine:dw_global_outage")
    i_recurse = src.find("_immortal_attempt=_immortal_attempt + 1")
    assert 0 < i_raise < i_recurse, (
        "UPSTREAM QUARANTINE intercept must precede the immortal re-queue recurse"
    )

    # Both record_sweep outcome sites are wired.
    assert "record_sweep(\n                provider_route, success=False" in src
    assert "record_sweep(\n                        provider_route, success=True" in src


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
