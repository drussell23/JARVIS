"""Environment variable bridge -- shadow-mode migration from os.environ to reactive state.

Provides ``BridgeMode`` (forward-only state machine: legacy -> shadow -> active),
canonical coercion functions for converting env-var strings into typed Python
values, ``EnvKeyMapping`` for per-key migration metadata, and ``EnvBridge``
for transparent read/write routing between ``os.environ`` and the
``ReactiveStateStore``.

Write pipeline (env bridge layer)
----------------------------------
1. Caller reads/writes via ``EnvBridge`` instead of ``os.environ`` directly.
2. In **legacy** mode, all reads/writes pass through to ``os.environ``.
3. In **shadow** mode, reads come from ``os.environ`` but are compared against
   the reactive store.  Writes go to both.  Mismatches are logged via
   ``ShadowParityLogger``.
4. In **active** mode, reads/writes go to the reactive store only.

Design rules
------------
* **No** third-party imports -- stdlib only (plus sibling modules and UMF).
* ``BridgeMode`` is ``class BridgeMode(str, enum.Enum)`` with forward-only
  transitions enforced by ``can_transition_to()``.
* Canonical coercion functions are module-level, stateless, and pure.
"""
from __future__ import annotations

import enum
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from backend.core.reactive_state.schemas import SchemaRegistry
from backend.core.reactive_state.types import StateEntry
from backend.core.umf.shadow_parity import ShadowParityLogger

logger = logging.getLogger(__name__)


# ── BridgeMode enum ──────────────────────────────────────────────────

_MODE_ORDER: Dict[str, int] = {
    "legacy": 0,
    "shadow": 1,
    "active": 2,
}


class BridgeMode(str, enum.Enum):
    """Operating mode for the environment bridge.

    Transitions are forward-only: ``legacy -> shadow -> active``.
    No skipping, no reverse, no self-transitions.
    """

    LEGACY = "legacy"
    SHADOW = "shadow"
    ACTIVE = "active"

    def can_transition_to(self, target: BridgeMode) -> bool:
        """Return True iff *target* is the next valid forward step."""
        current_order = _MODE_ORDER[self.value]
        target_order = _MODE_ORDER[target.value]
        return target_order == current_order + 1


# ── Canonical coercion functions ─────────────────────────────────────

_TRUTHY: frozenset[str] = frozenset({"true", "1", "yes"})
_FALSY: frozenset[str] = frozenset({"false", "0", "no", ""})


def canonical_bool(value: Any) -> Optional[bool]:
    """Coerce an env-style value to ``Optional[bool]``.

    Accepted truthy strings (case-insensitive): ``"true"``, ``"1"``, ``"yes"``.
    Accepted falsy strings (case-insensitive): ``"false"``, ``"0"``, ``"no"``,
    ``""``.  ``None`` passes through.  Native ``bool`` passes through.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    lowered = str(value).lower()
    if lowered in _TRUTHY:
        return True
    if lowered in _FALSY:
        return False
    return None


def canonical_int(value: Any) -> Optional[int]:
    """Coerce an env-style value to ``Optional[int]``.

    Strings are parsed with ``int()``.  Empty string and ``None`` yield
    ``None``.  Non-numeric strings yield ``None``.  Native ``int``
    passes through.
    """
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value == "":
            return None
        try:
            return int(value)
        except (ValueError, TypeError):
            return None
    return None


def canonical_float(value: Any) -> Optional[float]:
    """Coerce an env-style value to ``Optional[float]``.

    Strings are parsed with ``float()``.  Empty string and ``None``
    yield ``None``.  Native ``int`` is coerced to ``float``.  Native
    ``float`` passes through.
    """
    if value is None:
        return None
    if isinstance(value, float):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        if value == "":
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None
    return None


def canonical_str(value: Any) -> Optional[str]:
    """Coerce a value to ``Optional[str]``.

    ``None`` yields ``None``.  Non-string values are coerced via ``str()``.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def canonical_enum(value: Any) -> Optional[str]:
    """Coerce a value to a stripped, case-sensitive ``Optional[str]``.

    Whitespace is stripped from both ends.  Case is preserved.
    ``None`` yields ``None``.
    """
    if value is None:
        return None
    return str(value).strip()


# ── Coerce-to-env helpers (private) ──────────────────────────────────


def _bool_to_env(value: Any) -> str:
    """Convert a store bool to env string: True->"true", False->"false", None->""."""
    if value is None:
        return ""
    return "true" if value else "false"


def _int_to_env(value: Any) -> str:
    """Convert a store int to env string. None->""."""
    if value is None:
        return ""
    return str(int(value))


def _float_to_env(value: Any) -> str:
    """Convert a store float to env string. None->""."""
    if value is None:
        return ""
    return str(float(value))


def _str_to_env(value: Any) -> str:
    """Convert a store value to env string. None->""."""
    if value is None:
        return ""
    return str(value)


def _enum_to_env(value: Any) -> str:
    """Convert an enum-like store value to env string. None->""."""
    if value is None:
        return ""
    return str(value)


# ── Coerce-from-env helpers (private) ────────────────────────────────


def _bool_from_env(value: str) -> bool:
    """Convert an env string to bool, defaulting to False."""
    result = canonical_bool(value)
    if result is None:
        return False
    return result


def _int_from_env(value: str) -> int:
    """Convert an env string to int via canonical_int.

    Returns 0 for empty/invalid strings (non-nullable int keys).
    """
    result = canonical_int(value)
    if result is None:
        return 0
    return result


def _nullable_int_from_env(value: str) -> Optional[int]:
    """Convert an env string to Optional[int] via canonical_int.

    Returns None for empty strings (nullable int keys).
    """
    return canonical_int(value)


