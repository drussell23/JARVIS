"""Reflection-based boot-phase labels — no dict rot, no raw symbols.

Design language §3 ("never show a field name"): the wake checklist was
rendering internal timer marks (``boot_governed_loop_service``) verbatim
on the product surface. A static translation dictionary inside the UI
loop would rot the moment a boot phase is renamed; the root mechanism is
REFLECTION with an algorithmic floor:

  1. **``@ui_label("…")``** — boot-phase methods declare their own
     presentation token as ``__ui_label__``; :func:`resolve_label` finds
     the attribute by walking the harness class for a callable named
     exactly like the mark. Rename the method → the lookup follows for
     free; delete it → the humanizer floor takes over. Presentation
     stays declared AT the lifecycle site, yet the layers remain
     decoupled (the UI only ever reads an attribute).
  2. **Humanizer floor** — marks without a method (sub-marks like
     ``oracle_load_cache``) degrade to an algorithmic prettification:
     structural prefix tokens drop, snake_case becomes spaces. Always
     produces SOMETHING human; never raises.

Pure presentation module: reads attributes, never calls anything.
"""
from __future__ import annotations

import logging
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

#: Structural tokens the humanizer floor drops from the FRONT of a mark
#: (they describe plumbing, not meaning). Order matters — longest first.
_STRUCTURAL_PREFIXES = ("harness_run_", "boot_")


def ui_label(label: str) -> Callable:
    """Declare a boot phase's presentation token at its lifecycle site.

    Usage::

        @ui_label("governed loop")
        async def boot_governed_loop_service(self) -> None: ...
    """
    def _decorate(fn: Callable) -> Callable:
        try:
            fn.__ui_label__ = str(label)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        return fn
    return _decorate


def humanize_mark(name: str) -> str:
    """Algorithmic floor: ``boot_governed_loop_service`` →
    ``governed loop service``. Pure; NEVER raises."""
    try:
        n = str(name or "").strip()
        for prefix in _STRUCTURAL_PREFIXES:
            if n.startswith(prefix):
                n = n[len(prefix):]
                break
        n = n.replace("_", " ").strip()
        return n or str(name)
    except Exception:  # noqa: BLE001
        return str(name)


_CACHE: Dict[str, str] = {}


def _reflect_label(mark: str) -> Optional[str]:
    """Walk the harness class for a callable named exactly like *mark*
    and read its ``__ui_label__``. Lazy import — the UI layer never
    holds a lifecycle reference. NEVER raises."""
    try:
        from backend.core.ouroboros.battle_test.harness import (
            BattleTestHarness,
        )
        fn = getattr(BattleTestHarness, mark, None)
        if fn is None:
            return None
        label = getattr(fn, "__ui_label__", None)
        return str(label) if label else None
    except Exception:  # noqa: BLE001
        return None


def resolve_label(mark: str) -> str:
    """The wake checklist's ONE label source: reflection first,
    humanizer floor always. Cached per mark. NEVER raises."""
    try:
        cached = _CACHE.get(mark)
        if cached is not None:
            return cached
        label = _reflect_label(mark) or humanize_mark(mark)
        _CACHE[mark] = label
        return label
    except Exception:  # noqa: BLE001
        return str(mark)


def reset_label_cache_for_tests() -> None:
    _CACHE.clear()


__all__ = [
    "humanize_mark",
    "reset_label_cache_for_tests",
    "resolve_label",
    "ui_label",
]
