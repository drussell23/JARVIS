"""DW Topology Early-Detection Circuit Breaker — regression spine.

Pins:
  * Master flag matrix
  * 6-step decision tree (matching candidate_generator.py:1404-1465 semantics)
  * Read-only Nervous-System-Reflex carve-out preserved
  * NEVER-raises smoke (broken topology / non-string route)
  * Helpers (terminal_reason_code / ledger_reason_label) shape contracts
  * Authority/cage invariants (no orchestrator imports, stdlib + provider_topology only)
"""
from __future__ import annotations

import os
from typing import Any, Optional

import pytest

from backend.core.ouroboros.governance import (
    dw_topology_circuit_breaker as _cb,
)
from backend.core.ouroboros.governance.dw_topology_circuit_breaker import (
    is_circuit_breaker_enabled,
    ledger_reason_label,
    should_circuit_break,
    terminal_reason_code,
)


# ---------------------------------------------------------------------------
# Master flag matrix
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(
        "JARVIS_DW_TOPOLOGY_EARLY_REJECT_ENABLED", raising=False,
    )
    yield


def test_master_flag_default_false():
    assert is_circuit_breaker_enabled() is False


@pytest.mark.parametrize("val", ["true", "1", "yes", "on", "TRUE"])
def test_master_flag_truthy(monkeypatch: pytest.MonkeyPatch, val: str):
    monkeypatch.setenv(
        "JARVIS_DW_TOPOLOGY_EARLY_REJECT_ENABLED", val,
    )
    assert is_circuit_breaker_enabled() is True


@pytest.mark.parametrize("val", ["false", "0", "no", "off", ""])
def test_master_flag_falsy(monkeypatch: pytest.MonkeyPatch, val: str):
    monkeypatch.setenv(
        "JARVIS_DW_TOPOLOGY_EARLY_REJECT_ENABLED", val,
    )
    assert is_circuit_breaker_enabled() is False


# ---------------------------------------------------------------------------
# Decision tree — using a fake topology to control the inputs
# ---------------------------------------------------------------------------