def _float_from_env(value: str) -> Optional[float]:
    """Convert an env string to float via canonical_float.

    Returns 0.0 for empty/invalid strings.
    """
    result = canonical_float(value)
    if result is None:
        return 0.0
    return result


def _str_from_env(value: str) -> str:
    """Passthrough -- env strings are already strings."""
    return value


def _enum_from_env(value: str) -> str:
    """Strip whitespace from an env string for enum-like values."""
    return value.strip()


# ── EnvKeyMapping dataclass ──────────────────────────────────────────


@dataclass(frozen=True)
class EnvKeyMapping:
    """Per-key metadata for bridging between ``os.environ`` and the reactive store.

    Attributes
    ----------
    env_var:
        The environment variable name, e.g. ``"JARVIS_GCP_OFFLOAD_ACTIVE"``.
    state_key:
        The reactive store key path, e.g. ``"gcp.offload_active"``.
    coerce_to_env:
        Callable that converts a store value to an env-var string.
    coerce_from_env:
        Callable that converts an env-var string back to a store value.
    sensitive:
        If ``True``, values are redacted in logs.
    """

    env_var: str
    state_key: str
    coerce_to_env: Callable[[Any], str]
    coerce_from_env: Callable[[str], Any]
    sensitive: bool = False


# ── ENV_KEY_MAPPINGS table ───────────────────────────────────────────

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

# ── EnvBridge class ──────────────────────────────────────────────────


class EnvBridge:
    """Transparent bridge between ``os.environ`` and the reactive state store.

    Routes reads and writes according to the current ``BridgeMode``:

    * **legacy** -- all traffic through ``os.environ``.
    * **shadow** -- reads from ``os.environ``, compared against store;
      writes to both; mismatches logged via ``ShadowParityLogger``.
    * **active** -- reads/writes go to the reactive store only.

    Parameters
    ----------
    schema_registry:
        The ``SchemaRegistry`` used for key validation and coercion.
    initial_mode:
        If provided, the bridge starts in this mode directly.
        If ``None``, the mode is resolved from the
        ``JARVIS_STATE_BRIDGE_MODE`` environment variable.
    parity_logger:
        A ``ShadowParityLogger`` for recording shadow-mode mismatches.
        If ``None``, a default instance is created.
    """

    _ENV_MODE_VAR = "JARVIS_STATE_BRIDGE_MODE"

    def __init__(
        self,
        *,
        schema_registry: SchemaRegistry,
        initial_mode: Optional[BridgeMode] = None,
        parity_logger: Optional[ShadowParityLogger] = None,
    ) -> None:
        self._lock = threading.Lock()
        self._schema_registry = schema_registry
        self._parity_logger = parity_logger if parity_logger is not None else ShadowParityLogger()

        if initial_mode is not None:
            self._mode = initial_mode
        else:
            self._mode = EnvBridge._resolve_bootstrap_mode()

        # Build O(1) lookup dicts from the global mapping table.
        self._by_state_key: Dict[str, EnvKeyMapping] = {}
        self._by_env_var: Dict[str, EnvKeyMapping] = {}
        for mapping in ENV_KEY_MAPPINGS:
            self._by_state_key[mapping.state_key] = mapping
            self._by_env_var[mapping.env_var] = mapping

    # -- bootstrap ---------------------------------------------------------

    @staticmethod
    def _resolve_bootstrap_mode() -> BridgeMode:
        """Resolve the initial bridge mode from the environment.

        Reads ``JARVIS_STATE_BRIDGE_MODE``.  Returns the corresponding
        ``BridgeMode`` if valid, otherwise falls back to ``BridgeMode.LEGACY``
        (logging an error for non-empty invalid values).
        """
        raw = os.environ.get(EnvBridge._ENV_MODE_VAR, "")
        if not raw:
            return BridgeMode.LEGACY
        try:
            return BridgeMode(raw)
        except ValueError:
            logger.error(
                "Invalid %s value %r -- falling back to legacy",
                EnvBridge._ENV_MODE_VAR,
                raw,
            )
            return BridgeMode.LEGACY

    # -- properties --------------------------------------------------------

    @property
    def mode(self) -> BridgeMode:
        """The current operating mode of this bridge."""
        return self._mode

    @property
    def parity_logger(self) -> ShadowParityLogger:
        """The shadow-parity logger used by this bridge."""
        return self._parity_logger

    # -- mode transitions --------------------------------------------------

    def transition_to(self, target: BridgeMode) -> None:
        """Transition to *target* mode.

        Only forward single-step transitions are allowed
        (``legacy -> shadow -> active``).

        Raises
        ------
        ValueError
            If the requested transition is not permitted.
        """
        with self._lock:
            if not self._mode.can_transition_to(target):
                raise ValueError(
                    f"Cannot transition from {self._mode.value!r} "
                    f"to {target.value!r} -- only forward single-step "
                    f"transitions are allowed"
                )
            logger.info(
                "EnvBridge mode transition: %s -> %s",
                self._mode.value,
                target.value,
            )
            self._mode = target

    # -- lookups -----------------------------------------------------------

    def get_mapping_by_state_key(self, state_key: str) -> Optional[EnvKeyMapping]:
        """Return the ``EnvKeyMapping`` for *state_key*, or ``None``."""
        return self._by_state_key.get(state_key)

    def get_mapping_by_env_var(self, env_var: str) -> Optional[EnvKeyMapping]:
        """Return the ``EnvKeyMapping`` for *env_var*, or ``None``."""
        return self._by_env_var.get(env_var)
