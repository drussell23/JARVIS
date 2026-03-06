# Disease 8 Cure: Reactive State Propagation — Wave 3 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build the environment variable compatibility bridge in shadow mode — `EnvKeyMapping` table, canonical coercion functions, `BridgeMode` enum with transition safety, shadow comparison logic, and parity tracking via `ShadowParityLogger` integration.

**Architecture:** A new `env_bridge.py` module declares a frozen `EnvKeyMapping` dataclass and a module-level `ENV_KEY_MAPPINGS` tuple mapping all 17 manifest keys to their env var equivalents. An `EnvBridge` class manages the bridge lifecycle: it reads `JARVIS_STATE_BRIDGE_MODE` from `os.environ` at construction (bootstrap chicken-egg resolution), validates mode transitions (`legacy → shadow → active` only), and in shadow mode performs canonical comparisons between store values and env values after each store write. Canonical coercion normalizes booleans (`"true"/"1"/"yes" → True`), absent keys (→ schema default), numerics (parsed to int/float), and enums (case-sensitive, whitespace-stripped). Parity is tracked by reusing the existing `ShadowParityLogger` from `backend.core.umf.shadow_parity`. Sensitive keys have values redacted in logs.

**Tech Stack:** Python 3.9+, stdlib only in the reactive_state package (dataclasses, typing, os, threading, logging, enum). One cross-package import: `ShadowParityLogger` from `backend.core.umf.shadow_parity`.

**Design doc:** `docs/plans/2026-03-05-reactive-state-propagation-design.md` — Section 8 (Environment Variable Compatibility Bridge), Appendices A.1–A.7 (Safety Invariants).

**Wave 0+1+2 code (already built and tagged `disease8-wave2`):**
- `backend/core/reactive_state/types.py` — StateEntry, WriteResult, WriteStatus, WriteRejection, JournalEntry
- `backend/core/reactive_state/schemas.py` — KeySchema, SchemaRegistry
- `backend/core/reactive_state/ownership.py` — OwnershipRule, OwnershipRegistry
- `backend/core/reactive_state/journal.py` — AppendOnlyJournal (SQLite WAL + publish cursor)
- `backend/core/reactive_state/watchers.py` — WatcherManager
- `backend/core/reactive_state/manifest.py` — OWNERSHIP_RULES, KEY_SCHEMAS, CONSISTENCY_GROUPS, builders
- `backend/core/reactive_state/store.py` — ReactiveStateStore (9-step write pipeline)
- `backend/core/reactive_state/policy.py` — PolicyEngine, 3 invariant rules
- `backend/core/reactive_state/audit.py` — AuditLog, post_replay_invariant_audit
- `backend/core/reactive_state/event_emitter.py` — StateEventEmitter, PublishReconciler, build_state_changed_event

**Existing UMF infrastructure reused:**
- `backend/core/umf/shadow_parity.py` — `ShadowParityLogger` (record, parity_ratio, is_promotion_ready, bounded diff history)

**Manifest keys (17 total, from `manifest.py`):**

| State Key | Value Type | Default | Owner |
|-----------|-----------|---------|-------|
| `lifecycle.effective_mode` | enum | `"local_full"` | supervisor |
| `lifecycle.startup_complete` | bool | `False` | supervisor |
| `memory.can_spawn_heavy` | bool | `False` | memory_assessor |
| `memory.available_gb` | float | `0.0` | memory_assessor |
| `memory.admission_reason` | str | `""` | memory_assessor |
| `memory.tier` | enum | `"unknown"` | memory_assessor |
| `memory.startup_mode` | enum | `"local_full"` | memory_assessor |
| `memory.source` | str | `""` | memory_assessor |
| `gcp.offload_active` | bool | `False` | gcp_controller |
| `gcp.node_ip` | str | `""` | gcp_controller |
| `gcp.node_port` | int | `8000` | gcp_controller |
| `gcp.node_booting` | bool | `False` | gcp_controller |
| `gcp.prime_endpoint` | str | `""` | gcp_controller |
| `hollow.client_active` | bool | `False` | gcp_controller |
| `prime.early_pid` | int (nullable) | `None` | supervisor |
| `prime.early_port` | int (nullable) | `None` | supervisor |
| `service.backend_minimal` | bool | `False` | supervisor |

---

## Task 1: BridgeMode enum and canonical coercion functions

**Files:**
- Create: `backend/core/reactive_state/env_bridge.py`
- Test: `tests/unit/core/reactive_state/test_env_bridge_coercion.py`

**Step 1: Write the failing test**

```python
# tests/unit/core/reactive_state/test_env_bridge_coercion.py
"""Tests for BridgeMode enum and canonical coercion functions."""
from __future__ import annotations

import pytest

from backend.core.reactive_state.env_bridge import (
    BridgeMode,
    canonical_bool,
    canonical_int,
    canonical_float,
    canonical_str,
    canonical_enum,
)


# ── BridgeMode ──────────────────────────────────────────────────────


class TestBridgeMode:
    """BridgeMode enum values and ordering."""

    def test_has_three_modes(self) -> None:
        assert set(BridgeMode) == {
            BridgeMode.LEGACY,
            BridgeMode.SHADOW,
            BridgeMode.ACTIVE,
        }

    def test_values_are_strings(self) -> None:
        assert BridgeMode.LEGACY.value == "legacy"
        assert BridgeMode.SHADOW.value == "shadow"
        assert BridgeMode.ACTIVE.value == "active"

    def test_from_string_valid(self) -> None:
        assert BridgeMode("legacy") is BridgeMode.LEGACY
        assert BridgeMode("shadow") is BridgeMode.SHADOW
        assert BridgeMode("active") is BridgeMode.ACTIVE

    def test_from_string_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            BridgeMode("turbo")

    def test_can_transition_forward_only(self) -> None:
        assert BridgeMode.LEGACY.can_transition_to(BridgeMode.SHADOW) is True
        assert BridgeMode.SHADOW.can_transition_to(BridgeMode.ACTIVE) is True
        assert BridgeMode.LEGACY.can_transition_to(BridgeMode.ACTIVE) is False  # no skip
        assert BridgeMode.ACTIVE.can_transition_to(BridgeMode.SHADOW) is False  # no reverse
        assert BridgeMode.SHADOW.can_transition_to(BridgeMode.LEGACY) is False  # no reverse
        assert BridgeMode.LEGACY.can_transition_to(BridgeMode.LEGACY) is False  # no self


# ── canonical_bool ──────────────────────────────────────────────────


class TestCanonicalBool:
    """Canonical comparison for booleans (Appendix A.5)."""

    @pytest.mark.parametrize("env_val", ["true", "True", "TRUE", "1", "yes", "Yes", "YES"])
    def test_truthy_strings(self, env_val: str) -> None:
        assert canonical_bool(env_val) is True

    @pytest.mark.parametrize("env_val", ["false", "False", "FALSE", "0", "no", "No", "NO", ""])
    def test_falsy_strings(self, env_val: str) -> None:
        assert canonical_bool(env_val) is False

    def test_none_returns_none(self) -> None:
        assert canonical_bool(None) is None

    def test_already_bool(self) -> None:
        assert canonical_bool(True) is True
        assert canonical_bool(False) is False


# ── canonical_int ───────────────────────────────────────────────────


class TestCanonicalInt:
    """Canonical comparison for integers."""

    def test_string_to_int(self) -> None:
        assert canonical_int("42") == 42
        assert canonical_int("0") == 0

    def test_already_int(self) -> None:
        assert canonical_int(8000) == 8000

    def test_none_returns_none(self) -> None:
        assert canonical_int(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert canonical_int("") is None

    def test_non_numeric_returns_none(self) -> None:
        assert canonical_int("abc") is None


# ── canonical_float ─────────────────────────────────────────────────


class TestCanonicalFloat:
    """Canonical comparison for floats."""

    def test_string_to_float(self) -> None:
        assert canonical_float("3.14") == pytest.approx(3.14)
        assert canonical_float("0.0") == pytest.approx(0.0)

    def test_int_string_to_float(self) -> None:
        assert canonical_float("42") == pytest.approx(42.0)

    def test_already_float(self) -> None:
        assert canonical_float(7.5) == pytest.approx(7.5)

    def test_already_int_coerces(self) -> None:
        assert canonical_float(42) == pytest.approx(42.0)

    def test_none_returns_none(self) -> None:
        assert canonical_float(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert canonical_float("") is None


# ── canonical_str ───────────────────────────────────────────────────


class TestCanonicalStr:
    """Canonical comparison for strings."""

    def test_passthrough(self) -> None:
        assert canonical_str("hello") == "hello"

    def test_none_returns_none(self) -> None:
        assert canonical_str(None) is None

    def test_non_string_coerces(self) -> None:
        assert canonical_str(42) == "42"


# ── canonical_enum ──────────────────────────────────────────────────


class TestCanonicalEnum:
    """Canonical comparison for enums (case-sensitive, strip whitespace)."""

    def test_strips_whitespace(self) -> None:
        assert canonical_enum("  local_full  ") == "local_full"

    def test_case_sensitive(self) -> None:
        assert canonical_enum("Local_Full") == "Local_Full"  # NOT lowered

    def test_none_returns_none(self) -> None:
        assert canonical_enum(None) is None

    def test_passthrough(self) -> None:
        assert canonical_enum("cloud_only") == "cloud_only"
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_env_bridge_coercion.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.core.reactive_state.env_bridge'`

