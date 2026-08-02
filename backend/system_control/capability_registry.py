"""One derived answer to "what can this machine do".

Three hand-kept vocabularies describe one Mac today: the HUD declares 9 tools in
`hud/tool_definitions.py`, Venom declares 16 in `tool_executor.py`, and the web
app reaches ~18 named capabilities through 42 routers. `macos_controller` can
lock the screen; the HUD cannot, because nobody wrote `lock_screen` into its
list. The capability was never missing — its NAME was.

A fourth hand-written list would be the same defect in a new costume. So this
DERIVES the vocabulary from the implementation, the way
`repl_dispatch_registry` derives verbs from `dispatch_<verb>_command` methods
rather than keeping a table beside them.

THE INVERSION THAT MAKES RISK SAFE
------------------------------------
The obvious rule is "classify a method as mutating if it looks mutating".
Every version of that is a guess, and it fails in the direction that hurts: a
method named `get_display_config` that quietly changes the display would be
auto-approved forever, and nothing would ever say so.

So the default is REVERSED. A capability requires approval unless it has
explicitly declared itself read-only. Silence means APPROVAL_REQUIRED, not
SAFE_AUTO — the same rule as `unverified != safe` in the coordination probe and
`UNKNOWN != done` in the intent journal, applied where the cost of being wrong
is somebody's screen locking mid-sentence.

Declaring is cheap and local:

    @capability(reads_only=True)
    async def get_battery(self) -> dict: ...

    @capability(mutates=True, tier="approval_required")
    async def lock_screen(self) -> tuple: ...

...and a docstring tag (``Capability: read-only``) works for methods whose
module should not import this one. Every classification carries its PROVENANCE
— declared, tagged, or defaulted — so "we decided this is safe" and "nobody has
looked at this yet" never render the same, and `unclassified()` gives a ratchet
a number to drive to zero.

SCHEMA
--------
Emits the exact shape `hud/tool_definitions.TOOL_SCHEMAS` already uses
(``name`` / ``description`` / ``parameters``), so a consumer swaps its source
without changing how it reads one. Parameters come from `inspect.signature`
with types mapped to JSON-schema names, and descriptions are lifted from the
Google-style ``Args:`` block the controller already writes.

Callback and internal parameters are EXCLUDED: `lock_screen` takes a
`progress_callback`, and offering a language model a parameter it can only fill
with nonsense is how a tool loop wastes a turn.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import enum
import inspect
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("JARVIS.CapabilityRegistry")

CAPABILITY_REGISTRY_SCHEMA_VERSION: str = "capability_registry.v1"


def registry_enabled() -> bool:
    """Master gate. Default TRUE — pure reflection, no side effects. NEVER raises."""
    return (os.environ.get("JARVIS_CAPABILITY_REGISTRY_ENABLED", "true")
            or "").strip().lower() not in ("0", "false", "no", "off")


class Provenance(str, enum.Enum):
    """HOW a capability's risk was decided — never conflated with the decision."""

    DECLARED = "declared"      # an explicit @capability(...) on the method
    TAGGED = "tagged"          # a `Capability:` line in the docstring
    DEFAULTED = "defaulted"    # nobody has said — treated as needing approval


class Tier(str, enum.Enum):
    """Mirrors `risk_engine.RiskTier` by VALUE rather than importing it.

    A leaf reflection service that imports the governance risk engine drags the
    whole pipeline into any process that wants to ask what a Mac can do — the
    same dependency inversion that kept `lock_manager` out of the CU sensor.
    The strings are the contract; `iron_gate_required` is the only question a
    caller actually asks.
    """

    SAFE_AUTO = "safe_auto"
    NOTIFY_APPLY = "notify_apply"
    APPROVAL_REQUIRED = "approval_required"
    BLOCKED = "blocked"


#: The tier a capability gets when nobody has classified it. APPROVAL_REQUIRED
#: rather than SAFE_AUTO, deliberately: see the module docstring.
_DEFAULT_TIER = Tier.APPROVAL_REQUIRED

#: Parameters a language model must never be offered. Callbacks, sinks and
#: internals it can only fill with a hallucinated value.
_EXCLUDED_PARAMS = frozenset({"self", "cls", "progress_callback", "callback",
                              "on_progress", "loop", "session", "ctx",
                              "context", "_internal"})

_DOC_TAG = re.compile(r"^\s*Capability:\s*(?P<body>.+)$",
                      re.IGNORECASE | re.MULTILINE)
