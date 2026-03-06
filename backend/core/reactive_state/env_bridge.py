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


# ── EnvKeyMapping (Task 2) ───────────────────────────────────────────
# Placeholder section -- will be filled by Wave 3 Task 2.

# ── EnvBridge class (Task 3) ─────────────────────────────────────────
# Placeholder section -- will be filled by Wave 3 Task 3.
