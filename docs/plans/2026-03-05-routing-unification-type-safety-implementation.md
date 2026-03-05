# Routing Unification & Type Safety Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix 8 systemic issues: 4 type-safety crashes and 4 routing split-brain diseases, using C+ phased approach with verification gates.

**Architecture:** Layered cure — Phase 0 (contract invariants), Phase 1 (boundary type safety), Phase 2 (routing authority hierarchy), Phase 2.5 (supervisor contract gate), Phase 3 (bypass elimination). Each phase has a hard verification gate.

**Tech Stack:** Python 3.11+, asyncio, ast module (static analysis), pytest, aiohttp, YAML config

---

## Phase 0: Contract Package & Invariant Tests

### Task 1: Create Contract Package — Capability Taxonomy

**Files:**
- Create: `backend/contracts/__init__.py`
- Create: `backend/contracts/capability_taxonomy.py`

**Step 1: Create directory and __init__.py**

```bash
mkdir -p backend/contracts
```

```python
# backend/contracts/__init__.py
"""
Cross-repo contract definitions for JARVIS ecosystem.

This is a neutral contract module — not owned by any single repo.
All capability, version, and routing authority definitions live here.
"""
from .capability_taxonomy import Capability, CAPABILITY_REGISTRY
from .contract_version import ContractVersion
from .routing_authority import RoutingAuthority, ROUTING_INVARIANTS
from .manifest_schema import ProviderManifest
```

**Step 2: Create capability_taxonomy.py**

```python
# backend/contracts/capability_taxonomy.py
"""
Capability Taxonomy — stable string IDs with deprecation metadata.

Capabilities are strings (not Enums) to survive partial upgrades.
"""
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class Capability:
    """A capability that a model provider can offer."""
    id: str
    deprecated: bool = False
    deprecated_by: Optional[str] = None
    since_version: str = "0.1.0"


# Canonical registry — single source of truth for all capability IDs
CAPABILITY_REGISTRY: Dict[str, Capability] = {
    # Core inference
    "chat": Capability(id="chat"),
    "reasoning": Capability(id="reasoning"),
    "code": Capability(id="code"),
    "tool_use": Capability(id="tool_use"),
    "embedding": Capability(id="embedding"),
    # Vision
    "vision": Capability(id="vision"),
    "multimodal": Capability(id="multimodal"),
    "screen_analysis": Capability(id="screen_analysis"),
    "vision_analyze_heavy": Capability(id="vision_analyze_heavy"),
    "object_detection": Capability(id="object_detection"),
    "ui_detection": Capability(id="ui_detection"),
    # Voice
    "voice_activation": Capability(id="voice_activation"),
    "wake_word_detection": Capability(id="wake_word_detection"),
    # Search
    "similarity_search": Capability(id="similarity_search"),
    "semantic_search": Capability(id="semantic_search"),
}


def is_valid_capability(cap_id: str) -> bool:
    """Check if a capability ID is in the canonical registry."""
    return cap_id in CAPABILITY_REGISTRY


def get_active_capabilities() -> Dict[str, Capability]:
    """Return non-deprecated capabilities only."""
    return {k: v for k, v in CAPABILITY_REGISTRY.items() if not v.deprecated}
```

**Step 3: Commit**

```bash
git add backend/contracts/__init__.py backend/contracts/capability_taxonomy.py
git commit -m "feat(contracts): add capability taxonomy with stable string IDs"
```

---

### Task 2: Create Contract Package — Version & Manifest

**Files:**
- Create: `backend/contracts/contract_version.py`
- Create: `backend/contracts/manifest_schema.py`

**Step 1: Create contract_version.py**

```python
# backend/contracts/contract_version.py
"""
Contract versioning with N/N-1 rolling compatibility.
"""
import hashlib
import json
from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class ContractVersion:
    """Versioned contract with rolling compatibility window."""
    current: Tuple[int, int, int]
    min_supported: Tuple[int, int, int]
    max_supported: Tuple[int, int, int]

    def is_compatible(self, remote_version: Tuple[int, int, int]) -> Tuple[bool, str]:
        """Check if a remote version is compatible with this contract."""
        if remote_version < self.min_supported:
            return False, f"remote {remote_version} below min_supported {self.min_supported}"
        if remote_version > self.max_supported:
            return False, f"remote {remote_version} above max_supported {self.max_supported}"
        return True, "compatible"

    def to_dict(self) -> dict:
        return {
            "current": list(self.current),
            "min_supported": list(self.min_supported),
            "max_supported": list(self.max_supported),
        }


# Local contract version — bumped when contracts change
LOCAL_CONTRACT = ContractVersion(
    current=(0, 3, 0),
    min_supported=(0, 2, 0),
    max_supported=(0, 4, 0),
)


def compute_policy_hash(policy_data: dict) -> str:
    """Deterministic hash of policy data for drift detection."""
    canonical = json.dumps(policy_data, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]
```

**Step 2: Create manifest_schema.py**

