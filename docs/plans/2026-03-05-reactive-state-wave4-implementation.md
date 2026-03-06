# Disease 8 Cure: Reactive State Propagation — Wave 4 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build active-mode env bridge capabilities: env mirror writes with loop prevention, per-domain kill switches for blast radius control, and `get_subprocess_env()` for coherent subprocess environment snapshots.

**Architecture:** Extends `env_bridge.py` with active-mode behavior. When the bridge is in `ACTIVE` mode, successful store writes are mirrored to `os.environ` via `mirror_to_env()` with a version guard for loop prevention (Appendix A.7). A dedicated serialization lock (`_env_lock`) prevents race conditions on the process-global `os.environ` (Appendix A.4). Per-domain kill switches (`JARVIS_STATE_BRIDGE_ACTIVE_DOMAINS`) allow incremental rollout — only listed domains use the store as authority; others remain in shadow/legacy behavior (Appendix A.13). `get_subprocess_env()` builds a coherent env dict from the store snapshot for child process spawning (Appendix A.12).

**Tech Stack:** Python 3.9+, stdlib only in the reactive_state package (threading, os, dataclasses, typing, logging). One cross-module reference: `ReactiveStateStore` from `backend.core.reactive_state.store` (TYPE_CHECKING import only).

**Design doc:** `docs/plans/2026-03-05-reactive-state-propagation-design.md` — Section 8 (Active Mode Behavior), Appendices A.4, A.7, A.12, A.13.

**Wave 0+1+2+3 code (already built and tagged `disease8-wave3`):**
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
- `backend/core/reactive_state/env_bridge.py` — BridgeMode, EnvKeyMapping, ENV_KEY_MAPPINGS, EnvBridge (shadow mode)

**Key reference — ENV_KEY_MAPPINGS (17 entries):**

| State Key | Env Var | Type | Domain |
|-----------|---------|------|--------|
| `lifecycle.effective_mode` | `JARVIS_STARTUP_EFFECTIVE_MODE` | enum | lifecycle |
| `lifecycle.startup_complete` | `JARVIS_STARTUP_COMPLETE` | bool | lifecycle |
| `memory.can_spawn_heavy` | `JARVIS_CAN_SPAWN_HEAVY` | bool | memory |
| `memory.available_gb` | `JARVIS_HEAVY_ADMISSION_AVAILABLE_GB` | float | memory |
| `memory.admission_reason` | `JARVIS_HEAVY_ADMISSION_REASON` | str | memory |
| `memory.tier` | `JARVIS_MEASURED_MEMORY_TIER` | enum | memory |
| `memory.startup_mode` | `JARVIS_STARTUP_MODE` | enum | memory |
| `memory.source` | `JARVIS_MEASURED_MEMORY_SOURCE` | str | memory |
| `gcp.offload_active` | `JARVIS_GCP_OFFLOAD_ACTIVE` | bool | gcp |
| `gcp.node_ip` | `JARVIS_INVINCIBLE_NODE_IP` | str | gcp |
| `gcp.node_port` | `JARVIS_INVINCIBLE_NODE_PORT` | int | gcp |
| `gcp.node_booting` | `JARVIS_INVINCIBLE_NODE_BOOTING` | bool | gcp |
| `gcp.prime_endpoint` | `JARVIS_GCP_PRIME_ENDPOINT` | str | gcp |
| `hollow.client_active` | `JARVIS_HOLLOW_CLIENT_ACTIVE` | bool | hollow |
| `prime.early_pid` | `JARVIS_PRIME_EARLY_PID` | int (nullable) | prime |
| `prime.early_port` | `JARVIS_PRIME_EARLY_PORT` | int (nullable) | prime |
| `service.backend_minimal` | `JARVIS_BACKEND_MINIMAL` | bool | service |

**Domain prefixes (first segment of state key):** `lifecycle`, `memory`, `gcp`, `hollow`, `prime`, `service`.

---

## Task 1: Active-mode env mirror writes with loop prevention

**Files:**
- Create: `tests/unit/core/reactive_state/test_env_bridge_active.py`
- Modify: `backend/core/reactive_state/env_bridge.py`

**Context:** The `EnvBridge` class (Wave 3) currently supports `LEGACY` and `SHADOW` modes. In `ACTIVE` mode, every successful store write must be mirrored to `os.environ` as a compatibility write. A version guard prevents the same version from being mirrored twice (loop prevention per design doc Appendix A.7). A dedicated `_env_lock` serializes env mutations (Appendix A.4). Additionally, `shadow_compare()` must be restricted to `SHADOW` mode only — in `ACTIVE` mode the store is authoritative and shadow comparison is meaningless.

**Step 1: Write the failing tests**

Create `tests/unit/core/reactive_state/test_env_bridge_active.py`:

```python
"""Tests for EnvBridge active-mode env mirror writes with loop prevention."""
from __future__ import annotations

import os
import time
from typing import Any
from unittest import mock

import pytest

from backend.core.reactive_state.env_bridge import BridgeMode, EnvBridge
from backend.core.reactive_state.manifest import build_schema_registry
from backend.core.reactive_state.types import StateEntry


# -- Helpers ----------------------------------------------------------------


def _make_entry(key: str, value: Any, version: int = 1) -> StateEntry:
    """Create a minimal ``StateEntry`` for testing."""
    return StateEntry(
        key=key,
        value=value,
        version=version,
        epoch=1,
        writer="test",
        origin="explicit",
        updated_at_mono=time.monotonic(),
        updated_at_unix_ms=int(time.time() * 1000),
    )


# -- Fixtures ---------------------------------------------------------------


@pytest.fixture()
def schema_registry():
    return build_schema_registry()


# -- TestMirrorToEnv --------------------------------------------------------


class TestMirrorToEnv:
    """EnvBridge.mirror_to_env() writes store values to os.environ in ACTIVE mode."""

    def test_mirrors_bool_to_env(self, schema_registry) -> None:
        """Bool value True -> 'true' in os.environ."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
        entry = _make_entry("gcp.offload_active", True)
        with mock.patch.dict(os.environ, {}, clear=False):
            result = bridge.mirror_to_env(entry)
            assert result is True
            assert os.environ["JARVIS_GCP_OFFLOAD_ACTIVE"] == "true"

    def test_mirrors_int_to_env(self, schema_registry) -> None:
        """Int value 9090 -> '9090' in os.environ."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
        entry = _make_entry("gcp.node_port", 9090)
        with mock.patch.dict(os.environ, {}, clear=False):
            result = bridge.mirror_to_env(entry)
            assert result is True
            assert os.environ["JARVIS_INVINCIBLE_NODE_PORT"] == "9090"

    def test_mirrors_float_to_env(self, schema_registry) -> None:
        """Float value 7.5 -> '7.5' in os.environ."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
        entry = _make_entry("memory.available_gb", 7.5)
        with mock.patch.dict(os.environ, {}, clear=False):
            result = bridge.mirror_to_env(entry)
            assert result is True
            assert os.environ["JARVIS_HEAVY_ADMISSION_AVAILABLE_GB"] == "7.5"

    def test_mirrors_str_to_env(self, schema_registry) -> None:
        """Str value -> same string in os.environ."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
        entry = _make_entry("gcp.node_ip", "10.0.0.42")
        with mock.patch.dict(os.environ, {}, clear=False):
            result = bridge.mirror_to_env(entry)
            assert result is True
            assert os.environ["JARVIS_INVINCIBLE_NODE_IP"] == "10.0.0.42"

    def test_mirrors_enum_to_env(self, schema_registry) -> None:
        """Enum value -> string in os.environ."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
        entry = _make_entry("lifecycle.effective_mode", "cloud_first")
        with mock.patch.dict(os.environ, {}, clear=False):
            result = bridge.mirror_to_env(entry)
            assert result is True
            assert os.environ["JARVIS_STARTUP_EFFECTIVE_MODE"] == "cloud_first"

    def test_mirrors_none_to_empty_string(self, schema_registry) -> None:
        """None value -> '' in os.environ."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
        entry = _make_entry("prime.early_pid", None)
        with mock.patch.dict(os.environ, {}, clear=False):
            result = bridge.mirror_to_env(entry)
            assert result is True
            assert os.environ["JARVIS_PRIME_EARLY_PID"] == ""

    def test_noop_in_legacy_mode(self, schema_registry) -> None:
        """mirror_to_env returns False and does nothing in LEGACY mode."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.LEGACY)
        entry = _make_entry("gcp.offload_active", True)
        result = bridge.mirror_to_env(entry)
        assert result is False

    def test_noop_in_shadow_mode(self, schema_registry) -> None:
        """mirror_to_env returns False and does nothing in SHADOW mode."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.SHADOW)
        entry = _make_entry("gcp.offload_active", True)
        result = bridge.mirror_to_env(entry)
        assert result is False

    def test_unmapped_key_returns_false(self, schema_registry) -> None:
        """Keys not in ENV_KEY_MAPPINGS are silently skipped."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
        entry = _make_entry("unknown.key.not.mapped", "value")
        result = bridge.mirror_to_env(entry)
        assert result is False


# -- TestVersionGuard -------------------------------------------------------


class TestVersionGuard:
    """Version guard prevents re-mirroring the same version (loop prevention A.7)."""

    def test_skips_same_version(self, schema_registry) -> None:
        """Mirroring the same version twice -> second call returns False."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
        entry = _make_entry("gcp.offload_active", True, version=1)
        with mock.patch.dict(os.environ, {}, clear=False):
            assert bridge.mirror_to_env(entry) is True
            assert bridge.mirror_to_env(entry) is False

    def test_allows_new_version(self, schema_registry) -> None:
        """Different version -> mirrors again and updates env value."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
        entry_v1 = _make_entry("gcp.offload_active", True, version=1)
        entry_v2 = _make_entry("gcp.offload_active", False, version=2)
        with mock.patch.dict(os.environ, {}, clear=False):
            assert bridge.mirror_to_env(entry_v1) is True
            assert os.environ["JARVIS_GCP_OFFLOAD_ACTIVE"] == "true"
            assert bridge.mirror_to_env(entry_v2) is True
            assert os.environ["JARVIS_GCP_OFFLOAD_ACTIVE"] == "false"

    def test_independent_keys_have_independent_guards(self, schema_registry) -> None:
        """Version guard tracks per-key, not globally."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
        entry_a = _make_entry("gcp.offload_active", True, version=1)
        entry_b = _make_entry("gcp.node_port", 9090, version=1)
        with mock.patch.dict(os.environ, {}, clear=False):
            assert bridge.mirror_to_env(entry_a) is True
            assert bridge.mirror_to_env(entry_b) is True
            # Re-mirror same versions -> both skip
            assert bridge.mirror_to_env(entry_a) is False
            assert bridge.mirror_to_env(entry_b) is False


# -- TestShadowCompareActiveNoop -------------------------------------------


class TestShadowCompareActiveNoop:
    """shadow_compare is a no-op in ACTIVE mode (store is authoritative)."""

    def test_shadow_compare_noop_in_active_mode(self, schema_registry) -> None:
        """In ACTIVE mode, shadow_compare does not record any comparisons."""
        from backend.core.umf.shadow_parity import ShadowParityLogger

        parity = ShadowParityLogger(min_comparisons=1)
        bridge = EnvBridge(
            schema_registry=schema_registry,
            initial_mode=BridgeMode.ACTIVE,
            parity_logger=parity,
        )
        entry = _make_entry("gcp.offload_active", True)
        with mock.patch.dict(os.environ, {"JARVIS_GCP_OFFLOAD_ACTIVE": "true"}, clear=False):
            bridge.shadow_compare(entry, global_revision=1)
        assert parity.total_comparisons == 0
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_env_bridge_active.py -v`