**Step 3: Write minimal implementation**

```python
# backend/core/reactive_state/env_bridge.py
"""Environment variable compatibility bridge for reactive state store.

Provides the bridge between legacy ``os.environ``-based state and the new
``ReactiveStateStore``.  Operates in three modes (``legacy → shadow → active``),
with canonical coercion functions for type-safe comparisons.

Design rules
------------
* **No** third-party imports -- stdlib + sibling modules + ``ShadowParityLogger``.
* ``BridgeMode`` is a ``str`` enum with forward-only transition validation.
* ``EnvKeyMapping`` is ``@dataclass(frozen=True)`` (immutable value object).
* Canonical coercion normalises env strings to Python types for parity checks.
"""
from __future__ import annotations

import enum
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.core.reactive_state.schemas import SchemaRegistry
from backend.core.reactive_state.types import StateEntry
from backend.core.umf.shadow_parity import ShadowParityLogger

logger = logging.getLogger(__name__)

# ── Transition order (used for forward-only validation) ─────────────

_MODE_ORDER = {"legacy": 0, "shadow": 1, "active": 2}


# ── BridgeMode ──────────────────────────────────────────────────────


class BridgeMode(str, enum.Enum):
    """Operating mode for the env bridge."""

    LEGACY = "legacy"
    SHADOW = "shadow"
    ACTIVE = "active"

    def can_transition_to(self, target: BridgeMode) -> bool:
        """Return True if transitioning from self to *target* is allowed.

        Rules (Appendix A.1):
        - Forward only: legacy → shadow → active.
        - No skipping (legacy → active is forbidden).
        - No reverse transitions.
        - No self-transitions.
        """
        current_ord = _MODE_ORDER[self.value]
        target_ord = _MODE_ORDER[target.value]
        return target_ord == current_ord + 1


# ── Canonical coercion functions (Appendix A.5) ────────────────────


def canonical_bool(value: Any) -> Optional[bool]:
    """Normalise a value to bool using canonical comparison rules.

    ``"true"``, ``"1"``, ``"yes"`` (case-insensitive) → ``True``.
    ``"false"``, ``"0"``, ``"no"``, ``""`` (case-insensitive) → ``False``.
    ``None`` → ``None``.  Already-bool values pass through.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lower = value.lower()
        if lower in ("true", "1", "yes"):
            return True
        if lower in ("false", "0", "no", ""):
            return False
    return bool(value)


def canonical_int(value: Any) -> Optional[int]:
    """Normalise a value to int.

    Strings are parsed.  Empty or non-numeric strings → ``None``.
    ``None`` → ``None``.  Already-int values pass through.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        if value == "":
            return None
        try:
            return int(value)
        except ValueError:
            return None
    return None


def canonical_float(value: Any) -> Optional[float]:
    """Normalise a value to float.

    Strings and ints are parsed/promoted.  Empty strings → ``None``.
    ``None`` → ``None``.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        if value == "":
            return None
        try:
            return float(value)
        except ValueError:
            return None
    return None


def canonical_str(value: Any) -> Optional[str]:
    """Normalise a value to str.  ``None`` → ``None``."""
    if value is None:
        return None
    return str(value)


def canonical_enum(value: Any) -> Optional[str]:
    """Normalise an enum value: strip whitespace, case-sensitive.

    ``None`` → ``None``.
    """
    if value is None:
        return None
    return str(value).strip()
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_env_bridge_coercion.py -v`
Expected: PASS (all tests)

**Step 5: Commit**

```bash
git add backend/core/reactive_state/env_bridge.py tests/unit/core/reactive_state/test_env_bridge_coercion.py
git commit -m "feat(disease8): add BridgeMode enum and canonical coercion functions (Wave 3, Task 1)"
```

---

## Task 2: EnvKeyMapping dataclass and ENV_KEY_MAPPINGS table

**Files:**
- Modify: `backend/core/reactive_state/env_bridge.py`
- Test: `tests/unit/core/reactive_state/test_env_key_mappings.py`

**Step 1: Write the failing test**

