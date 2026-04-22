"""FlagRegistry — typed directory of every JARVIS_* env flag the organism reads.

Kills the discoverability tax on 481+ scattered ``os.environ.get()`` calls
by providing a single descriptive source of truth: each flag has a name,
type, default, description, category, posture-relevance, source-file,
example, and since-version. Typos against registered names produce
Levenshtein-matched warnings instead of silent fallback.

Authority posture
-----------------

* §1 Boundary Principle — **descriptive only, zero execution authority**.
  The registry cannot mutate behavior — it observes env reads and names
  them. There is no ``registry.set(flag, value)``; there is no
  ``/help override``. Operators change env vars through the OS, not
  through this module.
* §5 Tier 0 — pure dict + threading.Lock; no LLM, no disk I/O on the
  hot read path; Levenshtein is O(N×M) over strings but bounded by the
  registered flag count (~50 at seed, grows linearly).
* §8 Observability — every typo detection is logged once per process
  lifetime; ``/help unregistered`` surfaces them on-demand.

Authority invariant (grep-pinned at Slice 4): this module imports
nothing from ``orchestrator`` / ``policy`` / ``iron_gate`` /
``risk_tier`` / ``change_engine`` / ``candidate_generator`` / ``gate``.

Kill switch
-----------

``JARVIS_FLAG_REGISTRY_ENABLED`` (default ``false`` at Slice 1,
graduates to ``true`` at Slice 4). When off, the registry data
structure stays alive (it's descriptive, not authoritative), but the
**surfaces** go dark: ``/help`` rejects operational verbs, GET
``/observability/flags`` returns 403, SSE ``flag_typo_detected`` drops,
typo warnings are silent. One flag kills every surface in lockstep.

Typed accessors
---------------

``get_bool(name) / get_int / get_float / get_str / get_json``. Each
both reads the env AND records usage (so late auditing knows which
registered flags were actually *read* during a session). Malformed
values fall back to the registered default with a WARNING log;
unregistered names log at DEBUG (caller may be using an opt-in flag
that was never registered).
"""
from __future__ import annotations

import enum
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


FLAG_REGISTRY_SCHEMA_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Env helpers (internal — the registry provides the public accessors)
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return default


def is_enabled() -> bool:
    """Master switch.

    Default: **``true``** (graduated 2026-04-21 via Slice 4 after
    Slices 1-3 shipped the primitive + 52-flag seed + /help dispatcher +
    GET /observability/flags + SSE flag_typo_detected/flag_registered
    with 135 governance tests + 3 live-fire proofs). Explicit
    ``"false"`` reverts to the Slice 1 deny-by-default posture so
    operators retain a runtime kill switch — when the flag is explicitly
    ``"false"`` every surface disables in lockstep:

      * /help REPL rejects operational verbs (/help help still works
        for discoverability so operators can find the flag name)
      * GET /observability/flags{,/{name},/unregistered} return 403
      * GET /observability/verbs returns 403
      * SSE publish_flag_typo_event + publish_flag_registered_event
        become no-ops (drop silently)
      * FlagRegistry.report_typos() logs nothing

    The registry DATA STRUCTURE remains alive when the flag is off —
    it's descriptive, not authoritative. Seed-registered specs stay in
    memory; typed accessors (get_bool/int/float/str/json) keep
    functioning; internal modules that use registry as a reader keep
    working. Only the **operator-facing surfaces** are gated.

    The authority invariants (grep-pinned zero imports of
    orchestrator/policy/iron_gate/risk_tier/change_engine/candidate_generator/gate),
    Levenshtein threshold caps, thread-safety via threading.Lock, and
    schema_version=1.0 discipline all remain in force regardless of
    this flag — graduation flips opt-in friction, NOT authority surface.
    """
    return _env_bool("JARVIS_FLAG_REGISTRY_ENABLED", True)