_ARGS_BLOCK = re.compile(
    r"^\s*Args?:\s*$(?P<body>.*?)(?=^\s*(?:Returns?|Raises?|Yields?|Examples?|Note)s?:|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL)
_ARG_LINE = re.compile(r"^\s{2,}(?P<name>\*{0,2}\w+)\s*(?:\([^)]*\))?\s*:\s*(?P<desc>.+)$")

_JSON_TYPES: Dict[Any, str] = {
    str: "string", int: "integer", float: "number", bool: "boolean",
    list: "array", dict: "object",
}


def capability(*, mutates: Optional[bool] = None,
               reads_only: Optional[bool] = None,
               tier: Optional[str] = None,
               description: str = "") -> Callable:
    """Declare a method's risk where the method is written. NEVER raises.

    A decorator rather than a central table for the reason `repl_dispatch_
    registry` gives about verbs: a table beside the code is a second thing to
    update, and the update that gets forgotten is invisible.
    """
    def _wrap(fn: Callable) -> Callable:
        try:
            resolved = tier
            if resolved is None:
                if reads_only is True or mutates is False:
                    resolved = Tier.SAFE_AUTO.value
                elif mutates is True:
                    resolved = _DEFAULT_TIER.value
            setattr(fn, "__capability__", {
                "tier": resolved or _DEFAULT_TIER.value,
                "declared": True,
                "description": description,
            })
        except Exception:  # noqa: BLE001 — a decorator never breaks an import
            pass
        return fn
    return _wrap


@dataclass
class CapabilityDef:
    """One machine capability, as both a tool schema and a risk decision."""

    name: str
    description: str
    parameters: Dict[str, Dict[str, str]] = field(default_factory=dict)
    tier: str = _DEFAULT_TIER.value
    provenance: str = Provenance.DEFAULTED.value
    is_async: bool = True
    schema_version: str = CAPABILITY_REGISTRY_SCHEMA_VERSION

    @property
    def iron_gate_required(self) -> bool:
        """The only question most callers ask.

        Anything above SAFE_AUTO goes through the gate. A defaulted capability
        answers True — the point of the inversion.
        """
        return self.tier != Tier.SAFE_AUTO.value

    @property
    def classified(self) -> bool:
        return self.provenance != Provenance.DEFAULTED.value

    def to_tool_schema(self) -> Dict[str, Any]:
        """The exact shape `TOOL_SCHEMAS` already uses. NEVER raises."""
        return {"name": self.name, "description": self.description,
                "parameters": dict(self.parameters)}


def _json_type(annotation: Any) -> str:
    """Best JSON-schema name for an annotation. NEVER raises."""
    try:
        if annotation is inspect.Parameter.empty:
            return "string"
        if annotation in _JSON_TYPES:
            return _JSON_TYPES[annotation]
        text = str(annotation)
        # `Optional[str]` / `Union[str, None]` / `List[int]` — read the inside.
        for py, js in _JSON_TYPES.items():
            if getattr(py, "__name__", "") and f"{py.__name__}" in text:
                return js
        return "string"
    except Exception:  # noqa: BLE001
        return "string"


def _arg_descriptions(doc: str) -> Dict[str, str]:
    """Parameter descriptions from a Google-style ``Args:`` block. NEVER raises."""
    out: Dict[str, str] = {}
    try:
        m = _ARGS_BLOCK.search(doc or "")
        if not m:
            return out
        for line in (m.group("body") or "").splitlines():
            am = _ARG_LINE.match(line)
            if am:
                out[am.group("name").lstrip("*")] = am.group("desc").strip()
    except Exception:  # noqa: BLE001
        pass
    return out


def _summary(doc: str, fallback: str) -> str:
    """First meaningful docstring line. NEVER raises."""
    try:
        for line in (doc or "").strip().splitlines():
            line = line.strip()
            if line:
                return line
    except Exception:  # noqa: BLE001
        pass
    return fallback


def _classify(fn: Any, doc: str) -> Tuple[str, str]:
    """(tier, provenance) for one method. NEVER raises.

    Order matters: an explicit decorator beats a docstring tag, and both beat
    the default — but the DEFAULT IS NOT SAFE. A capability nobody has looked
    at is treated exactly like one somebody flagged as dangerous, because from
    the registry's position those are the same state of knowledge.
    """
    try:
        declared = getattr(fn, "__capability__", None)
        if isinstance(declared, dict) and declared.get("declared"):
            return (str(declared.get("tier") or _DEFAULT_TIER.value),
                    Provenance.DECLARED.value)
        tag = _DOC_TAG.search(doc or "")
        if tag:
            body = (tag.group("body") or "").strip().lower()
            if "read-only" in body or "read only" in body or "readonly" in body:
                return (Tier.SAFE_AUTO.value, Provenance.TAGGED.value)
            for t in Tier:
                if t.value in body.replace("-", "_"):
                    return (t.value, Provenance.TAGGED.value)
            return (_DEFAULT_TIER.value, Provenance.TAGGED.value)
    except Exception:  # noqa: BLE001
        pass
    return (_DEFAULT_TIER.value, Provenance.DEFAULTED.value)


def describe(name: str, fn: Any) -> Optional[CapabilityDef]:
    """Turn one bound method into a CapabilityDef. None if unusable. NEVER raises."""
    try:
        doc = inspect.getdoc(fn) or ""
        tier, prov = _classify(fn, doc)
        params: Dict[str, Dict[str, str]] = {}
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            sig = None
        if sig is not None:
            descs = _arg_descriptions(doc)
            for pname, p in sig.parameters.items():
                if pname in _EXCLUDED_PARAMS:
                    continue
                if p.kind in (inspect.Parameter.VAR_POSITIONAL,
                              inspect.Parameter.VAR_KEYWORD):
                    continue
                # A callable default or annotation is a hook, not an argument a
                # model can supply.
                if callable(p.default) and p.default is not inspect.Parameter.empty:
                    continue
                if "Callable" in str(p.annotation):
                    continue
                params[pname] = {
                    "type": _json_type(p.annotation),
                    "description": descs.get(pname, f"{pname} parameter"),
                }
        declared = getattr(fn, "__capability__", None) or {}
        return CapabilityDef(
            name=name,
            description=(declared.get("description")
                         or _summary(doc, f"Invoke {name} on macOS.")),
            parameters=params,
            tier=tier,
            provenance=prov,
            is_async=inspect.iscoroutinefunction(fn),
        )
    except Exception:  # noqa: BLE001
        logger.debug("[CapabilityRegistry] describe(%s) degraded", name,
                     exc_info=True)
        return None


class CapabilityRegistry:
    """Every public capability of a controller, derived. NEVER raises."""

    def __init__(self, target: Any = None) -> None:
        self._target = target
        self._defs: Dict[str, CapabilityDef] = {}
        self._hydrated = False

    def hydrate(self) -> "CapabilityRegistry":
        """Introspect the target. Idempotent. NEVER raises."""
        self._defs = {}
        self._hydrated = True
        if not registry_enabled():
            return self
        try:
            target = self._target if self._target is not None else _default_target()
            if target is None:
                return self
            for name, member in inspect.getmembers(target, callable):
                if name.startswith("_"):
                    continue            # private is not a capability
                d = describe(name, member)
                if d is not None:
                    self._defs[name] = d
        except Exception:  # noqa: BLE001
            logger.debug("[CapabilityRegistry] hydrate degraded", exc_info=True)
        return self

    def _ensure(self) -> None:
        if not self._hydrated:
            self.hydrate()

    def names(self) -> List[str]:
        self._ensure()
        return sorted(self._defs)

    def get(self, name: str) -> Optional[CapabilityDef]:
        self._ensure()
        return self._defs.get(name)

    def all(self) -> List[CapabilityDef]:
        self._ensure()
        return [self._defs[n] for n in sorted(self._defs)]

    def tool_schemas(self) -> Dict[str, Dict[str, Any]]:
        """Drop-in for `TOOL_SCHEMAS`. NEVER raises."""
        self._ensure()
        return {n: self._defs[n].to_tool_schema() for n in sorted(self._defs)}

    def iron_gate_required(self, name: str) -> bool:
        """True unless the capability has DECLARED itself read-only.

        An unknown name answers True as well: a caller asking about a
        capability this registry has never seen is the last place to assume
        the permissive answer.
        """
        d = self.get(name)
        return True if d is None else d.iron_gate_required

    def unclassified(self) -> List[str]:
        """Capabilities nobody has declared — the ratchet's number. NEVER raises."""
        self._ensure()
        return sorted(n for n, d in self._defs.items() if not d.classified)

    def stats(self) -> Dict[str, Any]:
        self._ensure()
        gated = [d for d in self._defs.values() if d.iron_gate_required]
        return {
            "schema_version": CAPABILITY_REGISTRY_SCHEMA_VERSION,
            "enabled": registry_enabled(),
            "capabilities": len(self._defs),
            "iron_gate_required": len(gated),
            "safe_auto": len(self._defs) - len(gated),
            "unclassified": len(self.unclassified()),
            # An empty registry MUST explain itself. Zero capabilities and
            # "the controller would not import" are different facts, and a
            # consumer that cannot tell them apart shows an operator a Mac
            # that apparently does nothing.
            "degraded_reason": (_degraded[0] if not self._defs else ""),
            "by_provenance": {
                p.value: sum(1 for d in self._defs.values()
                             if d.provenance == p.value) for p in Provenance
            },
        }


def _default_target() -> Any:
    """The macOS controller CLASS, not an instance. NEVER raises.

    Describing a capability must not require constructing the thing that has
    it. The first version instantiated `MacOSController`, whose constructor
    starts async pipeline work — it raised on this machine and the registry
    reported ZERO capabilities with no explanation, which is precisely the
    silent-emptiness failure this codebase keeps finding.

    Class-level reflection finds the same 42 methods, costs nothing, and has no
    side effects. `inspect.signature` reports `self` for an unbound function,
    which `_EXCLUDED_PARAMS` already drops.
    """
    try:
        from backend.system_control.macos_controller import MacOSController
        return MacOSController
    except Exception as exc:  # noqa: BLE001
        _degraded[0] = f"controller unimportable: {type(exc).__name__}: {exc}"
        logger.debug("[CapabilityRegistry] no controller available",
                     exc_info=True)
        return None


#: Why the registry is empty, when it is. An empty vocabulary that
#: cannot say why is indistinguishable from a Mac that can do nothing.
_degraded: list = [""]


_REGISTRY: Optional[CapabilityRegistry] = None


def get_capability_registry() -> CapabilityRegistry:
    """Process-wide registry over the live controller. NEVER raises."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = CapabilityRegistry().hydrate()
    return _REGISTRY


def reset_capability_registry() -> None:
    """Testing seam. NEVER raises."""
    global _REGISTRY
    _REGISTRY = None
