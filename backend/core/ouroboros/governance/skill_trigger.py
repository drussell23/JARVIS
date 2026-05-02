"""SkillTrigger -- Slice 1 of the SkillRegistry-AutonomousReach arc.
====================================================================

Pure-stdlib decision primitive that lets the existing :class:`SkillCatalog`
fire skills autonomously on signal preconditions (posture transitions,
drift signals, sensor fires) -- the proactive surplus over Claude
Code's reactive Skills surface.

This module is INTENTIONALLY decoupled from
:mod:`skill_manifest` and :mod:`skill_catalog` -- it imports from
neither, so the existing arc keeps its current import graph and the
new types compose by reference rather than inheritance. The
``compute_should_fire`` function accepts ``SkillManifest`` instances
via duck-typing (``getattr`` over the additive fields) so the
existing arc can adopt the new surface incrementally.

Reverse-Russian-Doll posture
----------------------------

* O+V (the inner doll, the builder) gains a structured trigger
  vocabulary so its proactive observers can reach for skills the
  way CC's reactive UX reaches for them via human invocation.
* Antivenom (the constraint, the immune system) scales
  proportionally:
    - Every taxonomy is closed-5 (J.A.R.M.A.T.R.I.X.).
    - Every dataclass is frozen.
    - The decision function is total + NEVER raises.
    - Strict dialect validators reject unknown reach values + malformed
      trigger specs at parse time -- not at fire time -- so a
      misconfigured manifest fails LOUDLY when an operator installs
      it, not silently three weeks later when the signal arrives.
    - Pure-stdlib at hot path: zero governance imports outside the
      module-owned ``register_flags`` / ``register_shipped_invariants``
      contract (Slice 5).

Closed taxonomies (5 values each)
---------------------------------

* :class:`SkillReach` -- which surfaces a skill is reachable from.
  ``OPERATOR_PLUS_MODEL`` is the CC-equivalent default; ``ANY``
  adds autonomous fires; ``AUTONOMOUS`` is the "background only"
  reach for skills that should never be human-typed.
* :class:`SkillTriggerKind` -- what kind of signal produced (or
  might produce) an invocation. ``DISABLED`` is the muted state
  -- spec is registered but the kind never matches.
* :class:`SkillOutcome` -- the verdict
  :func:`compute_should_fire` returns. ``SKIPPED_PRECONDITION``
  (trigger / posture / payload mismatch) is distinct from
  ``SKIPPED_DISABLED`` (master flag off / reach excludes the
  invocation kind) so the observer can dedup on the right axis.

Phase C tightening contract
---------------------------

``INVOKED`` stamps the canonical
``MonotonicTighteningVerdict.PASSED`` literal -- autonomous skill
firing IS structural tightening (acts on observed evidence, not
speculation). All other outcomes stamp empty.
"""
from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple

logger = logging.getLogger("Ouroboros.SkillTrigger")


SKILL_TRIGGER_SCHEMA_VERSION: str = "skill_trigger.v1"

# Phase C MonotonicTighteningVerdict.PASSED canonical string -- mirrors
# the literal used by Move 6 / Priority #1-#5 closures so operators can
# correlate skill-fire events with broader tightening telemetry via
# shared vocabulary.
_TIGHTENING_PASSED_STR: str = "passed"


# ---------------------------------------------------------------------------
# Master flag + env knobs
# ---------------------------------------------------------------------------


def skill_trigger_enabled() -> bool:
    """``JARVIS_SKILL_TRIGGER_ENABLED`` (default ``false`` until
    Slice 5 graduation).

    Asymmetric env semantics -- empty/whitespace = unset = current
    default; explicit ``0``/``false``/``no``/``off`` evaluates
    false; explicit ``1``/``true``/``yes``/``on`` evaluates true.
    Re-read on every call so flag flips hot-revert without
    restart.

    Default stays off through Slices 1-4 because graduating before
    the autonomous observer (Slice 3) is wired would expose the
    decision surface to the existing SkillCatalog without anyone
    listening -- operator-confusing.
    """
    raw = os.environ.get("JARVIS_SKILL_TRIGGER_ENABLED", "")
    raw = raw.strip().lower()
    if raw == "":
        return False  # pre-graduation default
    return raw in ("1", "true", "yes", "on")


