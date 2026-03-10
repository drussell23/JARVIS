import pytest
from dataclasses import replace

from backend.core.ouroboros.governance.contracts.inventory_handshake_contract import (
    BrainDescriptor,
    PolicyManifest,
    RuntimeInventory,
    HandshakeMode,
    HandshakeEngine,
    HandshakeDiff,
    HandshakeResult,
)


# -----------------------------------------------------------------------------
# Minimal fake engine skeleton for contract tests
# Replace with real implementation class once wired.
# -----------------------------------------------------------------------------

class FakeHandshakeEngine(HandshakeEngine):
    def validate_schema(self, policy: PolicyManifest, runtime: RuntimeInventory) -> None:
        if not policy.schema_version or not runtime.schema_version:
            raise ValueError("schema invalid")

    def validate_contract_versions(self, policy: PolicyManifest, runtime: RuntimeInventory) -> None:
        if runtime.contract_version < policy.min_runtime_contract_version:
            raise ValueError("runtime contract too old")
        if runtime.contract_version > policy.max_runtime_contract_version:
            raise ValueError("runtime contract too new")

    def diff(self, policy: PolicyManifest, runtime: RuntimeInventory) -> HandshakeDiff:
        routable_ready = {
            b.brain_id for b in runtime.brains.values() if b.routable and b.health_state == "ready"
        }

        phantom_required = set(policy.required_brains) - routable_ready
        optional_missing = set(policy.optional_brains) - routable_ready
        unexpected_runtime = routable_ready - set(policy.allowed_brains)

        capability_mismatch = set()
        for brain_id, req_caps in policy.required_capabilities.items():
            if brain_id in runtime.brains:
                actual = set(runtime.brains[brain_id].capabilities)
                if not req_caps.issubset(actual):
                    capability_mismatch.add(brain_id)

        return HandshakeDiff(
            phantom_required=frozenset(phantom_required),
            optional_missing=frozenset(optional_missing),
            unexpected_runtime=frozenset(unexpected_runtime),
            capability_mismatch=frozenset(capability_mismatch),
        )

    def evaluate(self, policy: PolicyManifest, runtime: RuntimeInventory) -> HandshakeResult:
        self.validate_schema(policy, runtime)
        self.validate_contract_versions(policy, runtime)
        d = self.diff(policy, runtime)

        routable_ready = {
            b.brain_id for b in runtime.brains.values() if b.routable and b.health_state == "ready"
        }
        active = frozenset(set(policy.allowed_brains) & routable_ready)

        reason_codes = []
        accepted = True
        degraded = False

        if d.phantom_required:
            reason_codes.append("CONTRACT_REQUIRED_BRAIN_MISSING")
            if policy.mode == HandshakeMode.HARD_FAIL:
                accepted = False
            else:
                degraded = True

        if d.capability_mismatch:
            reason_codes.append("CONTRACT_CAPABILITY_MISMATCH")
            accepted = False

        if d.unexpected_runtime:
            reason_codes.append("CONTRACT_UNEXPECTED_RUNTIME_BRAIN")

        return HandshakeResult(
            accepted=accepted,
            degraded=degraded,
            reason_codes=reason_codes,
            active_brain_set=active,
            diff=d,
        )


@pytest.fixture
def policy() -> PolicyManifest:
    return PolicyManifest(
        schema_version="1.0.0",
        contract_version="1.0.0",
        min_runtime_contract_version="1.0.0",
        max_runtime_contract_version="1.0.999",
        required_brains=frozenset({"phi3_lightweight", "qwen_coder"}),
        optional_brains=frozenset({"mistral_7b_fallback"}),
        allowed_brains=frozenset({"phi3_lightweight", "qwen_coder", "mistral_7b_fallback"}),
        required_capabilities={
            "phi3_lightweight": frozenset({"chat", "trivial_ops"}),
            "qwen_coder": frozenset({"code_generation"}),
        },
        mode=HandshakeMode.HARD_FAIL,
    )