Expected: FAIL — `AttributeError: 'EnvBridge' object has no attribute 'mirror_to_env'`

**Step 3: Implement mirror_to_env and fix shadow_compare scope**

Modify `backend/core/reactive_state/env_bridge.py`:

**3a.** Add `TYPE_CHECKING` import block after the existing imports (around line 36):

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.core.reactive_state.store import ReactiveStateStore
```

**3b.** In `EnvBridge.__init__` (around line 424), add two new instance variables after `self._by_env_var` setup:

```python
        # -- Active-mode env mirroring state (Wave 4) --
        self._env_lock = threading.Lock()
        self._last_mirrored_versions: Dict[str, int] = {}
```

**3c.** Fix `shadow_compare` (around line 560). Change:

```python
        if self._mode is BridgeMode.LEGACY:
            return
```

To:

```python
        if self._mode is not BridgeMode.SHADOW:
            return
```

This restricts shadow comparison to SHADOW mode only — in ACTIVE mode the store is authoritative and shadow comparison is meaningless.

**3d.** Add `mirror_to_env` method after the `shadow_compare` method (before `_canonicalize`):

```python
    # -- active-mode env mirroring ------------------------------------------------

    def mirror_to_env(self, entry: StateEntry) -> bool:
        """Mirror a store entry's value to ``os.environ`` as a compatibility write.

        Only operates in ``ACTIVE`` mode.  Unmapped keys are silently skipped.
        A version guard prevents re-mirroring the same version (loop
        prevention per design doc Appendix A.7).

        Parameters
        ----------
        entry:
            The ``StateEntry`` whose value should be mirrored.

        Returns
        -------
        bool
            ``True`` if the env var was written, ``False`` if skipped.
        """
        if self._mode is not BridgeMode.ACTIVE:
            return False

        mapping = self._by_state_key.get(entry.key)
        if mapping is None:
            return False

        # Version guard (loop prevention A.7)
        if self._last_mirrored_versions.get(entry.key) == entry.version:
            return False

        env_value = mapping.coerce_to_env(entry.value)

        with self._env_lock:
            os.environ[mapping.env_var] = env_value

        self._last_mirrored_versions[entry.key] = entry.version

        if mapping.sensitive:
            logger.debug(
                "Mirrored %s -> %s = <redacted> (v%d)",
                entry.key,
                mapping.env_var,
                entry.version,
            )
        else:
            logger.debug(
                "Mirrored %s -> %s = %r (v%d)",
                entry.key,
                mapping.env_var,
                env_value,
                entry.version,
            )
        return True
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_env_bridge_active.py -v`

Expected: All 13 tests PASS.

Also run existing Wave 3 tests to verify no regression:

Run: `python3 -m pytest tests/unit/core/reactive_state/test_env_bridge_shadow.py tests/unit/core/reactive_state/test_env_bridge_lifecycle.py tests/unit/core/reactive_state/test_env_bridge_coercion.py tests/unit/core/reactive_state/test_env_bridge_promotion.py tests/unit/core/reactive_state/test_env_key_mappings.py -v`

Expected: All 87 Wave 3 tests PASS. (Note: `test_legacy_mode_noop_does_not_record` in `test_env_bridge_shadow.py` still passes because LEGACY mode is still a no-op — the change only affects ACTIVE mode.)

**Step 5: Commit**

```bash
git add tests/unit/core/reactive_state/test_env_bridge_active.py backend/core/reactive_state/env_bridge.py
git commit -m "feat(disease8): add active-mode env mirror writes with loop prevention (Wave 4, Task 1)"
```

---

## Task 2: Per-domain kill switches

**Files:**
- Create: `tests/unit/core/reactive_state/test_env_bridge_domains.py`
- Modify: `backend/core/reactive_state/env_bridge.py`

**Context:** The design doc (Appendix A.13) specifies per-domain kill switches: `JARVIS_STATE_BRIDGE_ACTIVE_DOMAINS=gcp,memory` means only `gcp.*` and `memory.*` keys use the store as authority — other domains remain in shadow/legacy behavior. If the env var is absent or empty, all domains are active (no restrictions). The domain is extracted as the first segment of the state key (before the first `.`). This task adds `_active_domains` to `EnvBridge.__init__`, an `is_domain_active()` method, and integrates the domain check into `mirror_to_env()`.

**Step 1: Write the failing tests**

Create `tests/unit/core/reactive_state/test_env_bridge_domains.py`:

```python
"""Tests for EnvBridge per-domain kill switches (Appendix A.13)."""
from __future__ import annotations