def _env_int_clamped(
    name: str, *, default: int, floor: int, ceiling: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        n = int(raw) if raw else default
    except ValueError:
        n = default
    return max(floor, min(ceiling, n))


def _env_float_clamped(
    name: str, *, default: float, floor: float, ceiling: float,
) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        n = float(raw) if raw else default
    except ValueError:
        n = default
    return max(floor, min(ceiling, n))


def skill_per_window_max_invocations() -> int:
    """``JARVIS_SKILL_PER_WINDOW_MAX`` (default 5, floor 1,
    ceiling 100).

    Per-skill rate limit consumed by the Slice 3 observer.
    Independent of the master flag; helper still returns the
    clamped value when master is off so observability consumers
    see consistent numbers.
    """
    return _env_int_clamped(
        "JARVIS_SKILL_PER_WINDOW_MAX",
        default=5, floor=1, ceiling=100,
    )


def skill_window_default_s() -> float:
    """``JARVIS_SKILL_WINDOW_S`` (default 60.0, floor 1.0,
    ceiling 3600.0).

    Default rate-limit window for trigger specs that don't set
    their own ``window_s`` override.
    """
    return _env_float_clamped(
        "JARVIS_SKILL_WINDOW_S",
        default=60.0, floor=1.0, ceiling=3600.0,
    )


# ---------------------------------------------------------------------------
# Closed-5-value taxonomies (J.A.R.M.A.T.R.I.X.)
# ---------------------------------------------------------------------------


class SkillReach(str, enum.Enum):
    """Which surfaces a skill is reachable from. Closed taxonomy.

    * ``OPERATOR`` -- typed via ``/skills run <qname>`` only.
    * ``MODEL`` -- exposed in Venom's tool surface only.
    * ``AUTONOMOUS`` -- fired by SkillObserver on signal
      preconditions only (the proactive surplus over CC).
    * ``OPERATOR_PLUS_MODEL`` -- CC-equivalent reach (operator +
      model, no autonomous). Default for backward-compat with
      pre-arc SkillManifest instances.
    * ``ANY`` -- all three reaches.
    """

    OPERATOR = "operator"
    MODEL = "model"
    AUTONOMOUS = "autonomous"
    OPERATOR_PLUS_MODEL = "operator_plus_model"
    ANY = "any"


class SkillTriggerKind(str, enum.Enum):
    """What kind of trigger produced (or might produce) an
    invocation. Closed taxonomy.

    * ``POSTURE_TRANSITION`` -- DirectionInferrer posture changed.
    * ``DRIFT_DETECTED`` -- Coherence Auditor / InvariantDriftAuditor
      / CIGW emitted a drift signal.
    * ``SENSOR_FIRED`` -- one of the 16 sensors emitted an
      IntentSignal.
    * ``EXPLICIT_INVOCATION`` -- operator typed ``/skills run`` OR
      model reached for the tool. Both fold to one kind because
      from the firing decision's perspective they're identical.
    * ``DISABLED`` -- the spec is muted (registered but never
      matches).
    """

    POSTURE_TRANSITION = "posture_transition"
    DRIFT_DETECTED = "drift_detected"
    SENSOR_FIRED = "sensor_fired"
    EXPLICIT_INVOCATION = "explicit_invocation"
    DISABLED = "disabled"


class SkillOutcome(str, enum.Enum):
    """Decision reached by :func:`compute_should_fire`. Closed.

    * ``INVOKED`` -- skill should fire. Stamps Phase C tightening.
    * ``SKIPPED_PRECONDITION`` -- trigger / posture / payload
      mismatch (no spec in manifest matched the invocation).
    * ``SKIPPED_DISABLED`` -- master flag off OR manifest's reach
      excludes the kind we tried to invoke from.
    * ``DENIED_POLICY`` -- risk-floor blocked the skill (e.g.,
      floor is BLOCKED).
    * ``FAILED`` -- defensive degradation; null inputs / unexpected
      taxonomy values. Last-resort guard so callers can branch.
    """

    INVOKED = "invoked"
    SKIPPED_PRECONDITION = "skipped_precondition"
    SKIPPED_DISABLED = "skipped_disabled"
    DENIED_POLICY = "denied_policy"
    FAILED = "failed"


# Closed-set quick-lookups (consumed by the Slice 5 AST validator
# + the strict dialect validator below).
VALID_REACHES: FrozenSet[str] = frozenset(r.value for r in SkillReach)
VALID_TRIGGER_KINDS: FrozenSet[str] = frozenset(
    k.value for k in SkillTriggerKind
)
VALID_OUTCOMES: FrozenSet[str] = frozenset(
    o.value for o in SkillOutcome
)


# ---------------------------------------------------------------------------
# Frozen dataclasses (the typed vocabulary)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillTriggerSpec:
    """Declarative precondition for autonomous invocation.

    Structured fields (no regex / eval / dynamic code) so AST pins
    can validate trigger specs at register-time. Empty string in a
    structural field means "match any" for that dimension.

    ``signal_pattern`` is the event-bus channel pattern the Slice 3
    observer subscribes to (e.g., ``"posture.changed"`` or
    ``"sensor.fired.test_failure"``). Pattern matching is done at
    subscribe-time by the observer; this struct just holds the
    declaration.
    """

    kind: SkillTriggerKind
    signal_pattern: str = ""
    required_posture: str = ""        # empty = any
    required_drift_kind: str = ""     # empty = any
    required_sensor_name: str = ""    # empty = any
    max_invocations: int = 0          # 0 = use env default
    window_s: float = 0.0             # 0.0 = use env default
    dedup_key_template: str = ""      # empty = structural fingerprint
    schema_version: str = SKILL_TRIGGER_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind.value,
            "signal_pattern": self.signal_pattern,
            "required_posture": self.required_posture,
            "required_drift_kind": self.required_drift_kind,
            "required_sensor_name": self.required_sensor_name,
            "max_invocations": self.max_invocations,
            "window_s": self.window_s,
            "dedup_key_template": self.dedup_key_template,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class SkillInvocation:
    """One firing attempt -- pre-decision payload.

    ``triggered_by_signal`` is the event-bus signal name when the
    invocation came from the autonomous observer; for explicit
    operator or model invocations it's the empty string.

    ``caller_op_id`` is set when the model reached for the skill
    during a GENERATE round. Empty for autonomous + operator
    invocations.

    ``payload`` is the typed signal data the trigger evaluates
    against (e.g., ``{"posture": "HARDEN", "previous": "EXPLORE"}``
    for a POSTURE_TRANSITION trigger). Free-form Mapping; the
    decision function only inspects the structured fields the
    trigger spec declares.
    """

    skill_name: str
    triggered_by_kind: SkillTriggerKind
    triggered_by_signal: str = ""
    triggered_at_monotonic: float = 0.0
    arguments: Mapping[str, Any] = field(default_factory=dict)
    payload: Mapping[str, Any] = field(default_factory=dict)
    caller_op_id: str = ""
    schema_version: str = SKILL_TRIGGER_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "triggered_by_kind": self.triggered_by_kind.value,
            "triggered_by_signal": self.triggered_by_signal,
            "triggered_at_monotonic": self.triggered_at_monotonic,
            "arguments": dict(self.arguments),
            "payload": dict(self.payload),
            "caller_op_id": self.caller_op_id,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class SkillResult:
    """Total verdict from :func:`compute_should_fire`.

    ``matched_trigger_index`` points to the spec in
    ``manifest.trigger_specs`` that matched the invocation (or
    ``None`` for non-INVOKED outcomes / EXPLICIT_INVOCATION which
    matches without a spec).

    ``monotonic_tightening_verdict`` is empty for everything
    except INVOKED -- aligns with the Phase C tightening contract
    used by Move 6 + Priority #1-#5.
    """

    outcome: SkillOutcome
    skill_name: str
    reason: str = ""
    matched_trigger_index: Optional[int] = None
    monotonic_tightening_verdict: str = ""
    schema_version: str = SKILL_TRIGGER_SCHEMA_VERSION

    @property
    def is_invoked(self) -> bool:
        return self.outcome is SkillOutcome.INVOKED

    @property
    def is_tightening(self) -> bool:
        return self.outcome is SkillOutcome.INVOKED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "skill_name": self.skill_name,
            "reason": self.reason,
            "matched_trigger_index": self.matched_trigger_index,
            "monotonic_tightening_verdict": (
                self.monotonic_tightening_verdict
            ),
            "schema_version": self.schema_version,
        }


