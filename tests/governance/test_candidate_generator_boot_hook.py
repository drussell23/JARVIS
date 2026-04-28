"""Phase 12.2 Slice F — Autonomic Pacemaker integration pins.

Source-level pins on the eager-boot pattern. Replaces the Phase 12
Slice E lazy-boot pins which pinned the OLD pattern (boot_discovery_once
called from CandidateGenerator._dispatch_via_sentinel, idempotent on
subsequent dispatches).

The Slice E pattern created a deadlock in idle environments:
  - Empty catalog → BG topology block → no dispatch → no lazy boot
  - No lazy boot → catalog stays empty → loop forever

The Slice F pattern eradicates lazy boot entirely. The Autonomic
Pacemaker fires boot_discovery_once eagerly at GovernedLoopService
startup as a fire-and-forget asyncio task. Discovery armed BEFORE
any sensor signal is pulled. Refresh task heartbeats every 30 min
independently of operator traffic.

Pins:
  §1 Lazy boot REMOVED from _dispatch_via_sentinel (eradication)
  §2 Pacemaker present in GovernedLoopService boot
  §3 Pacemaker fires asyncio.create_task (fire-and-forget, non-blocking)
  §4 Pacemaker gated by discovery_enabled() (hot-revert preserved)
  §5 Pacemaker is defensive — try/except wraps the arm
  §6 Pacemaker passes session + base_url + api_key from tier0
  §7 Pacemaker has the canonical task name "dw_autonomic_pacemaker"
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import (
    candidate_generator as cg,
    governed_loop_service as gls,
)


CANDIDATE_GEN_PATH = Path(cg.__file__)
GLS_PATH = Path(gls.__file__)


# ---------------------------------------------------------------------------
# §1 — Lazy boot eradicated from _dispatch_via_sentinel
# ---------------------------------------------------------------------------


def test_lazy_boot_removed_from_dispatch_via_sentinel() -> None:
    """Phase 12.2 Slice F directive: 'Eradicate Lazy-Booting'.

    The dispatcher MUST NOT call boot_discovery_once. The Autonomic
    Pacemaker is the single source of truth for discovery boot. Pin
    at source level so a future refactor that re-introduces lazy
    boot fails this test."""
    src = inspect.getsource(cg.CandidateGenerator._dispatch_via_sentinel)
    assert "boot_discovery_once" not in src, (
        "Phase 12.2 Slice F: _dispatch_via_sentinel must NOT call "
        "boot_discovery_once. Eager Pacemaker (governed_loop_service) "
        "owns discovery boot."
    )
    assert "await _boot_discovery_once" not in src, (
        "Phase 12.2 Slice F: lazy-boot await call must be removed"
    )


def test_lazy_boot_eradication_documented_in_dispatcher() -> None:
    """The replacement comment block in _dispatch_via_sentinel MUST
    reference Slice F + 'Autonomic Pacemaker' so future readers find
    the eager-boot owner without git-blame archaeology."""
    src = inspect.getsource(cg.CandidateGenerator._dispatch_via_sentinel)
    assert "Slice F" in src or "Autonomic Pacemaker" in src, (
        "Dispatcher source must reference Slice F / Pacemaker as the "
        "discovery-boot owner so the lazy-boot removal is discoverable"
    )


# ---------------------------------------------------------------------------
# §2-§7 — Autonomic Pacemaker pinned in GovernedLoopService
# ---------------------------------------------------------------------------


def _gls_source() -> str:
    """Read the GovernedLoopService source. Cached implicitly via
    Python's source-cache."""
    return GLS_PATH.read_text(encoding="utf-8")


def test_pacemaker_present_in_governed_loop_service() -> None:
    """The Autonomic Pacemaker MUST exist in GovernedLoopService.
    Pin a search for the canonical marker."""
    src = _gls_source()
    assert "Autonomic Pacemaker" in src, (
        "GovernedLoopService must contain the Autonomic Pacemaker "
        "marker (Phase 12.2 Slice F)"
    )
    assert "dw_autonomic_pacemaker" in src, (
        "Autonomic Pacemaker task must use the canonical name "
        "'dw_autonomic_pacemaker' for cross-process discoverability"
    )