```python
# backend/contracts/manifest_schema.py
"""
Provider capability manifest — published by Prime, consumed by JARVIS.
"""
from dataclasses import dataclass
from typing import FrozenSet, Tuple


@dataclass(frozen=True)
class ProviderManifest:
    """Capability manifest published by a model provider."""
    provider_id: str
    capabilities: FrozenSet[str]
    contract_version: Tuple[int, int, int]
    policy_hash: str
    timestamp: float  # time.monotonic() at publish

    def supports(self, capability: str) -> bool:
        """Check if provider supports a capability."""
        return capability in self.capabilities

    def to_dict(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "capabilities": sorted(self.capabilities),
            "contract_version": list(self.contract_version),
            "policy_hash": self.policy_hash,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ProviderManifest":
        return cls(
            provider_id=data["provider_id"],
            capabilities=frozenset(data.get("capabilities", [])),
            contract_version=tuple(data.get("contract_version", [0, 0, 0])),
            policy_hash=data.get("policy_hash", ""),
            timestamp=data.get("timestamp", 0.0),
        )
```

**Step 3: Commit**

```bash
git add backend/contracts/contract_version.py backend/contracts/manifest_schema.py
git commit -m "feat(contracts): add contract versioning and provider manifest schema"
```

---

### Task 3: Create Contract Package — Routing Authority & Non-Functional Invariants

**Files:**
- Create: `backend/contracts/routing_authority.py`
- Create: `backend/contracts/non_functional_invariants.py`

**Step 1: Create routing_authority.py**

```python
# backend/contracts/routing_authority.py
"""
Routing authority declarations — who owns which decision.
"""
from enum import Enum
from typing import Dict

from .contract_version import compute_policy_hash


class RoutingAuthority(Enum):
    """Which system owns which routing concern."""
    POLICY = "ModelRouter"       # Which provider to use
    HEALTH = "PrimeRouter"       # Whether a provider is reachable
    DATA = "ModelRegistry"       # Model metadata and capabilities


# Frozen invariants — authority cannot be shared or duplicated
ROUTING_INVARIANTS: Dict[str, str] = {
    "vision_provider_selection": RoutingAuthority.POLICY.value,
    "chat_provider_selection": RoutingAuthority.POLICY.value,
    "endpoint_health_check": RoutingAuthority.HEALTH.value,
    "endpoint_failover": RoutingAuthority.HEALTH.value,
    "model_capability_data": RoutingAuthority.DATA.value,
    "model_lifecycle_state": RoutingAuthority.DATA.value,
    "circuit_breaker_state": RoutingAuthority.POLICY.value,
}


def get_routing_policy_hash() -> str:
    """Compute deterministic hash of routing invariants for drift detection."""
    return compute_policy_hash(ROUTING_INVARIANTS)
```

**Step 2: Create non_functional_invariants.py**

```python
# backend/contracts/non_functional_invariants.py
"""
Non-functional invariants — timeout ownership, cancellation, idempotency, reason codes.

Declared up front so all phases can reference them.
"""
from typing import Dict, List

# Who owns each timeout budget
TIMEOUT_OWNERSHIP: Dict[str, str] = {
    "vision_inference": "ModelRouter",
    "health_probe": "PrimeRouter",
    "startup_phase": "ProgressAwareStartupController",
    "cross_repo_handshake": "ContractGate",
    "model_load": "ModelLifecycleManager",
}

# What to do when a task is cancelled
CANCELLATION_POLICY: Dict[str, str] = {
    "inference_in_flight": "propagate_to_client",
    "health_probe": "abandon_silently",
    "startup_phase": "shield_then_timeout",
    "model_load": "propagate_to_client",
}

# Scope of idempotency keys
IDEMPOTENCY_SCOPE: Dict[str, str] = {
    "routing_decision": "request_id",
    "contract_check": "boot_session_id",
    "capability_refresh": "manifest_hash",
}

# Structured reason codes for observability
ROUTING_REASON_CODES: List[str] = [
    "primary_available",
    "primary_timeout",
    "capability_mismatch",
    "circuit_open",
    "provider_unavailable",
    "manifest_stale",
    "fallback_selected",
    "health_check_failed",
]

CONTRACT_REASON_CODES: List[str] = [
    "compatible",
    "version_incompatible",
    "schema_mismatch",
    "manifest_stale",
    "handshake_timeout",
    "service_unreachable",
    "degraded_mode",
]
```

**Step 3: Commit**

```bash
git add backend/contracts/routing_authority.py backend/contracts/non_functional_invariants.py
git commit -m "feat(contracts): add routing authority declarations and non-functional invariants"
```

---

### Task 4: Create Invariant Tests (AST-based)

**Files:**
- Create: `tests/contracts/__init__.py`
- Create: `tests/contracts/test_routing_invariants.py`
- Create: `tests/contracts/test_schema_compatibility.py`

**Step 1: Write invariant tests**

```python
# tests/contracts/__init__.py
```