def typo_warn_enabled() -> bool:
    """Sub-gate for Levenshtein typo warnings."""
    if not is_enabled():
        return False
    return _env_bool("JARVIS_FLAG_TYPO_WARN_ENABLED", True)


def typo_max_distance() -> int:
    """Levenshtein threshold. Default 3 — catches single-char typos
    and minor transpositions without over-matching unrelated flags."""
    return max(1, _env_int("JARVIS_FLAG_TYPO_MAX_DISTANCE", 3, minimum=1))


# ---------------------------------------------------------------------------
# Type vocabulary
# ---------------------------------------------------------------------------


class FlagType(str, enum.Enum):
    """Supported env flag types. JSON is for structured overrides
    (like ``JARVIS_POSTURE_WEIGHTS_OVERRIDE``)."""

    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    STR = "str"
    JSON = "json"


class Category(str, enum.Enum):
    """Fixed taxonomy — 8 slots, chosen to cover every flag we know of."""

    SAFETY = "safety"            # kill switches, gates
    TIMING = "timing"            # intervals, timeouts, windows
    CAPACITY = "capacity"        # sizes, pools, caps
    ROUTING = "routing"          # provider cascade, model selection
    OBSERVABILITY = "observability"  # SSE, GET, logging, audit
    INTEGRATION = "integration"  # GitHub, IDE, MCP, voice
    EXPERIMENTAL = "experimental"    # shadow / not-graduated
    TUNING = "tuning"            # weights, thresholds, floors


class Relevance(str, enum.Enum):
    """Per-posture relevance tags for the ``/help posture`` filter."""

    CRITICAL = "critical"  # operator MUST know about this when in posture P
    RELEVANT = "relevant"  # useful to know
    IGNORED = "ignored"    # safe to ignore in posture P


# ---------------------------------------------------------------------------
# FlagSpec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlagSpec:
    """Frozen descriptor for one env flag.

    ``posture_relevance`` maps Posture-value-strings (e.g. ``"HARDEN"``)
    to a :class:`Relevance`. We use strings instead of importing
    :class:`Posture` to keep this module authority-free and free of a
    hard dependency on the DirectionInferrer arc — consumers map strings
    to their own Posture enum values at query time.
    """

    name: str
    type: FlagType
    default: Any
    description: str
    category: Category
    source_file: str
    example: Optional[str] = None
    since: str = "v1.0"
    posture_relevance: Mapping[str, Relevance] = field(default_factory=dict)
    # Populated by typed accessors; tracks which flags have been read
    # this session. Not part of equality (frozen dataclass — can't mutate
    # the dict anyway after construction).
    aliases: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type.value,
            "default": self.default,
            "description": self.description,
            "category": self.category.value,
            "source_file": self.source_file,
            "example": self.example,
            "since": self.since,
            "posture_relevance": {
                k: v.value for k, v in (self.posture_relevance or {}).items()
            },
            "aliases": list(self.aliases),
        }


# ---------------------------------------------------------------------------
# Levenshtein (bounded; no numpy dep)
# ---------------------------------------------------------------------------