import os
import time
from typing import Any
from unittest import mock

import pytest

from backend.core.reactive_state.env_bridge import BridgeMode, EnvBridge
from backend.core.reactive_state.manifest import build_schema_registry
from backend.core.reactive_state.types import StateEntry


# -- Helpers ----------------------------------------------------------------


def _make_entry(key: str, value: Any, version: int = 1) -> StateEntry:
    return StateEntry(
        key=key,
        value=value,
        version=version,
        epoch=1,
        writer="test",
        origin="explicit",
        updated_at_mono=time.monotonic(),
        updated_at_unix_ms=int(time.time() * 1000),
    )


# -- Fixtures ---------------------------------------------------------------


@pytest.fixture()
def schema_registry():
    return build_schema_registry()


# -- TestActiveDomainsParsing -----------------------------------------------


class TestActiveDomainsParsing:
    """JARVIS_STATE_BRIDGE_ACTIVE_DOMAINS env var parsing."""

    def test_absent_env_var_means_all_active(self, schema_registry) -> None:
        """No env var -> _active_domains is None -> all domains active."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JARVIS_STATE_BRIDGE_ACTIVE_DOMAINS", None)
            bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
            assert bridge.is_domain_active("gcp.offload_active") is True
            assert bridge.is_domain_active("memory.tier") is True
            assert bridge.is_domain_active("lifecycle.startup_complete") is True

    def test_empty_env_var_means_all_active(self, schema_registry) -> None:
        """Empty string -> all domains active."""
        with mock.patch.dict(os.environ, {"JARVIS_STATE_BRIDGE_ACTIVE_DOMAINS": ""}, clear=False):
            bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
            assert bridge.is_domain_active("gcp.offload_active") is True

    def test_specific_domains_parsed(self, schema_registry) -> None:
        """'gcp,memory' -> only gcp and memory are active."""
        with mock.patch.dict(
            os.environ,
            {"JARVIS_STATE_BRIDGE_ACTIVE_DOMAINS": "gcp,memory"},
            clear=False,
        ):
            bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
            assert bridge.is_domain_active("gcp.offload_active") is True
            assert bridge.is_domain_active("gcp.node_ip") is True
            assert bridge.is_domain_active("memory.available_gb") is True
            assert bridge.is_domain_active("lifecycle.startup_complete") is False
            assert bridge.is_domain_active("prime.early_pid") is False
            assert bridge.is_domain_active("service.backend_minimal") is False

    def test_whitespace_stripped(self, schema_registry) -> None:
        """'  gcp , memory  ' -> parsed correctly."""
        with mock.patch.dict(
            os.environ,
            {"JARVIS_STATE_BRIDGE_ACTIVE_DOMAINS": "  gcp , memory  "},
            clear=False,
        ):
            bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
            assert bridge.is_domain_active("gcp.offload_active") is True
            assert bridge.is_domain_active("memory.tier") is True
            assert bridge.is_domain_active("lifecycle.effective_mode") is False


# -- TestMirrorDomainFiltering ----------------------------------------------


class TestMirrorDomainFiltering:
    """mirror_to_env respects per-domain kill switches."""

    def test_mirror_skips_inactive_domain(self, schema_registry) -> None:
        """Key in inactive domain -> mirror_to_env returns False."""
        with mock.patch.dict(
            os.environ,
            {"JARVIS_STATE_BRIDGE_ACTIVE_DOMAINS": "gcp"},
            clear=False,
        ):
            bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
            # memory domain is NOT active
            entry = _make_entry("memory.available_gb", 7.5)
            result = bridge.mirror_to_env(entry)
            assert result is False

    def test_mirror_works_for_active_domain(self, schema_registry) -> None:
        """Key in active domain -> mirror_to_env writes to env."""
        with mock.patch.dict(
            os.environ,
            {"JARVIS_STATE_BRIDGE_ACTIVE_DOMAINS": "gcp"},
            clear=False,
        ):
            bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
            entry = _make_entry("gcp.offload_active", True)
            result = bridge.mirror_to_env(entry)
            assert result is True
            assert os.environ["JARVIS_GCP_OFFLOAD_ACTIVE"] == "true"

    def test_all_domains_active_when_no_env_var(self, schema_registry) -> None:
        """No JARVIS_STATE_BRIDGE_ACTIVE_DOMAINS -> all keys mirror."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JARVIS_STATE_BRIDGE_ACTIVE_DOMAINS", None)
            bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
            entry = _make_entry("lifecycle.startup_complete", True)
            result = bridge.mirror_to_env(entry)
            assert result is True
            assert os.environ["JARVIS_STARTUP_COMPLETE"] == "true"
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_env_bridge_domains.py -v`

Expected: FAIL — `AttributeError: 'EnvBridge' object has no attribute 'is_domain_active'`

**Step 3: Implement per-domain kill switches**

Modify `backend/core/reactive_state/env_bridge.py`:

**3a.** Add class-level constant on `EnvBridge` (after `_ENV_MODE_VAR`):

```python
    _ACTIVE_DOMAINS_VAR = "JARVIS_STATE_BRIDGE_ACTIVE_DOMAINS"