```python
# tests/contracts/test_routing_invariants.py
"""
AST-based invariant tests for routing contracts.

These tests enforce structural rules that prevent drift.
Some tests are expected to FAIL initially — they define the target state.
"""
import ast
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent

# Factory modules that are ALLOWED to construct clients directly
FACTORY_ALLOWLIST = {
    str(REPO_ROOT / "backend" / "intelligence" / "unified_model_serving.py"),
}

PROHIBITED_CONSTRUCTORS = {"PrimeAPIClient", "PrimeCloudRunClient", "PrimeLocalClient"}


class TestNoDirectClientConstruction:
    """Enforce: no direct client construction outside factory module."""

    def _scan_file(self, filepath: str) -> list:
        """Scan a Python file for prohibited constructor calls."""
        violations = []
        try:
            with open(filepath) as f:
                tree = ast.parse(f.read(), filename=filepath)
        except SyntaxError:
            return []

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in PROHIBITED_CONSTRUCTORS:
                    violations.append(
                        f"{filepath}:{node.lineno} — direct {node.func.id}()"
                    )
        return violations

    @pytest.mark.xfail(reason="Phase 3: bypass elimination not yet implemented")
    def test_no_bypass_construction(self):
        """No direct PrimeAPIClient/PrimeCloudRunClient outside factory."""
        violations = []
        backend_dir = REPO_ROOT / "backend"
        for py_file in backend_dir.rglob("*.py"):
            filepath = str(py_file)
            if filepath in FACTORY_ALLOWLIST:
                continue
            if "test" in filepath.lower() or "__pycache__" in filepath:
                continue
            violations.extend(self._scan_file(filepath))

        assert not violations, (
            f"Direct client construction found outside factory:\n"
            + "\n".join(violations)
        )


class TestCapabilityTaxonomyConsistency:
    """Enforce: no hardcoded capability sets in routing code."""

    def test_no_hardcoded_vision_providers(self):
        """The hardcoded vision_providers set must not exist."""
        serving_path = REPO_ROOT / "backend" / "intelligence" / "unified_model_serving.py"
        with open(serving_path) as f:
            content = f.read()

        # This pattern is the exact disease: hardcoded provider filtering
        assert "vision_providers = {" not in content, (
            "Hardcoded vision_providers set found in unified_model_serving.py. "
            "Vision routing must use manifest-driven capability checks."
        )

    def test_capability_registry_imported(self):
        """Contract package must be importable."""
        from backend.contracts.capability_taxonomy import CAPABILITY_REGISTRY
        assert "vision" in CAPABILITY_REGISTRY
        assert "chat" in CAPABILITY_REGISTRY


class TestContractVersioning:
    """Enforce: contract versions are declared and compatible."""

    def test_local_contract_valid(self):
        from backend.contracts.contract_version import LOCAL_CONTRACT
        assert LOCAL_CONTRACT.current >= LOCAL_CONTRACT.min_supported
        assert LOCAL_CONTRACT.current <= LOCAL_CONTRACT.max_supported

    def test_self_compatibility(self):
        from backend.contracts.contract_version import LOCAL_CONTRACT
        compatible, reason = LOCAL_CONTRACT.is_compatible(LOCAL_CONTRACT.current)
        assert compatible, f"Contract not self-compatible: {reason}"
```

```python
# tests/contracts/test_schema_compatibility.py
"""
Schema compatibility tests — validate contract structures locally.
Cross-repo handshake tests are in integration tests (supervisor contract gate).
"""
import pytest
from backend.contracts.manifest_schema import ProviderManifest


class TestProviderManifest:
    """Enforce: manifest serialization roundtrips correctly."""

    def test_roundtrip(self):
        manifest = ProviderManifest(
            provider_id="jprime",
            capabilities=frozenset(["vision", "chat", "multimodal"]),
            contract_version=(0, 3, 0),
            policy_hash="abcdef01",
            timestamp=1000.0,
        )
        data = manifest.to_dict()
        restored = ProviderManifest.from_dict(data)
        assert restored.provider_id == manifest.provider_id
        assert restored.capabilities == manifest.capabilities
        assert restored.contract_version == manifest.contract_version

    def test_supports_capability(self):
        manifest = ProviderManifest(
            provider_id="jprime",
            capabilities=frozenset(["vision", "chat"]),
            contract_version=(0, 3, 0),
            policy_hash="abc",
            timestamp=0,
        )
        assert manifest.supports("vision")
        assert manifest.supports("chat")
        assert not manifest.supports("embedding")

    def test_empty_capabilities(self):
        manifest = ProviderManifest(
            provider_id="empty",
            capabilities=frozenset(),
            contract_version=(0, 1, 0),
            policy_hash="",
            timestamp=0,
        )
        assert not manifest.supports("vision")
```

**Step 2: Run tests**

Run: `python3 -m pytest tests/contracts/ -v`
Expected: `test_no_bypass_construction` xfails (Phase 3). `test_no_hardcoded_vision_providers` FAILS (Phase 2 not yet done). All others PASS.

**Step 3: Commit**

```bash
git add tests/contracts/
git commit -m "test(contracts): add AST-based invariant tests and schema compatibility tests"
```

---

## Phase 1: Type/Boundary Stabilization

### Task 5: Create Boundary Adapters

**Files:**
- Create: `backend/vision/intelligence/boundary_adapters.py`
- Create: `tests/unit/vision/test_boundary_adapters.py`

**Step 1: Write failing tests**

```python
# tests/unit/vision/test_boundary_adapters.py
"""Tests for vision intelligence boundary adapters."""
import pytest
from backend.vision.intelligence.boundary_adapters import safe_state_key, safe_text


class TestSafeStateKey:
    def test_none_returns_sentinel(self):
        assert safe_state_key(None) == "__none__"

    def test_string_passthrough(self):
        assert safe_state_key("error_state") == "error_state"

    def test_int_converts(self):
        assert safe_state_key(42) == "42"

    def test_empty_string(self):
        assert safe_state_key("") == ""


class TestSafeText:
    def test_none_returns_empty(self):
        assert safe_text(None) == ""

    def test_string_passthrough(self):
        assert safe_text("hello") == "hello"

    def test_int_converts(self):
        assert safe_text(123) == "123"

    def test_empty_string(self):
        assert safe_text("") == ""
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/vision/test_boundary_adapters.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.vision.intelligence.boundary_adapters'`

**Step 3: Write implementation**

