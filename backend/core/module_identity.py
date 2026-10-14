"""
module_identity - one module object under both of its import names.

``backend/`` is importable two ways: as the ``backend`` package and, with
``backend/`` itself on sys.path, top-level (``neural_mesh.x`` next to
``backend.neural_mesh.x``). Python then loads the file twice, and every
module-level singleton exists twice: the kernel's mesh coordinator was a
different object from AGI OS's, and an environment-variable guard meant to
paper over it returned the second, never-initialized one ("Coordinator not
initialized"). A module calls :func:`unify` with its ``__name__`` first
thing, and whichever name loads it, the other name resolves to the same
object.
"""
from __future__ import annotations

import importlib
import sys

_PREFIX = "backend."


def unify(name: str) -> None:
    """Register the module loaded as *name* under its other import name too."""
    module = sys.modules.get(name)
    if module is None:
        return
    other = name[len(_PREFIX):] if name.startswith(_PREFIX) else _PREFIX + name
    sys.modules.setdefault(other, module)
    # A loaded parent package also exposes it as an attribute, so the
    # `import a.b.c` form resolves it too. That form reaches the module
    # THROUGH its parent, and a name answered from sys.modules never loads
    # the parent itself: `import voice.speaker_verification_service as sv`
    # raised "cannot import name 'voice'" when only `backend.voice` had been
    # loaded. So an unloaded parent package is loaded here (they are lazy).
    parent, _, child = other.rpartition(".")
    if parent and parent not in sys.modules:
        try:
            importlib.import_module(parent)
        except Exception:  # noqa: BLE001 -- the alias still serves `from a.b.c import x`
            return
    if parent in sys.modules and not hasattr(sys.modules[parent], child):
        setattr(sys.modules[parent], child, sys.modules[other])


__all__ = ["unify"]
