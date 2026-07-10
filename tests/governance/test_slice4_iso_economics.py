from __future__ import annotations

import importlib
import re
from pathlib import Path

DRIVER = Path("scripts/isomorphic_a1_local.py").read_text(encoding="utf-8")


def test_driver_composes_funded_cost_cap():
    """The composed child env must carry a non-zero OUROBOROS_BATTLE_COST_CAP.
    Run #14 booted the harness at budget=$0.00 -> every GENERATE preflight
    refused -> APPLY structurally unreachable."""
    assert "OUROBOROS_BATTLE_COST_CAP" in DRIVER
    assert "JARVIS_ISO_SESSION_BUDGET_USD" in DRIVER  # env-tunable, not hardcoded


def test_driver_has_zero_budget_failfast():
    """Driver must abort (not burn 83 min) if the effective budget resolves
    to 0. Assert the guard exists and references the autopsy failure class."""
    assert "budget_failfast" in DRIVER or "ZERO-BUDGET" in DRIVER


def test_driver_pins_doc_staleness_off():
    assert re.search(r"JARVIS_DOC_STALENESS_ENABLED[\"']?\s*[:=]\s*[\"']false", DRIVER)


def test_driver_asserts_offload_substrate_armed():
    """Evidence-pack #5: either cooperative-fs-io or posture wholesale-offload
    being off silently reintroduces on-loop scans. The driver must pin both."""
    assert re.search(r"JARVIS_COOPERATIVE_FS_IO_ENABLED[\"']?\s*[:=]\s*[\"']true", DRIVER)
    assert re.search(r"JARVIS_POSTURE_WHOLESALE_OFFLOAD_ENABLED[\"']?\s*[:=]\s*[\"']true", DRIVER)


def test_malformed_iso_session_budget_env_does_not_raise_at_import(monkeypatch):
    """Reviewer finding (fail-soft parity): _ISO_SESSION_BUDGET_USD used a raw
    float() on JARVIS_ISO_SESSION_BUDGET_USD, bypassing the module's own
    documented-NEVER-raises _env_float() helper. A malformed operator value
    (e.g. "=abc") must NOT raise at import/reload -- it must fail soft to the
    documented 2.00 default, exactly like every other _env_float() consumer."""
    monkeypatch.setenv("JARVIS_ISO_SESSION_BUDGET_USD", "abc")
    import scripts.isomorphic_a1_local as iso

    importlib.reload(iso)
    try:
        assert iso._ISO_SESSION_BUDGET_USD == 2.00
    finally:
        # Restore a sane module state for any other test that imports this
        # module later in the same process.
        monkeypatch.delenv("JARVIS_ISO_SESSION_BUDGET_USD", raising=False)
        importlib.reload(iso)


def test_zero_budget_failfast_guard_routes_through_env_float():
    """Reviewer finding (fail-soft parity): the zero-budget fail-fast guard
    (OUROBOROS_BATTLE_COST_CAP / JARVIS_S2_SESSION_BUDGET_USD) must read via
    the fail-soft _env_float() helper, not a bare float() that would crash
    the driver on a malformed operator-supplied value instead of emitting
    the clean FATAL ZERO-BUDGET message."""
    assert re.search(r'_env_float\(\s*"OUROBOROS_BATTLE_COST_CAP"', DRIVER)
    assert re.search(r'_env_float\(\s*"JARVIS_S2_SESSION_BUDGET_USD"', DRIVER)
    # Neither guard variable may still be built with a bare float(...) call.
    assert not re.search(
        r'_effective_cap\s*=\s*float\(', DRIVER
    )
    assert not re.search(
        r'_s2_budget\s*=\s*float\(', DRIVER
    )


def test_zero_budget_failfast_persists_verdict_via_telemetry():
    """Reviewer finding (dead verdict stamp): the zero-budget abort used to
    build a `verdict` dict that was never read/persisted before `return 2`.
    Assert the guard now feeds `verdict["failure_locus"]` into the existing
    T5 capture_failure_telemetry() helper (the earliest-available artifact
    mechanism at this point in run() -- local_autopsy needs a debug_log that
    doesn't exist yet), reusing the same helper the other non-exception
    failure paths in run() already call, instead of leaving a dead local."""
    assert re.search(
        r'capture_failure_telemetry\(\s*\n\s*output_dir=Path\(run_dir\)',
        DRIVER,
    )
    # The dead-stamp pattern (assign then immediately `return 2` with no
    # read of `verdict` in between) must be gone.
    guard_idx = DRIVER.index("ZERO-BUDGET fail-fast")
    next_return_idx = DRIVER.index("return 2", guard_idx)
    window = DRIVER[guard_idx:next_return_idx]
    assert "capture_failure_telemetry(" in window
    assert 'verdict["failure_locus"]' in window