```python
# backend/vision/intelligence/boundary_adapters.py
"""
Boundary adapters for vision intelligence subsystem.

These normalize untyped data at ingestion boundaries.
Applied at the boundary (once), not scattered across call sites.
"""
from typing import Any


def safe_state_key(key: Any) -> str:
    """Normalize state transition keys at ingestion boundary.

    Used where dict keys may be None (e.g., transition_matrix iteration).
    """
    if key is None:
        return "__none__"
    return str(key)


def safe_text(value: Any) -> str:
    """Normalize text values at ingestion boundary.

    Used where text from external sources may be None.
    """
    if value is None:
        return ""
    return str(value)
```

**Step 4: Run tests to verify pass**

Run: `python3 -m pytest tests/unit/vision/test_boundary_adapters.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add backend/vision/intelligence/boundary_adapters.py tests/unit/vision/test_boundary_adapters.py
git commit -m "feat(vision): add boundary adapters for type-safe ingestion"
```

---

### Task 6: Fix state_intelligence.py NoneType.lower() Crash

**Files:**
- Modify: `backend/vision/intelligence/state_intelligence.py:598-602,930-933`

**Step 1: Fix _identify_error_prone_states (line 598-602)**

At line 598, import the adapter and normalize the key before `.lower()`:

```python
# At top of file (after existing imports), add:
from .boundary_adapters import safe_state_key

# Line 598-602 changes from:
        for from_state, transitions in self.transition_matrix.items():
            for to_state, count in transitions.items():
                total_transitions[from_state] += count
                if 'error' in to_state.lower() or 'fail' in to_state.lower():
                    error_transitions[from_state] += count

# To:
        for from_state, transitions in self.transition_matrix.items():
            for to_state, count in transitions.items():
                total_transitions[from_state] += count
                to_state_str = safe_state_key(to_state)
                if 'error' in to_state_str.lower() or 'fail' in to_state_str.lower():
                    error_transitions[from_state] += count
```

**Step 2: Fix _get_common_error_transitions (line 930-933)**

```python
# Line 930-933 changes from:
        error_transitions = [
            (to_state, count)
            for to_state, count in self.transition_matrix[state_id].items()
            if 'error' in to_state.lower() or 'fail' in to_state.lower()
        ]

# To:
        error_transitions = [
            (to_state, count)
            for to_state, count in self.transition_matrix[state_id].items()
            if 'error' in safe_state_key(to_state).lower() or 'fail' in safe_state_key(to_state).lower()
        ]
```

**Step 3: Commit**

```bash
git add backend/vision/intelligence/state_intelligence.py
git commit -m "fix(vision): guard against NoneType.lower() in state intelligence transition iteration"
```

---

### Task 7: Fix feedback_aware_vision.py NoneType.lower() Crash

**Files:**
- Modify: `backend/vision/intelligence/feedback_aware_vision.py:172-189`

**Step 1: Fix user_response handling (line 172-179)**

```python
# At top of file (after existing imports), add:
from .boundary_adapters import safe_text

# Line 172-179 changes from:
            user_response = await original_callback(change)

            # Calculate response time
            time_to_respond = time.time() - notification_start_time

            # Map response to UserResponse enum
            from backend.core.learning.feedback_loop import UserResponse
            if 'detail' in user_response.lower() or 'yes' in user_response.lower() or 'tell me' in user_response.lower():

# To:
            user_response = safe_text(await original_callback(change))

            # Calculate response time
            time_to_respond = time.time() - notification_start_time

            # Map response to UserResponse enum
            from backend.core.learning.feedback_loop import UserResponse
            user_response_lower = user_response.lower()
            if 'detail' in user_response_lower or 'yes' in user_response_lower or 'tell me' in user_response_lower:
```

Also update lines 181-185 to use `user_response_lower` instead of repeated `.lower()` calls:

```python
            elif 'no' in user_response_lower or 'dismiss' in user_response_lower:
                response_type = UserResponse.DISMISSED
            elif 'later' in user_response_lower or 'not now' in user_response_lower:
                response_type = UserResponse.DEFERRED
            elif 'stop' in user_response_lower or 'never' in user_response_lower:
                response_type = UserResponse.NEGATIVE_FEEDBACK
```

**Step 2: Commit**

```bash
git add backend/vision/intelligence/feedback_aware_vision.py
git commit -m "fix(vision): guard against None callback response in feedback-aware vision"
```

---

### Task 8: Fix SceneGraphNode properties=None Crash

**Files:**
- Modify: `backend/vision/intelligence/semantic_scene_graph.py:120-131`

**Step 1: Add __post_init__ guard**

After line 127 (the existing `get_property` method), add `__post_init__`:

```python
# After line 126 (timestamp field), before get_property, add:
    def __post_init__(self):
        if self.properties is None:
            self.properties = {}
```

The dataclass `SceneGraphNode` at line 120 already has `properties: Dict[str, Any] = field(default_factory=dict)`. The `__post_init__` catches explicit `properties=None` from deserialization/reconstruction.

**Step 2: Commit**

```bash
git add backend/vision/intelligence/semantic_scene_graph.py
git commit -m "fix(vision): guard SceneGraphNode against explicit properties=None"
```

---

### Task 9: Fix Predictive Engine — Type Enforcement + Cache Versioning

**Files:**
- Modify: `backend/vision/intelligence/predictive_precomputation_engine.py:217-219,286-312`

**Step 1: Add type enforcement at add_state (line 217-219)**