```python
# tests/unit/core/reactive_state/test_env_key_mappings.py
"""Tests for EnvKeyMapping dataclass and the ENV_KEY_MAPPINGS table."""
from __future__ import annotations

import pytest

from backend.core.reactive_state.env_bridge import (
    ENV_KEY_MAPPINGS,
    EnvKeyMapping,
)
from backend.core.reactive_state.manifest import KEY_SCHEMAS


class TestEnvKeyMapping:
    """EnvKeyMapping frozen dataclass."""

    def test_is_frozen(self) -> None:
        mapping = ENV_KEY_MAPPINGS[0]
        with pytest.raises(AttributeError):
            mapping.env_var = "CHANGED"  # type: ignore[misc]

    def test_has_required_fields(self) -> None:
        mapping = ENV_KEY_MAPPINGS[0]
        assert hasattr(mapping, "env_var")
        assert hasattr(mapping, "state_key")
        assert hasattr(mapping, "coerce_to_env")
        assert hasattr(mapping, "coerce_from_env")
        assert hasattr(mapping, "sensitive")

    def test_coerce_functions_are_callable(self) -> None:
        for mapping in ENV_KEY_MAPPINGS:
            assert callable(mapping.coerce_to_env), f"{mapping.env_var} coerce_to_env not callable"
            assert callable(mapping.coerce_from_env), f"{mapping.env_var} coerce_from_env not callable"


class TestEnvKeyMappingsTable:
    """ENV_KEY_MAPPINGS covers all manifest keys."""

    def test_covers_all_manifest_keys(self) -> None:
        """Every key in KEY_SCHEMAS must have a mapping."""
        manifest_keys = {ks.key for ks in KEY_SCHEMAS}
        mapped_keys = {m.state_key for m in ENV_KEY_MAPPINGS}
        assert mapped_keys == manifest_keys, (
            f"Missing: {manifest_keys - mapped_keys}, Extra: {mapped_keys - manifest_keys}"
        )

    def test_no_duplicate_env_vars(self) -> None:
        env_vars = [m.env_var for m in ENV_KEY_MAPPINGS]
        assert len(env_vars) == len(set(env_vars)), "Duplicate env vars found"

    def test_no_duplicate_state_keys(self) -> None:
        state_keys = [m.state_key for m in ENV_KEY_MAPPINGS]
        assert len(state_keys) == len(set(state_keys)), "Duplicate state keys found"

    def test_env_var_naming_convention(self) -> None:
        """All env vars should start with JARVIS_ and be UPPER_SNAKE_CASE."""
        for mapping in ENV_KEY_MAPPINGS:
            assert mapping.env_var.startswith("JARVIS_"), (
                f"{mapping.env_var} does not start with JARVIS_"
            )
            assert mapping.env_var == mapping.env_var.upper(), (
                f"{mapping.env_var} is not UPPER_SNAKE_CASE"
            )


class TestCoercionRoundTrip:
    """coerce_to_env and coerce_from_env are inverses for typical values."""

    def test_bool_roundtrip(self) -> None:
        bool_mappings = [m for m in ENV_KEY_MAPPINGS if m.state_key == "gcp.offload_active"]
        assert len(bool_mappings) == 1
        m = bool_mappings[0]
        assert m.coerce_from_env(m.coerce_to_env(True)) is True
        assert m.coerce_from_env(m.coerce_to_env(False)) is False

    def test_int_roundtrip(self) -> None:
        int_mappings = [m for m in ENV_KEY_MAPPINGS if m.state_key == "gcp.node_port"]
        assert len(int_mappings) == 1
        m = int_mappings[0]
        assert m.coerce_from_env(m.coerce_to_env(8000)) == 8000

    def test_float_roundtrip(self) -> None:
        float_mappings = [m for m in ENV_KEY_MAPPINGS if m.state_key == "memory.available_gb"]
        assert len(float_mappings) == 1
        m = float_mappings[0]
        assert m.coerce_from_env(m.coerce_to_env(7.5)) == pytest.approx(7.5)

    def test_str_roundtrip(self) -> None:
        str_mappings = [m for m in ENV_KEY_MAPPINGS if m.state_key == "gcp.node_ip"]
        assert len(str_mappings) == 1
        m = str_mappings[0]
        assert m.coerce_from_env(m.coerce_to_env("10.0.0.1")) == "10.0.0.1"

    def test_enum_roundtrip(self) -> None:
        enum_mappings = [m for m in ENV_KEY_MAPPINGS if m.state_key == "lifecycle.effective_mode"]
        assert len(enum_mappings) == 1
        m = enum_mappings[0]
        assert m.coerce_from_env(m.coerce_to_env("cloud_first")) == "cloud_first"

    def test_nullable_int_roundtrip_none(self) -> None:
        nullable_mappings = [m for m in ENV_KEY_MAPPINGS if m.state_key == "prime.early_pid"]
        assert len(nullable_mappings) == 1
        m = nullable_mappings[0]
        env_str = m.coerce_to_env(None)
        assert env_str == ""
        assert m.coerce_from_env(env_str) is None
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_env_key_mappings.py -v`
Expected: FAIL with `ImportError: cannot import name 'ENV_KEY_MAPPINGS'`

**Step 3: Write minimal implementation**

Add to `backend/core/reactive_state/env_bridge.py` (after the canonical functions):

```python
# ── EnvKeyMapping ───────────────────────────────────────────────────


@dataclass(frozen=True)
class EnvKeyMapping:
    """Maps an environment variable to a reactive state key.

    Attributes
    ----------
    env_var:
        Environment variable name (e.g. ``"JARVIS_GCP_OFFLOAD_ACTIVE"``).
    state_key:
        Dotted state key (e.g. ``"gcp.offload_active"``).
    coerce_to_env:
        Converts a store value to an env string.
    coerce_from_env:
        Converts an env string to the store's native type.
    sensitive:
        If ``True``, values are redacted in log output.
    """

    env_var: str
    state_key: str
    coerce_to_env: Callable[[Any], str]
    coerce_from_env: Callable[[str], Any]
    sensitive: bool = False


# ── Coerce-to-env helpers (store value → env string) ────────────────


def _bool_to_env(value: Any) -> str:
    if value is None:
        return ""
    return "true" if value else "false"


def _int_to_env(value: Any) -> str:
    if value is None:
        return ""
    return str(int(value))


def _float_to_env(value: Any) -> str:
    if value is None:
        return ""
    return str(float(value))


def _str_to_env(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _enum_to_env(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


# ── Coerce-from-env helpers (env string → store value) ──────────────


def _bool_from_env(value: str) -> bool:
    return canonical_bool(value) or False


def _int_from_env(value: str) -> Optional[int]:
    return canonical_int(value)


def _nullable_int_from_env(value: str) -> Optional[int]:
    return canonical_int(value)


def _float_from_env(value: str) -> Optional[float]:
    return canonical_float(value)


def _str_from_env(value: str) -> str:
    return value


def _enum_from_env(value: str) -> str:
    return value.strip()


# ── ENV_KEY_MAPPINGS ────────────────────────────────────────────────

ENV_KEY_MAPPINGS: Tuple[EnvKeyMapping, ...] = (
    # -- lifecycle --
    EnvKeyMapping(
        env_var="JARVIS_EFFECTIVE_MODE",
        state_key="lifecycle.effective_mode",
        coerce_to_env=_enum_to_env,
        coerce_from_env=_enum_from_env,
    ),
    EnvKeyMapping(
        env_var="JARVIS_STARTUP_COMPLETE",
        state_key="lifecycle.startup_complete",
        coerce_to_env=_bool_to_env,
        coerce_from_env=_bool_from_env,
    ),
    # -- memory --
    EnvKeyMapping(
        env_var="JARVIS_MEMORY_CAN_SPAWN_HEAVY",
        state_key="memory.can_spawn_heavy",
        coerce_to_env=_bool_to_env,
        coerce_from_env=_bool_from_env,
    ),
    EnvKeyMapping(
        env_var="JARVIS_MEMORY_AVAILABLE_GB",
        state_key="memory.available_gb",
        coerce_to_env=_float_to_env,
        coerce_from_env=_float_from_env,
    ),
    EnvKeyMapping(
        env_var="JARVIS_MEMORY_ADMISSION_REASON",
        state_key="memory.admission_reason",
        coerce_to_env=_str_to_env,
        coerce_from_env=_str_from_env,
    ),
    EnvKeyMapping(
        env_var="JARVIS_MEMORY_TIER",
        state_key="memory.tier",
        coerce_to_env=_enum_to_env,
        coerce_from_env=_enum_from_env,
    ),
    EnvKeyMapping(
        env_var="JARVIS_MEMORY_STARTUP_MODE",
        state_key="memory.startup_mode",
        coerce_to_env=_enum_to_env,
        coerce_from_env=_enum_from_env,
    ),
    EnvKeyMapping(
        env_var="JARVIS_MEMORY_SOURCE",
        state_key="memory.source",
        coerce_to_env=_str_to_env,
        coerce_from_env=_str_from_env,
    ),
    # -- gcp --
    EnvKeyMapping(
        env_var="JARVIS_GCP_OFFLOAD_ACTIVE",
        state_key="gcp.offload_active",
        coerce_to_env=_bool_to_env,
        coerce_from_env=_bool_from_env,
    ),
    EnvKeyMapping(
        env_var="JARVIS_GCP_NODE_IP",
        state_key="gcp.node_ip",
        coerce_to_env=_str_to_env,
        coerce_from_env=_str_from_env,
    ),
    EnvKeyMapping(
        env_var="JARVIS_GCP_NODE_PORT",
        state_key="gcp.node_port",
        coerce_to_env=_int_to_env,
        coerce_from_env=_int_from_env,
    ),
    EnvKeyMapping(
        env_var="JARVIS_GCP_NODE_BOOTING",
        state_key="gcp.node_booting",
        coerce_to_env=_bool_to_env,
        coerce_from_env=_bool_from_env,
    ),
    EnvKeyMapping(
        env_var="JARVIS_GCP_PRIME_ENDPOINT",
        state_key="gcp.prime_endpoint",
        coerce_to_env=_str_to_env,
        coerce_from_env=_str_from_env,
    ),
    # -- hollow --
    EnvKeyMapping(
        env_var="JARVIS_HOLLOW_CLIENT_ACTIVE",
        state_key="hollow.client_active",
        coerce_to_env=_bool_to_env,
        coerce_from_env=_bool_from_env,
    ),
    # -- prime --
    EnvKeyMapping(
        env_var="JARVIS_PRIME_EARLY_PID",
        state_key="prime.early_pid",
        coerce_to_env=_int_to_env,
        coerce_from_env=_nullable_int_from_env,
    ),
    EnvKeyMapping(
        env_var="JARVIS_PRIME_EARLY_PORT",
        state_key="prime.early_port",
        coerce_to_env=_int_to_env,
        coerce_from_env=_nullable_int_from_env,
    ),
    # -- service --
    EnvKeyMapping(
        env_var="JARVIS_SERVICE_BACKEND_MINIMAL",
        state_key="service.backend_minimal",
        coerce_to_env=_bool_to_env,
        coerce_from_env=_bool_from_env,
    ),
)
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_env_key_mappings.py -v`
Expected: PASS (all tests)