@pytest.fixture
def runtime_ok() -> RuntimeInventory:
    return RuntimeInventory(
        schema_version="1.0.0",
        contract_version="1.0.0",
        generated_at_epoch_s=0,
        brains={
            "phi3_lightweight": BrainDescriptor(
                brain_id="phi3_lightweight",
                provider="local",
                capabilities=frozenset({"chat", "trivial_ops"}),
                routable=True,
                health_state="ready",
                version="v1",
                contract_version="1.0.0",
            ),
            "qwen_coder": BrainDescriptor(
                brain_id="qwen_coder",
                provider="gcp_prime",
                capabilities=frozenset({"code_generation", "code_refactor"}),
                routable=True,
                health_state="ready",
                version="v1",
                contract_version="1.0.0",
            ),
        },
    )


def test_handshake_accepts_when_required_present(policy: PolicyManifest, runtime_ok: RuntimeInventory) -> None:
    engine = FakeHandshakeEngine()
    result = engine.evaluate(policy, runtime_ok)

    assert result.accepted is True
    assert result.degraded is False
    assert "phi3_lightweight" in result.active_brain_set
    assert "qwen_coder" in result.active_brain_set
    assert not result.diff.phantom_required


def test_handshake_hard_fails_on_phantom_required(policy: PolicyManifest, runtime_ok: RuntimeInventory) -> None:
    engine = FakeHandshakeEngine()

    runtime_missing = replace(
        runtime_ok,
        brains={k: v for k, v in runtime_ok.brains.items() if k != "qwen_coder"},
    )
    result = engine.evaluate(policy, runtime_missing)

    assert result.accepted is False
    assert "CONTRACT_REQUIRED_BRAIN_MISSING" in result.reason_codes
    assert "qwen_coder" in result.diff.phantom_required


def test_handshake_degraded_if_policy_allows_missing_required(policy: PolicyManifest, runtime_ok: RuntimeInventory) -> None:
    engine = FakeHandshakeEngine()

    degraded_policy = replace(policy, mode=HandshakeMode.DEGRADED)
    runtime_missing = replace(
        runtime_ok,
        brains={k: v for k, v in runtime_ok.brains.items() if k != "qwen_coder"},
    )
    result = engine.evaluate(degraded_policy, runtime_missing)

    assert result.accepted is True
    assert result.degraded is True
    assert "CONTRACT_REQUIRED_BRAIN_MISSING" in result.reason_codes


def test_handshake_fails_on_capability_mismatch(policy: PolicyManifest, runtime_ok: RuntimeInventory) -> None:
    engine = FakeHandshakeEngine()

    bad_qwen = replace(
        runtime_ok.brains["qwen_coder"],
        capabilities=frozenset({"chat"}),  # missing code_generation
    )
    runtime_bad = replace(runtime_ok, brains={**runtime_ok.brains, "qwen_coder": bad_qwen})
    result = engine.evaluate(policy, runtime_bad)

    assert result.accepted is False
    assert "CONTRACT_CAPABILITY_MISMATCH" in result.reason_codes
    assert "qwen_coder" in result.diff.capability_mismatch


def test_handshake_rejects_contract_version_mismatch(policy: PolicyManifest, runtime_ok: RuntimeInventory) -> None:
    engine = FakeHandshakeEngine()

    runtime_old = replace(runtime_ok, contract_version="0.9.0")
    with pytest.raises(ValueError):
        engine.evaluate(policy, runtime_old)


def test_active_set_is_intersection_of_allowlist_and_runtime_ready(
    policy: PolicyManifest,
    runtime_ok: RuntimeInventory,
) -> None:
    engine = FakeHandshakeEngine()

    runtime_extra = replace(
        runtime_ok,
        brains={
            **runtime_ok.brains,
            "rogue_brain": BrainDescriptor(
                brain_id="rogue_brain",
                provider="gcp_prime",
                capabilities=frozenset({"chat"}),
                routable=True,
                health_state="ready",
                version="v1",
                contract_version="1.0.0",
            ),
        },
    )

    result = engine.evaluate(policy, runtime_extra)
    assert "rogue_brain" not in result.active_brain_set