```python
# Line 217-219 changes from:
    def add_state(self, state: StateVector) -> int:
        """Add state to matrix if not exists"""
        state_tuple = state.to_tuple()

# To:
    def add_state(self, state: StateVector) -> int:
        """Add state to matrix if not exists"""
        if not isinstance(state, StateVector):
            raise TypeError(
                f"TransitionMatrix.add_state expects StateVector, got {type(state).__name__}"
            )
        state_tuple = state.to_tuple()
```

**Step 2: Add migration guard in get_predictions (line 306-310)**

```python
# Line 306-310 changes from:
            for idx in top_indices:
                if idx in self.idx_to_state and probs[idx] > 0:
                    predictions.append(
                        (self.idx_to_state[idx], float(probs[idx]), float(confidences[idx]))
                    )

# To:
            for idx in top_indices:
                if idx in self.idx_to_state and probs[idx] > 0:
                    candidate = self.idx_to_state[idx]
                    if not isinstance(candidate, StateVector):
                        # Legacy dict entry from pre-migration cache — skip
                        logger.debug(f"Skipping legacy non-StateVector at idx {idx}")
                        continue
                    predictions.append(
                        (candidate, float(probs[idx]), float(confidences[idx]))
                    )
```

**Step 3: Add cache schema versioning**

Near the top of the file (after the existing constants around line 57), add:

```python
# Cache schema version — bump when StateVector fields change
CACHE_SCHEMA_VERSION = 2
```

Then in any cache save/load methods, wrap with versioned envelope. If no explicit save/load exists, add a note that pickle serialization of TransitionMatrix should use the envelope pattern.

**Step 4: Commit**

```bash
git add backend/vision/intelligence/predictive_precomputation_engine.py
git commit -m "fix(vision): enforce StateVector types in TransitionMatrix, add cache versioning"
```

---

### Task 10: Gate A Verification

**Step 1: Run all Phase 0 + Phase 1 tests**

Run: `python3 -m pytest tests/contracts/ tests/unit/vision/test_boundary_adapters.py -v`
Expected: All pass except `test_no_hardcoded_vision_providers` (Phase 2) and `test_no_bypass_construction` (xfail Phase 3)

**Step 2: Verify no broad except clauses were added**

```bash
git diff HEAD~5..HEAD -- '*.py' | grep -n 'except Exception' | head -20
```

Expected: Zero new `except Exception` lines in our changes.

**Step 3: Tag gate**

```bash
git tag gate-a-type-safety
```

---

## Phase 2: Routing Authority Establishment

### Task 11: Remove Hardcoded Vision Provider Exclusion

**Files:**
- Modify: `backend/intelligence/unified_model_serving.py:2419-2422`

**Step 1: Replace hardcoded set with manifest-driven check**

```python
# Line 2419-2422 changes from:
        if request.require_vision:
            # Only providers with vision support
            vision_providers = {ModelProvider.CLAUDE, ModelProvider.PRIME_CLOUD_RUN}
            result = [p for p in result if p in vision_providers]

# To:
        if request.require_vision:
            # v290.1: Manifest-driven capability check (replaces hardcoded set)
            result = [
                p for p in result
                if self._provider_supports_capability(p, "vision")
            ]
```

**Step 2: Add _provider_supports_capability method to ModelRouter class**

Add after `get_preferred_providers` method (around line 2432):

```python
    def _provider_supports_capability(self, provider: 'ModelProvider', capability: str) -> bool:
        """Check if a provider supports a capability via cached manifest.

        Returns True when no manifest exists (bootstrap safety).
        Circuit breaker handles actual failures at runtime.
        """
        if not hasattr(self, '_capability_manifests'):
            return True  # No manifests loaded yet — don't exclude
        manifest = self._capability_manifests.get(provider)
        if manifest is None:
            return True  # Unknown = don't exclude, let circuit breaker handle
        return manifest.supports(capability)

    def set_capability_manifests(self, manifests: dict):
        """Set cached capability manifests from contract gate."""
        self._capability_manifests = manifests
```

**Step 3: Initialize _capability_manifests in __init__ (line 2253)**

Add to `ModelRouter.__init__`:

```python
        # v290.1: Cached capability manifests (populated by contract gate)
        self._capability_manifests: Dict['ModelProvider', 'ProviderManifest'] = {}
```

**Step 4: Commit**

```bash
git add backend/intelligence/unified_model_serving.py
git commit -m "fix(routing): replace hardcoded vision_providers with manifest-driven capability check"
```

---

### Task 12: Wire PrimeRouter Health Into ModelRouter

**Files:**
- Modify: `backend/intelligence/unified_model_serving.py` (ModelRouter class)
- Modify: `backend/core/prime_router.py` (add is_endpoint_healthy query)

**Step 1: Add is_endpoint_healthy to PrimeRouter**

In `backend/core/prime_router.py`, add a public read-only query method to the PrimeRouter class. Find the class definition and add:

```python
    def is_endpoint_healthy(self, endpoint_name: str) -> bool:
        """Read-only health query for ModelRouter delegation.

        Args:
            endpoint_name: One of 'local_prime', 'gcp_prime', 'cloud_run'
        Returns:
            True if endpoint is considered healthy, False otherwise.
        """
        cb = self._circuit_breakers.get(endpoint_name)
        if cb is None:
            return True  # Unknown endpoint — assume healthy
        can_exec, _ = cb.can_execute()
        return can_exec
```

**Step 2: Add health authority wiring to ModelRouter**

In `ModelRouter.__init__` (unified_model_serving.py), add:

```python
        # v290.1: Health authority delegation (PrimeRouter tells us IF healthy)
        self._health_authority = None  # Set via set_health_authority()
```

Add method:

```python
    def set_health_authority(self, prime_router) -> None:
        """Wire PrimeRouter as health authority (called during startup)."""
        self._health_authority = prime_router
        self.logger.info("ModelRouter: health authority wired to PrimeRouter")

    _PROVIDER_TO_ENDPOINT = {
        # ModelProvider enum value -> PrimeRouter endpoint name
    }
```

Note: The `_PROVIDER_TO_ENDPOINT` mapping needs to map `ModelProvider.PRIME_API` -> `"local_prime"`, `ModelProvider.PRIME_CLOUD_RUN` -> `"cloud_run"`, etc. Read the PrimeRouter's circuit breaker keys at implementation time to get the exact mapping.

**Step 3: Commit**

```bash
git add backend/intelligence/unified_model_serving.py backend/core/prime_router.py
git commit -m "feat(routing): wire PrimeRouter health into ModelRouter as read-only authority"
```

---

### Task 13: Verify jprime_llava Loads From Config

**Files:**
- Check: `backend/intelligence/model_registry.py:307-345`
- Create: `tests/unit/intelligence/test_model_registry_dynamic.py`

**Step 1: Write test that jprime_llava loads**

```python
# tests/unit/intelligence/test_model_registry_dynamic.py
"""Test that models defined in hybrid_config.yaml are loaded dynamically."""
import pytest
from pathlib import Path


class TestDynamicModelLoading:
    def test_jprime_llava_in_registry(self):
        """jprime_llava defined in config must appear in registry."""
        from backend.intelligence.model_registry import get_model_registry
        registry = get_model_registry()
        assert "jprime_llava" in registry.models, (
            f"jprime_llava not in registry. Found: {list(registry.models.keys())}"
        )

    def test_jprime_llava_has_vision_capability(self):
        """jprime_llava must have 'vision' capability."""
        from backend.intelligence.model_registry import get_model_registry
        registry = get_model_registry()
        if "jprime_llava" not in registry.models:
            pytest.skip("jprime_llava not loaded")
        model = registry.models["jprime_llava"]
        assert "vision" in model.capabilities, (
            f"jprime_llava capabilities: {model.capabilities}"
        )

    def test_config_models_section_exists(self):
        """hybrid_config.yaml must have models section under gcp."""
        import yaml
        config_path = Path(__file__).parent.parent.parent.parent / "backend" / "core" / "hybrid_config.yaml"
        if not config_path.exists():
            pytest.skip(f"Config not found at {config_path}")
        with open(config_path) as f:
            config = yaml.safe_load(f)
        gcp = config.get("hybrid", {}).get("backends", {}).get("gcp", {})
        models = gcp.get("models", {})
        assert "jprime_llava" in models, (
            f"jprime_llava not in config models. Found: {list(models.keys())}"
        )
```

**Step 2: Run test**

Run: `python3 -m pytest tests/unit/intelligence/test_model_registry_dynamic.py -v`
Expected: If dynamic loading at lines 307-345 works, tests PASS. If not, debug the config structure mismatch.

**Step 3: If tests fail, fix _load_gcp_models indentation/guard**

The dynamic loading loop at line 307 must be OUTSIDE the `if "llm_inference"` guard at line 254. Verify indentation. If the loop is inside the guard, dedent it to method body level.

**Step 4: Commit**

```bash
git add tests/unit/intelligence/test_model_registry_dynamic.py
# If model_registry.py was modified:
git add backend/intelligence/model_registry.py
git commit -m "test(registry): verify jprime_llava loads from config dynamically"
```

---

### Task 14: Absorb CapabilityRouter Into ModelRouter

**Files:**
- Modify: `backend/core/capability_router.py` (thin shim)

**Step 1: Replace CapabilityRouter with delegation shim**