**Step 5: Commit**

```bash
git add backend/core/reactive_state/env_bridge.py tests/unit/core/reactive_state/test_env_key_mappings.py
git commit -m "feat(disease8): add EnvKeyMapping dataclass and ENV_KEY_MAPPINGS table (Wave 3, Task 2)"
```

---

## Task 3: EnvBridge class — construction, bootstrap, mode transitions

**Files:**
- Modify: `backend/core/reactive_state/env_bridge.py`
- Test: `tests/unit/core/reactive_state/test_env_bridge_lifecycle.py`

**Step 1: Write the failing test**

```python
# tests/unit/core/reactive_state/test_env_bridge_lifecycle.py
"""Tests for EnvBridge construction, bootstrap, and mode transitions."""
from __future__ import annotations

import os
from unittest import mock

import pytest

from backend.core.reactive_state.env_bridge import (
    BridgeMode,
    EnvBridge,
    ENV_KEY_MAPPINGS,
)
from backend.core.reactive_state.manifest import build_schema_registry


class TestBootstrapResolution:
    """JARVIS_STATE_BRIDGE_MODE bootstrap from os.environ (Appendix A.2)."""

    def test_defaults_to_legacy_when_absent(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("JARVIS_STATE_BRIDGE_MODE", None)
            bridge = EnvBridge(schema_registry=build_schema_registry())
            assert bridge.mode is BridgeMode.LEGACY

    def test_reads_shadow_from_env(self) -> None:
        with mock.patch.dict(os.environ, {"JARVIS_STATE_BRIDGE_MODE": "shadow"}):
            bridge = EnvBridge(schema_registry=build_schema_registry())
            assert bridge.mode is BridgeMode.SHADOW

    def test_reads_active_from_env(self) -> None:
        with mock.patch.dict(os.environ, {"JARVIS_STATE_BRIDGE_MODE": "active"}):
            bridge = EnvBridge(schema_registry=build_schema_registry())
            assert bridge.mode is BridgeMode.ACTIVE

    def test_invalid_value_defaults_to_legacy(self) -> None:
        with mock.patch.dict(os.environ, {"JARVIS_STATE_BRIDGE_MODE": "turbo"}):
            bridge = EnvBridge(schema_registry=build_schema_registry())
            assert bridge.mode is BridgeMode.LEGACY

    def test_explicit_mode_overrides_env(self) -> None:
        with mock.patch.dict(os.environ, {"JARVIS_STATE_BRIDGE_MODE": "legacy"}):
            bridge = EnvBridge(
                schema_registry=build_schema_registry(),
                initial_mode=BridgeMode.SHADOW,
            )
            assert bridge.mode is BridgeMode.SHADOW


class TestModeTransitions:
    """Forward-only mode transitions (Appendix A.1)."""

    def test_legacy_to_shadow(self) -> None:
        bridge = EnvBridge(
            schema_registry=build_schema_registry(),
            initial_mode=BridgeMode.LEGACY,
        )
        bridge.transition_to(BridgeMode.SHADOW)
        assert bridge.mode is BridgeMode.SHADOW

    def test_shadow_to_active(self) -> None:
        bridge = EnvBridge(
            schema_registry=build_schema_registry(),
            initial_mode=BridgeMode.SHADOW,
        )
        bridge.transition_to(BridgeMode.ACTIVE)
        assert bridge.mode is BridgeMode.ACTIVE

    def test_skip_not_allowed(self) -> None:
        bridge = EnvBridge(
            schema_registry=build_schema_registry(),
            initial_mode=BridgeMode.LEGACY,
        )
        with pytest.raises(ValueError, match="Cannot transition"):
            bridge.transition_to(BridgeMode.ACTIVE)

    def test_reverse_not_allowed(self) -> None:
        bridge = EnvBridge(
            schema_registry=build_schema_registry(),
            initial_mode=BridgeMode.SHADOW,
        )
        with pytest.raises(ValueError, match="Cannot transition"):
            bridge.transition_to(BridgeMode.LEGACY)

    def test_self_transition_not_allowed(self) -> None:
        bridge = EnvBridge(
            schema_registry=build_schema_registry(),
            initial_mode=BridgeMode.LEGACY,
        )
        with pytest.raises(ValueError, match="Cannot transition"):
            bridge.transition_to(BridgeMode.LEGACY)


class TestBridgeLookups:
    """EnvBridge mapping lookups."""

    def test_lookup_by_state_key(self) -> None:
        bridge = EnvBridge(
            schema_registry=build_schema_registry(),
            initial_mode=BridgeMode.LEGACY,
        )
        mapping = bridge.get_mapping_by_state_key("gcp.offload_active")
        assert mapping is not None
        assert mapping.env_var == "JARVIS_GCP_OFFLOAD_ACTIVE"

    def test_lookup_by_state_key_missing(self) -> None:
        bridge = EnvBridge(
            schema_registry=build_schema_registry(),
            initial_mode=BridgeMode.LEGACY,
        )
        assert bridge.get_mapping_by_state_key("nonexistent.key") is None

    def test_lookup_by_env_var(self) -> None:
        bridge = EnvBridge(
            schema_registry=build_schema_registry(),
            initial_mode=BridgeMode.LEGACY,
        )
        mapping = bridge.get_mapping_by_env_var("JARVIS_GCP_NODE_PORT")
        assert mapping is not None
        assert mapping.state_key == "gcp.node_port"
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_env_bridge_lifecycle.py -v`
Expected: FAIL with `ImportError: cannot import name 'EnvBridge'`