# ---------------------------------------------------------------------------
# Strict dialect validators (used by SkillManifest.from_mapping
# in Slice 1 to fail loudly on malformed trigger spec)
# ---------------------------------------------------------------------------


class SkillTriggerError(ValueError):
    """Raised when a trigger spec or reach value fails strict
    dialect validation. Mirrors :class:`SkillManifestError`'s
    role -- fail at parse time, not at fire time."""


# Allowed top-level keys in a trigger-spec dict. Anything outside
# this set is rejected per the existing dialect-narrow discipline
# (see ``_validate_arg_schema`` in ``skill_manifest.py``).
_ALLOWED_TRIGGER_KEYS: FrozenSet[str] = frozenset({
    "kind",
    "signal_pattern",
    "required_posture",
    "required_drift_kind",
    "required_sensor_name",
    "max_invocations",
    "window_s",
    "dedup_key_template",
})


def parse_reach(value: Any) -> SkillReach:
    """Parse a raw reach value (from YAML / dict) into the typed
    enum. Raises :class:`SkillTriggerError` on unknown values --
    fail loudly per the strict dialect.
    """
    if isinstance(value, SkillReach):
        return value
    if not isinstance(value, str) or not value.strip():
        raise SkillTriggerError(
            f"reach must be a non-empty string; got {value!r}"
        )
    normalized = value.strip().lower()
    if normalized not in VALID_REACHES:
        raise SkillTriggerError(
            f"unknown reach {value!r} "
            f"(allowed: {sorted(VALID_REACHES)})"
        )
    return SkillReach(normalized)