Keep `CircuitBreaker` and `CircuitState` classes (they're useful primitives). Replace `CapabilityRouter` class body with delegation to `get_model_serving()`:

```python
class CapabilityRouter:
    """Compatibility shim — delegates to ModelRouter via UnifiedModelServing.

    The CapabilityRouter's circuit breaker logic has been absorbed into
    ModelRouter. This shim exists for import compatibility only.

    New code should use get_model_serving().router directly.
    """

    def __init__(self, registry=None):
        self._registry = registry
        self.logger = logging.getLogger("jarvis.capability_router")
        self.logger.info("CapabilityRouter: using ModelRouter delegation shim")

    async def route(self, capability: str, **kwargs):
        """Delegate to ModelRouter."""
        try:
            from backend.intelligence.unified_model_serving import get_model_serving
            serving = await get_model_serving()
            return await serving.router.route_by_capability(capability, **kwargs)
        except Exception:
            self.logger.debug(f"CapabilityRouter shim: delegation failed for {capability}")
            return None

    def is_capability_available(self, capability: str) -> bool:
        """Check via registry if available."""
        if self._registry:
            return self._registry.is_capability_available(capability)
        return False
```

**Step 2: Commit**

```bash
git add backend/core/capability_router.py
git commit -m "refactor(routing): absorb CapabilityRouter into ModelRouter delegation shim"
```

---

### Task 15: Add Shadow-Routing Parity Metrics

**Files:**
- Modify: `backend/intelligence/unified_model_serving.py` (ModelRouter.get_preferred_providers)

**Step 1: Add shadow routing to get_preferred_providers**

At the end of `get_preferred_providers`, before `return result`, add:

```python
        # v290.1: Shadow routing parity check (transition safety net)
        if os.environ.get("JARVIS_SHADOW_ROUTING") == "true" and request.require_vision:
            legacy_vision = {ModelProvider.CLAUDE, ModelProvider.PRIME_CLOUD_RUN}
            legacy_result = [p for p in preferred if p in available_providers and p in legacy_vision]
            if set(result) != set(legacy_result):
                self.logger.warning(
                    f"Shadow routing parity mismatch: legacy={legacy_result} "
                    f"new={result} task={request.task_type}"
                )
```

**Step 2: Commit**

```bash
git add backend/intelligence/unified_model_serving.py
git commit -m "feat(routing): add shadow-routing parity metrics for transition safety"
```

---

### Task 16: Gate B Verification

**Step 1: Run contract invariant tests**

Run: `python3 -m pytest tests/contracts/test_routing_invariants.py::TestCapabilityTaxonomyConsistency::test_no_hardcoded_vision_providers -v`
Expected: PASS (hardcoded set removed in Task 11)

**Step 2: Run registry tests**

Run: `python3 -m pytest tests/unit/intelligence/test_model_registry_dynamic.py -v`
Expected: PASS

**Step 3: Tag gate**

```bash
git tag gate-b-routing-authority
```

---

## Phase 2.5: Supervisor Contract Gate

### Task 17: Add Contract Validation to Supervisor

**Files:**
- Modify: `unified_supervisor.py` (add _validate_cross_repo_contracts method)

**Step 1: Add contract validation method**

Find the section between Trinity startup completion and "ready" declaration. Add a new method `_validate_cross_repo_contracts`:

```python
    async def _validate_cross_repo_contracts(self) -> str:
        """Validate cross-repo contracts before declaring ready.

        Returns:
            "pass" — all contracts satisfied
            "degraded" — some services unreachable, continuing with warnings
            "fail" — incompatible versions, blocking startup
        """
        from backend.contracts.contract_version import LOCAL_CONTRACT, compute_policy_hash
        from backend.contracts.manifest_schema import ProviderManifest
        from backend.contracts.routing_authority import ROUTING_INVARIANTS

        results = {}
        local_hash = compute_policy_hash(ROUTING_INVARIANTS)

        # 1. Check Prime capability manifest
        try:
            prime_url = self._get_prime_health_url()
            if prime_url:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"{prime_url}/capabilities", timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            manifest = ProviderManifest.from_dict(data)
                            compat, reason = LOCAL_CONTRACT.is_compatible(manifest.contract_version)
                            if not compat:
                                results["prime"] = ("fail", reason)
                            else:
                                results["prime"] = ("pass", "compatible")
                        else:
                            results["prime"] = ("degraded", f"http_{resp.status}")
            else:
                results["prime"] = ("degraded", "no_endpoint")
        except Exception as e:
            results["prime"] = ("degraded", f"unreachable: {type(e).__name__}")

        # 2. Log contract status
        for service, (status, reason) in results.items():
            level = logging.WARNING if status != "pass" else logging.INFO
            self._log(level, f"Contract gate: {service} = {status} ({reason})")

        # 3. Decision
        if any(s == "fail" for s, _ in results.values()):
            return "fail"
        if any(s == "degraded" for s, _ in results.values()):
            return "degraded"
        return "pass"
```

**Step 2: Wire into startup sequence**

Find the startup point after Trinity startup completes and add:

```python
        # v290.1: Cross-repo contract validation
        contract_result = await self._validate_cross_repo_contracts()
        if contract_result == "fail":
            self._log(logging.ERROR, "Contract gate FAILED — incompatible cross-repo versions")
            # Continue in degraded mode rather than hard-block
            # (operator can investigate via health endpoint)
        self._contract_status = contract_result
```

**Step 3: Commit**

```bash
git add unified_supervisor.py
git commit -m "feat(supervisor): add cross-repo contract gate with version compatibility check"
```

---

### Task 18: Add /capabilities Endpoint to JARVIS-Prime

**Files:**
- Modify: JARVIS-Prime `run_server.py` or `jarvis_prime/server.py`

**Step 1: Add capabilities endpoint**

This change is in the JARVIS-Prime repo. Add a `/capabilities` route:

```python
@app.get("/capabilities")
async def get_capabilities():
    """Publish capability manifest for JARVIS contract gate."""
    import time
    return {
        "provider_id": "jprime",
        "capabilities": ["chat", "reasoning", "code", "vision", "multimodal"],
        "contract_version": [0, 3, 0],
        "policy_hash": "",  # Will be populated when Prime has its own contract package
        "timestamp": time.monotonic(),
    }
```

**Step 2: Commit (in JARVIS-Prime repo)**

```bash
cd /path/to/JARVIS-Prime
git add run_server.py  # or jarvis_prime/server.py
git commit -m "feat(api): add /capabilities endpoint for JARVIS contract gate"
```

---

### Task 19: Gate C Verification

**Step 1: Test contract gate with mock**

Start JARVIS with Prime not running. Verify:
- Contract gate logs `degraded` with reason `unreachable`
- Startup continues (not blocked)

**Step 2: Test with incompatible version**

Temporarily set `min_supported=(99, 0, 0)` in LOCAL_CONTRACT. Start JARVIS with Prime running.
- Contract gate logs `fail` with reason `below min_supported`
- Revert the test change

**Step 3: Tag gate**

```bash
git tag gate-c-contract-gate
```

---

## Phase 3: Bypass Elimination

### Task 20: Add Factory Token to Client Constructors

**Files:**
- Modify: `backend/intelligence/unified_model_serving.py` (PrimeAPIClient, PrimeCloudRunClient, PrimeLocalClient)

**Step 1: Add factory token**

Near the top of unified_model_serving.py (module level), add:

```python
# Factory token — only the factory function can construct clients
_FACTORY_SECRET = "__unified_model_serving_factory__"
```

Then modify each client constructor to require it. For `PrimeAPIClient.__init__`:

```python
    def __init__(self, *, _factory_token: str = "", **kwargs):
        if _factory_token != _FACTORY_SECRET:
            import warnings
            warnings.warn(
                "Direct PrimeAPIClient() construction is deprecated. "
                "Use get_model_serving() factory. "
                "This will become an error in a future version.",
                DeprecationWarning,
                stacklevel=2,
            )
        # ... existing init code
```

Note: Start with `DeprecationWarning` (not `RuntimeError`) to avoid breaking existing code during transition. Upgrade to error after soak test confirms no callers.

**Step 2: Update internal construction sites to pass token**

At lines 2586, 2603, 2612, 2620 (inside `UnifiedModelServing.start()`), add the token:

```python
# Line 2586:
probe_client = PrimeAPIClient(wait_timeout=_quick_timeout, _factory_token=_FACTORY_SECRET)

# Line 2603:
client = PrimeLocalClient(_factory_token=_FACTORY_SECRET)

# Line 2612:
client = PrimeCloudRunClient(_factory_token=_FACTORY_SECRET)
```

**Step 3: Commit**

```bash
git add backend/intelligence/unified_model_serving.py
git commit -m "feat(routing): add factory token to client constructors, deprecate direct construction"
```

---

### Task 21: Remove xfail From AST Invariant Test

**Files:**
- Modify: `tests/contracts/test_routing_invariants.py`

**Step 1: Remove xfail marker**

```python
# Change from:
    @pytest.mark.xfail(reason="Phase 3: bypass elimination not yet implemented")
    def test_no_bypass_construction(self):

# To:
    def test_no_bypass_construction(self):
```

**Step 2: Run test**

Run: `python3 -m pytest tests/contracts/test_routing_invariants.py::TestNoDirectClientConstruction -v`
Expected: PASS (if all bypass sites are inside factory allowlist) or FAIL (if external callers remain — fix those first)

**Step 3: Commit**

```bash
git add tests/contracts/test_routing_invariants.py
git commit -m "test(contracts): enforce no-bypass-construction invariant (remove xfail)"
```

---

### Task 22: Gate D + Final Verification

**Step 1: Run full test suite**

```bash
python3 -m pytest tests/contracts/ tests/unit/vision/ tests/unit/intelligence/ -v
```

Expected: All PASS

**Step 2: Verify no regressions**

```bash
git diff gate-a-type-safety..HEAD --stat
```

Review: only expected files changed, no accidental modifications.

**Step 3: Tag final gate**

```bash
git tag gate-d-bypass-elimination
```

**Step 4: Final commit summary**

```bash
git log --oneline gate-a-type-safety..HEAD
```

Verify commit chain is clean and each commit is atomic.

---

## File Manifest Summary

| Task | File | Action |
|------|------|--------|
| 1 | `backend/contracts/__init__.py` | Create |
| 1 | `backend/contracts/capability_taxonomy.py` | Create |
| 2 | `backend/contracts/contract_version.py` | Create |
| 2 | `backend/contracts/manifest_schema.py` | Create |
| 3 | `backend/contracts/routing_authority.py` | Create |
| 3 | `backend/contracts/non_functional_invariants.py` | Create |
| 4 | `tests/contracts/__init__.py` | Create |
| 4 | `tests/contracts/test_routing_invariants.py` | Create |
| 4 | `tests/contracts/test_schema_compatibility.py` | Create |
| 5 | `backend/vision/intelligence/boundary_adapters.py` | Create |
| 5 | `tests/unit/vision/test_boundary_adapters.py` | Create |
| 6 | `backend/vision/intelligence/state_intelligence.py` | Edit (lines 598-602, 930-933) |
| 7 | `backend/vision/intelligence/feedback_aware_vision.py` | Edit (lines 172-189) |
| 8 | `backend/vision/intelligence/semantic_scene_graph.py` | Edit (line ~127) |
| 9 | `backend/vision/intelligence/predictive_precomputation_engine.py` | Edit (lines 217-219, 306-310) |
| 11 | `backend/intelligence/unified_model_serving.py` | Edit (lines 2419-2422) |
| 12 | `backend/intelligence/unified_model_serving.py` | Edit (ModelRouter class) |
| 12 | `backend/core/prime_router.py` | Edit (add method) |
| 13 | `backend/intelligence/model_registry.py` | Verify/Edit |
| 13 | `tests/unit/intelligence/test_model_registry_dynamic.py` | Create |
| 14 | `backend/core/capability_router.py` | Edit (shim) |
| 15 | `backend/intelligence/unified_model_serving.py` | Edit (shadow routing) |
| 17 | `unified_supervisor.py` | Edit (contract gate) |
| 18 | JARVIS-Prime `run_server.py` | Edit (add endpoint) |
| 20 | `backend/intelligence/unified_model_serving.py` | Edit (factory token) |
| 21 | `tests/contracts/test_routing_invariants.py` | Edit (remove xfail) |
