"""One env var, one default — enforced, not hoped for.

Two flags were read in two places with two different defaults today, and both
were real defects rather than curiosities:

``JARVIS_PALETTE_HEIGHT`` was 4 in ``palette_render`` and 12 in
``bipartite_layout``, so the operator's menu height depended on which
renderer happened to mount. ``JARVIS_MEMORY_ROUTING_ENABLED`` was OFF in
``module_routing`` and ON in ``memory_surface``, so ``/memory`` reported
"routing: on" for the entire period routing was disabled — the surface built
to tell an operator the truth was the one asserting the falsehood.

A static scan then found **21** such variables. The count is not the point.
The point is that nothing prevented any of them: ``os.environ.get(NAME,
default)`` is a decentralised declaration, every call site is free to invent
its own default, and two of them only disagree where nobody is looking.

Detection is total; the RAISE is a mode
----------------------------------------
There are 21 known collisions in this tree right now. An arbiter that raised
on the default path would make the system unbootable on the first import,
which is not a safety property — it is an outage.

So collisions are ALWAYS recorded and logged, and
``JARVIS_CONFIG_ARBITER_STRICT=1`` turns recording into raising. Strict mode
is what CI and tests run; the runtime default is loud-but-alive with a
deterministic winner. When the 21 are fixed, strict becomes the default and
the mode disappears.

The winner is FIRST-REGISTERED, deliberately. "Last wins" makes behaviour
depend on import order, which changes when an unrelated module adds a lazy
import — a config value that moves for reasons invisible at the call site is
worse than a wrong one.

Static scanning covers what runtime cannot
-------------------------------------------
Runtime arbitration only sees a collision once BOTH call sites have executed.
For a pair on rarely-taken branches that may be never, and adoption across
4,132 flags is not a same-day change.

So :func:`scan_static` finds them by AST instead, with no adoption required
and no code executed — the same technique that found all 21 in about a
second. Runtime arbitration then covers new code as it adopts, and the two
are complementary rather than redundant: static sees every literal default,
runtime sees computed ones a scanner cannot evaluate.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import ast
import inspect
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("Ouroboros.ConfigArbiter")

CONFIG_ARBITER_SCHEMA_VERSION: str = "config_arbiter.1"

__all__ = [
    "CONFIG_ARBITER_SCHEMA_VERSION",
    "ConfigurationCollisionError",
    "Collision",
    "collisions",
    "resolve",
    "resolve_bool",
    "resolve_int",
    "reset_for_tests",
    "scan_static",
    "strict_enabled",
]


class ConfigurationCollisionError(RuntimeError):
    """Two call sites declared different defaults for one env var.

    Carries both declarations so the message names what to reconcile rather
    than only that something is wrong.
    """

    def __init__(self, name: str, existing: Any, existing_by: str,
                 offered: Any, offered_by: str) -> None:
        super().__init__(
            f"config collision on {name!r}: {existing_by} declares default "
            f"{existing!r}, {offered_by} declares {offered!r}. One variable "
            f"must have one default — move it to a single accessor both "
            f"call, the way `palette_render.palette_rows` owns "
            f"JARVIS_PALETTE_HEIGHT."
        )
        self.name = name
        self.existing = existing
        self.existing_by = existing_by
        self.offered = offered
        self.offered_by = offered_by


def strict_enabled() -> bool:
    """``JARVIS_CONFIG_ARBITER_STRICT`` (default false). NEVER raises.

    False records and logs; True raises. Default false ONLY because 21
    collisions predate this module — see the module docstring.
    """
    try:
        return os.environ.get(
            "JARVIS_CONFIG_ARBITER_STRICT", "0",
        ).strip().lower() in ("1", "true", "yes", "on")
    except Exception:  # noqa: BLE001
        return False


@dataclass(frozen=True)
class Collision:
    """One variable declared with more than one default."""

    name: str
    declarations: Tuple[Tuple[str, str], ...]  # (default_repr, declared_by)

    def describe(self) -> str:
        return f"{self.name}: " + " vs ".join(
            f"{d!r} ({who})" for d, who in self.declarations)


@dataclass
class _Registry:
    #: name -> {default_repr: {declaring modules}}
    declared: Dict[str, Dict[str, Set[str]]] = field(default_factory=dict)
    #: name -> the FIRST default seen, which is the one that wins
    winner: Dict[str, Any] = field(default_factory=dict)


_registry = _Registry()
_lock = threading.RLock()


def reset_for_tests() -> None:
    """Drop all recorded declarations. Test-only."""
    global _registry  # noqa: PLW0603
    with _lock:
        _registry = _Registry()


def _caller_module(depth: int = 2) -> str:
    """The module that asked, for attribution. NEVER raises.

    Derived from the stack rather than passed in: a caller that has to name
    itself will eventually name itself wrong after a copy-paste, and the
    whole point is to attribute a declaration to where it actually lives.

    ``depth=2`` is exact for every public entry point: frame 0 is this
    function, frame 1 is the ``resolve*`` that called it, frame 2 is the
    declaring module. ``resolve_int``/``resolve_bool`` resolve their own
    attribution and pass it down rather than letting ``resolve`` re-derive at
    a different depth — otherwise the answer would depend on which wrapper
    was used, and a misattributed collision names the wrong file to fix.
    """
    try:
        frame = inspect.stack()[depth]
        return Path(frame.filename).name
    except Exception:  # noqa: BLE001
        return "<unknown>"


def _record(name: str, default: Any, by: str) -> None:
    """Record a declaration; raise in strict mode on conflict. NEVER raises
    except :class:`ConfigurationCollisionError`."""
    key = repr(default)
    with _lock:
        slot = _registry.declared.setdefault(name, {})
        if name not in _registry.winner:
            _registry.winner[name] = default
        # Snapshot the DIFFERING declarations BEFORE inserting this one.
        #
        # Computing the conflict after insertion, then recovering the other
        # key with a bare `next(...)`, made correctness depend on the
        # insertion having actually changed the dict — and a bare `next` on
        # an empty generator raises StopIteration, which is a crash rather
        # than a collision report. A guard whose failure mode is an exception
        # in the reporting path is worse than no guard: it converts a config
        # smell into an outage.
        prior = sorted(k for k in slot if k != key)
        slot.setdefault(key, set()).add(by)
        if not prior:
            return
        existing_key = prior[0]
        existing_by = ", ".join(sorted(slot.get(existing_key, ()))) or "<unknown>"
        winner = _registry.winner[name]

    if strict_enabled():
        raise ConfigurationCollisionError(
            name, existing=winner, existing_by=existing_by,
            offered=default, offered_by=by,
        )
    logger.warning(
        "[ConfigArbiter] COLLISION %s — %s declares %r, %s declares %r; "
        "using %r (first registered). Set JARVIS_CONFIG_ARBITER_STRICT=1 to "
        "make this fatal.",
        name, existing_by, winner, by, default, winner,
    )


def resolve(name: str, default: Any = "", *,
            declared_by: Optional[str] = None) -> str:
    """``os.environ.get`` with the default ARBITRATED. NEVER raises except
    :class:`ConfigurationCollisionError` in strict mode.

    Returns the environment value when set, otherwise the winning default —
    which is the first one declared, so an adopted call site cannot have its
    value changed by a later module's disagreement.
    """
    by = declared_by or _caller_module()
    _record(name, default, by)
    try:
        raw = os.environ.get(name)
    except Exception:  # noqa: BLE001
        raw = None
    if raw is not None and raw.strip() != "":
        return raw
    with _lock:
        return str(_registry.winner.get(name, default))


def resolve_bool(name: str, default: bool = False, *,
                 declared_by: Optional[str] = None) -> bool:
    """Arbitrated boolean. Empty/unset takes the default. NEVER raises."""
    by = declared_by or _caller_module()
    raw = resolve(name, default, declared_by=by)
    try:
        token = str(raw).strip().lower()
        if token in ("1", "true", "yes", "on"):
            return True
        if token in ("0", "false", "no", "off"):
            return False
        return bool(default)
    except Exception:  # noqa: BLE001
        return bool(default)


def resolve_int(name: str, default: int, lo: Optional[int] = None,
                hi: Optional[int] = None, *,
                declared_by: Optional[str] = None) -> int:
    """Arbitrated, clamped integer. NEVER raises."""
    by = declared_by or _caller_module()
    raw = resolve(name, default, declared_by=by)
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        value = int(default)
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


def collisions() -> List[Collision]:
    """Every variable declared with more than one default. NEVER raises."""
    out: List[Collision] = []
    with _lock:
        for name, slot in sorted(_registry.declared.items()):
            if len(slot) > 1:
                out.append(Collision(
                    name=name,
                    declarations=tuple(
                        (k, ", ".join(sorted(v))) for k, v in sorted(slot.items())
                    ),
                ))
    return out


# ---------------------------------------------------------------------------
# Static scan — finds collisions without adoption and without executing code
# ---------------------------------------------------------------------------


def _literal(node: ast.AST) -> Optional[str]:
    """A literal default rendered for comparison, or None if computed.

    Computed defaults (``defaults.poll_interval_s``, ``os.path.join(...)``)
    are deliberately NOT compared: two call sites naming the same attribute
    agree, and a scanner that guessed at their values would produce false
    collisions — which is how a guard stops being read.
    """
    if isinstance(node, ast.Constant):
        return repr(node.value)
    return None


def scan_static(root: Optional[Path] = None, *,
                prefix: str = "JARVIS_") -> List[Collision]:
    """Collisions found by AST, no adoption required. NEVER raises.

    Complements runtime arbitration rather than duplicating it: runtime only
    sees a collision once BOTH sites have executed, which for a pair on
    rarely-taken branches may be never. This sees every literal default in
    the tree in about a second.
    """
    try:
        base = Path(root) if root is not None else (
            Path(__file__).resolve().parents[3])
        found: Dict[str, Dict[str, Set[str]]] = {}
        for path in base.rglob("*.py"):
            if "test" in path.name:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except (SyntaxError, OSError):
                continue
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "get"
                        and len(node.args) == 2):
                    continue
                name_node = node.args[0]
                if not (isinstance(name_node, ast.Constant)
                        and isinstance(name_node.value, str)
                        and name_node.value.startswith(prefix)):
                    continue
                rendered = _literal(node.args[1])
                if rendered is None:
                    continue
                found.setdefault(name_node.value, {}).setdefault(
                    rendered, set()).add(path.name)
        return [
            Collision(name=n, declarations=tuple(
                (k, ", ".join(sorted(v))) for k, v in sorted(slot.items())))
            for n, slot in sorted(found.items()) if len(slot) > 1
        ]
    except Exception:  # noqa: BLE001
        logger.debug("[ConfigArbiter] static scan degraded", exc_info=True)
        return []