def parse_trigger_kind(value: Any) -> SkillTriggerKind:
    """Parse a raw kind value into the typed enum. Raises
    :class:`SkillTriggerError` on unknown values."""
    if isinstance(value, SkillTriggerKind):
        return value
    if not isinstance(value, str) or not value.strip():
        raise SkillTriggerError(
            f"trigger kind must be a non-empty string; got {value!r}"
        )
    normalized = value.strip().lower()
    if normalized not in VALID_TRIGGER_KINDS:
        raise SkillTriggerError(
            f"unknown trigger kind {value!r} "
            f"(allowed: {sorted(VALID_TRIGGER_KINDS)})"
        )
    return SkillTriggerKind(normalized)


def parse_trigger_spec_mapping(
    data: Mapping[str, Any], *, path: str = "trigger_spec",
) -> SkillTriggerSpec:
    """Validate + build one :class:`SkillTriggerSpec` from a
    mapping. Raises :class:`SkillTriggerError` on any malformed
    shape -- the dialect is intentionally narrow (see
    ``_ALLOWED_TRIGGER_KEYS``).

    Numeric fields (``max_invocations`` / ``window_s``) are NOT
    clamped here -- clamping is the observer's responsibility at
    runtime via the env-knob helpers, so operators can declare
    higher-than-default windows (subject to ceiling at fire time).
    """
    if not isinstance(data, Mapping):
        raise SkillTriggerError(
            f"{path} must be a mapping; got {type(data).__name__}"
        )
    unknown = set(data.keys()) - _ALLOWED_TRIGGER_KEYS
    if unknown:
        raise SkillTriggerError(
            f"{path}: unknown key(s) {sorted(unknown)} "
            f"(allowed: {sorted(_ALLOWED_TRIGGER_KEYS)})"
        )
    if "kind" not in data:
        raise SkillTriggerError(
            f"{path}: missing required field 'kind'"
        )
    try:
        kind = parse_trigger_kind(data["kind"])
    except SkillTriggerError as exc:
        # Re-raise with the spec path threaded in so list-element
        # errors carry the index (e.g., "trigger_specs[1].kind: ...").
        raise SkillTriggerError(f"{path}.kind: {exc}") from exc

    def _opt_str(key: str, default: str = "") -> str:
        v = data.get(key, default)
        if v is None:
            return default
        if not isinstance(v, str):
            raise SkillTriggerError(
                f"{path}.{key} must be a string if present; "
                f"got {type(v).__name__}"
            )
        return v.strip()

    def _opt_nonneg_int(key: str) -> int:
        v = data.get(key, 0)
        if v is None:
            return 0
        # bool is a subclass of int -- exclude
        if isinstance(v, bool):
            raise SkillTriggerError(
                f"{path}.{key} must be an integer; got bool"
            )
        if not isinstance(v, int):
            raise SkillTriggerError(
                f"{path}.{key} must be an integer; got "
                f"{type(v).__name__}"
            )
        if v < 0:
            raise SkillTriggerError(
                f"{path}.{key} must be >= 0; got {v}"
            )
        return v

    def _opt_nonneg_float(key: str) -> float:
        v = data.get(key, 0.0)
        if v is None:
            return 0.0
        if isinstance(v, bool):
            raise SkillTriggerError(
                f"{path}.{key} must be a number; got bool"
            )
        if not isinstance(v, (int, float)):
            raise SkillTriggerError(
                f"{path}.{key} must be a number; got "
                f"{type(v).__name__}"
            )
        f = float(v)
        if f < 0.0:
            raise SkillTriggerError(
                f"{path}.{key} must be >= 0.0; got {f}"
            )
        return f

    return SkillTriggerSpec(
        kind=kind,
        signal_pattern=_opt_str("signal_pattern"),
        required_posture=_opt_str("required_posture"),
        required_drift_kind=_opt_str("required_drift_kind"),
        required_sensor_name=_opt_str("required_sensor_name"),
        max_invocations=_opt_nonneg_int("max_invocations"),
        window_s=_opt_nonneg_float("window_s"),
        dedup_key_template=_opt_str("dedup_key_template"),
    )