def levenshtein_distance(a: str, b: str) -> int:
    """Classic DP implementation. O(|a|×|b|). Used only on flag names,
    which are short (<60 chars) and few (<500)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    # Keep rolling single row — O(min(|a|,|b|)) memory
    if len(a) > len(b):
        a, b = b, a
    previous = list(range(len(a) + 1))
    for j, cb in enumerate(b, start=1):
        current = [j] + [0] * len(a)
        for i, ca in enumerate(a, start=1):
            cost = 0 if ca == cb else 1
            current[i] = min(
                previous[i] + 1,          # deletion
                current[i - 1] + 1,       # insertion
                previous[i - 1] + cost,   # substitution
            )
        previous = current
    return previous[-1]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class FlagRegistry:
    """Process-wide typed directory of env flags.

    Thread-safe. Duplicate registration defaults to override-with-warning
    (tests pin). Typed accessors both read env AND record usage.
    """

    def __init__(self) -> None:
        self._specs: Dict[str, FlagSpec] = {}
        self._read_names: set = set()
        self._reported_typos: set = set()
        self._lock = threading.Lock()

    # -- registration -------------------------------------------------------

    def register(
        self,
        spec: FlagSpec,
        *,
        override: bool = True,
    ) -> None:
        """Install a FlagSpec. ``override=True`` silently replaces an
        existing entry (with a DEBUG log) so seed files can be re-run
        during tests. ``override=False`` raises if name already exists."""
        if not isinstance(spec, FlagSpec):
            raise TypeError(
                f"register expects FlagSpec, got {type(spec).__name__}"
            )
        with self._lock:
            if spec.name in self._specs and not override:
                raise ValueError(
                    f"FlagSpec {spec.name!r} already registered "
                    f"(pass override=True to replace)"
                )
            if spec.name in self._specs:
                logger.debug(
                    "[FlagRegistry] overriding existing spec: %s",
                    spec.name,
                )
            self._specs[spec.name] = spec

    def bulk_register(self, specs: List[FlagSpec], *, override: bool = True) -> None:
        for s in specs:
            self.register(s, override=override)

    # -- lookup -------------------------------------------------------------

    def get_spec(self, name: str) -> Optional[FlagSpec]:
        with self._lock:
            return self._specs.get(name)

    def list_all(self) -> List[FlagSpec]:
        with self._lock:
            return sorted(self._specs.values(), key=lambda s: s.name)

    def list_by_category(self, category: Category) -> List[FlagSpec]:
        with self._lock:
            return sorted(
                (s for s in self._specs.values() if s.category is category),
                key=lambda s: s.name,
            )

    def find(self, query: str) -> List[FlagSpec]:
        """Case-insensitive substring search on name + description."""
        q = query.strip().lower()
        if not q:
            return []
        with self._lock:
            return sorted(
                (s for s in self._specs.values()
                 if q in s.name.lower() or q in s.description.lower()),
                key=lambda s: s.name,
            )

    def relevant_to_posture(
        self,
        posture: str,
        *,
        min_relevance: Relevance = Relevance.RELEVANT,
    ) -> List[FlagSpec]:
        """Filter to flags tagged CRITICAL or RELEVANT for the posture.

        ``posture`` is a string (e.g. ``"HARDEN"``) — we don't import the
        Posture enum to stay authority-free and decoupled. Flags with no
        ``posture_relevance`` are NOT returned (use ``list_all`` for that).
        """
        p = posture.strip().upper()
        order = {
            Relevance.CRITICAL: 0, Relevance.RELEVANT: 1, Relevance.IGNORED: 2,
        }
        min_order = order[min_relevance]
        with self._lock:
            hits: List[FlagSpec] = []
            for spec in self._specs.values():
                rel = spec.posture_relevance.get(p)
                if rel is None:
                    continue
                if order[rel] <= min_order:
                    hits.append(spec)
        return sorted(hits, key=lambda s: s.name)

    # -- typed accessors ----------------------------------------------------

    def _record_read(self, name: str) -> None:
        """Track which flags were actually read this session.

        Called by every typed accessor. Unregistered names are also
        recorded (so ``/help stats`` can surface "flags in use but not
        in registry" — the unregistered-env-path discovery loop)."""
        with self._lock:
            self._read_names.add(name)

    def get_bool(self, name: str, *, default: Optional[bool] = None) -> bool:
        """Read ``name`` as bool. If registered, use its default when env
        is absent. If unregistered, ``default`` kwarg required."""
        spec = self.get_spec(name)
        self._record_read(name)
        raw = os.environ.get(name)
        if raw is None:
            if spec is not None and spec.type is FlagType.BOOL:
                return bool(spec.default)
            if default is not None:
                return default
            return False
        return raw.strip().lower() in ("1", "true", "yes", "on")

    def get_int(
        self, name: str, *,
        default: Optional[int] = None,
        minimum: Optional[int] = None,
    ) -> int:
        spec = self.get_spec(name)
        self._record_read(name)
        raw = os.environ.get(name)
        if raw is None:
            fallback = (
                spec.default if spec is not None and spec.type is FlagType.INT
                else default
            )
            if fallback is None:
                return 0
            out = int(fallback)
        else:
            try:
                out = int(raw)
            except (TypeError, ValueError):
                logger.warning(
                    "[FlagRegistry] %s malformed int %r; using default",
                    name, raw,
                )
                fallback = (
                    spec.default if spec is not None and spec.type is FlagType.INT
                    else default
                )
                out = int(fallback) if fallback is not None else 0
        if minimum is not None:
            out = max(minimum, out)
        return out

    def get_float(
        self, name: str, *,
        default: Optional[float] = None,
        minimum: Optional[float] = None,
    ) -> float:
        spec = self.get_spec(name)
        self._record_read(name)
        raw = os.environ.get(name)
        if raw is None:
            fallback = (
                spec.default if spec is not None and spec.type is FlagType.FLOAT
                else default
            )
            if fallback is None:
                return 0.0
            out = float(fallback)
        else:
            try:
                out = float(raw)
            except (TypeError, ValueError):
                logger.warning(
                    "[FlagRegistry] %s malformed float %r; using default",
                    name, raw,
                )
                fallback = (
                    spec.default if spec is not None and spec.type is FlagType.FLOAT
                    else default
                )
                out = float(fallback) if fallback is not None else 0.0
        if minimum is not None:
            out = max(minimum, out)
        return out

    def get_str(self, name: str, *, default: Optional[str] = None) -> str:
        spec = self.get_spec(name)
        self._record_read(name)
        raw = os.environ.get(name)
        if raw is not None:
            return raw
        if spec is not None and spec.type is FlagType.STR and spec.default is not None:
            return str(spec.default)
        return default if default is not None else ""

    def get_json(
        self, name: str, *,
        default: Optional[Any] = None,
    ) -> Any:
        spec = self.get_spec(name)
        self._record_read(name)
        raw = os.environ.get(name)
        if raw is None:
            return (
                spec.default if spec is not None and spec.type is FlagType.JSON
                else default
            )
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(
                "[FlagRegistry] %s malformed JSON; using default", name,
            )
            return (
                spec.default if spec is not None and spec.type is FlagType.JSON
                else default
            )

    # -- typo detection -----------------------------------------------------

    def suggest_similar(
        self, name: str, *, max_distance: Optional[int] = None, limit: int = 3,
    ) -> List[Tuple[str, int]]:
        """Return up to ``limit`` registered flag names with Levenshtein
        distance ≤ threshold to ``name``, sorted by distance ascending.
        """
        threshold = max_distance if max_distance is not None else typo_max_distance()
        results: List[Tuple[str, int]] = []
        with self._lock:
            for known in self._specs.keys():
                d = levenshtein_distance(name.upper(), known.upper())
                if d == 0:
                    continue
                if d <= threshold:
                    results.append((known, d))
        results.sort(key=lambda kv: (kv[1], kv[0]))
        return results[:max(1, int(limit))]

    def unregistered_env(self) -> List[Tuple[str, List[Tuple[str, int]]]]:
        """Scan os.environ for ``JARVIS_*`` vars not in the registry.
        Returns a list of ``(env_var_name, suggestions)`` tuples.

        Suggestions are the top-3 Levenshtein-closest registered flags.
        Empty suggestions list means the env var is unique — possibly a
        new flag awaiting registration, or a typo past the threshold."""
        with self._lock:
            registered = set(self._specs.keys())
        hits: List[Tuple[str, List[Tuple[str, int]]]] = []
        for key in sorted(os.environ.keys()):
            if not key.startswith("JARVIS_"):
                continue
            if key in registered:
                continue
            suggestions = self.suggest_similar(key)
            hits.append((key, suggestions))
        return hits

    def report_typos(self) -> List[Tuple[str, str, int]]:
        """Emit a WARNING log entry for every unregistered ``JARVIS_*``
        env var that has a Levenshtein match within threshold. Called
        once per unique typo per process. Returns the triples emitted.
        """
        emitted: List[Tuple[str, str, int]] = []
        if not typo_warn_enabled():
            return emitted
        for env_name, suggestions in self.unregistered_env():
            if not suggestions:
                continue
            if env_name in self._reported_typos:
                continue
            with self._lock:
                self._reported_typos.add(env_name)
            top_name, top_d = suggestions[0]
            logger.warning(
                "[FlagRegistry] Possible typo: %s not registered; "
                "closest match is %s (Levenshtein distance %d)",
                env_name, top_name, top_d,
            )
            emitted.append((env_name, top_name, top_d))
            # Best-effort SSE publish — lazy import keeps this module
            # authority-free of the stream layer at import time.
            try:
                from backend.core.ouroboros.governance.ide_observability_stream import (
                    publish_flag_typo_event,
                )
                publish_flag_typo_event(env_name, top_name, top_d)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[FlagRegistry] SSE typo publish failed", exc_info=True,
                )
        return emitted

    # -- diagnostics / export -----------------------------------------------

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            by_category: Dict[str, int] = {}
            by_type: Dict[str, int] = {}
            for s in self._specs.values():
                by_category[s.category.value] = by_category.get(s.category.value, 0) + 1
                by_type[s.type.value] = by_type.get(s.type.value, 0) + 1
            return {
                "schema_version": FLAG_REGISTRY_SCHEMA_VERSION,
                "total": len(self._specs),
                "by_category": by_category,
                "by_type": by_type,
                "read_count": len(self._read_names),
                "reported_typos": len(self._reported_typos),
            }

    def to_json(self) -> str:
        with self._lock:
            payload = {
                "schema_version": FLAG_REGISTRY_SCHEMA_VERSION,
                "total": len(self._specs),
                "flags": [s.to_dict() for s in sorted(
                    self._specs.values(), key=lambda x: x.name,
                )],
            }
        return json.dumps(payload, indent=2, sort_keys=True)

    def clear(self) -> None:
        """Test helper — reset in-memory state."""
        with self._lock:
            self._specs.clear()
            self._read_names.clear()
            self._reported_typos.clear()


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