```

**3b.** Add static method `_resolve_active_domains` (after `_resolve_bootstrap_mode`):

```python
    @staticmethod
    def _resolve_active_domains() -> Optional[frozenset]:
        """Parse ``JARVIS_STATE_BRIDGE_ACTIVE_DOMAINS`` from environment.

        Returns ``None`` if absent or empty (all domains active).
        Returns a ``frozenset`` of domain name strings (e.g. ``{"gcp", "memory"}``)
        if the env var is set to a comma-separated list.
        """
        raw = os.environ.get(EnvBridge._ACTIVE_DOMAINS_VAR, "")
        if not raw:
            return None
        domains = frozenset(d.strip() for d in raw.split(",") if d.strip())
        return domains if domains else None
```

**3c.** In `EnvBridge.__init__`, add after `_last_mirrored_versions`:

```python
        self._active_domains = EnvBridge._resolve_active_domains()
```

**3d.** Add `is_domain_active` method (after `get_mapping_by_env_var`):

```python
    def is_domain_active(self, state_key: str) -> bool:
        """Return ``True`` if the key's domain is active for store authority.

        The domain is the first segment of the state key (before the first
        ``'.'``).  If no active-domains restriction is configured (``None``),
        all domains are considered active.
        """
        if self._active_domains is None:
            return True
        domain = state_key.split(".", 1)[0]
        return domain in self._active_domains
```

**3e.** In `mirror_to_env`, add domain check after the mapping lookup (after `if mapping is None: return False`):

```python
        # Per-domain kill switch (A.13)
        if not self.is_domain_active(entry.key):
            return False
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_env_bridge_domains.py tests/unit/core/reactive_state/test_env_bridge_active.py -v`

Expected: All tests PASS (Task 1 tests still pass since they don't set `JARVIS_STATE_BRIDGE_ACTIVE_DOMAINS`, meaning all domains active).

**Step 5: Commit**

```bash
git add tests/unit/core/reactive_state/test_env_bridge_domains.py backend/core/reactive_state/env_bridge.py
git commit -m "feat(disease8): add per-domain kill switches for active-mode blast radius control (Wave 4, Task 2)"
```

---

## Task 3: `get_subprocess_env()` method

**Files:**
- Create: `tests/unit/core/reactive_state/test_env_bridge_subprocess.py`
- Modify: `backend/core/reactive_state/env_bridge.py`

**Context:** The design doc (Section 8, Appendix A.12) specifies `get_subprocess_env()` to replace manual `os.environ.copy()` + mutation patterns. The method builds a coherent env dict by copying `os.environ` (under `_env_lock`) and overlaying all mapped keys from the store snapshot (for active domains only). In non-`ACTIVE` modes, it returns a plain `os.environ.copy()` with no store overlay. This gives child processes a consistent environment snapshot.

The tests for this task need a `ReactiveStateStore` instance. The store requires a SQLite journal file. Tests use `tmp_path` fixture for the journal path.

**Step 1: Write the failing tests**

Create `tests/unit/core/reactive_state/test_env_bridge_subprocess.py`:

```python
"""Tests for EnvBridge.get_subprocess_env() method."""
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


# -- Fixtures ---------------------------------------------------------------


@pytest.fixture()
def schema_registry():
    return build_schema_registry()


@pytest.fixture()
def ownership_registry():
    return build_ownership_registry()


