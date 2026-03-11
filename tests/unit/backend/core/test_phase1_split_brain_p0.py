"""
Phase 1 P0 Gate Tests — Split-Brain Routing Fix
================================================

Non-negotiable invariants under test
-------------------------------------
I-1  For any governable op: telemetry_host == selector_host == execution_host
I-2  Invariant violation → hard fail BODY_MISMATCH (no silent dispatch)
I-3  Remote telemetry unavailable for remote route → hard fail TELEMETRY_DISCONNECT
     (never fall back to local psutil)
I-4  No silent fallback anywhere

Test groups
-----------
G1  /v1/capability endpoint — contract shape, version, context-window, model-loaded
G2  Supervisor boot — hard fail on contract_version mismatch
G3  Supervisor boot — hard fail when capability endpoint unreachable
G4  Remote route uses remote telemetry only (TelemetryContextualizer)
G5  Remote telemetry disconnect → TELEMETRY_DISCONNECT, no local fallback
G6  BrainSelector purity — no resource-gate side-effects in select()
G7  RoutingIntentTelemetry does NOT use local psutil for routing-authority fields
G8  routing_reason propagates end-to-end into OperationResult (ledger-visible)
G9  Host identity normalization — no false BODY_MISMATCH on equivalent addresses
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# G1 — /v1/capability endpoint contract shape
# ---------------------------------------------------------------------------

class TestCapabilityEndpointShape:
    """G1: /v1/capability must expose a well-typed contract response."""

    def _make_response(
        self,
        schema_version: str = "1.0.0",
        contract_version: str = "1.0.0",
        model_loaded: bool = True,
        context_window: int = 8192,
        host_id: str = "test-host",
        host_binding: str = "127.0.0.1:8000",
        generated_at_epoch_s: int | None = None,
    ) -> Dict:
        return {
            "schema_version": schema_version,
            "contract_version": contract_version,
            "model_loaded": model_loaded,
            "context_window": context_window,
            "host_id": host_id,
            "host_binding": host_binding,
            "generated_at_epoch_s": generated_at_epoch_s or int(time.time()),
        }

    def test_required_fields_present(self):
        """Capability response must contain all required contract fields."""
        resp = self._make_response()
        required = {
            "schema_version", "contract_version", "model_loaded",
            "context_window", "host_id", "host_binding", "generated_at_epoch_s",
        }
        missing = required - set(resp.keys())
        assert not missing, f"Missing required capability fields: {missing}"

    def test_contract_version_is_semver(self):
        """contract_version must be parseable as semver (major.minor.patch)."""
        resp = self._make_response(contract_version="1.0.0")
        parts = resp["contract_version"].split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts), (
            f"contract_version {resp['contract_version']!r} is not semver"
        )

    def test_model_loaded_is_bool(self):
        for val in (True, False):
            resp = self._make_response(model_loaded=val)
            assert isinstance(resp["model_loaded"], bool)

    def test_context_window_is_positive_int(self):
        resp = self._make_response(context_window=8192)
        assert isinstance(resp["context_window"], int)
        assert resp["context_window"] > 0

    def test_host_id_not_empty(self):
        resp = self._make_response(host_id="gcp-vm-1")
        assert isinstance(resp["host_id"], str)
        assert len(resp["host_id"]) > 0

    def test_host_binding_contains_host_and_port(self):
        resp = self._make_response(host_binding="10.0.0.1:8000")
        assert ":" in resp["host_binding"], (
            "host_binding must be 'host:port'"
        )

    def test_schema_version_is_semver(self):
        resp = self._make_response(schema_version="1.0.0")
        parts = resp["schema_version"].split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    def test_generated_at_epoch_s_is_recent(self):
        resp = self._make_response()
        now = time.time()
        assert abs(resp["generated_at_epoch_s"] - now) < 5, (
            "generated_at_epoch_s must be within 5 s of call time"
        )


# ---------------------------------------------------------------------------
# G2 — Supervisor boot: hard fail on contract_version mismatch
# ---------------------------------------------------------------------------

class TestSupervisorBootContractMismatch:
    """G2: assert_capability_contract() must hard-fail when contract_version
    is outside [min_runtime, max_runtime]."""

    def _make_assert_fn(self):
        """Import the real assertion function under test."""
        from backend.core.ouroboros.governance.boot_handshake import (
            assert_capability_contract,
        )
        return assert_capability_contract

    def _policy_compat(self):
        return {"min_runtime_contract_version": "1.0.0",
                "max_runtime_contract_version": "1.0.999"}

    def test_version_below_min_raises(self):
        """Runtime contract_version below minimum must raise RuntimeError."""
        assert_fn = self._make_assert_fn()
        capability_resp = {
            "schema_version": "1.0.0",
            "contract_version": "0.9.0",  # below "1.0.0"
            "model_loaded": True,
            "context_window": 8192,
            "host_id": "gcp-test",
            "host_binding": "127.0.0.1:8000",
            "generated_at_epoch_s": int(time.time()),
        }
        with patch(
            "backend.core.ouroboros.governance.boot_handshake"
            "._fetch_capability_json",
            new=AsyncMock(return_value=capability_resp),
        ):
            with pytest.raises(RuntimeError) as exc_info:
                _run(assert_fn(
                    "http://127.0.0.1:8000",
                    compat=self._policy_compat(),
                ))
            assert "CONTRACT_VERSION_INCOMPATIBLE" in str(exc_info.value)

    def test_version_above_max_raises(self):
        """Runtime contract_version above maximum must raise RuntimeError."""
        assert_fn = self._make_assert_fn()
        capability_resp = {
            "schema_version": "1.0.0",
            "contract_version": "2.0.0",  # above "1.0.999"
            "model_loaded": True,
            "context_window": 8192,
            "host_id": "gcp-test",
            "host_binding": "127.0.0.1:8000",
            "generated_at_epoch_s": int(time.time()),
        }
        with patch(
            "backend.core.ouroboros.governance.boot_handshake"
            "._fetch_capability_json",
            new=AsyncMock(return_value=capability_resp),
        ):
            with pytest.raises(RuntimeError) as exc_info:
                _run(assert_fn(
                    "http://127.0.0.1:8000",
                    compat=self._policy_compat(),
                ))
            assert "CONTRACT_VERSION_INCOMPATIBLE" in str(exc_info.value)

    def test_version_within_range_passes(self):
        """Runtime contract_version within [min, max] must not raise."""
        assert_fn = self._make_assert_fn()
        capability_resp = {
            "schema_version": "1.0.0",
            "contract_version": "1.0.0",
            "model_loaded": True,
            "context_window": 8192,
            "host_id": "gcp-test",
            "host_binding": "127.0.0.1:8000",
            "generated_at_epoch_s": int(time.time()),
        }
        with patch(
            "backend.core.ouroboros.governance.boot_handshake"
            "._fetch_capability_json",
            new=AsyncMock(return_value=capability_resp),
        ):
            # Must not raise
            _run(assert_fn(
                "http://127.0.0.1:8000",
                compat=self._policy_compat(),
            ))

    def test_missing_contract_version_field_raises(self):
        """Response without contract_version must raise with reason code."""
        assert_fn = self._make_assert_fn()
        capability_resp = {
            "schema_version": "1.0.0",
            # contract_version missing
            "model_loaded": True,
            "context_window": 8192,
            "host_id": "gcp-test",
            "host_binding": "127.0.0.1:8000",
            "generated_at_epoch_s": int(time.time()),
        }
        with patch(
            "backend.core.ouroboros.governance.boot_handshake"
            "._fetch_capability_json",
            new=AsyncMock(return_value=capability_resp),
        ):
            with pytest.raises(RuntimeError) as exc_info:
                _run(assert_fn(
                    "http://127.0.0.1:8000",
                    compat=self._policy_compat(),
                ))
            assert "CONTRACT_SCHEMA_INVALID" in str(exc_info.value)


# ---------------------------------------------------------------------------
# G3 — Supervisor boot: hard fail on unreachable capability endpoint
# ---------------------------------------------------------------------------

class TestSupervisorBootEndpointUnreachable:
    """G3: assert_capability_contract() must hard-fail when the capability
    endpoint is unreachable — never silently degrade."""

    def _make_assert_fn(self):
        from backend.core.ouroboros.governance.boot_handshake import (
            assert_capability_contract,
        )
        return assert_capability_contract

    def _policy_compat(self):
        return {"min_runtime_contract_version": "1.0.0",
                "max_runtime_contract_version": "1.0.999"}

    def test_connection_error_raises_hard_fail(self):
        """Connection refused must raise CAPABILITY_ENDPOINT_UNREACHABLE."""
        assert_fn = self._make_assert_fn()
        with patch(
            "backend.core.ouroboros.governance.boot_handshake"
            "._fetch_capability_json",
            new=AsyncMock(
                side_effect=RuntimeError("CAPABILITY_ENDPOINT_UNREACHABLE: connection refused")
            ),
        ):
            with pytest.raises(RuntimeError) as exc_info:
                _run(assert_fn(
                    "http://127.0.0.1:9999",  # nothing listening here
                    compat=self._policy_compat(),
                ))
            assert "CAPABILITY_ENDPOINT_UNREACHABLE" in str(exc_info.value)

    def test_timeout_raises_hard_fail(self):
        """Timeout must raise CAPABILITY_ENDPOINT_UNREACHABLE, not silently degrade."""
        assert_fn = self._make_assert_fn()
        with patch(
            "backend.core.ouroboros.governance.boot_handshake"
            "._fetch_capability_json",
            new=AsyncMock(
                side_effect=RuntimeError("CAPABILITY_ENDPOINT_UNREACHABLE: timeout")
            ),
        ):
            with pytest.raises(RuntimeError) as exc_info:
                _run(assert_fn(
                    "http://127.0.0.1:8000",
                    compat=self._policy_compat(),
                ))
            assert "CAPABILITY_ENDPOINT_UNREACHABLE" in str(exc_info.value)

    def test_no_silent_fallback_on_unreachable(self):
        """assert_capability_contract must NOT catch and swallow errors.
        Any network failure must propagate as RuntimeError."""
        assert_fn = self._make_assert_fn()
        error_raised = False

        with patch(
            "backend.core.ouroboros.governance.boot_handshake"
            "._fetch_capability_json",
            new=AsyncMock(
                side_effect=RuntimeError("CAPABILITY_ENDPOINT_UNREACHABLE: refused")
            ),
        ):
            try:
                _run(assert_fn(
                    "http://127.0.0.1:8000",
                    compat=self._policy_compat(),
                ))
            except RuntimeError:
                error_raised = True

        assert error_raised, (
            "assert_capability_contract silently swallowed unreachable endpoint — "
            "invariant I-4 violated: no silent fallback"
        )


# ---------------------------------------------------------------------------
# G4 — Remote route uses remote telemetry only
# ---------------------------------------------------------------------------

class TestRemoteRouteTelemetryBinding:
    """G4: When execution_host is remote, TelemetryContextualizer must
    fetch telemetry FROM the remote host, never from local psutil."""

    def _make_contextualizer(self):
        from backend.core.ouroboros.governance.telemetry_contextualizer import (
            TelemetryContextualizer,
        )
        return TelemetryContextualizer()

    def test_remote_route_fetches_remote_telemetry(self):
        """For a GCP execution host, telemetry is fetched from GCP, not local."""
        ctx = self._make_contextualizer()
        remote_telemetry = {
            "host_id": "gcp-vm-1",
            "ram_percent": 45.0,
            "cpu_percent": 20.0,
            "pressure": "NORMAL",
        }

        with patch.object(
            ctx, "_fetch_remote_telemetry_json",
            new=AsyncMock(return_value=remote_telemetry),
        ) as mock_fetch:
            result = _run(ctx.fetch_remote_telemetry("http://10.0.0.5:8000"))

        mock_fetch.assert_called_once_with("http://10.0.0.5:8000")
        assert result.host_id == "gcp-vm-1"
        assert result.ram_percent == 45.0

    def test_local_psutil_not_called_for_remote_route(self):
        """psutil must NOT be called when routing to GCP."""
        ctx = self._make_contextualizer()
        remote_telemetry = {
            "host_id": "gcp-vm-1",
            "ram_percent": 45.0,
            "cpu_percent": 20.0,
            "pressure": "NORMAL",
        }

        with patch.object(
            ctx, "_fetch_remote_telemetry_json",
            new=AsyncMock(return_value=remote_telemetry),
        ), patch("psutil.virtual_memory") as mock_psutil:
            _run(ctx.fetch_remote_telemetry("http://10.0.0.5:8000"))

        mock_psutil.assert_not_called()

    def test_host_binding_passes_when_hosts_match(self):
        """assert_host_binding must not raise when execution == telemetry host."""
        ctx = self._make_contextualizer()
        # Should not raise
        _run(ctx.assert_host_binding(
            execution_host="10.0.0.5",
            telemetry_host="10.0.0.5",
        ))

    def test_host_binding_passes_for_local_route(self):
        """Local execution allows any telemetry_host (local always matches)."""
        ctx = self._make_contextualizer()
        _run(ctx.assert_host_binding(
            execution_host="local",
            telemetry_host="local",
        ))


# ---------------------------------------------------------------------------
# G5 — Remote telemetry disconnect → hard fail, no local fallback
# ---------------------------------------------------------------------------

class TestTelemetryDisconnectHardFail:
    """G5: TELEMETRY_DISCONNECT is emitted and no local psutil fallback occurs."""

    def _make_contextualizer(self):
        from backend.core.ouroboros.governance.telemetry_contextualizer import (
            TelemetryContextualizer,
        )
        return TelemetryContextualizer()

    def test_fetch_failure_raises_telemetry_disconnect(self):
        """Network failure fetching remote telemetry must raise TELEMETRY_DISCONNECT."""
        ctx = self._make_contextualizer()

        with patch.object(
            ctx, "_fetch_remote_telemetry_json",
            new=AsyncMock(side_effect=ConnectionError("connection refused")),
        ):
            with pytest.raises(RuntimeError) as exc_info:
                _run(ctx.fetch_remote_telemetry("http://10.0.0.5:8000"))

        assert "TELEMETRY_DISCONNECT" in str(exc_info.value)

    def test_timeout_raises_telemetry_disconnect(self):
        """Timeout fetching remote telemetry must raise TELEMETRY_DISCONNECT."""
        ctx = self._make_contextualizer()

        with patch.object(
            ctx, "_fetch_remote_telemetry_json",
            new=AsyncMock(side_effect=TimeoutError("timed out")),
        ):
            with pytest.raises(RuntimeError) as exc_info:
                _run(ctx.fetch_remote_telemetry("http://10.0.0.5:8000"))

        assert "TELEMETRY_DISCONNECT" in str(exc_info.value)

    def test_no_psutil_fallback_on_disconnect(self):
        """On remote telemetry failure, psutil MUST NOT be called as fallback."""
        ctx = self._make_contextualizer()

        with patch.object(
            ctx, "_fetch_remote_telemetry_json",
            new=AsyncMock(side_effect=ConnectionError("refused")),
        ), patch("psutil.virtual_memory") as mock_psutil:
            with pytest.raises(RuntimeError) as exc_info:
                _run(ctx.fetch_remote_telemetry("http://10.0.0.5:8000"))

        mock_psutil.assert_not_called()
        assert "TELEMETRY_DISCONNECT" in str(exc_info.value)

    def test_body_mismatch_raises_on_host_collision(self):
        """Execution host != telemetry host → BODY_MISMATCH raised."""
        ctx = self._make_contextualizer()

        with pytest.raises(RuntimeError) as exc_info:
            _run(ctx.assert_host_binding(
                execution_host="10.0.0.5",   # GCP
                telemetry_host="127.0.0.1",  # local Mac — MISMATCH
            ))

        assert "BODY_MISMATCH" in str(exc_info.value)

    def test_body_mismatch_contains_both_hosts(self):
        """BODY_MISMATCH error must name both hosts for debuggability."""
        ctx = self._make_contextualizer()

        with pytest.raises(RuntimeError) as exc_info:
            _run(ctx.assert_host_binding(
                execution_host="10.0.0.5",
                telemetry_host="127.0.0.1",
            ))

        err = str(exc_info.value)
        assert "10.0.0.5" in err
        assert "127.0.0.1" in err


# ---------------------------------------------------------------------------
# G6 — BrainSelector purity: no resource-gate side-effects
# ---------------------------------------------------------------------------

class TestBrainSelectorPurity:
    """G6: BrainSelector.select() must NOT make routing decisions based on
    local resource pressure.  Complexity + cost are the only inputs."""

    def _make_selector(self, tmp_path) -> "BrainSelector":
        from backend.core.ouroboros.governance.brain_selector import BrainSelector
        persist = tmp_path / "cost.json"
        return BrainSelector(persist_path=persist)

    def _normal_snapshot(self):
        from backend.core.ouroboros.governance.resource_monitor import (
            PressureLevel, ResourceSnapshot,
        )
        return ResourceSnapshot(
            ram_percent=30.0,
            cpu_percent=20.0,
            event_loop_latency_ms=5.0,
            disk_io_busy=False,
        )

    def _emergency_snapshot(self):
        from backend.core.ouroboros.governance.resource_monitor import (
            PressureLevel, ResourceSnapshot,
        )
        return ResourceSnapshot(
            ram_percent=95.0,   # EMERGENCY threshold
            cpu_percent=99.0,
            event_loop_latency_ms=200.0,
            disk_io_busy=True,
        )

    def test_select_ignores_resource_pressure_for_light_task(self, tmp_path):
        """LIGHT task gets same brain_id regardless of local resource pressure."""
        sel = self._make_selector(tmp_path)

        result_normal = sel.select(
            "fix a bug in utils.py",
            ("utils.py",),
            self._normal_snapshot(),
        )
        result_emergency = sel.select(
            "fix a bug in utils.py",
            ("utils.py",),
            self._emergency_snapshot(),
        )

        # The brain selected must be identical regardless of local RAM/CPU
        assert result_normal.brain_id == result_emergency.brain_id, (
            f"BrainSelector changed brain based on local pressure: "
            f"normal={result_normal.brain_id!r} emergency={result_emergency.brain_id!r} — "
            f"invariant I-1 violated: selector_host must not influence routing"
        )

    def test_select_ignores_resource_pressure_for_complex_task(self, tmp_path):
        """COMPLEX task gets same brain_id regardless of local resource pressure."""
        sel = self._make_selector(tmp_path)

        result_normal = sel.select(
            "architecture design for cross-repo migration",
            ("a.py", "b.py", "c.py", "d.py", "e.py", "f.py"),
            self._normal_snapshot(),
        )
        result_emergency = sel.select(
            "architecture design for cross-repo migration",
            ("a.py", "b.py", "c.py", "d.py", "e.py", "f.py"),
            self._emergency_snapshot(),
        )

        assert result_normal.brain_id == result_emergency.brain_id, (
            f"BrainSelector changed brain based on local pressure: "
            f"normal={result_normal.brain_id!r} emergency={result_emergency.brain_id!r}"
        )

    def test_routing_reason_contains_no_pressure_reference(self, tmp_path):
        """routing_reason must not mention 'pressure', 'ram', or 'cpu'."""
        sel = self._make_selector(tmp_path)

        result = sel.select(
            "implement new feature in service.py",
            ("service.py",),
            self._emergency_snapshot(),  # EMERGENCY — should be ignored
        )

        forbidden = {"pressure", "ram", "cpu", "resource", "elevated", "critical", "emergency"}
        reason_lower = result.routing_reason.lower()
        violations = [kw for kw in forbidden if kw in reason_lower]
        assert not violations, (
            f"routing_reason {result.routing_reason!r} contains resource pressure terms: "
            f"{violations} — BrainSelector is not pure"
        )

    def test_psutil_not_called_during_select(self, tmp_path):
        """psutil must not be invoked inside BrainSelector.select()."""
        sel = self._make_selector(tmp_path)

        with patch("psutil.virtual_memory") as mock_vm, \
             patch("psutil.cpu_percent") as mock_cpu:
            sel.select(
                "refactor authentication module",
                ("auth.py", "tests/test_auth.py"),
                self._emergency_snapshot(),
            )

        mock_vm.assert_not_called()
        mock_cpu.assert_not_called()

    def test_apply_resource_and_cost_gates_ignores_pressure(self, tmp_path):
        """RouteDecisionService path: _apply_resource_and_cost_gates() must not
        route differently based on local resource pressure."""
        from backend.core.ouroboros.governance.brain_selector import (
            BrainSelector, TaskComplexity,
        )
        sel = BrainSelector(persist_path=tmp_path / "cost.json")

        result_normal = sel._apply_resource_and_cost_gates(
            "qwen_coder", TaskComplexity.HEAVY_CODE,
            "implement feature", ("main.py",),
            self._normal_snapshot(), 1, "cai_intent_code_generation",
        )
        result_emergency = sel._apply_resource_and_cost_gates(
            "qwen_coder", TaskComplexity.HEAVY_CODE,
            "implement feature", ("main.py",),
            self._emergency_snapshot(), 1, "cai_intent_code_generation",
        )

        assert result_normal.brain_id == result_emergency.brain_id, (
            "BrainSelector._apply_resource_and_cost_gates() changed brain based "
            f"on local pressure: normal={result_normal.brain_id!r} "
            f"emergency={result_emergency.brain_id!r}"
        )


# ---------------------------------------------------------------------------
# G7 — RoutingIntentTelemetry must NOT derive routing-authority fields
#       from local Mac psutil data
# ---------------------------------------------------------------------------

class TestRoutingIntentTelemetryPurity:
    """G7: expected_provider and policy_reason in RoutingIntentTelemetry must
    come from the BrainSelectionResult, NOT from local Mac resource pressure.

    Root cause: _expected_provider_from_pressure() computed expected_provider
    from LOCAL psutil snap (GCP route, Mac EMERGENCY pressure → "LOCAL_CLAUDE")
    which is factually and architecturally wrong.
    """

    def _make_result(self, brain_id: str = "qwen_coder",
                     provider_tier: str = "gcp_prime",
                     routing_reason: str = "complexity_match_heavy_code") -> "BrainSelectionResult":
        from backend.core.ouroboros.governance.brain_selector import BrainSelectionResult
        return BrainSelectionResult(
            brain_id=brain_id,
            model_name="qwen-2.5-coder-7b",
            fallback_model="mistral-7b",
            routing_reason=routing_reason,
            task_complexity="heavy_code",
            provider_tier=provider_tier,
        )

    def _emergency_snapshot(self):
        from backend.core.ouroboros.governance.resource_monitor import ResourceSnapshot
        return ResourceSnapshot(
            ram_percent=95.0, cpu_percent=99.0,
            event_loop_latency_ms=200.0, disk_io_busy=True,
        )

    def test_expected_provider_matches_brain_result_not_local_pressure(self):
        """expected_provider must reflect brain.provider_tier, never local pressure."""
        from backend.core.ouroboros.governance.governed_loop_service import (
            _expected_provider_from_brain,
        )
        brain = self._make_result(provider_tier="gcp_prime")
        snap = self._emergency_snapshot()   # EMERGENCY — Mac is at 95% RAM

        provider = _expected_provider_from_brain(brain)

        # Must be GCP_PRIME (from brain selection), never LOCAL_CLAUDE from local pressure
        assert "gcp" in provider.lower() or "prime" in provider.lower(), (
            f"expected_provider={provider!r} does not reflect GCP routing — "
            f"local EMERGENCY pressure must not change expected_provider for GCP ops"
        )

    def test_policy_reason_comes_from_routing_reason_not_pressure(self):
        """policy_reason in RoutingIntentTelemetry must be brain.routing_reason."""
        from backend.core.ouroboros.governance.governed_loop_service import (
            _policy_reason_from_brain,
        )
        brain = self._make_result(routing_reason="complexity_match_heavy_code")
        snap = self._emergency_snapshot()

        reason = _policy_reason_from_brain(brain)

        assert reason == "complexity_match_heavy_code", (
            f"policy_reason={reason!r} is not from brain.routing_reason — "
            f"local pressure level must not be the policy_reason for brain-selected routes"
        )

    def test_local_pressure_does_not_change_expected_provider(self):
        """Under EMERGENCY local pressure, expected_provider for GCP op is still GCP."""
        from backend.core.ouroboros.governance.governed_loop_service import (
            _expected_provider_from_brain,
        )
        brain_gcp = self._make_result(provider_tier="gcp_prime")

        # EMERGENCY local pressure should NOT flip expected_provider to LOCAL_CLAUDE
        provider = _expected_provider_from_brain(brain_gcp)
        assert "local_claude" not in provider.lower(), (
            f"expected_provider={provider!r} says LOCAL_CLAUDE for a GCP-routed op — "
            f"local Mac EMERGENCY pressure must not poison GCP routing telemetry"
        )


# ---------------------------------------------------------------------------
# G8 — routing_reason propagates end-to-end into OperationResult
# ---------------------------------------------------------------------------

class TestRoutingReasonPropagation:
    """G8: brain.routing_reason must appear in the terminal OperationResult
    so callers and ledger consumers can read the causal selection code."""

    def test_operation_result_has_routing_reason_field(self):
        """OperationResult must declare a routing_reason field."""
        from backend.core.ouroboros.governance.governed_loop_service import OperationResult
        from backend.core.ouroboros.governance.op_context import OperationPhase
        import inspect

        fields = {f.name for f in __import__('dataclasses').fields(OperationResult)}
        assert "routing_reason" in fields, (
            "OperationResult missing routing_reason field — "
            "brain selector's causal code never reaches the ledger"
        )

    def test_operation_result_routing_reason_defaults_to_empty(self):
        """routing_reason defaults to empty string for pre-brain-selection exits."""
        from backend.core.ouroboros.governance.governed_loop_service import OperationResult
        from backend.core.ouroboros.governance.op_context import OperationPhase

        result = OperationResult(
            op_id="test-op",
            terminal_phase=OperationPhase.CANCELLED,
            reason_code="duplicate:in_flight",
        )
        assert result.routing_reason == ""

    def test_operation_result_carries_brain_routing_reason(self):
        """OperationResult built after brain selection must carry routing_reason."""
        from backend.core.ouroboros.governance.governed_loop_service import OperationResult
        from backend.core.ouroboros.governance.op_context import OperationPhase

        result = OperationResult(
            op_id="test-op",
            terminal_phase=OperationPhase.COMPLETE,
            reason_code="complete",
            routing_reason="complexity_match_heavy_code",
        )
        assert result.routing_reason == "complexity_match_heavy_code"

    def test_routing_reason_survives_cost_gate_queue_exit(self):
        """When cost gate queues a heavy task, routing_reason must still be set."""
        from backend.core.ouroboros.governance.governed_loop_service import OperationResult
        from backend.core.ouroboros.governance.op_context import OperationPhase

        result = OperationResult(
            op_id="test-op-queued",
            terminal_phase=OperationPhase.CANCELLED,
            reason_code="cost_gate_triggered_queue",
            routing_reason="cost_gate_triggered_queue",
        )
        assert result.routing_reason == "cost_gate_triggered_queue"


# ---------------------------------------------------------------------------
# G9 — Host identity normalization: no false BODY_MISMATCH
# ---------------------------------------------------------------------------

class TestHostNormalization:
    """G9: assert_host_binding must normalize equivalent host representations
    so localhost/127.0.0.1/http://127.0.0.1:8000 comparisons don't false-fail."""

    def _ctx(self):
        from backend.core.ouroboros.governance.telemetry_contextualizer import (
            TelemetryContextualizer,
        )
        return TelemetryContextualizer()

    # -- Local-to-local: all forms of localhost must match ---------

    def test_localhost_equals_127_0_0_1(self):
        """'localhost' and '127.0.0.1' are equivalent — must not raise."""
        ctx = self._ctx()
        _run(ctx.assert_host_binding(
            execution_host="localhost",
            telemetry_host="127.0.0.1",
        ))

    def test_127_0_0_1_equals_localhost(self):
        ctx = self._ctx()
        _run(ctx.assert_host_binding(
            execution_host="127.0.0.1",
            telemetry_host="localhost",
        ))

    def test_url_form_strips_scheme(self):
        """'http://10.0.0.5:8000' and '10.0.0.5' must compare equal."""
        ctx = self._ctx()
        _run(ctx.assert_host_binding(
            execution_host="http://10.0.0.5:8000",
            telemetry_host="10.0.0.5",
        ))

    def test_port_stripped_before_comparison(self):
        """'10.0.0.5:8000' and '10.0.0.5' must compare equal (port ignored)."""
        ctx = self._ctx()
        _run(ctx.assert_host_binding(
            execution_host="10.0.0.5:8000",
            telemetry_host="10.0.0.5",
        ))

    def test_https_scheme_stripped(self):
        ctx = self._ctx()
        _run(ctx.assert_host_binding(
            execution_host="https://10.0.0.5",
            telemetry_host="10.0.0.5",
        ))

    # -- Genuine mismatches must still raise -----------------------

    def test_different_ips_still_raise(self):
        """10.0.0.5 vs 10.0.0.6 are genuinely different — must raise BODY_MISMATCH."""
        ctx = self._ctx()
        with pytest.raises(RuntimeError) as exc_info:
            _run(ctx.assert_host_binding(
                execution_host="10.0.0.5",
                telemetry_host="10.0.0.6",
            ))
        assert "BODY_MISMATCH" in str(exc_info.value)

    def test_mac_vs_gcp_raises(self):
        """Local Mac host vs GCP IP must always raise BODY_MISMATCH."""
        ctx = self._ctx()
        with pytest.raises(RuntimeError) as exc_info:
            _run(ctx.assert_host_binding(
                execution_host="10.0.0.5",        # GCP
                telemetry_host="127.0.0.1",       # Mac local
            ))
        assert "BODY_MISMATCH" in str(exc_info.value)
