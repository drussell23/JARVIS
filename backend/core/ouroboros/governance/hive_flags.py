"""Flag registration delegate for the Hive emission edge (Hive Step 2).

The FlagRegistry's dynamic discovery walks curated governance packages only —
walking ``backend.api`` (86 modules, FastAPI apps, import side effects) at seed
time would be the wrong blast radius. This 5-line delegate lives in the walked
package and forwards to the ONE source of truth in ``backend.api.hive_emitter``
(lazy import — hive_emitter is stdlib+pydantic-light). NEVER raises.
"""
from __future__ import annotations

from typing import Any


def register_flags(registry: Any) -> int:
    try:
        from backend.api.hive_emitter import register_flags as _rf
        _rf(registry)
        return 5
    except Exception:  # noqa: BLE001
        return 0


__all__ = ["register_flags"]