def parse_trigger_specs_list(
    data: Any, *, path: str = "trigger_specs",
) -> Tuple[SkillTriggerSpec, ...]:
    """Parse a list of trigger spec dicts into a frozen tuple.
    Raises :class:`SkillTriggerError` on malformed top-level
    shape or any element."""
    if data is None:
        return ()
    if not isinstance(data, (list, tuple)):
        raise SkillTriggerError(
            f"{path} must be a list; got {type(data).__name__}"
        )
    out: List[SkillTriggerSpec] = []
    for idx, entry in enumerate(data):
        spec = parse_trigger_spec_mapping(
            entry, path=f"{path}[{idx}]",
        )
        out.append(spec)
    return tuple(out)


# ---------------------------------------------------------------------------
# Reach helpers
# ---------------------------------------------------------------------------


def reach_includes(reach: SkillReach, target: SkillReach) -> bool:
    """Return True when ``reach`` covers ``target``.

    * ``ANY`` covers every target.
    * ``OPERATOR_PLUS_MODEL`` covers OPERATOR or MODEL.
    * Everything else covers only itself.

    NEVER raises -- garbage input returns False.
    """
    try:
        if not isinstance(reach, SkillReach):
            return False
        if not isinstance(target, SkillReach):
            return False
        if reach is SkillReach.ANY:
            return True
        if reach is SkillReach.OPERATOR_PLUS_MODEL:
            # OPERATOR_PLUS_MODEL is a set; it contains itself plus
            # the singleton reaches it composes. Excludes AUTONOMOUS
            # (the proactive surplus -- distinct surface) and ANY
            # (a strict superset).
            return target in (
                SkillReach.OPERATOR,
                SkillReach.MODEL,
                SkillReach.OPERATOR_PLUS_MODEL,
            )
        return reach is target
    except Exception:  # noqa: BLE001 -- defensive
        return False


