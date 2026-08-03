"""Many subsystems, one vocabulary — derived, namespaced, and lazily paid for.

`capability_registry` answered "what can this Mac do" for ONE controller. The
same question over the whole organism has three more answers that were never
reachable from the HUD: multi-space intelligence, video multi-space
intelligence, and ghost touch. Roughly 6,500 lines of working capability that
the HUD could not name — the same defect the registry was built to delete, one
level up.

WHY NOT A LIST OF PROVIDERS
-----------------------------
A `PROVIDERS = [...]` table beside the code is the hand-kept vocabulary again,
and the entry that gets forgotten is invisible. So providers are DISCOVERED the
way the surface is: a class opts in by saying so in its own docstring.

    class MultiSpaceCaptureEngine:
        '''...

        Capability-Namespace: space
        '''

One mark does two jobs — it declares the namespace AND it IS the discovery
signal. Discovery is a static AST scan, so finding providers costs no imports
at all; only a namespace someone actually calls is ever loaded.

THREE MEASURED FACTS THAT SHAPED THIS
---------------------------------------
1. **Importing the ten candidate classes takes 6.5 s** (`EnhancedMultiSpaceSystem`
   alone 4.3 s). That cannot sit on a boot path, so hydration is off-boot and
   concurrent, and `ensure()` awaits only the ONE namespace a call needs.

2. **Five method names collide across subsystems** — `start`, `stop`,
   `get_instance`, `cleanup`, `register_callback`. In a flat namespace one
   silently shadows another and the model asking for "start" gets whichever
   sorted first. Namespacing is correctness here, not decoration; and a
   collision that survives namespacing is REFUSED and reported, never resolved
   by arrival order.

3. **`public and callable` over-collects on a subsystem class.** These are not
   capability façades: the same rule that was right for `MacOSController` also
   yields `get_instance` and `cleanup`. Hence `require_declaration` — silence
   means "not offered" rather than "gated", which is strictly safer and makes
   the ratchet honest.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import ast
import asyncio
import enum
import importlib
import inspect
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.system_control.capability_registry import (
    CapabilityDef,
    CapabilityRegistry,
)

logger = logging.getLogger("JARVIS.CapabilityFederation")

CAPABILITY_FEDERATION_SCHEMA_VERSION: str = "capability_federation.v1"

#: The declaration that makes a class a provider AND names its namespace.
_NS_TAG = re.compile(r"^\s*Capability-Namespace:\s*(?P<ns>[a-z0-9_]+)\s*$",
                     re.IGNORECASE | re.MULTILINE)
#: Optional: how to obtain the live instance. Resolved on the class first, then
#: the module — `get_ghost_hands` is module-level and async, `get_instance` is a
#: classmethod, and both are legitimate.
_FACTORY_TAG = re.compile(r"^\s*Capability-Factory:\s*(?P<fn>[A-Za-z_][A-Za-z0-9_]*)\s*$",
                          re.IGNORECASE | re.MULTILINE)

#: Where to look. Package ROOTS, not module names — the point is that adding a
#: provider means annotating a class, never editing this file.
_DEFAULT_ROOTS = ("backend/vision", "backend/ghost_hands", "backend/system_control")


def federation_enabled() -> bool:
    """Master gate. Default TRUE — discovery is a static scan. NEVER raises."""
    return (os.environ.get("JARVIS_CAPABILITY_FEDERATION_ENABLED", "true")
            or "").strip().lower() not in ("0", "false", "no", "off")


def scan_roots() -> Tuple[str, ...]:
    """Package roots to scan. NEVER raises."""
    raw = (os.environ.get("JARVIS_CAPABILITY_FEDERATION_ROOTS") or "").strip()
    if not raw:
        return _DEFAULT_ROOTS
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def hydrate_timeout_s() -> float:
    """Per-namespace hydration budget. NEVER raises.

    A provider whose import wedges must not wedge the federation. 4.3 s was the
    measured worst case, so the default leaves real headroom without being
    unbounded.
    """
    try:
        return max(1.0, float(os.environ.get(
            "JARVIS_CAPABILITY_HYDRATE_TIMEOUT_S", "30")))
    except (TypeError, ValueError):
        return 30.0


class Readiness(str, enum.Enum):
    """Whether a namespace can answer — with UNKNOWN kept distinct from EMPTY.

    A namespace that has not been hydrated yet and one that hydrated to nothing
    are different facts, and a surface that renders them the same shows an
    operator a Mac that apparently cannot see its own screens.
    """

    UNHYDRATED = "unhydrated"
    READY = "ready"
    DEGRADED = "degraded"      # tried, and could not — `detail` says why


class ProviderBindings:
    """Values a provider's constructor needs and the federation cannot invent.

    `VideoStreamCapture.__init__(self, vision_analyzer, config=None)` takes a
    REQUIRED collaborator. `_construct`'s `cls()` fallback raises `TypeError` on
    it, so the single most valuable provider in the video namespace could never
    be built — discovered, described, offered to the model, and then dead on the
    first call.

    The two obvious fixes are both wrong. Special-casing the class here is the
    hand-kept table this whole module exists to delete. Making the parameter
    optional edits a working subsystem to suit its consumer, and gives it a
    half-built object to fail on later instead of a clear failure now.

    So dependencies are resolved BY PARAMETER NAME from a registry the host
    fills once at boot. `vision_analyzer` is a thing the HUD already builds —
    `tool_use_orchestrator._make_vision_analyzer` — and this is how the one that
    already exists gets handed to the provider that needs one, rather than a
    second one being constructed in the dark.

    A name nobody bound stays UNRESOLVED, and the provider says so. That is the
    same inversion as the rest of this stack: silence is "not offered", never
    "improvise something".
    """

    def __init__(self) -> None:
        self._values: Dict[str, Any] = {}

    def bind(self, name: str, value: Any) -> None:
        """Supply one dependency by parameter name. NEVER raises.

        *value* may be the collaborator itself or a zero-arg callable returning
        it. The callable form exists because the honest boot order is that a
        provider's dependency is often not built yet when binding happens, and
        deferring is better than binding a None that looks like a real object.
        """
        try:
            if name:
                self._values[str(name)] = value
        except Exception:  # noqa: BLE001
            pass

    def resolve(self, name: str) -> Tuple[bool, Any]:
        """(found, value). NEVER raises.

        Returns a PAIR rather than a value-or-None because `None` is a legal
        thing to bind, and a resolver that cannot distinguish "bound to None"
        from "not bound" would silently construct providers with missing
        collaborators.
        """
        try:
            if name not in self._values:
                return (False, None)
            v = self._values[name]
            if callable(v) and not inspect.isclass(v):
                try:
                    sig = inspect.signature(v)
                    required = [p for p in sig.parameters.values()
                                if p.default is inspect.Parameter.empty
                                and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)]
                    # A zero-arg callable is a FACTORY. Anything else is the
                    # dependency itself — `vision_analyzer` is an async
                    # callable, and calling it here would fire a vision request
                    # instead of binding one.
                    if not required and not inspect.iscoroutinefunction(v):
                        return (True, v())
                except (TypeError, ValueError):
                    pass
            return (True, v)
        except Exception:  # noqa: BLE001
            return (False, None)

    def names(self) -> List[str]:
        return sorted(self._values)


_BINDINGS: Optional[ProviderBindings] = None


def get_bindings() -> ProviderBindings:
    """Process-wide provider bindings. NEVER raises."""
    global _BINDINGS
    if _BINDINGS is None:
        _BINDINGS = ProviderBindings()
    return _BINDINGS


def bind(name: str, value: Any) -> None:
    """Shorthand for `get_bindings().bind`. NEVER raises."""
    get_bindings().bind(name, value)


def reset_bindings() -> None:
    """Testing seam. NEVER raises."""
    global _BINDINGS
    _BINDINGS = None


@dataclass
class ProviderSpec:
    """One annotated class, found statically. Nothing imported yet."""

    namespace: str
    module: str
    class_name: str
    factory: str = ""
    source: str = ""

    @property
    def key(self) -> str:
        return f"{self.module}:{self.class_name}"


@dataclass
class _NamespaceState:
    readiness: str = Readiness.UNHYDRATED.value
    detail: str = ""
    hydrate_ms: float = 0.0
    #: Bare export name -> (spec, CapabilityDef).
    defs: Dict[str, Tuple[ProviderSpec, CapabilityDef]] = field(default_factory=dict)
    #: name -> the owners that wanted it. Refused, never silently resolved.
    conflicts: Dict[str, List[str]] = field(default_factory=dict)
    instances: Dict[str, Any] = field(default_factory=dict)
    #: provider key -> why it could not be built. A provider that DESCRIBES
    #: fine and CONSTRUCTS never is the worst shape available: the model is
    #: offered a tool that fails on every call. Recorded so it is a number on a
    #: dashboard rather than a mystery in a log.
    construct_errors: Dict[str, str] = field(default_factory=dict)


def _docstring_of(node: ast.AST) -> str:
    try:
        return ast.get_docstring(node) or ""   # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        return ""


def _module_name(path: Path, repo_root: Path) -> str:
    try:
        rel = path.relative_to(repo_root).with_suffix("")
        return ".".join(rel.parts)
    except Exception:  # noqa: BLE001
        return ""


def discover(roots: Optional[Tuple[str, ...]] = None,
             repo_root: Optional[Path] = None) -> List[ProviderSpec]:
    """Find annotated provider classes by AST. Imports NOTHING. NEVER raises.

    Static rather than dynamic deliberately: importing every module under
    `backend/vision` to ask which ones are providers would cost far more than
    the 6.5 s measured for ten known classes, and would run arbitrary
    module-level code to answer a question the source text already answers.
    """
    out: List[ProviderSpec] = []
    if not federation_enabled():
        return out
    try:
        root = repo_root or Path(__file__).resolve().parents[2]
        for rel in (roots or scan_roots()):
            base = root / rel
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*.py")):
                if "__pycache__" in path.parts:
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                # Cheap reject before paying for a parse — the tag is rare and
                # these trees are thousands of files.
                if "Capability-Namespace" not in text:
                    continue
                try:
                    tree = ast.parse(text)
                except SyntaxError:
                    logger.debug("[Federation] unparseable: %s", path)
                    continue
                mod = _module_name(path, root)
                if not mod:
                    continue
                for node in ast.walk(tree):
                    if not isinstance(node, ast.ClassDef):
                        continue
                    doc = _docstring_of(node)
                    m = _NS_TAG.search(doc)
                    if not m:
                        continue
                    fm = _FACTORY_TAG.search(doc)
                    out.append(ProviderSpec(
                        namespace=m.group("ns").strip().lower(),
                        module=mod, class_name=node.name,
                        factory=(fm.group("fn").strip() if fm else ""),
                        source=str(path)))
    except Exception:  # noqa: BLE001
        logger.debug("[Federation] discovery degraded", exc_info=True)
    return sorted(out, key=lambda s: (s.namespace, s.module, s.class_name))


def _export_name(namespace: str, bare: str) -> str:
    return f"{namespace}.{bare}"


def split(name: str) -> Tuple[str, str]:
    """`space.capture` -> ("space", "capture"). NEVER raises."""
    ns, _, bare = (name or "").partition(".")
    return (ns, bare) if bare else ("", ns)


class CapabilityFederation:
    """Every namespace's capabilities, derived on demand. NEVER raises.

    Duck-compatible with `CapabilityRegistry` for the three things
    `CapabilityRouter` asks — `get`, `iron_gate_required`, `tool_schemas` — so
    the router federates by being handed one of these instead of the other.
    """

    def __init__(self, specs: Optional[List[ProviderSpec]] = None) -> None:
        self._specs = specs if specs is not None else discover()
        self._ns: Dict[str, _NamespaceState] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    # -- discovery-time facts (no imports) --------------------------------

    def namespaces(self) -> List[str]:
        return sorted({s.namespace for s in self._specs})

    def providers(self, namespace: str = "") -> List[ProviderSpec]:
        return [s for s in self._specs
                if not namespace or s.namespace == namespace]

    # -- hydration ---------------------------------------------------------

    def _state(self, ns: str) -> _NamespaceState:
        st = self._ns.get(ns)
        if st is None:
            st = _NamespaceState()
            self._ns[ns] = st
        return st

    def _lock(self, ns: str) -> asyncio.Lock:
        lk = self._locks.get(ns)
        if lk is None:
            lk = asyncio.Lock()
            self._locks[ns] = lk
        return lk

    def _hydrate_ns_blocking(self, ns: str) -> _NamespaceState:
        """Import and reflect ONE namespace. Runs off-loop. NEVER raises."""
        st = _NamespaceState()
        t0 = time.perf_counter()
        failures: List[str] = []
        for spec in self.providers(ns):
            try:
                mod = importlib.import_module(spec.module)
                cls = getattr(mod, spec.class_name, None)
                if cls is None:
                    failures.append(f"{spec.class_name}: missing")
                    continue
                # Reflect over the CLASS: describing must not require
                # constructing, the lesson `_default_target` paid for.
                reg = CapabilityRegistry(cls, require_declaration=True).hydrate()
                for d in reg.all():
                    # `export_name` is the method's own `as=` when it declared
                    # one. Parsed by the registry with every other attribute of
                    # the tag — this module used to re-read the docstring with a
                    # second regex, which is two parsers for one grammar.
                    bare = d.export_name
                    prior = st.defs.get(bare)
                    if prior is not None and prior[0].key != spec.key:
                        # Two providers, one name. REFUSE — the whole reason
                        # namespacing exists is that arrival order must never
                        # decide which subsystem a caller reached.
                        st.conflicts.setdefault(bare, [prior[0].key]).append(spec.key)
                        st.defs.pop(bare, None)
                        continue
                    if bare in st.conflicts:
                        st.conflicts[bare].append(spec.key)
                        continue
                    st.defs[bare] = (spec, d)
            except BaseException as exc:  # noqa: BLE001 — an import may do anything
                failures.append(f"{spec.class_name}: {type(exc).__name__}: {exc}")
                logger.debug("[Federation] %s failed to hydrate", spec.key,
                             exc_info=True)
        st.hydrate_ms = (time.perf_counter() - t0) * 1000.0
        if st.defs:
            st.readiness = Readiness.READY.value
            st.detail = "; ".join(failures)          # partial success says so
        else:
            st.readiness = Readiness.DEGRADED.value
            st.detail = ("; ".join(failures)
                         or "no declared capabilities in this namespace")
        return st

    async def ensure(self, namespace: str) -> _NamespaceState:
        """Hydrate ONE namespace if needed. NEVER raises.

        Off-loop via `to_thread`: these imports measured 4.3 s at worst and
        blocking the event loop for that long would stall the IPC read loop the
        HUD depends on. Bounded, because a wedged import must not become a
        wedged federation.
        """
        st = self._state(namespace)
        if st.readiness != Readiness.UNHYDRATED.value:
            return st
        try:
            async with self._lock(namespace):
                st = self._state(namespace)
                if st.readiness != Readiness.UNHYDRATED.value:
                    return st
                fresh = await asyncio.wait_for(
                    _to_thread(self._hydrate_ns_blocking, namespace),
                    timeout=hydrate_timeout_s())
                self._ns[namespace] = fresh
                logger.info("[Federation] '%s' %s — %d capabilities in %.0fms",
                            namespace, fresh.readiness, len(fresh.defs),
                            fresh.hydrate_ms)
                return fresh
        except asyncio.TimeoutError:
            st = _NamespaceState(
                readiness=Readiness.DEGRADED.value,
                detail=f"hydration exceeded {hydrate_timeout_s():.0f}s")
            self._ns[namespace] = st
            logger.warning("[Federation] '%s' hydration timed out", namespace)
            return st
        except Exception as exc:  # noqa: BLE001
            st = _NamespaceState(readiness=Readiness.DEGRADED.value,
                                 detail=f"{type(exc).__name__}: {exc}")
            self._ns[namespace] = st
            return st

    async def warm(self) -> Dict[str, str]:
        """Hydrate every namespace CONCURRENTLY, off the boot path. NEVER raises.

        Serial cost measured at 6.5 s; concurrently it is bounded by the
        slowest single namespace. Intended to be fired as a background task at
        boot so the first voice command does not pay for it — and it is only an
        optimisation: `ensure()` is what correctness depends on.
        """
        try:
            names = self.namespaces()
            await asyncio.gather(*(self.ensure(n) for n in names),
                                 return_exceptions=True)
            return {n: self._state(n).readiness for n in names}
        except Exception:  # noqa: BLE001
            return {}

    # -- the registry-shaped surface --------------------------------------

    def get(self, name: str) -> Optional[CapabilityDef]:
        """Look up `namespace.capability`. NEVER raises, NEVER hydrates.

        Synchronous because the router's fast path is synchronous. An
        unhydrated namespace answers None, which the router treats as unknown —
        and `ensure()` is what the caller does about it.
        """
        try:
            ns, bare = split(name)
            entry = self._state(ns).defs.get(bare)
            return entry[1] if entry else None
        except Exception:  # noqa: BLE001
            return None

    def iron_gate_required(self, name: str) -> bool:
        """True unless DECLARED read-only. Unknown answers True."""
        d = self.get(name)
        return True if d is None else d.iron_gate_required

    def resolve_target(self, name: str) -> Any:
        """The live instance that owns `name`, constructed lazily. NEVER raises.

        Construction is deferred to the first EXECUTION and cached per provider:
        reflection is free, but `MultiSpaceCaptureEngine()` starts real work.
        A failure here is a failed call, never an import-time explosion — the
        doctrine `capability_router._instance` already follows.
        """
        try:
            ns, bare = split(name)
            st = self._state(ns)
            entry = st.defs.get(bare)
            if entry is None:
                return None
            spec, _ = entry
            inst = st.instances.get(spec.key)
            if inst is None:
                inst, err = _construct(spec)
                if inst is not None:
                    st.instances[spec.key] = inst
                    st.construct_errors.pop(spec.key, None)
                else:
                    st.construct_errors[spec.key] = err
            return inst
        except Exception:  # noqa: BLE001
            logger.debug("[Federation] resolve_target(%s) degraded", name,
                         exc_info=True)
            return None

    def method_for(self, name: str) -> str:
        """The METHOD to call for an exported name. NEVER raises.

        `touch.stop_actuator` is exported under its alias and implemented as
        `stop`. A caller that invoked the export name on the instance would get
        `AttributeError` — the collision fix breaking the calls it was added to
        make possible. This is the one translation, and it lives beside the one
        that created the alias.
        """
        try:
            ns, bare = split(name)
            entry = self._state(ns).defs.get(bare)
            return entry[1].name if entry else ""
        except Exception:  # noqa: BLE001
            return ""

    def names(self) -> List[str]:
        out: List[str] = []
        for ns, st in self._ns.items():
            out.extend(_export_name(ns, b) for b in st.defs)
        return sorted(out)

    def tool_schemas(self) -> Dict[str, Dict[str, Any]]:
        """Drop-in for `TOOL_SCHEMAS`, namespaced. NEVER raises."""
        out: Dict[str, Dict[str, Any]] = {}
        try:
            for ns, st in self._ns.items():
                for bare, (_, d) in st.defs.items():
                    full = _export_name(ns, bare)
                    schema = d.to_tool_schema()
                    schema["name"] = full
                    out[full] = schema
        except Exception:  # noqa: BLE001
            pass
        return dict(sorted(out.items()))

    # -- observability -----------------------------------------------------

    def conflicts(self) -> Dict[str, List[str]]:
        """Names two providers both claimed. Refused, not resolved. NEVER raises."""
        out: Dict[str, List[str]] = {}
        for ns, st in self._ns.items():
            for bare, owners in st.conflicts.items():
                out[_export_name(ns, bare)] = sorted(set(owners))
        return out

    def sessions(self) -> List[str]:
        return sorted(_export_name(ns, b)
                      for ns, st in self._ns.items()
                      for b, (_, d) in st.defs.items() if d.starts_session)

    def unreleasable(self) -> List[str]:
        """Session starts with no named release — an invisible leak. NEVER raises."""
        return sorted(_export_name(ns, b)
                      for ns, st in self._ns.items()
                      for b, (_, d) in st.defs.items()
                      if d.starts_session and not d.release)

    def broken_releases(self) -> Dict[str, str]:
        """START capabilities whose named release is not a usable END. NEVER raises.

        `unreleasable()` catches the start that named NOTHING. This catches the
        subtler one: a start that names a release which was misspelled, lives in
        another namespace, or was never tagged `session-end` — so it is not
        forced SAFE_AUTO and the reaper's call would SUSPEND awaiting consent
        that, by then, nobody is there to give.

        A defect a test can fail on, rather than a stream that quietly outlives
        every attempt to stop it.
        """
        out: Dict[str, str] = {}
        try:
            for ns, st in self._ns.items():
                for bare, (_, d) in st.defs.items():
                    if not d.starts_session or not d.release:
                        continue
                    target = st.defs.get(d.release)
                    full = _export_name(ns, bare)
                    if target is None:
                        out[full] = (f"release '{d.release}' is not an exported "
                                     f"capability of namespace '{ns}'")
                    elif not target[1].ends_session:
                        out[full] = (f"release '{d.release}' exists but is not "
                                     f"tagged session-end, so it is gated and "
                                     f"the reaper cannot call it")
        except Exception:  # noqa: BLE001
            pass
        return out

    def unbuildable(self) -> Dict[str, str]:
        """Providers that described fine and could not be constructed. NEVER raises."""
        out: Dict[str, str] = {}
        for st in self._ns.values():
            out.update({k: v[:300] for k, v in st.construct_errors.items()})
        return out

    def stats(self) -> Dict[str, Any]:
        return {
            "schema_version": CAPABILITY_FEDERATION_SCHEMA_VERSION,
            "enabled": federation_enabled(),
            "providers": len(self._specs),
            "bindings": get_bindings().names(),
            "namespaces": {
                n: {"readiness": self._state(n).readiness,
                    "capabilities": len(self._state(n).defs),
                    "hydrate_ms": round(self._state(n).hydrate_ms, 1),
                    "detail": self._state(n).detail[:300]}
                for n in self.namespaces()
            },
            "capabilities": len(self.names()),
            "conflicts": self.conflicts(),
            "sessions": len(self.sessions()),
            "unreleasable": self.unreleasable(),
            "broken_releases": self.broken_releases(),
            "unbuildable": self.unbuildable(),
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _construct(spec: ProviderSpec) -> Tuple[Any, str]:
    """Get a live instance for a provider. Returns (instance, error). NEVER raises.

    Order: declared factory, then `get_instance`, then the constructor with its
    required parameters resolved from :class:`ProviderBindings`. An async
    factory is NOT awaited here — `resolve_target` is synchronous by design — so
    a provider needing async construction declares a sync accessor.

    Returning `(None, why)` is the point. A half-built instance would be worse
    than no instance, and an empty `why` was how the `VideoStreamCapture`
    `TypeError` stayed a debug log for as long as it did.
    """
    try:
        mod = importlib.import_module(spec.module)
        cls = getattr(mod, spec.class_name, None)
        if cls is None:
            return (None, f"{spec.class_name} not found in {spec.module}")
        for cand in ([spec.factory] if spec.factory else []) + ["get_instance"]:
            if not cand:
                continue
            fn = getattr(cls, cand, None) or getattr(mod, cand, None)
            if callable(fn) and not inspect.iscoroutinefunction(fn):
                try:
                    inst = fn()
                except TypeError:
                    # A factory with its own required arguments is not a
                    # factory this can drive. Fall through to the constructor,
                    # which CAN have its dependencies resolved by name.
                    continue
                if inst is not None and not inspect.isawaitable(inst):
                    return (inst, "")
        kwargs, missing = _resolve_ctor_args(cls)
        if missing:
            return (None,
                    f"unbound constructor dependencies: {', '.join(missing)} — "
                    f"call capability_federation.bind('{missing[0]}', ...) "
                    f"during boot")
        return (cls(**kwargs), "")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Federation] cannot construct %s: %s: %s",
                       spec.key, type(exc).__name__, exc)
        return (None, f"{type(exc).__name__}: {exc}")


def _resolve_ctor_args(cls: Any) -> Tuple[Dict[str, Any], List[str]]:
    """Fill a constructor's REQUIRED parameters from bindings. NEVER raises.

    Only the required ones. A parameter with a default is the provider's own
    considered choice, and overriding it from a global registry because the
    names happened to match would be spooky action — `cache_size_mb=200` means
    the engine picked 200, not that it is waiting to be told.

    Unresolvable names are RETURNED rather than skipped, so the caller reports a
    provider that cannot be built instead of calling `cls()` and translating a
    `TypeError` into a shrug.
    """
    kwargs: Dict[str, Any] = {}
    missing: List[str] = []
    try:
        sig = inspect.signature(cls.__init__)
    except (TypeError, ValueError):
        return ({}, [])
    bindings = get_bindings()
    for pname, p in sig.parameters.items():
        if pname in ("self", "cls"):
            continue
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        if p.default is not inspect.Parameter.empty:
            continue
        found, value = bindings.resolve(pname)
        if found:
            kwargs[pname] = value
        else:
            missing.append(pname)
    return (kwargs, missing)


async def _to_thread(fn, *args):
    """`asyncio.to_thread` without requiring 3.9+. NEVER raises on its own."""
    try:
        return await asyncio.to_thread(fn, *args)      # 3.9+
    except AttributeError:                             # pragma: no cover
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: fn(*args))


_FEDERATION: Optional[CapabilityFederation] = None


def get_federation() -> CapabilityFederation:
    """Process-wide federation. Discovery only — no imports. NEVER raises."""
    global _FEDERATION
    if _FEDERATION is None:
        _FEDERATION = CapabilityFederation()
    return _FEDERATION


def reset_federation() -> None:
    """Testing seam. NEVER raises."""
    global _FEDERATION
    _FEDERATION = None