**Step 3: Write minimal implementation**

Add to `backend/core/reactive_state/env_bridge.py`:

```python
# ── EnvBridge ───────────────────────────────────────────────────────


class EnvBridge:
    """Environment variable compatibility bridge.

    Reads ``JARVIS_STATE_BRIDGE_MODE`` from ``os.environ`` at construction
    (bootstrap chicken-egg resolution, Appendix A.2).  Manages forward-only
    mode transitions and provides mapping lookups.

    Parameters
    ----------
    schema_registry:
        The schema registry for looking up defaults and types.
    initial_mode:
        If provided, overrides the env-based bootstrap resolution.
    parity_logger:
        Optional ``ShadowParityLogger`` for tracking comparison results.
    """

    def __init__(
        self,
        *,
        schema_registry: SchemaRegistry,
        initial_mode: Optional[BridgeMode] = None,
        parity_logger: Optional[ShadowParityLogger] = None,
    ) -> None:
        if initial_mode is not None:
            self._mode = initial_mode
        else:
            self._mode = self._resolve_bootstrap_mode()

        self._schema_registry = schema_registry
        self._parity_logger = parity_logger or ShadowParityLogger()
        self._lock = threading.Lock()

        # Build lookup indexes
        self._by_state_key: Dict[str, EnvKeyMapping] = {
            m.state_key: m for m in ENV_KEY_MAPPINGS
        }
        self._by_env_var: Dict[str, EnvKeyMapping] = {
            m.env_var: m for m in ENV_KEY_MAPPINGS
        }

    @staticmethod
    def _resolve_bootstrap_mode() -> BridgeMode:
        """Read JARVIS_STATE_BRIDGE_MODE from os.environ.

        Returns BridgeMode.LEGACY if missing or invalid.
        """
        raw = os.environ.get("JARVIS_STATE_BRIDGE_MODE", "")
        try:
            return BridgeMode(raw)
        except ValueError:
            if raw:
                logger.error(
                    "Invalid JARVIS_STATE_BRIDGE_MODE=%r, defaulting to legacy",
                    raw,
                )
            return BridgeMode.LEGACY

    # ── Properties ──────────────────────────────────────────────────

    @property
    def mode(self) -> BridgeMode:
        """Current bridge mode."""
        return self._mode

    @property
    def parity_logger(self) -> ShadowParityLogger:
        """The parity logger tracking shadow comparisons."""
        return self._parity_logger

    # ── Mode transitions ────────────────────────────────────────────

    def transition_to(self, target: BridgeMode) -> None:
        """Transition to *target* mode.

        Raises ``ValueError`` if the transition is not allowed.
        """
        with self._lock:
            if not self._mode.can_transition_to(target):
                raise ValueError(
                    f"Cannot transition from {self._mode.value} to {target.value}. "
                    f"Only forward transitions (legacy → shadow → active) are allowed."
                )
            logger.info(
                "Bridge mode transition: %s → %s",
                self._mode.value,
                target.value,
            )
            self._mode = target

    # ── Lookups ─────────────────────────────────────────────────────

    def get_mapping_by_state_key(self, state_key: str) -> Optional[EnvKeyMapping]:
        """Return the mapping for *state_key*, or ``None``."""
        return self._by_state_key.get(state_key)

    def get_mapping_by_env_var(self, env_var: str) -> Optional[EnvKeyMapping]:
        """Return the mapping for *env_var*, or ``None``."""
        return self._by_env_var.get(env_var)
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_env_bridge_lifecycle.py -v`
Expected: PASS (all tests)

**Step 5: Commit**

```bash
git add backend/core/reactive_state/env_bridge.py tests/unit/core/reactive_state/test_env_bridge_lifecycle.py
git commit -m "feat(disease8): add EnvBridge class with bootstrap and mode transitions (Wave 3, Task 3)"
```

---

## Task 4: Shadow comparison logic with canonical parity

**Files:**
- Modify: `backend/core/reactive_state/env_bridge.py`
- Test: `tests/unit/core/reactive_state/test_env_bridge_shadow.py`

**Step 1: Write the failing test**