# ---------------------------------------------------------------------------
# Internal precondition matching
# ---------------------------------------------------------------------------


def _trigger_kind_to_required_reach(
    kind: SkillTriggerKind,
) -> SkillReach:
    """Map a trigger kind to the reach it implies. Used to
    short-circuit when a manifest's reach excludes the invocation
    kind (e.g., MODEL-only manifest invoked by AUTONOMOUS observer
    -> SKIPPED_DISABLED).

    ``EXPLICIT_INVOCATION`` resolves to OPERATOR_PLUS_MODEL because
    the same kind covers both operator and model invocations -- the
    manifest's reach is what decides.
    """
    if kind is SkillTriggerKind.EXPLICIT_INVOCATION:
        return SkillReach.OPERATOR_PLUS_MODEL
    return SkillReach.AUTONOMOUS


def _spec_matches_invocation(
    spec: SkillTriggerSpec,
    invocation: SkillInvocation,
) -> bool:
    """Structured precondition match. NEVER raises."""
    try:
        if not isinstance(spec, SkillTriggerSpec):
            return False
        if not isinstance(invocation, SkillInvocation):
            return False
        if spec.kind is SkillTriggerKind.DISABLED:
            return False
        if spec.kind is not invocation.triggered_by_kind:
            return False
        payload = invocation.payload or {}
        if spec.kind is SkillTriggerKind.POSTURE_TRANSITION:
            req = (spec.required_posture or "").strip()
            if req:
                got = str(payload.get("posture", "")).strip()
                if got != req:
                    return False
        elif spec.kind is SkillTriggerKind.DRIFT_DETECTED:
            req = (spec.required_drift_kind or "").strip()
            if req:
                got = str(payload.get("drift_kind", "")).strip()
                if got != req:
                    return False
        elif spec.kind is SkillTriggerKind.SENSOR_FIRED:
            req = (spec.required_sensor_name or "").strip()
            if req:
                got = str(payload.get("sensor_name", "")).strip()
                if got != req:
                    return False
        # EXPLICIT_INVOCATION matches without payload narrowing --
        # operator + model carry their intent in the call itself.
        return True
    except Exception:  # noqa: BLE001 -- defensive
        return False


# ---------------------------------------------------------------------------
# Total decision function -- the load-bearing piece
# ---------------------------------------------------------------------------