class _FakeTopology:
    """Stub matching ProviderTopology's surface for the 4 methods
    `should_circuit_break` reads."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        dw_allowed_map: Optional[dict] = None,
        block_mode_map: Optional[dict] = None,
        reason_map: Optional[dict] = None,
    ) -> None:
        self.enabled = enabled
        self._dw = dict(dw_allowed_map or {})
        self._block = dict(block_mode_map or {})
        self._reason = dict(reason_map or {})

    def dw_allowed_for_route(self, route: str) -> bool:
        return self._dw.get(route, True)

    def block_mode_for_route(self, route: str) -> str:
        return self._block.get(route, "cascade_to_claude")

    def reason_for_route(self, route: str) -> str:
        return self._reason.get(route, "stub_reason")


def _patch_topology(
    monkeypatch: pytest.MonkeyPatch, topology: Any,
) -> None:
    from backend.core.ouroboros.governance import provider_topology
    monkeypatch.setattr(
        provider_topology, "get_topology", lambda: topology,
    )


def test_step_1_topology_disabled_no_break(
    monkeypatch: pytest.MonkeyPatch,
):
    _patch_topology(monkeypatch, _FakeTopology(enabled=False))
    verdict, reason = should_circuit_break(
        provider_route="background", is_read_only=False,
    )
    assert verdict is False
    assert reason == "topology_disabled"


def test_step_2_route_in_topology_dw_allowed_no_break(
    monkeypatch: pytest.MonkeyPatch,
):
    """If topology says DW IS allowed for this route, circuit
    breaker stays out — gate 3 in decision tree."""
    _patch_topology(
        monkeypatch,
        _FakeTopology(dw_allowed_map={"background": True}),
    )
    verdict, reason = should_circuit_break(
        provider_route="background", is_read_only=False,
    )
    assert verdict is False
    assert reason == "dw_allowed"


def test_step_3_route_dw_blocked_cascade_to_claude_no_break(
    monkeypatch: pytest.MonkeyPatch,
):
    """IMMEDIATE/COMPLEX routes typically get cascade_to_claude on
    DW block. Circuit breaker stays out — late-detection path
    handles the cascade."""
    _patch_topology(
        monkeypatch,
        _FakeTopology(
            dw_allowed_map={"immediate": False},
            block_mode_map={"immediate": "cascade_to_claude"},
        ),
    )
    verdict, reason = should_circuit_break(
        provider_route="immediate", is_read_only=False,
    )
    assert verdict is False
    assert reason == "block_mode:cascade_to_claude"


def test_step_4_skip_and_queue_read_only_bg_carve_out(
    monkeypatch: pytest.MonkeyPatch,
):
    """Read-only BG ops on skip_and_queue routes bypass via the
    Nervous-System-Reflex carve-out — circuit breaker DOES NOT fire,
    matching candidate_generator.py:1418-1440 semantics."""
    _patch_topology(
        monkeypatch,
        _FakeTopology(
            dw_allowed_map={"background": False},
            block_mode_map={"background": "skip_and_queue"},
            reason_map={"background": "Gemma topology"},
        ),
    )
    verdict, reason = should_circuit_break(
        provider_route="background", is_read_only=True,
    )
    assert verdict is False
    assert reason == "read_only_carve_out"


def test_step_4_carve_out_only_for_background_route(
    monkeypatch: pytest.MonkeyPatch,
):
    """Read-only carve-out is BACKGROUND-only. SPECULATIVE +
    skip_and_queue + read-only still circuit-breaks."""
    _patch_topology(
        monkeypatch,
        _FakeTopology(
            dw_allowed_map={"speculative": False},
            block_mode_map={"speculative": "skip_and_queue"},
            reason_map={"speculative": "spec topology"},
        ),
    )
    verdict, reason = should_circuit_break(
        provider_route="speculative", is_read_only=True,
    )
    assert verdict is True
    assert reason == "spec topology"


def test_step_5_skip_and_queue_not_read_only_circuit_breaks(
    monkeypatch: pytest.MonkeyPatch,
):
    """The bug case: BG + skip_and_queue + not-read-only. Circuit
    breaker fires; topology reason flows through verbatim."""
    _patch_topology(
        monkeypatch,
        _FakeTopology(
            dw_allowed_map={"background": False},
            block_mode_map={"background": "skip_and_queue"},
            reason_map={
                "background": (
                    "Gemma 4 31B stream-stalls on DW endpoint"
                ),
            },
        ),
    )
    verdict, reason = should_circuit_break(
        provider_route="background", is_read_only=False,
    )
    assert verdict is True
    assert "Gemma 4 31B" in reason


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_route_no_break():
    verdict, reason = should_circuit_break(
        provider_route="", is_read_only=False,
    )
    assert verdict is False
    assert reason == "empty_route"


def test_whitespace_route_no_break():
    verdict, reason = should_circuit_break(
        provider_route="   ", is_read_only=False,
    )
    assert verdict is False
    assert reason == "empty_route"


def test_route_normalized_to_lower(
    monkeypatch: pytest.MonkeyPatch,
):
    """Topology read uses lowercase keys; the function must
    normalize input even when caller passes 'BACKGROUND'."""
    _patch_topology(
        monkeypatch,
        _FakeTopology(
            dw_allowed_map={"background": False},
            block_mode_map={"background": "skip_and_queue"},
            reason_map={"background": "ok"},
        ),
    )
    verdict, _ = should_circuit_break(
        provider_route="BACKGROUND", is_read_only=False,
    )
    assert verdict is True


# ---------------------------------------------------------------------------
# NEVER-raises smoke
# ---------------------------------------------------------------------------


def test_never_raises_when_topology_get_raises(
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.core.ouroboros.governance import provider_topology

    def _boom() -> Any:
        raise RuntimeError("simulated topology failure")

    monkeypatch.setattr(provider_topology, "get_topology", _boom)
    verdict, reason = should_circuit_break(
        provider_route="background", is_read_only=False,
    )
    assert verdict is False
    assert reason.startswith("topology_read_error:")


def test_never_raises_when_dw_allowed_check_raises(
    monkeypatch: pytest.MonkeyPatch,
):
    class _BrokenTop:
        enabled = True

        def dw_allowed_for_route(self, route: str) -> bool:
            raise RuntimeError("boom")

    _patch_topology(monkeypatch, _BrokenTop())
    verdict, reason = should_circuit_break(
        provider_route="background", is_read_only=False,
    )
    assert verdict is False
    assert reason.startswith("dw_allowed_check_error:")


def test_never_raises_when_block_mode_check_raises(
    monkeypatch: pytest.MonkeyPatch,
):
    class _BrokenTop:
        enabled = True

        def dw_allowed_for_route(self, route: str) -> bool:
            return False

        def block_mode_for_route(self, route: str) -> str:
            raise RuntimeError("boom")

    _patch_topology(monkeypatch, _BrokenTop())
    verdict, reason = should_circuit_break(
        provider_route="background", is_read_only=False,
    )
    assert verdict is False
    assert reason.startswith("block_mode_check_error:")


def test_never_raises_when_reason_read_raises(
    monkeypatch: pytest.MonkeyPatch,
):
    """When everything except reason_for_route works, the verdict
    is still True but the reason carries a placeholder."""
    class _BrokenReason:
        enabled = True

        def dw_allowed_for_route(self, route: str) -> bool:
            return False

        def block_mode_for_route(self, route: str) -> str:
            return "skip_and_queue"

        def reason_for_route(self, route: str) -> str:
            raise RuntimeError("boom")

    _patch_topology(monkeypatch, _BrokenReason())
    verdict, reason = should_circuit_break(
        provider_route="background", is_read_only=False,
    )
    assert verdict is True
    assert reason.startswith("reason_read_error:")


# ---------------------------------------------------------------------------
# Helper shape contracts
# ---------------------------------------------------------------------------


def test_terminal_reason_code_format():
    code = terminal_reason_code("Gemma topology blocked")
    assert code.startswith("circuit_breaker_dw_topology:")
    assert "Gemma topology blocked" in code


def test_terminal_reason_code_truncates_to_80():
    long = "x" * 200
    code = terminal_reason_code(long)
    # Prefix + truncated payload <= prefix_len + 80.
    assert len(code) <= len("circuit_breaker_dw_topology:") + 80


def test_terminal_reason_code_handles_empty():
    code = terminal_reason_code("")
    assert code == "circuit_breaker_dw_topology:"


def test_ledger_reason_label_constant():
    assert (
        ledger_reason_label("anything")
        == "circuit_breaker_dw_topology_blocked"
    )


# ---------------------------------------------------------------------------
# Authority / cage invariants
# ---------------------------------------------------------------------------


def test_does_not_import_orchestrator_or_gate_modules():
    import ast
    import inspect
    src = inspect.getsource(_cb)
    tree = ast.parse(src)
    banned = [
        "orchestrator", "iron_gate", "risk_tier_floor",
        "policy_engine", "candidate_generator", "tool_executor",
        "change_engine", "semantic_guardian",
    ]
    for node in ast.walk(tree):
        names: list = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names = [node.module]
        for mod in names:
            for token in banned:
                assert token not in mod, (
                    f"circuit_breaker imports {mod!r} (banned token "
                    f"{token!r})"
                )


def test_top_level_imports_stdlib_only():
    import ast
    import inspect
    src = inspect.getsource(_cb)
    tree = ast.parse(src)
    top_level: list = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level.append(node.module)
    forbidden = {
        "backend.core.ouroboros.governance.provider_topology",
        "backend.core.ouroboros.governance.candidate_generator",
        "backend.core.ouroboros.governance.orchestrator",
    }
    leaked = forbidden & set(top_level)
    assert not leaked, f"hoisted to top level: {leaked}"


def test_no_secret_leakage_in_constants():
    text = repr(vars(_cb))
    for needle in ("sk-", "ghp_", "AKIA", "BEGIN PRIVATE KEY"):
        assert needle not in text


def test_public_api_count_pinned():
    public = sorted(
        n for n in dir(_cb)
        if not n.startswith("_") and callable(getattr(_cb, n))
    )
    required = {
        "is_circuit_breaker_enabled",
        "should_circuit_break",
        "terminal_reason_code",
        "ledger_reason_label",
    }
    missing = required - set(public)
    assert not missing


# ---------------------------------------------------------------------------
# Orchestrator integration — source-level pin
# ---------------------------------------------------------------------------


def test_orchestrator_integration_present():
    """Source-level pin: orchestrator imports + invokes
    `should_circuit_break` after `ctx.advance(OperationPhase.GENERATE)`,
    mass-gated by `is_circuit_breaker_enabled()`."""
    import inspect
    from backend.core.ouroboros.governance import orchestrator
    src = inspect.getsource(orchestrator)
    assert "dw_topology_circuit_breaker" in src
    assert "is_circuit_breaker_enabled" in src
    assert "should_circuit_break" in src
    # The literal "circuit_breaker_dw_topology" string lives in the
    # helper module (constructed by terminal_reason_code()), not in
    # orchestrator source. Assert on the helper alias instead.
    assert "_cb_terminal_code" in src
    assert "circuit_breaker_fired" in src
    # Import block AFTER the GENERATE-advance, BEFORE PreActionNarrator.
    advance_idx = src.index("ctx.advance(OperationPhase.GENERATE)")
    cb_import_idx = src.index(
        "from backend.core.ouroboros.governance.dw_topology_circuit_breaker",
    )
    narrator_idx = src.index("PreActionNarrator: voice WHAT before GENERATE")
    assert advance_idx < cb_import_idx < narrator_idx, (
        "circuit-breaker hook must sit between GENERATE-advance and "
        "PreActionNarrator"
    )


def test_orchestrator_integration_records_ledger():
    """Source-level pin: when circuit breaker fires, orchestrator
    must call _record_ledger with the standard reason label."""
    import inspect
    from backend.core.ouroboros.governance import orchestrator
    src = inspect.getsource(orchestrator)
    # ledger_reason_label() helper used + circuit_breaker_fired flag.
    assert "_cb_ledger_label" in src
    assert "circuit_breaker_fired" in src


def test_orchestrator_integration_uses_terminal_reason_code():
    """Source-level pin: the ctx.advance(CANCELLED) carries the
    standard terminal_reason_code from the helper, not a hardcoded
    string."""
    import inspect
    from backend.core.ouroboros.governance import orchestrator
    src = inspect.getsource(orchestrator)
    assert "_cb_terminal_code(_cb_reason)" in src


def test_orchestrator_integration_default_off_no_op():
    """Source-level pin: orchestrator must gate the entire block on
    is_circuit_breaker_enabled() so master-off is byte-identical to
    pre-Option-C behavior."""
    import inspect
    from backend.core.ouroboros.governance import orchestrator
    src = inspect.getsource(orchestrator)
    # The gate sits inside the try-block where the import lives.
    cb_block_start = src.index("Option C: DW topology")
    cb_gate_idx = src.index("if _cb_enabled():", cb_block_start)
    # The cb_should_break call comes AFTER the gate.
    cb_should_break_idx = src.index("_cb_should_break(", cb_gate_idx)
    assert cb_gate_idx < cb_should_break_idx