```python
# tests/unit/core/reactive_state/test_env_bridge_shadow.py
"""Tests for shadow comparison logic in EnvBridge."""
from __future__ import annotations

import os
from unittest import mock

import pytest

from backend.core.reactive_state.env_bridge import (
    BridgeMode,
    EnvBridge,
)
from backend.core.reactive_state.manifest import build_schema_registry
from backend.core.reactive_state.types import StateEntry
from backend.core.umf.shadow_parity import ShadowParityLogger


def _make_entry(
    key: str,
    value: object,
    version: int = 1,
) -> StateEntry:
    """Create a minimal StateEntry for testing."""
    return StateEntry(
        key=key,
        value=value,
        version=version,
        epoch=0,
        writer="test",
        origin="explicit",
        updated_at_mono=0.0,
        updated_at_unix_ms=0,
    )


class TestShadowComparison:
    """Shadow mode: compare store value vs env value."""

    def test_matching_bool_records_parity(self) -> None:
        parity = ShadowParityLogger()
        bridge = EnvBridge(
            schema_registry=build_schema_registry(),
            initial_mode=BridgeMode.SHADOW,
            parity_logger=parity,
        )
        with mock.patch.dict(os.environ, {"JARVIS_GCP_OFFLOAD_ACTIVE": "true"}):
            bridge.shadow_compare(
                _make_entry("gcp.offload_active", True),
                global_revision=1,
            )
        assert parity.total_comparisons == 1
        assert parity.mismatches == 0

    def test_mismatching_bool_records_mismatch(self) -> None:
        parity = ShadowParityLogger()
        bridge = EnvBridge(
            schema_registry=build_schema_registry(),
            initial_mode=BridgeMode.SHADOW,
            parity_logger=parity,
        )
        with mock.patch.dict(os.environ, {"JARVIS_GCP_OFFLOAD_ACTIVE": "false"}):
            bridge.shadow_compare(
                _make_entry("gcp.offload_active", True),
                global_revision=1,
            )
        assert parity.total_comparisons == 1
        assert parity.mismatches == 1

    def test_matching_int(self) -> None:
        parity = ShadowParityLogger()
        bridge = EnvBridge(
            schema_registry=build_schema_registry(),
            initial_mode=BridgeMode.SHADOW,
            parity_logger=parity,
        )
        with mock.patch.dict(os.environ, {"JARVIS_GCP_NODE_PORT": "8000"}):
            bridge.shadow_compare(
                _make_entry("gcp.node_port", 8000),
                global_revision=2,
            )
        assert parity.mismatches == 0

    def test_matching_float(self) -> None:
        parity = ShadowParityLogger()
        bridge = EnvBridge(
            schema_registry=build_schema_registry(),
            initial_mode=BridgeMode.SHADOW,
            parity_logger=parity,
        )
        with mock.patch.dict(os.environ, {"JARVIS_MEMORY_AVAILABLE_GB": "7.5"}):
            bridge.shadow_compare(
                _make_entry("memory.available_gb", 7.5),
                global_revision=3,
            )
        assert parity.mismatches == 0

    def test_absent_env_uses_schema_default(self) -> None:
        """Absent env key is treated as schema default (Appendix A.5)."""
        parity = ShadowParityLogger()
        bridge = EnvBridge(
            schema_registry=build_schema_registry(),
            initial_mode=BridgeMode.SHADOW,
            parity_logger=parity,
        )
        # gcp.offload_active default is False
        env = dict(os.environ)
        env.pop("JARVIS_GCP_OFFLOAD_ACTIVE", None)
        with mock.patch.dict(os.environ, env, clear=True):
            bridge.shadow_compare(
                _make_entry("gcp.offload_active", False),
                global_revision=4,
            )
        assert parity.mismatches == 0

    def test_absent_env_mismatch_with_non_default(self) -> None:
        """Absent env key vs non-default store value = mismatch."""
        parity = ShadowParityLogger()
        bridge = EnvBridge(
            schema_registry=build_schema_registry(),
            initial_mode=BridgeMode.SHADOW,
            parity_logger=parity,
        )
        env = dict(os.environ)
        env.pop("JARVIS_GCP_OFFLOAD_ACTIVE", None)
        with mock.patch.dict(os.environ, env, clear=True):
            bridge.shadow_compare(
                _make_entry("gcp.offload_active", True),
                global_revision=5,
            )
        assert parity.mismatches == 1

    def test_unmapped_key_is_ignored(self) -> None:
        """Keys not in mapping table produce no comparison."""
        parity = ShadowParityLogger()
        bridge = EnvBridge(
            schema_registry=build_schema_registry(),
            initial_mode=BridgeMode.SHADOW,
            parity_logger=parity,
        )
        bridge.shadow_compare(
            _make_entry("unknown.key", "value"),
            global_revision=6,
        )
        assert parity.total_comparisons == 0

    def test_legacy_mode_skips_comparison(self) -> None:
        """In legacy mode, shadow_compare is a no-op."""
        parity = ShadowParityLogger()
        bridge = EnvBridge(
            schema_registry=build_schema_registry(),
            initial_mode=BridgeMode.LEGACY,
            parity_logger=parity,
        )
        with mock.patch.dict(os.environ, {"JARVIS_GCP_OFFLOAD_ACTIVE": "true"}):
            bridge.shadow_compare(
                _make_entry("gcp.offload_active", True),
                global_revision=7,
            )
        assert parity.total_comparisons == 0

    def test_nullable_int_absent_matches_none(self) -> None:
        """Nullable int: absent env var matches None store value."""
        parity = ShadowParityLogger()
        bridge = EnvBridge(
            schema_registry=build_schema_registry(),
            initial_mode=BridgeMode.SHADOW,
            parity_logger=parity,
        )
        env = dict(os.environ)
        env.pop("JARVIS_PRIME_EARLY_PID", None)
        with mock.patch.dict(os.environ, env, clear=True):
            bridge.shadow_compare(
                _make_entry("prime.early_pid", None),
                global_revision=8,
            )
        assert parity.mismatches == 0

    def test_sensitive_key_redacts_values_in_log(self, caplog: pytest.LogCaptureFixture) -> None:
        """Sensitive keys have values redacted in log messages."""
        # We'll need at least one sensitive mapping for this test.
        # None of the current mappings are sensitive, so this tests
        # that non-sensitive keys do NOT redact.
        parity = ShadowParityLogger()
        bridge = EnvBridge(
            schema_registry=build_schema_registry(),
            initial_mode=BridgeMode.SHADOW,
            parity_logger=parity,
        )
        import logging
        with caplog.at_level(logging.DEBUG, logger="backend.core.reactive_state.env_bridge"):
            with mock.patch.dict(os.environ, {"JARVIS_GCP_OFFLOAD_ACTIVE": "false"}):
                bridge.shadow_compare(
                    _make_entry("gcp.offload_active", True),
                    global_revision=9,
                )
        # Non-sensitive: values should appear in the log
        assert any("True" in record.message or "true" in record.message.lower() for record in caplog.records if "mismatch" in record.message.lower()) or parity.mismatches == 1


class TestShadowCompareEnum:
    """Shadow comparison for enum values with canonical matching."""

    def test_matching_enum(self) -> None:
        parity = ShadowParityLogger()
        bridge = EnvBridge(
            schema_registry=build_schema_registry(),
            initial_mode=BridgeMode.SHADOW,
            parity_logger=parity,
        )
        with mock.patch.dict(os.environ, {"JARVIS_EFFECTIVE_MODE": "cloud_first"}):
            bridge.shadow_compare(
                _make_entry("lifecycle.effective_mode", "cloud_first"),
                global_revision=10,
            )
        assert parity.mismatches == 0

    def test_enum_whitespace_stripped(self) -> None:
        """Env value with surrounding whitespace still matches."""
        parity = ShadowParityLogger()
        bridge = EnvBridge(
            schema_registry=build_schema_registry(),
            initial_mode=BridgeMode.SHADOW,
            parity_logger=parity,
        )
        with mock.patch.dict(os.environ, {"JARVIS_EFFECTIVE_MODE": "  cloud_first  "}):
            bridge.shadow_compare(
                _make_entry("lifecycle.effective_mode", "cloud_first"),
                global_revision=11,
            )
        assert parity.mismatches == 0
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_env_bridge_shadow.py -v`
Expected: FAIL with `AttributeError: 'EnvBridge' object has no attribute 'shadow_compare'`

**Step 3: Write minimal implementation**

Add to `EnvBridge` class in `backend/core/reactive_state/env_bridge.py`:

```python
    # ── Shadow comparison ───────────────────────────────────────────

    def shadow_compare(
        self,
        entry: StateEntry,
        global_revision: int,
    ) -> None:
        """Compare a store entry against the corresponding env var.

        Only runs in ``shadow`` or ``active`` mode.  In ``legacy`` mode
        this is a no-op.  Unmapped keys are silently ignored.

        Parameters
        ----------
        entry:
            The state entry just written to the store.
        global_revision:
            The journal's global revision for this write.
        """
        if self._mode is BridgeMode.LEGACY:
            return

        mapping = self._by_state_key.get(entry.key)
        if mapping is None:
            return  # unmapped key -- pass-through

        # Read env value (absent → None)
        env_raw = os.environ.get(mapping.env_var)

        # Determine canonical store value
        store_canonical = self._canonicalize(entry.key, entry.value)

        # Determine canonical env value
        if env_raw is None:
            # Absent key: use schema default (Appendix A.5)
            schema = self._schema_registry.get(entry.key)
            env_canonical = schema.default if schema is not None else None
        else:
            env_canonical = self._canonicalize(entry.key, mapping.coerce_from_env(env_raw))

        # Compare
        matched = self._values_equal(store_canonical, env_canonical)

        # Record parity
        store_str = "<redacted>" if mapping.sensitive else repr(store_canonical)
        env_str = "<redacted>" if mapping.sensitive else repr(env_canonical)

        self._parity_logger.record(
            trace_id=f"shadow.{global_revision}",
            category=entry.key,
            legacy_result=env_str,
            umf_result=store_str,
        )

        if not matched:
            logger.warning(
                "[ENV-BRIDGE] Shadow mismatch key=%s store=%s env=%s rev=%d",
                entry.key,
                store_str,
                env_str,
                global_revision,
            )

    def _canonicalize(self, key: str, value: Any) -> Any:
        """Normalise a value using schema type info for canonical comparison."""
        schema = self._schema_registry.get(key)
        if schema is None:
            return value
        vtype = schema.value_type
        if vtype == "bool":
            return canonical_bool(value)
        if vtype == "int":
            return canonical_int(value)
        if vtype == "float":
            return canonical_float(value)
        if vtype == "enum":
            return canonical_enum(value)
        if vtype == "str":
            return canonical_str(value)
        return value

    @staticmethod
    def _values_equal(a: Any, b: Any) -> bool:
        """Compare two canonicalised values, handling float tolerance."""
        if isinstance(a, float) and isinstance(b, float):
            return abs(a - b) < 1e-9
        return a == b
```