_default_registry: Optional[FlagRegistry] = None
_default_lock = threading.Lock()
_seed_applied = False


def get_default_registry() -> FlagRegistry:
    global _default_registry
    with _default_lock:
        if _default_registry is None:
            _default_registry = FlagRegistry()
        return _default_registry


def reset_default_registry() -> None:
    """Test helper — clear singleton."""
    global _default_registry, _seed_applied
    with _default_lock:
        _default_registry = None
        _seed_applied = False


def ensure_seeded() -> FlagRegistry:
    """Install the seed registrations on the default registry if they
    haven't been yet. Idempotent — safe to call many times. Seed module
    is imported lazily so the registry primitive stays authority-free
    and decoupled from the seed data."""
    global _seed_applied
    registry = get_default_registry()
    with _default_lock:
        if _seed_applied:
            return registry
        _seed_applied = True
    try:
        from backend.core.ouroboros.governance.flag_registry_seed import (
            seed_default_registry,
        )
        seed_default_registry(registry)
    except ImportError:
        logger.debug(
            "[FlagRegistry] seed module unavailable; registry starts empty",
        )
    return registry


__all__ = [
    "Category",
    "FLAG_REGISTRY_SCHEMA_VERSION",
    "FlagRegistry",
    "FlagSpec",
    "FlagType",
    "Relevance",
    "ensure_seeded",
    "get_default_registry",
    "is_enabled",
    "levenshtein_distance",
    "reset_default_registry",
    "typo_max_distance",
    "typo_warn_enabled",
]