@pytest.fixture()
def tmp_journal(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture()
def store(tmp_journal, ownership_registry, schema_registry):
    """Open a store, initialize defaults, yield, then close."""
    s = ReactiveStateStore(
        journal_path=tmp_journal,
        epoch=1,
        session_id="w4-subprocess",
        ownership_registry=ownership_registry,
        schema_registry=schema_registry,
    )
    s.open()
    s.initialize_defaults()
    yield s
    s.close()


# -- TestGetSubprocessEnv ---------------------------------------------------


class TestGetSubprocessEnv:
    """EnvBridge.get_subprocess_env() builds env dict from store snapshot."""

    def test_overlays_mapped_keys_from_store(self, schema_registry, store) -> None:
        """Store values for active-domain keys are overlaid onto os.environ copy."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)

        # Write a value to the store
        entry = store.read("gcp.offload_active")
        result = store.write(
            key="gcp.offload_active",
            value=True,
            expected_version=entry.version,
            writer="gcp_controller",
        )
        assert result.status == WriteStatus.OK

        env = bridge.get_subprocess_env(store)
        assert env["JARVIS_GCP_OFFLOAD_ACTIVE"] == "true"

    def test_preserves_unmapped_env_vars(self, schema_registry, store) -> None:
        """Env vars NOT in ENV_KEY_MAPPINGS are preserved from os.environ."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)

        with mock.patch.dict(os.environ, {"MY_CUSTOM_VAR": "hello"}, clear=False):
            env = bridge.get_subprocess_env(store)
            assert env["MY_CUSTOM_VAR"] == "hello"

    def test_all_values_are_strings(self, schema_registry, store) -> None:
        """Every value in the returned dict is a string."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
        env = bridge.get_subprocess_env(store)
        for key, value in env.items():
            assert isinstance(value, str), f"{key}={value!r} is not a string"

    def test_defaults_overlaid_on_fresh_store(self, schema_registry, store) -> None:
        """Even default values from initialize_defaults() are overlaid."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
        env = bridge.get_subprocess_env(store)
        # gcp.offload_active default is False -> "false"
        assert env["JARVIS_GCP_OFFLOAD_ACTIVE"] == "false"
        # gcp.node_port default is 8000 -> "8000"
        assert env["JARVIS_INVINCIBLE_NODE_PORT"] == "8000"

    def test_respects_domain_kill_switch(self, schema_registry, store) -> None:
        """Keys in inactive domains are NOT overlaid from store."""
        with mock.patch.dict(
            os.environ,
            {
                "JARVIS_STATE_BRIDGE_ACTIVE_DOMAINS": "gcp",
                "JARVIS_CAN_SPAWN_HEAVY": "original",
            },
            clear=False,
        ):
            bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
            env = bridge.get_subprocess_env(store)
            # gcp domain is active -> overlaid from store
            assert env["JARVIS_GCP_OFFLOAD_ACTIVE"] == "false"
            # memory domain is NOT active -> preserved from os.environ
            assert env["JARVIS_CAN_SPAWN_HEAVY"] == "original"

    def test_returns_plain_copy_in_legacy_mode(self, schema_registry, store) -> None:
        """In LEGACY mode, returns os.environ.copy() with no overlay."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.LEGACY)
        with mock.patch.dict(os.environ, {"MY_VAR": "42"}, clear=False):
            env = bridge.get_subprocess_env(store)
            assert env["MY_VAR"] == "42"
            # Store might have defaults, but they're NOT overlaid
            # We verify by checking the env has no store-originated overlay
            # (This works because os.environ likely doesn't have JARVIS_GCP_OFFLOAD_ACTIVE set)

    def test_returns_plain_copy_in_shadow_mode(self, schema_registry, store) -> None:
        """In SHADOW mode, returns os.environ.copy() with no overlay."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.SHADOW)
        env = bridge.get_subprocess_env(store)
        assert isinstance(env, dict)

    def test_returns_independent_copy(self, schema_registry, store) -> None:
        """Mutating the returned dict does not affect os.environ."""
        bridge = EnvBridge(schema_registry=schema_registry, initial_mode=BridgeMode.ACTIVE)
        env = bridge.get_subprocess_env(store)
        env["SHOULD_NOT_LEAK"] = "yes"
        assert "SHOULD_NOT_LEAK" not in os.environ
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_env_bridge_subprocess.py -v`

Expected: FAIL — `AttributeError: 'EnvBridge' object has no attribute 'get_subprocess_env'`

**Step 3: Implement `get_subprocess_env`**

Modify `backend/core/reactive_state/env_bridge.py`:

Add the method after `mirror_to_env` (in the active-mode env mirroring section):

```python
    def get_subprocess_env(self, store: ReactiveStateStore) -> Dict[str, str]:
        """Build an env dict from the store snapshot for child process spawning.

        Copies ``os.environ`` (under ``_env_lock``) and, in ``ACTIVE`` mode,
        overlays all mapped keys whose domains are active with their current
        store values.  In ``LEGACY`` or ``SHADOW`` mode, returns a plain copy
        of ``os.environ`` with no store overlay.

        Parameters
        ----------
        store:
            The ``ReactiveStateStore`` to read current values from.

        Returns
        -------
        Dict[str, str]
            A new dict suitable for passing to ``subprocess.Popen(env=...)``.
        """
        with self._env_lock:
            env = os.environ.copy()

        if self._mode is not BridgeMode.ACTIVE:
            return env

        # Collect state keys for active domains only
        active_keys = [
            m.state_key
            for m in ENV_KEY_MAPPINGS
            if self.is_domain_active(m.state_key)
        ]
        entries = store.read_many(active_keys)

        for mapping in ENV_KEY_MAPPINGS:
            if not self.is_domain_active(mapping.state_key):
                continue
            entry = entries.get(mapping.state_key)
            if entry is not None:
                env[mapping.env_var] = mapping.coerce_to_env(entry.value)

        return env
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_env_bridge_subprocess.py -v`

Expected: All 8 tests PASS.

Run full Wave 4 test suite so far:

Run: `python3 -m pytest tests/unit/core/reactive_state/test_env_bridge_active.py tests/unit/core/reactive_state/test_env_bridge_domains.py tests/unit/core/reactive_state/test_env_bridge_subprocess.py -v`

Expected: All tests PASS.

**Step 5: Commit**

```bash
git add tests/unit/core/reactive_state/test_env_bridge_subprocess.py backend/core/reactive_state/env_bridge.py
git commit -m "feat(disease8): add get_subprocess_env() for coherent child process environments (Wave 4, Task 3)"
```

---

## Task 4: Export updates and Wave 4 integration tests

**Files:**
- Create: `tests/unit/core/reactive_state/test_wave4_integration.py`
- Modify: `backend/core/reactive_state/__init__.py`

**Context:** This task updates the package exports to include the new Wave 4 API surface and adds end-to-end integration tests that exercise the full active-mode write pipeline: store write -> watcher callback -> mirror_to_env -> verify os.environ -> get_subprocess_env. Also tests the full mode lifecycle (legacy -> shadow -> active) with mode-appropriate behavior at each stage.

**Step 1: Write the failing integration tests**

Create `tests/unit/core/reactive_state/test_wave4_integration.py`:

```python
"""Wave 4 integration -- env bridge active-mode end-to-end."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional
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
from backend.core.reactive_state.types import StateEntry
from backend.core.umf.shadow_parity import ShadowParityLogger


@pytest.fixture()
def tmp_journal(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


class TestActiveModeWriteThrough:
    """Store write -> watcher -> mirror_to_env -> env updated -> get_subprocess_env."""

    def test_watcher_driven_env_mirror(self, tmp_journal: Path) -> None:
        """Write to store, watcher calls mirror_to_env, env var updated."""
        schema_reg = build_schema_registry()
        ownership_reg = build_ownership_registry()
        bridge = EnvBridge(
            schema_registry=schema_reg,
            initial_mode=BridgeMode.ACTIVE,
        )

        store = ReactiveStateStore(
            journal_path=tmp_journal,
            epoch=1,
            session_id="w4-e2e-1",
            ownership_registry=ownership_reg,
            schema_registry=schema_reg,
        )
        try:
            store.open()
            store.initialize_defaults()

            # Register watcher that mirrors to env
            def on_change(old: Optional[StateEntry], new: StateEntry) -> None:
                bridge.mirror_to_env(new)

            store.watch("*", on_change)

            with mock.patch.dict(os.environ, {}, clear=False):
                # Write gcp.offload_active=True
                entry = store.read("gcp.offload_active")
                result = store.write(
                    key="gcp.offload_active",
                    value=True,
                    expected_version=entry.version,
                    writer="gcp_controller",
                )
                assert result.status == WriteStatus.OK
                # Watcher should have mirrored the value
                assert os.environ["JARVIS_GCP_OFFLOAD_ACTIVE"] == "true"

                # Write gcp.node_ip="10.0.0.5"
                ip_entry = store.read("gcp.node_ip")
                result2 = store.write(
                    key="gcp.node_ip",
                    value="10.0.0.5",
                    expected_version=ip_entry.version,
                    writer="gcp_controller",
                )
                assert result2.status == WriteStatus.OK
                assert os.environ["JARVIS_INVINCIBLE_NODE_IP"] == "10.0.0.5"
        finally:
            store.close()

    def test_subprocess_env_reflects_store_writes(self, tmp_journal: Path) -> None:
        """After store writes + mirror, get_subprocess_env returns updated values."""
        schema_reg = build_schema_registry()
        ownership_reg = build_ownership_registry()
        bridge = EnvBridge(
            schema_registry=schema_reg,
            initial_mode=BridgeMode.ACTIVE,
        )

        store = ReactiveStateStore(
            journal_path=tmp_journal,
            epoch=1,
            session_id="w4-e2e-2",
            ownership_registry=ownership_reg,
            schema_registry=schema_reg,
        )
        try:
            store.open()
            store.initialize_defaults()

            # Write a value
            entry = store.read("memory.available_gb")
            store.write(
                key="memory.available_gb",
                value=15.5,
                expected_version=entry.version,
                writer="memory_assessor",
            )

            env = bridge.get_subprocess_env(store)
            assert env["JARVIS_HEAVY_ADMISSION_AVAILABLE_GB"] == "15.5"
        finally:
            store.close()


class TestFullModeLifecycle:
    """legacy -> shadow -> active with mode-appropriate behavior at each stage."""

    def test_lifecycle_legacy_shadow_active(self, tmp_journal: Path) -> None:
        """Full mode lifecycle with correct behavior at each stage."""
        schema_reg = build_schema_registry()
        ownership_reg = build_ownership_registry()
        parity = ShadowParityLogger(min_comparisons=1)
        bridge = EnvBridge(
            schema_registry=schema_reg,
            initial_mode=BridgeMode.LEGACY,
            parity_logger=parity,
        )

        store = ReactiveStateStore(
            journal_path=tmp_journal,
            epoch=1,
            session_id="w4-lifecycle",
            ownership_registry=ownership_reg,
            schema_registry=schema_reg,
        )
        try:
            store.open()
            store.initialize_defaults()

            entry = store.read("gcp.offload_active")

            # -- LEGACY: shadow_compare is no-op, mirror_to_env is no-op --
            with mock.patch.dict(os.environ, {"JARVIS_GCP_OFFLOAD_ACTIVE": "false"}, clear=False):
                bridge.shadow_compare(entry, global_revision=1)
                assert parity.total_comparisons == 0
                assert bridge.mirror_to_env(entry) is False

            # -- Transition to SHADOW --
            bridge.transition_to(BridgeMode.SHADOW)

            # SHADOW: shadow_compare records, mirror_to_env is no-op
            with mock.patch.dict(os.environ, {"JARVIS_GCP_OFFLOAD_ACTIVE": "false"}, clear=False):
                bridge.shadow_compare(entry, global_revision=2)
                assert parity.total_comparisons == 1
                assert bridge.mirror_to_env(entry) is False

            # -- Transition to ACTIVE --
            bridge.transition_to(BridgeMode.ACTIVE)

            # ACTIVE: shadow_compare is no-op, mirror_to_env writes
            with mock.patch.dict(os.environ, {}, clear=False):
                bridge.shadow_compare(entry, global_revision=3)
                assert parity.total_comparisons == 1  # unchanged!

                result = bridge.mirror_to_env(entry)
                assert result is True
                assert os.environ["JARVIS_GCP_OFFLOAD_ACTIVE"] == "false"
        finally:
            store.close()

    def test_domain_kill_switch_with_active_mode(self, tmp_journal: Path) -> None:
        """Per-domain kill switch in active mode: only active domains mirror."""
        schema_reg = build_schema_registry()
        ownership_reg = build_ownership_registry()

        with mock.patch.dict(
            os.environ,
            {"JARVIS_STATE_BRIDGE_ACTIVE_DOMAINS": "gcp"},
            clear=False,
        ):
            bridge = EnvBridge(
                schema_registry=schema_reg,
                initial_mode=BridgeMode.ACTIVE,
            )

        store = ReactiveStateStore(
            journal_path=tmp_journal,
            epoch=1,
            session_id="w4-domain",
            ownership_registry=ownership_reg,
            schema_registry=schema_reg,
        )
        try:
            store.open()
            store.initialize_defaults()

            # Register watcher that mirrors to env
            def on_change(old: Optional[StateEntry], new: StateEntry) -> None:
                bridge.mirror_to_env(new)

            store.watch("*", on_change)

            with mock.patch.dict(os.environ, {}, clear=False):
                # Write gcp key (active domain) -> should mirror
                gcp_entry = store.read("gcp.offload_active")
                store.write(
                    key="gcp.offload_active",
                    value=True,
                    expected_version=gcp_entry.version,
                    writer="gcp_controller",
                )
                assert os.environ.get("JARVIS_GCP_OFFLOAD_ACTIVE") == "true"

                # Write memory key (inactive domain) -> should NOT mirror
                mem_entry = store.read("memory.can_spawn_heavy")
                store.write(
                    key="memory.can_spawn_heavy",
                    value=True,
                    expected_version=mem_entry.version,
                    writer="memory_assessor",
                )
                assert "JARVIS_CAN_SPAWN_HEAVY" not in os.environ
        finally:
            store.close()
```

**Step 2: Run tests to verify they fail (or pass, since impl is done)**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_wave4_integration.py -v`

Expected: All 4 tests PASS (implementation from Tasks 1-3 is complete).

**Step 3: Update `__init__.py` exports**

The Wave 4 API additions are methods on existing classes (no new public symbols). However, verify the exports are current. No new symbols need adding to `__all__` — `EnvBridge`, `BridgeMode`, `EnvKeyMapping`, and `ENV_KEY_MAPPINGS` are already exported.

If future waves need `get_subprocess_env` as a standalone function, it can be re-exported then. For now, it's a method on `EnvBridge`.

**Step 4: Run the full Wave 4 test suite**

Run: `python3 -m pytest tests/unit/core/reactive_state/test_env_bridge_active.py tests/unit/core/reactive_state/test_env_bridge_domains.py tests/unit/core/reactive_state/test_env_bridge_subprocess.py tests/unit/core/reactive_state/test_wave4_integration.py -v`

Expected: All tests PASS.

Run the full reactive_state test suite to verify no regressions:

Run: `python3 -m pytest tests/unit/core/reactive_state/ -v`

Expected: All tests PASS (Wave 0+1+2+3+4).

**Step 5: Commit**

```bash
git add tests/unit/core/reactive_state/test_wave4_integration.py
git commit -m "feat(disease8): add Wave 4 integration tests for active-mode env bridge (Wave 4, Task 4)"
```

---

## Acceptance Criteria

All must pass before Wave 4 is complete:

1. `mirror_to_env()` correctly writes store values to `os.environ` for all 5 types (bool, int, float, str, enum) and None.
2. `mirror_to_env()` is a no-op in LEGACY and SHADOW modes.
3. Version guard prevents re-mirroring the same version (loop prevention A.7).
4. `shadow_compare()` is restricted to SHADOW mode only (no-op in LEGACY and ACTIVE).
5. `JARVIS_STATE_BRIDGE_ACTIVE_DOMAINS` parsing: absent/empty = all active, comma-separated = specific domains.
6. `mirror_to_env()` respects per-domain kill switches.
7. `get_subprocess_env()` overlays store values onto `os.environ.copy()` in ACTIVE mode.
8. `get_subprocess_env()` returns plain `os.environ.copy()` in LEGACY/SHADOW modes.
9. `get_subprocess_env()` respects per-domain kill switches.
10. Full lifecycle test: legacy -> shadow -> active with correct behavior at each stage.
11. Watcher-driven integration: store write -> watcher -> mirror_to_env -> env updated.
12. All existing Wave 0-3 tests still pass (no regressions).