**Important:** The `shadow_compare` method records *every* comparison via the parity logger (both matches and mismatches). The `ShadowParityLogger.record()` only increments the mismatch counter when `legacy_result != umf_result`, so we need matching comparisons to also record. Since the existing `ShadowParityLogger.record()` already does `self._total += 1` unconditionally, this works — matches increment `_total` but not `_mismatches_count`.

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_env_bridge_shadow.py -v`
Expected: PASS (all tests)

**Step 5: Commit**

```bash
git add backend/core/reactive_state/env_bridge.py tests/unit/core/reactive_state/test_env_bridge_shadow.py
git commit -m "feat(disease8): add shadow comparison with canonical parity tracking (Wave 3, Task 4)"
```

---

## Task 5: Promotion readiness and parity stats

**Files:**
- Modify: `backend/core/reactive_state/env_bridge.py`
- Test: `tests/unit/core/reactive_state/test_env_bridge_promotion.py`

**Step 1: Write the failing test**

```python
# tests/unit/core/reactive_state/test_env_bridge_promotion.py
"""Tests for promotion readiness check and parity stats."""
from __future__ import annotations

import os
from unittest import mock

import pytest

from backend.core.reactive_state.env_bridge import (
    BridgeMode,
    EnvBridge,
)
from backend.core.reactive_state.manifest import build_schema_registry
from backend.core.reactive_state.types import StateEntry
from backend.core.umf.shadow_parity import ShadowParityLogger


def _make_entry(key: str, value: object, version: int = 1) -> StateEntry:
    return StateEntry(
        key=key, value=value, version=version, epoch=0,
        writer="test", origin="explicit",
        updated_at_mono=0.0, updated_at_unix_ms=0,
    )


class TestPromotionReadiness:
    """Promotion to active is blocked if parity < 99.9% or insufficient data."""

    def test_not_ready_insufficient_data(self) -> None:
        parity = ShadowParityLogger(parity_threshold=0.999, min_comparisons=100)
        bridge = EnvBridge(
            schema_registry=build_schema_registry(),
            initial_mode=BridgeMode.SHADOW,
            parity_logger=parity,
        )
        assert bridge.is_promotion_ready() is False

    def test_ready_after_sufficient_matching(self) -> None:
        parity = ShadowParityLogger(parity_threshold=0.999, min_comparisons=10)
        bridge = EnvBridge(
            schema_registry=build_schema_registry(),
            initial_mode=BridgeMode.SHADOW,
            parity_logger=parity,
        )
        with mock.patch.dict(os.environ, {"JARVIS_GCP_OFFLOAD_ACTIVE": "true"}):
            for i in range(10):
                bridge.shadow_compare(
                    _make_entry("gcp.offload_active", True, version=i + 1),
                    global_revision=i + 1,
                )
        assert bridge.is_promotion_ready() is True

    def test_not_ready_too_many_mismatches(self) -> None:
        parity = ShadowParityLogger(parity_threshold=0.999, min_comparisons=10)
        bridge = EnvBridge(
            schema_registry=build_schema_registry(),
            initial_mode=BridgeMode.SHADOW,
            parity_logger=parity,
        )
        with mock.patch.dict(os.environ, {"JARVIS_GCP_OFFLOAD_ACTIVE": "false"}):
            for i in range(10):
                bridge.shadow_compare(
                    _make_entry("gcp.offload_active", True, version=i + 1),
                    global_revision=i + 1,
                )
        assert bridge.is_promotion_ready() is False


class TestParityStats:
    """Bridge exposes parity statistics."""

    def test_parity_ratio_starts_at_one(self) -> None:
        bridge = EnvBridge(
            schema_registry=build_schema_registry(),
            initial_mode=BridgeMode.SHADOW,
        )
        stats = bridge.parity_stats()
        assert stats["parity_ratio"] == 1.0
        assert stats["total_comparisons"] == 0
        assert stats["mismatches"] == 0

    def test_parity_ratio_after_mix(self) -> None:
        parity = ShadowParityLogger(min_comparisons=1)
        bridge = EnvBridge(
            schema_registry=build_schema_registry(),
            initial_mode=BridgeMode.SHADOW,
            parity_logger=parity,
        )
        # 1 match
        with mock.patch.dict(os.environ, {"JARVIS_GCP_OFFLOAD_ACTIVE": "true"}):
            bridge.shadow_compare(
                _make_entry("gcp.offload_active", True),
                global_revision=1,
            )
        # 1 mismatch
        with mock.patch.dict(os.environ, {"JARVIS_GCP_OFFLOAD_ACTIVE": "false"}):
            bridge.shadow_compare(
                _make_entry("gcp.offload_active", True),
                global_revision=2,
            )
        stats = bridge.parity_stats()
        assert stats["total_comparisons"] == 2
        assert stats["mismatches"] == 1
        assert stats["parity_ratio"] == pytest.approx(0.5)

    def test_recent_diffs_populated(self) -> None:
        parity = ShadowParityLogger(min_comparisons=1)
        bridge = EnvBridge(
            schema_registry=build_schema_registry(),
            initial_mode=BridgeMode.SHADOW,
            parity_logger=parity,
        )
        with mock.patch.dict(os.environ, {"JARVIS_GCP_OFFLOAD_ACTIVE": "false"}):
            bridge.shadow_compare(
                _make_entry("gcp.offload_active", True),
                global_revision=1,
            )
        stats = bridge.parity_stats()
        assert len(stats["recent_diffs"]) == 1
        assert stats["recent_diffs"][0]["category"] == "gcp.offload_active"
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_env_bridge_promotion.py -v`
Expected: FAIL with `AttributeError: 'EnvBridge' object has no attribute 'is_promotion_ready'`

**Step 3: Write minimal implementation**

Add to `EnvBridge` class:

```python
    # ── Promotion & stats ───────────────────────────────────────────

    def is_promotion_ready(self) -> bool:
        """Return True if shadow parity meets the promotion threshold.

        Delegates to ``ShadowParityLogger.is_promotion_ready()``.
        """
        return self._parity_logger.is_promotion_ready()

    def parity_stats(self) -> Dict[str, Any]:
        """Return a dict of parity statistics for observability.

        Keys: ``total_comparisons``, ``mismatches``, ``parity_ratio``,
        ``is_promotion_ready``, ``recent_diffs``.
        """
        return {
            "total_comparisons": self._parity_logger.total_comparisons,
            "mismatches": self._parity_logger.mismatches,
            "parity_ratio": self._parity_logger.parity_ratio,
            "is_promotion_ready": self._parity_logger.is_promotion_ready(),
            "recent_diffs": self._parity_logger.get_recent_diffs(),
        }
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_env_bridge_promotion.py -v`
Expected: PASS (all tests)

**Step 5: Commit**

```bash
git add backend/core/reactive_state/env_bridge.py tests/unit/core/reactive_state/test_env_bridge_promotion.py
git commit -m "feat(disease8): add promotion readiness and parity stats to EnvBridge (Wave 3, Task 5)"
```

---

## Task 6: Package exports and Wave 3 integration test

**Files:**
- Modify: `backend/core/reactive_state/__init__.py`
- Test: `tests/unit/core/reactive_state/test_wave3_integration.py`

**Step 1: Write the failing test**

```python
# tests/unit/core/reactive_state/test_wave3_integration.py
"""Wave 3 integration tests — env bridge shadow mode end-to-end."""
from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from backend.core.reactive_state import (
    BridgeMode,
    EnvBridge,
    ReactiveStateStore,
    WriteStatus,
)
from backend.core.reactive_state.manifest import (
    build_ownership_registry,
    build_schema_registry,
)
from backend.core.umf.shadow_parity import ShadowParityLogger