def compute_should_fire(
    manifest: Any,
    invocation: SkillInvocation,
    *,
    posture: str = "",
    risk_floor: str = "",
    enabled: Optional[bool] = None,
) -> SkillResult:
    """Total decision -- maps every (manifest, invocation,
    posture, risk_floor, enabled) tuple to exactly one
    :class:`SkillResult`. NEVER raises.

    ``manifest`` is duck-typed (we read ``name``, ``reach``,
    ``trigger_specs``, ``risk_class`` via ``getattr``) so this
    module stays decoupled from :mod:`skill_manifest`. The
    Slice 1 additive extension to :class:`SkillManifest` adds the
    ``reach`` + ``trigger_specs`` fields with safe defaults.

    Decision tree (deterministic, no heuristics, no regex):
      1. ``enabled=False`` (or master flag off when None) ->
         SKIPPED_DISABLED.
      2. Garbage / type-incompatible inputs -> FAILED.
      3. Manifest reach excludes the invocation kind's required
         reach -> SKIPPED_DISABLED.
      4. risk_floor explicitly ``"blocked"`` OR
         manifest.risk_class explicitly ``"blocked"`` ->
         DENIED_POLICY.
      5. EXPLICIT_INVOCATION + reach permits + zero specs ->
         INVOKED (operator/model can always invoke an exposed skill).
      6. Walk ``manifest.trigger_specs`` in order. First spec
         where :func:`_spec_matches_invocation` returns True wins.
      7. No matching spec -> SKIPPED_PRECONDITION.
      8. Matching spec -> INVOKED with Phase C tightening stamped.

    The ``posture`` arg is reserved for future per-skill posture
    gating (today's matching is via ``spec.required_posture``).
    """
    try:
        # 1. Master flag short-circuit.
        is_enabled = (
            enabled if enabled is not None
            else skill_trigger_enabled()
        )
        if not is_enabled:
            return SkillResult(
                outcome=SkillOutcome.SKIPPED_DISABLED,
                skill_name=getattr(manifest, "name", "") or "",
                reason="master flag disabled",
            )

        # 2. Defensive type guards. last-resort -> FAILED.
        if manifest is None:
            return SkillResult(
                outcome=SkillOutcome.FAILED,
                skill_name="",
                reason="manifest is None",
            )
        if not isinstance(invocation, SkillInvocation):
            return SkillResult(
                outcome=SkillOutcome.FAILED,
                skill_name=str(getattr(manifest, "name", "") or ""),
                reason="invocation not a SkillInvocation instance",
            )
        # Manifest must expose .name + .reach (additive Slice 1
        # extension); duck-typed via getattr.
        manifest_name = getattr(manifest, "name", None)
        if not isinstance(manifest_name, str) or not manifest_name:
            return SkillResult(
                outcome=SkillOutcome.FAILED,
                skill_name="",
                reason="manifest.name missing or not a string",
            )
        manifest_reach = getattr(manifest, "reach", None)
        if not isinstance(manifest_reach, SkillReach):
            return SkillResult(
                outcome=SkillOutcome.FAILED,
                skill_name=manifest_name,
                reason=(
                    "manifest.reach missing or not a SkillReach value"
                ),
            )
        if not isinstance(
            invocation.triggered_by_kind, SkillTriggerKind,
        ):
            return SkillResult(
                outcome=SkillOutcome.FAILED,
                skill_name=manifest_name,
                reason="invocation.triggered_by_kind not valid",
            )

        # 3. Reach gate -- the manifest must permit the kind that
        # tried to invoke.
        required_reach = _trigger_kind_to_required_reach(
            invocation.triggered_by_kind,
        )
        if not reach_includes(manifest_reach, required_reach):
            return SkillResult(
                outcome=SkillOutcome.SKIPPED_DISABLED,
                skill_name=manifest_name,
                reason=(
                    f"manifest.reach={manifest_reach.value!r} excludes "
                    f"required_reach={required_reach.value!r}"
                ),
            )

        # 4. Risk-floor gate. BLOCKED on either side denies.
        floor_str = (risk_floor or "").strip().lower()
        skill_risk = str(
            getattr(manifest, "risk_class", "") or "",
        ).strip().lower()
        if floor_str == "blocked" or skill_risk == "blocked":
            return SkillResult(
                outcome=SkillOutcome.DENIED_POLICY,
                skill_name=manifest_name,
                reason=(
                    f"risk gate blocked "
                    f"(floor={floor_str!r}, "
                    f"skill_risk={skill_risk!r})"
                ),
            )

        # 5/6/7. Walk trigger specs (additive Slice 1 field).
        try:
            specs = tuple(getattr(manifest, "trigger_specs", ()) or ())
        except Exception:  # noqa: BLE001
            specs = ()

        # 5. EXPLICIT_INVOCATION special case -- even with zero
        # specs, an operator/model can invoke an exposed skill.
        if (
            invocation.triggered_by_kind
            is SkillTriggerKind.EXPLICIT_INVOCATION
        ):
            # Walk specs first in case operator declared an explicit
            # filter; otherwise fall through to the no-spec INVOKED.
            for idx, spec in enumerate(specs):
                if _spec_matches_invocation(spec, invocation):
                    return SkillResult(
                        outcome=SkillOutcome.INVOKED,
                        skill_name=manifest_name,
                        reason=(
                            f"explicit invocation matched spec {idx}"
                        ),
                        matched_trigger_index=idx,
                        monotonic_tightening_verdict=(
                            _TIGHTENING_PASSED_STR
                        ),
                    )
            return SkillResult(
                outcome=SkillOutcome.INVOKED,
                skill_name=manifest_name,
                reason="explicit invocation -- reach permits",
                matched_trigger_index=None,
                monotonic_tightening_verdict=_TIGHTENING_PASSED_STR,
            )

        # 6. Autonomous path -- match required.
        for idx, spec in enumerate(specs):
            if _spec_matches_invocation(spec, invocation):
                return SkillResult(
                    outcome=SkillOutcome.INVOKED,
                    skill_name=manifest_name,
                    reason=f"trigger spec {idx} matched",
                    matched_trigger_index=idx,
                    monotonic_tightening_verdict=(
                        _TIGHTENING_PASSED_STR
                    ),
                )

        # 7. No match -> SKIPPED_PRECONDITION.
        return SkillResult(
            outcome=SkillOutcome.SKIPPED_PRECONDITION,
            skill_name=manifest_name,
            reason="no trigger spec matched the invocation",
        )
    except Exception as exc:  # noqa: BLE001 -- last-resort defensive
        logger.warning(
            "[SkillTrigger] compute_should_fire last-resort "
            "degraded: %s", exc,
        )
        return SkillResult(
            outcome=SkillOutcome.FAILED,
            skill_name=str(getattr(manifest, "name", "") or ""),
            reason=f"defensive fallthrough: {type(exc).__name__}",
        )


