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
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

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
        env_var="JARVIS_STARTUP_EFFECTIVE_MODE",
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
        env_var="JARVIS_CAN_SPAWN_HEAVY",
        state_key="memory.can_spawn_heavy",
        coerce_to_env=_bool_to_env,
        coerce_from_env=_bool_from_env,
    ),
    EnvKeyMapping(
        env_var="JARVIS_HEAVY_ADMISSION_AVAILABLE_GB",
        state_key="memory.available_gb",
        coerce_to_env=_float_to_env,
        coerce_from_env=_float_from_env,
    ),
    EnvKeyMapping(
        env_var="JARVIS_HEAVY_ADMISSION_REASON",
        state_key="memory.admission_reason",
        coerce_to_env=_str_to_env,
        coerce_from_env=_str_from_env,
    ),
    EnvKeyMapping(
        env_var="JARVIS_MEASURED_MEMORY_TIER",
        state_key="memory.tier",
        coerce_to_env=_enum_to_env,
        coerce_from_env=_enum_from_env,
    ),
    EnvKeyMapping(
        env_var="JARVIS_STARTUP_MODE",
        state_key="memory.startup_mode",
        coerce_to_env=_enum_to_env,
        coerce_from_env=_enum_from_env,
    ),
    EnvKeyMapping(
        env_var="JARVIS_MEASURED_MEMORY_SOURCE",
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
        env_var="JARVIS_INVINCIBLE_NODE_IP",
        state_key="gcp.node_ip",
        coerce_to_env=_str_to_env,
        coerce_from_env=_str_from_env,
    ),
    EnvKeyMapping(
        env_var="JARVIS_INVINCIBLE_NODE_PORT",
        state_key="gcp.node_port",
        coerce_to_env=_int_to_env,
        coerce_from_env=_int_from_env,
    ),
    EnvKeyMapping(
        env_var="JARVIS_INVINCIBLE_NODE_BOOTING",
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
        env_var="JARVIS_BACKEND_MINIMAL",
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
    _ACTIVE_DOMAINS_VAR = "JARVIS_STATE_BRIDGE_ACTIVE_DOMAINS"

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

        # -- Active-mode env mirroring state (Wave 4) --
        self._env_lock = threading.Lock()
        self._last_mirrored_versions: Dict[str, int] = {}
        self._active_domains = EnvBridge._resolve_active_domains()

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

    # -- domain filtering (A.13) -----------------------------------------------

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

    # -- promotion readiness ---------------------------------------------------

    def is_promotion_ready(self) -> bool:
        """Return True if parity exceeds threshold with sufficient data.

        Delegates entirely to the underlying ``ShadowParityLogger``.
        """
        return self._parity_logger.is_promotion_ready()

    def parity_stats(self) -> Dict[str, Any]:
        """Return aggregated parity statistics from the shadow logger.

        Returns
        -------
        dict
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

    # -- shadow comparison -----------------------------------------------------

    def shadow_compare(self, entry: StateEntry, global_revision: int) -> None:
        """Compare *entry* against the corresponding env var, recording parity.

        In ``LEGACY`` mode this is a no-op.  For unmapped keys (not in the
        ``ENV_KEY_MAPPINGS`` table) the call is silently ignored.

        For mapped keys the comparison works as follows:

        1. Canonicalize the store value via ``_canonicalize(key, entry.value)``.
        2. Read the env var.  If absent, use the **schema default** as the
           canonical env value (Appendix A.5).  If present, coerce through
           ``mapping.coerce_from_env`` then canonicalize.
        3. Compare via ``_values_equal``.
        4. Record via ``ShadowParityLogger.record()``.  For matching values
           both ``legacy_result`` and ``umf_result`` are the same string so
           that ``_total`` increments but ``_mismatches_count`` does not.
        5. If ``mapping.sensitive``, use ``"<redacted>"`` for both strings in
           the record (and in any warning log).
        """
        if self._mode is not BridgeMode.SHADOW:
            return

        mapping = self._by_state_key.get(entry.key)
        if mapping is None:
            return

        # -- canonicalize store value --
        store_canonical = self._canonicalize(entry.key, entry.value)

        # -- canonicalize env value --
        env_raw = os.environ.get(mapping.env_var)
        if env_raw is None:
            # Absent env var -> use schema default
            schema = self._schema_registry.get(entry.key)
            if schema is not None:
                env_canonical = self._canonicalize(entry.key, schema.default)
            else:
                env_canonical = None
        else:
            env_canonical = self._canonicalize(
                entry.key, mapping.coerce_from_env(env_raw),
            )

        # -- compare --
        match = self._values_equal(store_canonical, env_canonical)

        # -- build record strings --
        if mapping.sensitive:
            store_str = "<redacted>"
            env_str = "<redacted>"
        else:
            store_str = str(store_canonical)
            env_str = str(env_canonical)

        # For matching comparisons, pass identical strings so _total
        # increments but _mismatches_count does not.
        if match:
            self._parity_logger.record(
                trace_id=f"shadow.{global_revision}",
                category=entry.key,
                legacy_result=env_str,
                umf_result=env_str,
            )
        else:
            self._parity_logger.record(
                trace_id=f"shadow.{global_revision}",
                category=entry.key,
                legacy_result=env_str,
                umf_result=store_str,
            )
            # Extra warning with key context (redacted if sensitive)
            if mapping.sensitive:
                logger.warning(
                    "Shadow parity mismatch key=%s store=<redacted> env=<redacted> rev=%d",
                    entry.key,
                    global_revision,
                )
            else:
                logger.warning(
                    "Shadow parity mismatch key=%s store=%s env=%s rev=%d",
                    entry.key,
                    store_canonical,
                    env_canonical,
                    global_revision,
                )

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

        # Per-domain kill switch (A.13)
        if not self.is_domain_active(entry.key):
            return False

        env_value = mapping.coerce_to_env(entry.value)

        with self._env_lock:
            # Version guard (loop prevention A.7) — inside lock to avoid TOCTOU
            if self._last_mirrored_versions.get(entry.key) == entry.version:
                return False
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

    # -- canonicalization helpers -----------------------------------------------

    _CANONICAL_DISPATCH: Dict[str, Callable[[Any], Any]] = {
        "bool": canonical_bool,
        "int": canonical_int,
        "float": canonical_float,
        "str": canonical_str,
        "enum": canonical_enum,
    }

    def _canonicalize(self, key: str, value: Any) -> Any:
        """Canonicalize *value* according to the schema for *key*.

        Dispatches to the appropriate ``canonical_*`` function based on the
        schema's ``value_type``.  If no schema is registered, returns *value*
        unchanged.
        """
        schema = self._schema_registry.get(key)
        if schema is None:
            return value
        coercer = self._CANONICAL_DISPATCH.get(schema.value_type)
        if coercer is None:
            return value
        return coercer(value)

    @staticmethod
    def _values_equal(a: Any, b: Any) -> bool:
        """Compare two canonicalized values for equality.

        For two floats, uses a tolerance of ``1e-9``.  Otherwise uses ``==``.
        """
        if isinstance(a, float) and isinstance(b, float):
            return abs(a - b) < 1e-9
        return a == b