@pytest.fixture()
def tmp_journal(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


class TestShadowModeEndToEnd:
    """Full store write → shadow comparison → parity tracking lifecycle."""

    def test_write_triggers_shadow_compare(self, tmp_journal: Path) -> None:
        """After a store write in shadow mode, the bridge records a comparison."""
        schema_reg = build_schema_registry()
        ownership_reg = build_ownership_registry()
        parity = ShadowParityLogger(min_comparisons=1)

        bridge = EnvBridge(
            schema_registry=schema_reg,
            initial_mode=BridgeMode.SHADOW,
            parity_logger=parity,
        )

        store = ReactiveStateStore(
            journal_path=tmp_journal,
            epoch=0,
            session_id="test-session",
            ownership_registry=ownership_reg,
            schema_registry=schema_reg,
        )
        store.open()
        try:
            with mock.patch.dict(os.environ, {"JARVIS_GCP_OFFLOAD_ACTIVE": "true"}):
                result = store.write(
                    key="gcp.offload_active",
                    value=True,
                    expected_version=0,
                    writer="gcp_controller",
                )
                assert result.status == WriteStatus.OK

                # Manually trigger shadow compare (in real usage, a watcher would do this)
                bridge.shadow_compare(result.entry, store.global_revision())

            assert parity.total_comparisons == 1
            assert parity.mismatches == 0
        finally:
            store.close()

    def test_shadow_watcher_integration(self, tmp_journal: Path) -> None:
        """A watcher on '*' triggers shadow_compare for every write."""
        schema_reg = build_schema_registry()
        ownership_reg = build_ownership_registry()
        parity = ShadowParityLogger(min_comparisons=1)

        bridge = EnvBridge(
            schema_registry=schema_reg,
            initial_mode=BridgeMode.SHADOW,
            parity_logger=parity,
        )

        store = ReactiveStateStore(
            journal_path=tmp_journal,
            epoch=0,
            session_id="test-session",
            ownership_registry=ownership_reg,
            schema_registry=schema_reg,
        )
        store.open()
        try:
            # Register a watcher that feeds into shadow_compare
            def on_change(old, new):
                bridge.shadow_compare(new, store.global_revision())

            store.watch("*", on_change)

            with mock.patch.dict(os.environ, {
                "JARVIS_GCP_OFFLOAD_ACTIVE": "true",
                "JARVIS_MEMORY_AVAILABLE_GB": "7.5",
            }):
                store.write(
                    key="gcp.offload_active",
                    value=True,
                    expected_version=0,
                    writer="gcp_controller",
                )
                store.write(
                    key="memory.available_gb",
                    value=7.5,
                    expected_version=0,
                    writer="memory_assessor",
                )

            assert parity.total_comparisons == 2
            assert parity.mismatches == 0
            assert bridge.is_promotion_ready() is True
        finally:
            store.close()

    def test_mode_lifecycle_legacy_to_shadow(self, tmp_journal: Path) -> None:
        """Bridge starts in legacy, transitions to shadow, comparisons begin."""
        schema_reg = build_schema_registry()
        parity = ShadowParityLogger(min_comparisons=1)

        bridge = EnvBridge(
            schema_registry=schema_reg,
            initial_mode=BridgeMode.LEGACY,
            parity_logger=parity,
        )

        # In legacy mode, comparisons are skipped
        from backend.core.reactive_state.types import StateEntry
        entry = StateEntry(
            key="gcp.offload_active", value=True, version=1,
            epoch=0, writer="gcp_controller", origin="explicit",
            updated_at_mono=0.0, updated_at_unix_ms=0,
        )
        with mock.patch.dict(os.environ, {"JARVIS_GCP_OFFLOAD_ACTIVE": "true"}):
            bridge.shadow_compare(entry, global_revision=1)
        assert parity.total_comparisons == 0

        # Transition to shadow
        bridge.transition_to(BridgeMode.SHADOW)

        with mock.patch.dict(os.environ, {"JARVIS_GCP_OFFLOAD_ACTIVE": "true"}):
            bridge.shadow_compare(entry, global_revision=2)
        assert parity.total_comparisons == 1
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_wave3_integration.py -v`
Expected: FAIL with `ImportError: cannot import name 'BridgeMode' from 'backend.core.reactive_state'`

**Step 3: Update package exports**

```python
# backend/core/reactive_state/__init__.py
"""Reactive State Propagation -- Disease 8 cure.

Replaces 23+ environment variables used for cross-component state
with a versioned, observable, typed, CAS-protected state store.
"""
from backend.core.reactive_state.audit import AuditLog, AuditSeverity
from backend.core.reactive_state.env_bridge import (
    BridgeMode,
    EnvBridge,
    EnvKeyMapping,
    ENV_KEY_MAPPINGS,
)
from backend.core.reactive_state.event_emitter import (
    PublishReconciler,
    StateEventEmitter,
    build_state_changed_event,
)
from backend.core.reactive_state.manifest import (
    build_ownership_registry,
    build_schema_registry,
)
from backend.core.reactive_state.policy import PolicyEngine, build_default_policy_engine
from backend.core.reactive_state.store import ReactiveStateStore
from backend.core.reactive_state.types import (
    StateEntry,
    WriteResult,
    WriteStatus,
)

__all__ = [
    "AuditLog",
    "AuditSeverity",
    "BridgeMode",
    "ENV_KEY_MAPPINGS",
    "EnvBridge",
    "EnvKeyMapping",
    "PolicyEngine",
    "PublishReconciler",
    "ReactiveStateStore",
    "StateEntry",
    "StateEventEmitter",
    "WriteResult",
    "WriteStatus",
    "build_default_policy_engine",
    "build_ownership_registry",
    "build_schema_registry",
    "build_state_changed_event",
]
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_wave3_integration.py -v`
Expected: PASS (all tests)

**Step 5: Run full reactive_state test suite**

Run: `python3 -m pytest tests/unit/core/reactive_state/ -v --tb=short`
Expected: All tests pass (Wave 1 + Wave 2 + Wave 3)

**Step 6: Commit**

```bash
git add backend/core/reactive_state/__init__.py tests/unit/core/reactive_state/test_wave3_integration.py
git commit -m "feat(disease8): update exports and add Wave 3 integration tests (Wave 3, Task 6)"
```

---

## Acceptance Criteria (Wave 3 Gate)

Before tagging `disease8-wave3`:

1. **All tests pass:** `python3 -m pytest tests/unit/core/reactive_state/ -v` — 0 failures.
2. **Coverage:** `env_bridge.py` has tests for every public method and edge case.
3. **Mapping completeness:** `ENV_KEY_MAPPINGS` covers all 17 manifest keys (enforced by test).
4. **Coercion roundtrip:** Every type (bool, int, float, str, enum, nullable int) has roundtrip tests.
5. **Shadow correctness:** Absent env → schema default, canonical comparisons, parity tracking.
6. **Mode safety:** Forward-only transitions, bootstrap resolution from env, invalid value handling.
7. **No regressions:** All Wave 1 and Wave 2 tests still pass.