def test_pacemaker_uses_create_task_fire_and_forget() -> None:
    """The Pacemaker MUST fire asyncio.create_task (NOT await). If
    it awaited, boot would block on DW catalog response — exactly
    the failure mode we're eliminating."""
    src = _gls_source()
    # Find the pacemaker block
    marker = "Autonomic Pacemaker armed"
    arm_idx = src.index(marker)
    # Walk backwards to find the create_task call
    preceding = src[max(0, arm_idx - 2000):arm_idx]
    assert "asyncio.create_task" in preceding, (
        "Pacemaker must use asyncio.create_task — awaiting would "
        "block boot on DW response and re-introduce the deadlock"
    )
    # Confirm there's no `await _boot_discovery_once` directly
    # (the create_task wraps the call, but no top-level await).
    assert "await _boot_discovery_once(" not in preceding, (
        "Pacemaker must NOT await boot_discovery_once directly — "
        "fire-and-forget is the contract"
    )


def test_pacemaker_gated_by_discovery_enabled() -> None:
    """The Pacemaker MUST honor JARVIS_DW_CATALOG_DISCOVERY_ENABLED
    so the Phase 12 hot-revert path still works. Pin the gate."""
    src = _gls_source()
    arm_idx = src.index("Autonomic Pacemaker armed")
    preceding = src[max(0, arm_idx - 2000):arm_idx]
    assert "discovery_enabled" in preceding, (
        "Pacemaker must consult discovery_enabled() — preserves "
        "Phase 12 hot-revert via JARVIS_DW_CATALOG_DISCOVERY_ENABLED=false"
    )


def test_pacemaker_is_defensive() -> None:
    """The Pacemaker arm MUST be wrapped in try/except. Boot must
    NEVER fail because discovery had a bad day. Pin the try/except."""
    src = _gls_source()
    arm_idx = src.index("Autonomic Pacemaker armed")
    # Walk backwards to find the try
    preceding = src[max(0, arm_idx - 3000):arm_idx]
    try_idx = preceding.rfind("try:")
    assert try_idx != -1, (
        "Pacemaker arm must be inside try/except — boot must NEVER "
        "fail because discovery had a bad day"
    )
    following = src[arm_idx:arm_idx + 2000]
    except_idx = following.find("except")
    assert except_idx != -1
    # The except must catch broadly (Exception family — Pacemaker is
    # best-effort)
    except_window = following[except_idx:except_idx + 80]
    assert "Exception" in except_window, (
        "Pacemaker except clause must catch Exception — best-effort"
    )


def test_pacemaker_passes_provider_credentials() -> None:
    """The Pacemaker MUST forward session + base_url + api_key from
    the DoublewordProvider (tier0) — pin the wiring contract."""
    src = _gls_source()
    arm_idx = src.index("Autonomic Pacemaker armed")
    preceding = src[max(0, arm_idx - 2000):arm_idx]
    # Find the create_task wrapping boot_discovery_once
    bdo_idx = preceding.rfind("_boot_discovery_once(")
    assert bdo_idx != -1, (
        "Pacemaker must invoke _boot_discovery_once"
    )
    call_window = preceding[bdo_idx:]
    for kw in ("session=", "base_url=", "api_key="):
        assert kw in call_window, (
            f"Pacemaker invocation must pass {kw}"
        )


def test_pacemaker_uses_get_session_lazily() -> None:
    """The Pacemaker fetches the aiohttp session via _get_session() —
    consistent with prior conventions. Sessions can rotate; tests
    inject mocks."""
    src = _gls_source()
    arm_idx = src.index("Autonomic Pacemaker armed")
    preceding = src[max(0, arm_idx - 2000):arm_idx]
    assert "_get_session()" in preceding, (
        "Pacemaker must call tier0._get_session() lazily — supports "
        "session rotation + test mocks"
    )


def test_pacemaker_log_includes_phase_12_2_marker() -> None:
    """The 'armed' log line includes the Phase 12.2 Slice F reference
    so operators tailing logs see what fired and which subsystem."""
    src = _gls_source()
    arm_idx = src.index("Autonomic Pacemaker armed")
    log_window = src[arm_idx:arm_idx + 400]
    assert "Phase 12.2" in log_window or "Slice F" in log_window, (
        "Pacemaker armed log must reference Phase 12.2 / Slice F"
    )