# ---------------------------------------------------------------------------
# Dedup-key helper -- consumed by Slice 3 observer rate limiting
# ---------------------------------------------------------------------------


def compute_dedup_key(
    invocation: SkillInvocation,
    spec: Optional[SkillTriggerSpec] = None,
) -> str:
    """Render a stable dedup key for an invocation. NEVER raises.

    When ``spec`` is provided AND has a non-empty
    ``dedup_key_template``, substitutes ``{posture}`` /
    ``{drift_kind}`` / ``{sensor_name}`` / ``{kind}`` /
    ``{signal}`` / ``{skill_name}`` from the invocation payload.

    When no template (or no spec), falls back to a structural
    fingerprint of (skill_name, kind, signal, sorted payload
    items).
    """
    try:
        if not isinstance(invocation, SkillInvocation):
            return ""
        if (
            isinstance(spec, SkillTriggerSpec)
            and spec.dedup_key_template
        ):
            payload = invocation.payload or {}
            substitutions = {
                "{posture}": str(payload.get("posture", "")),
                "{drift_kind}": str(payload.get("drift_kind", "")),
                "{sensor_name}": str(payload.get("sensor_name", "")),
                "{kind}": invocation.triggered_by_kind.value,
                "{signal}": invocation.triggered_by_signal,
                "{skill_name}": invocation.skill_name,
            }
            out = spec.dedup_key_template
            for k, v in substitutions.items():
                out = out.replace(k, v)
            return out
        try:
            payload_items = sorted(
                (str(k), str(v))
                for k, v in (invocation.payload or {}).items()
            )
        except Exception:  # noqa: BLE001
            payload_items = []
        parts = [
            invocation.skill_name,
            invocation.triggered_by_kind.value,
            invocation.triggered_by_signal,
        ]
        for k, v in payload_items:
            parts.append(f"{k}={v}")
        return "|".join(parts)
    except Exception as exc:  # noqa: BLE001 -- defensive
        logger.debug(
            "[SkillTrigger] compute_dedup_key degraded: %s", exc,
        )
        return ""


__all__ = [
    "SKILL_TRIGGER_SCHEMA_VERSION",
    "SkillInvocation",
    "SkillOutcome",
    "SkillReach",
    "SkillResult",
    "SkillTriggerError",
    "SkillTriggerKind",
    "SkillTriggerSpec",
    "VALID_OUTCOMES",
    "VALID_REACHES",
    "VALID_TRIGGER_KINDS",
    "compute_dedup_key",
    "compute_should_fire",
    "parse_reach",
    "parse_trigger_kind",
    "parse_trigger_spec_mapping",
    "parse_trigger_specs_list",
    "reach_includes",
    "skill_per_window_max_invocations",
    "skill_trigger_enabled",
    "skill_window_default_s",
]
