"""Phase 12 Slice E — candidate_generator boot-hook integration pins.

Source-level pins on the wiring of ``boot_discovery_once()`` into
``CandidateGenerator._dispatch_via_sentinel``. The boot hook fires on
the first dispatch and is idempotent for subsequent dispatches in
the same process.

Pins:
  §1 Boot hook is called inside _dispatch_via_sentinel
  §2 Boot hook fires BEFORE topology.dw_models_for_route is read
     (otherwise first dispatch sees empty catalog)
  §3 Boot hook is wrapped in try/except — NEVER blocks dispatch
  §4 Boot hook gracefully skips when self._tier0 is None or
     missing required attributes
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import candidate_generator as cg


CANDIDATE_GEN_PATH = Path(cg.__file__)


# ---------------------------------------------------------------------------
# §1 — Boot hook called inside _dispatch_via_sentinel
# ---------------------------------------------------------------------------


def test_boot_discovery_imported_inside_dispatch_via_sentinel() -> None:
    """The dispatcher MUST call ``boot_discovery_once`` — pinned at
    source level so a future refactor that lazy-imports / removes
    the call site fails this test."""
    src = inspect.getsource(cg.CandidateGenerator._dispatch_via_sentinel)
    assert "boot_discovery_once" in src, (
        "Phase 12 Slice E: _dispatch_via_sentinel must invoke "
        "boot_discovery_once for the first-dispatch boot hook"
    )


# ---------------------------------------------------------------------------
# §2 — Boot hook fires BEFORE the catalog read
# ---------------------------------------------------------------------------


def test_boot_discovery_fires_before_dw_models_read() -> None:
    """If the boot hook runs AFTER topology.dw_models_for_route, the
    first dispatch sees an empty catalog and falls through to YAML
    (which is purged in Slice E). Pin source ordering."""
    src = inspect.getsource(cg.CandidateGenerator._dispatch_via_sentinel)
    boot_idx = src.index("boot_discovery_once")
    read_idx = src.index("dw_models_for_route")
    assert boot_idx < read_idx, (
        "boot_discovery_once must run BEFORE the dw_models_for_route "
        "read so the catalog is populated for the first dispatch"
    )


# ---------------------------------------------------------------------------
# §3 — Boot hook wrapped in defensive try/except
# ---------------------------------------------------------------------------


def test_boot_discovery_wrapped_in_try_except() -> None:
    """The boot hook MUST be defensive — any exception during boot
    (network, import, ledger, anything) cannot block dispatch.
    Pin the try/except wrapper at source level."""
    src = inspect.getsource(cg.CandidateGenerator._dispatch_via_sentinel)
    # Find the boot_discovery_once call site
    boot_call_idx = src.index("await _boot_discovery_once")
    # Walk backwards to find the enclosing try:
    preceding = src[:boot_call_idx]
    try_idx = preceding.rfind("try:")
    assert try_idx != -1, (
        "boot_discovery_once must be inside a try/except — any "
        "boot failure must not propagate to dispatch"
    )
    # Walk forward from the call to find the except:
    following = src[boot_call_idx:]
    except_idx = following.find("except")
    assert except_idx != -1
    # The except must catch the broadest reasonable exception type
    # (Exception or BaseException) — boot is best-effort, not strict
    except_window = following[except_idx:except_idx + 80]
    assert (
        "Exception" in except_window
        or "BaseException" in except_window
    ), "boot_discovery_once except clause must catch Exception"


# ---------------------------------------------------------------------------
# §4 — Boot hook tolerates missing tier0
# ---------------------------------------------------------------------------


def test_boot_discovery_skipped_when_tier0_none() -> None:
    """When the candidate generator was constructed without a tier0
    DW provider (test contexts, J-Prime-only deployments, etc.), the
    boot hook must NOT crash. Pin source-level guard for self._tier0
    is None."""
    src = inspect.getsource(cg.CandidateGenerator._dispatch_via_sentinel)
    # The hook code reads self._tier0 then guards against None
    boot_block_start = src.index("boot_discovery_once")
    # Walk up to find _tier0 access
    boot_block_window = src[
        max(0, boot_block_start - 1500):boot_block_start + 500
    ]
    assert "self._tier0" in boot_block_window
    assert (
        "is not None" in boot_block_window
        or "_dw_provider is not None" in boot_block_window
    ), (
        "boot hook must guard against self._tier0 is None to avoid "
        "AttributeError when DW provider isn't configured"
    )


def test_boot_discovery_uses_provider_session_lazily() -> None:
    """The hook fetches the aiohttp session via _get_session() — it
    MUST NOT cache it on self (sessions can rotate, tests use mocks).
    Pin that we await _get_session() inline at the call site."""
    src = inspect.getsource(cg.CandidateGenerator._dispatch_via_sentinel)
    assert "_get_session()" in src, (
        "boot hook must call _dw_provider._get_session() lazily to "
        "respect session rotation + test mocks"
    )


def test_boot_discovery_passes_provider_credentials() -> None:
    """The hook passes session + base_url + api_key from the
    DoublewordProvider — pin at source so a refactor can't break
    the wiring contract."""
    src = inspect.getsource(cg.CandidateGenerator._dispatch_via_sentinel)
    # Find the actual await call site (NOT the import binding above it)
    call_marker = "await _boot_discovery_once("
    call_start = src.index(call_marker)
    # Walk forward to the matching close paren
    call_window = src[call_start:src.index(")", call_start) + 1]
    for kw in ("session=", "base_url=", "api_key="):
        assert kw in call_window, (
            f"boot_discovery_once invocation must pass {kw}"
        )
