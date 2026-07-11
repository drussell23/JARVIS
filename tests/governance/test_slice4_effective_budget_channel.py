"""Slice 4 T1b — the soak child's budget must flow through the AUTHORITATIVE
channel.

Run #15 live-fire root cause (2026-07-10, session bt-iso-1783737892): the T1
fix funded ``OUROBOROS_BATTLE_COST_CAP=2.00`` in the composed child env and
the zero-budget fail-fast validated that dict — but ``SoakRunner`` always
passes an explicit ``--cost-cap`` on the child argv sourced from a DIFFERENT
knob (``dw_session_budget`` ← ``JARVIS_ISO_DW_SESSION_BUDGET_USD``, default
0.0). In the harness the explicit CLI flag wins over env (the env only feeds
the argparse default, which is never consulted when the flag is present), and
the harness registers a $0 CostTracker (SBA Tier 1) — so the child booted at
``budget=$0.00`` again, one knob to the left of the T1 fix.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_DRIVER_PATH = Path("scripts/isomorphic_a1_local.py").resolve()
DRIVER_SRC = _DRIVER_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def driver_mod():
    spec = importlib.util.spec_from_file_location(
        "isomorphic_a1_local_under_test", _DRIVER_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
        yield mod
    finally:
        sys.modules.pop(spec.name, None)


def test_resolver_unset_failover_off_funds_iso_budget(driver_mod):
    """Unset budget + no failover -> funded from JARVIS_ISO_SESSION_BUDGET_USD
    (the T1 knob) so the funded intent reaches the channel that wins."""
    cap = driver_mod._resolve_soak_cost_cap(None, False)
    assert cap == driver_mod._ISO_SESSION_BUDGET_USD
    assert cap > 0.0


def test_resolver_unset_failover_on_preserves_starve(driver_mod):
    """Failover runs keep the legacy $0 starve — it IS the multi-vector-awaken
    forcing function (driver comment, sanctioned scenario)."""
    assert driver_mod._resolve_soak_cost_cap(None, True) == 0.0


def test_resolver_explicit_value_honored_verbatim(driver_mod):
    assert driver_mod._resolve_soak_cost_cap(5.0, False) == 5.0
    assert driver_mod._resolve_soak_cost_cap(0.0, True) == 0.0


def test_driver_default_effective_cap_is_funded(driver_mod):
    """Constructing the driver with defaults (no explicit budget, failover
    off) must yield a funded effective soak cost cap."""
    d = driver_mod.IsomorphicA1Driver(stub_soak=True)
    assert d.effective_soak_cost_cap == driver_mod._ISO_SESSION_BUDGET_USD
    assert d.effective_soak_cost_cap > 0.0


def test_driver_failover_default_keeps_starve(driver_mod):
    d = driver_mod.IsomorphicA1Driver(stub_soak=True, enable_failover=True)
    assert d.effective_soak_cost_cap == 0.0


def test_launch_site_uses_effective_cap():
    """AST-shape pin: the SoakRunner launch must pass the resolved effective
    cap — reverting to the raw dw_session_budget re-opens the $0 channel."""
    assert "cost_cap=self.effective_soak_cost_cap" in DRIVER_SRC
    assert "cost_cap=self.dw_session_budget" not in DRIVER_SRC


def test_failfast_covers_authoritative_cli_channel():
    """The zero-budget fail-fast must consult the effective --cost-cap value
    (the authoritative channel), not only the composed env dict."""
    ff_start = DRIVER_SRC.index("ZERO-BUDGET fail-fast")
    ff_block = DRIVER_SRC[ff_start:ff_start + 3000]
    assert "effective_soak_cost_cap" in ff_block


def test_argparse_env_default_uses_none_sentinel():
    """The --dw-session-budget default must distinguish 'unset' (None) from
    an explicit 0.0 — float(env or "0.0") cannot."""
    assert 'os.environ.get("JARVIS_ISO_DW_SESSION_BUDGET_USD", "0.0")' \
        not in DRIVER_SRC
