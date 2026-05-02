"""InlinePromptGate Slice 1 — phase-boundary primitive bridge.

This is the primitive layer that bridges the orchestrator's
phase-boundary decision points (NOTIFY_APPLY at GATE→APPLY, future
APPROVAL_REQUIRED at APPLY→VERIFY) to the existing
``InlinePromptController`` Future-registry substrate.

Architectural insight
---------------------

The substrate already exists in ``inline_permission_prompt.py``:

* ``InlinePromptController`` — Future-backed registry, 4 operator
  actions (allow_once / allow_always / deny / pause_op),
  timeout→EXPIRED, listener pattern, capacity limits, bounded
  history, singleton.
* 5 SSE event constants in ``ide_observability_stream.py``
  (``EVENT_TYPE_INLINE_PROMPT_{PENDING,ALLOWED,DENIED,EXPIRED,
  PAUSED}``) defined but never produced.

What it lacks is a **phase-boundary** producer. The existing
``InlinePromptRequest`` shape is tool-call-shaped (``tool``,
``arg_fingerprint``, ``arg_preview``, ``target_path``, ``verdict:
InlineGateVerdict``) — wrong shape for an orchestrator-level "the
whole op is about to APPLY a multi-file diff; confirm?" prompt.

This Slice 1 ships:

* :class:`PhaseInlineVerdict` — closed 5-value taxonomy
  (ALLOW / DENY / PAUSE_OP / EXPIRED / DISABLED).
* :class:`PhaseInlinePromptRequest` — frozen orchestrator-shaped
  request (op_id + phase + risk_tier + change summary +
  change_fingerprint + target_paths + rationale + route + timeouts).
* :class:`PhaseInlinePromptVerdict` — frozen verdict with Phase C
  ``MonotonicTighteningVerdict.PASSED`` stamping by canonical
  string (Slice 4's auto_action_router bridge will validate
  string-parity to the live enum).
* :func:`compute_phase_inline_verdict` — total mapping from
  controller terminal-state string → ``PhaseInlineVerdict``.
  NEVER raises.
* :func:`derive_prompt_id` — deterministic idempotent prompt-id
  derivation from ``op_id`` + ``change_fingerprint`` + phase +
  schema version.

Direct-solve principles (per the operator directive, mirrors
Priority #3 / Move 5+6 / Priority #1+#2 Slice 1):

* **Asynchronous-ready** — frozen dataclasses propagate cleanly
  across async boundaries; Slice 2's orchestrator producer will
  await the Future via the controller, not via this module.
* **Dynamic** — every numeric env-knob (timeout_s, summary
  truncation, fingerprint length) clamped floor + ceiling.
  No hardcoded magic constants.
* **Adaptive** — degraded inputs (missing state, garbage state,
  None/empty) all map to explicit closed-taxonomy values rather
  than raises. Defensive-degrade per §7 (fail-closed → DISABLED
  treats as if the prompt never reached an operator, which means
  the orchestrator falls through to its current behavior — the
  backward-compatible safe path).
* **Intelligent** — Phase C tightening stamping is outcome-aware:
  DENY/PAUSE_OP stamp PASSED (operator inserted friction);
  ALLOW/EXPIRED/DISABLED stamp empty (no tightening signal —
  ALLOW is operator-confirmed continuation, EXPIRED/DISABLED
  fall through to current behavior).
* **Robust** — every public function NEVER raises out. Garbage
  input → DISABLED + WARNING log rather than exception. Pure-data
  primitive can be called from any context, sync or async.
* **No hardcoding** — 5-value closed taxonomy; per-knob env
  helpers with floor + ceiling clamps. State-string constants
  mirror the live controller's exports, verified by byte-parity
  test (Slice 4's pin-test suite).
* **Phase boundary INSERTS a check** — the prompt is by-construction
  a tightening (operator confirmation gate that wasn't there
  before). The taxonomy reflects that: DENY/PAUSE_OP are explicit
  tightenings; ALLOW is a NEUTRAL operator-confirmed continuation
  (not a tightening — would have happened anyway under auto-apply);
  EXPIRED/DISABLED are no-op fall-through paths.

Authority invariants (AST-pinned by Slice 5):

* Imports stdlib ONLY. NEVER imports any governance module —
  strongest authority invariant. Slice 2's producer may import
  ``inline_permission_prompt`` and ``ide_observability_stream``;
  Slice 1 stays pure data.
* No async (Slice 2 wraps via the controller's existing async
  Future surface).
* Read-only — never writes a file, never executes code.
* No mutation tools.
* No exec/eval/compile (mirrors Move 6 Slice 2 + Priority #1/#2/
  #3/#4/#5 Slice 1 critical safety pin).

Master flag default-FALSE until Slice 5 graduation:
``JARVIS_INLINE_PROMPT_GATE_ENABLED``. Asymmetric env semantics —
empty/whitespace = unset = current default; explicit truthy/falsy
overrides at call time.
"""
from __future__ import annotations

import enum
import hashlib
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


INLINE_PROMPT_GATE_SCHEMA_VERSION: str = "inline_prompt_gate.1"


# ---------------------------------------------------------------------------
# Master flag — asymmetric env semantics
# ---------------------------------------------------------------------------


def inline_prompt_gate_enabled() -> bool:
    """``JARVIS_INLINE_PROMPT_GATE_ENABLED`` (default ``true`` —
    graduated 2026-05-02 in InlinePromptGate Slice 5).

    Asymmetric env semantics — empty/whitespace = unset = graduated
    default; explicit ``0``/``false``/``no``/``off`` evaluates false;
    explicit truthy values evaluate true.

    Re-read on every call so flips hot-revert without restart.
    Graduated default-true because the producer's cost is operator
    latency only (no $ cost — verdict propagates back through the
    existing controller's Future), and the full Slices 1-4 stack
    (primitive + producer/bridge + HTTP response surface +
    listener-based renderer) all proved out with 420/420 combined
    sweep. The HTTP write surface (separate flag) stays default-off
    pending operator-controlled cost ramp.
    """
    raw = os.environ.get(
        "JARVIS_INLINE_PROMPT_GATE_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default (Slice 5, 2026-05-02)
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Env-knob helpers — every numeric clamped (floor + ceiling)
# ---------------------------------------------------------------------------


def _env_int_clamped(
    name: str, default: int, *, floor: int, ceiling: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(floor, min(ceiling, int(raw)))
    except (TypeError, ValueError):
        return default


def _env_float_clamped(
    name: str, default: float, *, floor: float, ceiling: float,
) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(floor, min(ceiling, float(raw)))
    except (TypeError, ValueError):
        return default


def default_prompt_timeout_s() -> float:
    """``JARVIS_INLINE_PROMPT_GATE_TIMEOUT_S`` — floor 1s, ceiling
    3600s (1h), default 60s. The window an operator has to respond
    before the prompt EXPIRED-fires and the op falls through to its
    current auto-apply / approval-required behavior."""
    return _env_float_clamped(
        "JARVIS_INLINE_PROMPT_GATE_TIMEOUT_S",
        default=60.0, floor=1.0, ceiling=3600.0,
    )


def summary_max_chars() -> int:
    """``JARVIS_INLINE_PROMPT_GATE_SUMMARY_MAX_CHARS`` — floor 16,
    ceiling 1024, default 200. Truncation cap on the human-readable
    change summary rendered into the prompt."""
    return _env_int_clamped(
        "JARVIS_INLINE_PROMPT_GATE_SUMMARY_MAX_CHARS",
        default=200, floor=16, ceiling=1024,
    )


def fingerprint_hex_chars() -> int:
    """``JARVIS_INLINE_PROMPT_GATE_FINGERPRINT_HEX_CHARS`` — floor 8,
    ceiling 64, default 16. Length of the displayed change-fingerprint
    prefix (full sha256 hex always present in the request for audit;
    this is the rendered preview length)."""
    return _env_int_clamped(
        "JARVIS_INLINE_PROMPT_GATE_FINGERPRINT_HEX_CHARS",
        default=16, floor=8, ceiling=64,
    )


# ---------------------------------------------------------------------------
# Closed taxonomy — 5-value verdict (J.A.R.M.A.T.R.I.X.)
# ---------------------------------------------------------------------------


class PhaseInlineVerdict(str, enum.Enum):
    """Closed 5-value taxonomy. Every input maps to exactly one.

    * :attr:`ALLOW` — operator confirmed (controller STATE_ALLOWED);
      orchestrator resumes APPLY.
    * :attr:`DENY` — operator rejected (controller STATE_DENIED);
      orchestrator routes to CANCELLED via existing CancelToken
      substrate.
    * :attr:`PAUSE_OP` — operator paused (controller STATE_PAUSED);
      orchestrator holds the op with extended TTL for later
      resumption (Slice 2 wires the hold semantics).
    * :attr:`EXPIRED` — timeout window elapsed (controller
      STATE_EXPIRED); orchestrator falls through to its current
      auto-apply / approval-required behavior. Backward-compatible
      degraded-network path.
    * :attr:`DISABLED` — master flag off OR garbage input; the
      prompt never reached an operator. Equivalent to EXPIRED for
      orchestrator purposes (fall through), but distinct in
      observability so operators can tell "no operator action" from
      "operator did not respond in time".
    """

    ALLOW = "allow"
    DENY = "deny"
    PAUSE_OP = "pause_op"
    EXPIRED = "expired"
    DISABLED = "disabled"


_TERMINAL_VERDICTS: frozenset = frozenset({
    PhaseInlineVerdict.ALLOW,
    PhaseInlineVerdict.DENY,
    PhaseInlineVerdict.PAUSE_OP,
    PhaseInlineVerdict.EXPIRED,
    PhaseInlineVerdict.DISABLED,
})


# ---------------------------------------------------------------------------
# Byte-parity state-string constants
# ---------------------------------------------------------------------------

#: Mirror of ``inline_permission_prompt.STATE_*`` constants — kept
#: in this module verbatim so Slice 1 stays pure-stdlib (zero
#: governance imports). Slice 4 pin-test suite asserts byte-parity
#: against the live controller exports; any divergence is caught
#: structurally before shipping.
_CONTROLLER_STATE_ALLOWED: str = "allowed"
_CONTROLLER_STATE_DENIED: str = "denied"
_CONTROLLER_STATE_EXPIRED: str = "expired"
_CONTROLLER_STATE_PAUSED: str = "paused"

#: 1:1 mapping from controller terminal state → phase-boundary
#: verdict. Closed-taxonomy: every valid state maps to exactly one
#: verdict; unknown states fall through to DISABLED (defensive).
_STATE_TO_VERDICT: Dict[str, PhaseInlineVerdict] = {
    _CONTROLLER_STATE_ALLOWED: PhaseInlineVerdict.ALLOW,
    _CONTROLLER_STATE_DENIED: PhaseInlineVerdict.DENY,
    _CONTROLLER_STATE_EXPIRED: PhaseInlineVerdict.EXPIRED,
    _CONTROLLER_STATE_PAUSED: PhaseInlineVerdict.PAUSE_OP,
}


# ---------------------------------------------------------------------------
# Phase C MonotonicTighteningVerdict canonical strings
# ---------------------------------------------------------------------------

#: Canonical string from ``adaptation.ledger.MonotonicTighteningVerdict``.
#: Slice 4's bridge will import the live enum and assert
#: byte-parity to this constant via test (mirrors Priority #2's
#: PostmortemRecallConsumer pattern). Stamped on outputs that
#: represent operator-inserted tightening (DENY / PAUSE_OP).
_TIGHTENING_PASSED_STR: str = "passed"

#: Outcomes that constitute operator-inserted tightening — the
#: operator inserted friction that wasn't there before. ALLOW is
#: NOT a tightening (operator confirmed what would have happened
#: anyway under auto-apply); EXPIRED/DISABLED fall through to
#: current behavior so they're no-op for tightening purposes.
_TIGHTENING_OUTCOMES: frozenset = frozenset({
    PhaseInlineVerdict.DENY,
    PhaseInlineVerdict.PAUSE_OP,
})


# ---------------------------------------------------------------------------
# Frozen request — orchestrator-shaped (NOT tool-call-shaped)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PhaseInlinePromptRequest:
    """Frozen orchestrator-shaped prompt request.

    The phase-boundary analog to the existing tool-call-shaped
    ``InlinePromptRequest`` in ``inline_permission_prompt.py``.
    Where that one carries ``tool`` + ``arg_fingerprint`` +
    ``arg_preview`` + ``target_path`` + ``verdict: InlineGateVerdict``
    (per-tool-call), this one carries ``phase_at_request`` +
    ``risk_tier`` + ``change_summary`` + ``change_fingerprint`` +
    ``target_paths`` (per-op-at-phase-boundary).

    All fields immutable. ``rationale`` is model-generated display
    text only — never flows into authorization (§1).
    """

    prompt_id: str
    op_id: str
    phase_at_request: str  # "GATE" | "APPLY" | "VERIFY"
    risk_tier: str  # "NOTIFY_APPLY" | "APPROVAL_REQUIRED"
    change_summary: str
    change_fingerprint: str  # full sha256 hex
    target_paths: Tuple[str, ...]
    rationale: str = ""
    route: str = "interactive"
    created_ts: float = 0.0
    timeout_s: float = 0.0
    schema_version: str = INLINE_PROMPT_GATE_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "prompt_id": self.prompt_id,
            "op_id": self.op_id,
            "phase_at_request": self.phase_at_request,
            "risk_tier": self.risk_tier,
            "change_summary": self.change_summary,
            "change_fingerprint": self.change_fingerprint,
            "target_paths": list(self.target_paths),
            "rationale": self.rationale,
            "route": self.route,
            "created_ts": self.created_ts,
            "timeout_s": self.timeout_s,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "PhaseInlinePromptRequest":
        """Reconstruct from a serialized projection. Tolerates
        missing optional fields by falling back to dataclass
        defaults. NEVER raises on malformed input — returns a
        sentinel request with empty fields if structurally
        unrecoverable."""
        try:
            paths_raw = d.get("target_paths", ())
            if isinstance(paths_raw, (list, tuple)):
                paths = tuple(str(p) for p in paths_raw)
            else:
                paths = ()
            return cls(
                prompt_id=str(d.get("prompt_id", "")),
                op_id=str(d.get("op_id", "")),
                phase_at_request=str(d.get("phase_at_request", "")),
                risk_tier=str(d.get("risk_tier", "")),
                change_summary=str(d.get("change_summary", "")),
                change_fingerprint=str(d.get("change_fingerprint", "")),
                target_paths=paths,
                rationale=str(d.get("rationale", "")),
                route=str(d.get("route", "interactive")),
                created_ts=float(d.get("created_ts", 0.0) or 0.0),
                timeout_s=float(d.get("timeout_s", 0.0) or 0.0),
                schema_version=str(
                    d.get("schema_version", INLINE_PROMPT_GATE_SCHEMA_VERSION),
                ),
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "[InlinePromptGate] from_dict degraded: %s", exc,
            )
            return cls(
                prompt_id="",
                op_id="",
                phase_at_request="",
                risk_tier="",
                change_summary="",
                change_fingerprint="",
                target_paths=(),
            )


# ---------------------------------------------------------------------------
# Frozen verdict — terminal outcome with Phase C tightening stamp
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PhaseInlinePromptVerdict:
    """Frozen terminal verdict propagated to the orchestrator.

    ``monotonic_tightening_verdict`` is the canonical string from
    ``adaptation.ledger.MonotonicTighteningVerdict`` — populated to
    ``"passed"`` on DENY/PAUSE_OP outcomes (operator-inserted
    tightening) and empty on ALLOW/EXPIRED/DISABLED outcomes
    (no tightening signal — ALLOW is operator-confirmed continuation,
    EXPIRED/DISABLED fall through to current behavior).

    Slice 4's bridge to ``auto_action_router`` will assert
    string-parity to the live enum via byte-parity test (mirrors
    Priority #2's PostmortemRecallConsumer pattern)."""

    prompt_id: str
    op_id: str
    verdict: PhaseInlineVerdict
    elapsed_s: float = 0.0
    reviewer: str = ""
    operator_reason: str = ""
    monotonic_tightening_verdict: str = ""
    schema_version: str = INLINE_PROMPT_GATE_SCHEMA_VERSION

    @property
    def is_terminal(self) -> bool:
        return self.verdict in _TERMINAL_VERDICTS

    @property
    def is_tightening(self) -> bool:
        """True iff this verdict represents operator-inserted
        friction (DENY or PAUSE_OP). ALLOW is NOT a tightening
        because the operator merely confirmed what auto-apply would
        have done; EXPIRED/DISABLED are no-op fall-throughs."""
        return self.verdict in _TIGHTENING_OUTCOMES

    @property
    def allowed(self) -> bool:
        return self.verdict is PhaseInlineVerdict.ALLOW

    def to_dict(self) -> Dict[str, Any]:
        return {
            "prompt_id": self.prompt_id,
            "op_id": self.op_id,
            "verdict": self.verdict.value,
            "elapsed_s": self.elapsed_s,
            "reviewer": self.reviewer,
            "operator_reason": self.operator_reason,
            "monotonic_tightening_verdict": (
                self.monotonic_tightening_verdict
            ),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(
        cls, d: Mapping[str, Any],
    ) -> "PhaseInlinePromptVerdict":
        try:
            v_raw = str(d.get("verdict", PhaseInlineVerdict.DISABLED.value))
            try:
                v = PhaseInlineVerdict(v_raw)
            except ValueError:
                v = PhaseInlineVerdict.DISABLED
            return cls(
                prompt_id=str(d.get("prompt_id", "")),
                op_id=str(d.get("op_id", "")),
                verdict=v,
                elapsed_s=max(0.0, float(d.get("elapsed_s", 0.0) or 0.0)),
                reviewer=str(d.get("reviewer", "")),
                operator_reason=str(d.get("operator_reason", "")),
                monotonic_tightening_verdict=str(
                    d.get("monotonic_tightening_verdict", ""),
                ),
                schema_version=str(
                    d.get("schema_version", INLINE_PROMPT_GATE_SCHEMA_VERSION),
                ),
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "[InlinePromptGate] verdict from_dict degraded: %s", exc,
            )
            return cls(
                prompt_id="",
                op_id="",
                verdict=PhaseInlineVerdict.DISABLED,
            )


# ---------------------------------------------------------------------------
# Total mapping: controller state → PhaseInlineVerdict
# ---------------------------------------------------------------------------


def compute_phase_inline_verdict(
    *,
    prompt_id: str,
    op_id: str,
    state: Optional[str],
    elapsed_s: float = 0.0,
    reviewer: str = "",
    operator_reason: str = "",
    enabled: bool = True,
) -> PhaseInlinePromptVerdict:
    """Total mapping function — controller terminal-state string +
    metadata → :class:`PhaseInlinePromptVerdict`.

    NEVER raises. Garbage / missing / unknown ``state`` falls
    through to :attr:`PhaseInlineVerdict.DISABLED` (defensive
    degradation per §7 fail-closed). ``enabled=False`` short-circuits
    to DISABLED before any state inspection (master-flag-off path).

    Phase C tightening stamping is automatic and outcome-aware:
    DENY/PAUSE_OP stamp ``"passed"``; ALLOW/EXPIRED/DISABLED stamp
    empty string.
    """
    try:
        # Defensive sanitization — the orchestrator may pass
        # arbitrary metadata.
        pid = str(prompt_id) if prompt_id is not None else ""
        oid = str(op_id) if op_id is not None else ""
        # Clamp elapsed_s non-negative.
        try:
            es = max(0.0, float(elapsed_s))
        except (TypeError, ValueError):
            es = 0.0
        rv = str(reviewer) if reviewer is not None else ""
        rs = str(operator_reason) if operator_reason is not None else ""

        # Master-flag-off short-circuit. Never reached the operator.
        if not enabled:
            return PhaseInlinePromptVerdict(
                prompt_id=pid,
                op_id=oid,
                verdict=PhaseInlineVerdict.DISABLED,
                elapsed_s=es,
                reviewer=rv,
                operator_reason=rs,
                monotonic_tightening_verdict="",
            )

        # Normalize state. None / non-str / empty / unknown all
        # map to DISABLED (defensive).
        if state is None or not isinstance(state, str):
            logger.warning(
                "[InlinePromptGate] non-string state for prompt=%s "
                "type=%s — degrading to DISABLED",
                pid, type(state).__name__,
            )
            v = PhaseInlineVerdict.DISABLED
        else:
            normalized = state.strip().lower()
            if not normalized:
                v = PhaseInlineVerdict.DISABLED
            elif normalized in _STATE_TO_VERDICT:
                v = _STATE_TO_VERDICT[normalized]
            else:
                logger.warning(
                    "[InlinePromptGate] unknown controller state "
                    "%r for prompt=%s — degrading to DISABLED",
                    state, pid,
                )
                v = PhaseInlineVerdict.DISABLED

        tightening = (
            _TIGHTENING_PASSED_STR if v in _TIGHTENING_OUTCOMES
            else ""
        )
        return PhaseInlinePromptVerdict(
            prompt_id=pid,
            op_id=oid,
            verdict=v,
            elapsed_s=es,
            reviewer=rv,
            operator_reason=rs,
            monotonic_tightening_verdict=tightening,
        )
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        # NEVER raise — orchestrator-callable primitive.
        logger.warning(
            "[InlinePromptGate] compute_phase_inline_verdict "
            "last-resort degraded: %s", exc,
        )
        return PhaseInlinePromptVerdict(
            prompt_id=str(prompt_id) if prompt_id is not None else "",
            op_id=str(op_id) if op_id is not None else "",
            verdict=PhaseInlineVerdict.DISABLED,
            monotonic_tightening_verdict="",
        )


# ---------------------------------------------------------------------------
# Deterministic prompt-id derivation
# ---------------------------------------------------------------------------


def derive_prompt_id(
    *,
    op_id: str,
    change_fingerprint: str,
    phase: str = "GATE",
    schema_version: str = INLINE_PROMPT_GATE_SCHEMA_VERSION,
) -> str:
    """Deterministic prompt-id derivation: same inputs → same id.

    Idempotent across retries — if the orchestrator's GATE phase
    re-enters with identical change_fingerprint (signal coalescing,
    L2 repair re-attempt), the prompt-id is stable. The
    ``InlinePromptController`` will then refuse a duplicate request
    structurally rather than racing two prompts for the same op.

    NEVER raises. Garbage inputs → derived id over their str()
    coercion (audit trail still intact).
    """
    try:
        material = "|".join((
            "inline_prompt_gate",
            str(schema_version),
            str(phase or ""),
            str(op_id or ""),
            str(change_fingerprint or ""),
        )).encode("utf-8", errors="replace")
        digest = hashlib.sha256(material).hexdigest()
        return f"ipg-{digest[:24]}"
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "[InlinePromptGate] derive_prompt_id degraded: %s", exc,
        )
        return "ipg-degraded"


# ---------------------------------------------------------------------------
# Truncation helpers — render-time, not authority
# ---------------------------------------------------------------------------


def truncate_summary(text: str, *, max_chars: Optional[int] = None) -> str:
    """Truncate change-summary text for prompt rendering. Respects
    ``JARVIS_INLINE_PROMPT_GATE_SUMMARY_MAX_CHARS`` env-knob.
    NEVER raises."""
    try:
        cap = max_chars if max_chars is not None else summary_max_chars()
        cap = max(16, min(1024, int(cap)))
        s = str(text or "")
        if len(s) <= cap:
            return s
        return s[: max(1, cap - 14)] + "...<truncated>"
    except Exception:  # noqa: BLE001 — defensive
        return ""


def truncate_fingerprint(
    fingerprint: str, *, hex_chars: Optional[int] = None,
) -> str:
    """Truncate fingerprint hex for rendering. Full sha256 stays in
    the request for audit; this is the displayed prefix length.
    NEVER raises."""
    try:
        cap = (
            hex_chars if hex_chars is not None
            else fingerprint_hex_chars()
        )
        cap = max(8, min(64, int(cap)))
        s = str(fingerprint or "")
        return s[:cap]
    except Exception:  # noqa: BLE001 — defensive
        return ""


# ---------------------------------------------------------------------------
# Public surface — Slice 5 will pin via shipped_code_invariants
# ---------------------------------------------------------------------------

__all__ = [
    "INLINE_PROMPT_GATE_SCHEMA_VERSION",
    "PhaseInlineVerdict",
    "PhaseInlinePromptRequest",
    "PhaseInlinePromptVerdict",
    "compute_phase_inline_verdict",
    "derive_prompt_id",
    "default_prompt_timeout_s",
    "fingerprint_hex_chars",
    "inline_prompt_gate_enabled",
    "register_flags",
    "register_shipped_invariants",
    "summary_max_chars",
    "truncate_fingerprint",
    "truncate_summary",
]


# ---------------------------------------------------------------------------
# Slice 5 — Module-owned FlagRegistry contribution (4 producer-side flags)
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> int:
    """Module-owned :class:`FlagRegistry` registration for the 4
    producer-side env knobs. Discovered automatically by
    ``flag_registry_seed._discover_module_provided_flags``. Returns
    the count of flags registered."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "[InlinePromptGate] register_flags degraded: %s", exc,
        )
        return 0
    specs = [
        FlagSpec(
            name="JARVIS_INLINE_PROMPT_GATE_ENABLED",
            type=FlagType.BOOL, default=True,
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/inline_prompt_gate.py"
            ),
            example="JARVIS_INLINE_PROMPT_GATE_ENABLED=true",
            description=(
                "Master switch for the InlinePromptGate producer. "
                "Graduated default-true 2026-05-02 in Slice 5."
            ),
        ),
        FlagSpec(
            name="JARVIS_INLINE_PROMPT_GATE_TIMEOUT_S",
            type=FlagType.FLOAT, default=60.0,
            category=Category.TIMING,
            source_file=(
                "backend/core/ouroboros/governance/inline_prompt_gate.py"
            ),
            example="JARVIS_INLINE_PROMPT_GATE_TIMEOUT_S=120",
            description=(
                "Operator-response window for phase-boundary prompts. "
                "Floor 1s, ceiling 3600s. Default 60s."
            ),
        ),
        FlagSpec(
            name="JARVIS_INLINE_PROMPT_GATE_SUMMARY_MAX_CHARS",
            type=FlagType.INT, default=200,
            category=Category.CAPACITY,
            source_file=(
                "backend/core/ouroboros/governance/inline_prompt_gate.py"
            ),
            example="JARVIS_INLINE_PROMPT_GATE_SUMMARY_MAX_CHARS=400",
            description=(
                "Truncation cap for change-summary text rendered "
                "into the prompt. Floor 16, ceiling 1024."
            ),
        ),
        FlagSpec(
            name="JARVIS_INLINE_PROMPT_GATE_FINGERPRINT_HEX_CHARS",
            type=FlagType.INT, default=16,
            category=Category.CAPACITY,
            source_file=(
                "backend/core/ouroboros/governance/inline_prompt_gate.py"
            ),
            example=(
                "JARVIS_INLINE_PROMPT_GATE_FINGERPRINT_HEX_CHARS=32"
            ),
            description=(
                "Display-prefix length for change-fingerprint hex. "
                "Full sha256 stays in the request for audit. "
                "Floor 8, ceiling 64."
            ),
        ),
    ]
    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[InlinePromptGate] register_flags spec %s "
                "skipped: %s", spec.name, exc,
            )
    return count


# ---------------------------------------------------------------------------
# Slice 5 — Module-owned shipped_code_invariants contribution
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Module-owned :func:`shipped_code_invariants.register_shipped_code_invariant`
    contribution. Discovered automatically by
    ``shipped_code_invariants._discover_module_provided_invariants``.
    Returns the list of :class:`ShippedCodeInvariant` instances."""
    import ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    def _validate_pure_stdlib(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Slice 1 primitive must stay pure-stdlib at hot path —
        no governance imports outside the module-owned registration
        contract (``register_flags`` / ``register_shipped_invariants``)."""
        violations: list = []
        registration_funcs = {
            "register_flags",
            "register_shipped_invariants",
        }
        exempt_ranges = []
        for fnode in ast.walk(tree):
            if isinstance(fnode, ast.FunctionDef):
                if fnode.name in registration_funcs:
                    start = getattr(fnode, "lineno", 0)
                    end = getattr(fnode, "end_lineno", start) or start
                    exempt_ranges.append((start, end))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if "backend." in module or "governance" in module:
                    lineno = getattr(node, "lineno", 0)
                    if any(s <= lineno <= e for s, e in exempt_ranges):
                        continue
                    violations.append(
                        f"line {lineno}: Slice 1 must be pure-stdlib "
                        f"— found {module!r}"
                    )
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        violations.append(
                            f"line {getattr(node, 'lineno', '?')}: "
                            f"Slice 1 MUST NOT {node.func.id}()"
                        )
            if isinstance(node, ast.AsyncFunctionDef):
                violations.append(
                    f"line {getattr(node, 'lineno', '?')}: "
                    f"Slice 1 must remain sync — found async "
                    f"def {node.name!r}"
                )
        return tuple(violations)

    def _validate_taxonomy_5_values(
        tree: "ast.Module", source: str,
    ) -> tuple:
        """Closed-taxonomy invariant: PhaseInlineVerdict has
        EXACTLY 5 values. New verdict values require explicit
        scope-doc + bridge update."""
        violations: list = []
        required = {"ALLOW", "DENY", "PAUSE_OP", "EXPIRED", "DISABLED"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if node.name == "PhaseInlineVerdict":
                    seen = set()
                    for stmt in node.body:
                        if isinstance(stmt, ast.Assign):
                            for tgt in stmt.targets:
                                if isinstance(tgt, ast.Name):
                                    seen.add(tgt.id)
                    missing = required - seen
                    extras = seen - required
                    if missing:
                        violations.append(
                            f"PhaseInlineVerdict missing required "
                            f"values: {sorted(missing)}"
                        )
                    if extras:
                        violations.append(
                            f"PhaseInlineVerdict has unexpected "
                            f"values (closed-taxonomy violation): "
                            f"{sorted(extras)}"
                        )
        return tuple(violations)

    def _validate_state_byte_parity(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Slice 1 redefines controller STATE_* string constants
        verbatim to stay pure-stdlib. The literals must match the
        controller's exports — a runtime test asserts byte-parity,
        but this AST pin asserts the constants are PRESENT with
        the exact literal string values."""
        violations: list = []
        expected = {
            "_CONTROLLER_STATE_ALLOWED": "allowed",
            "_CONTROLLER_STATE_DENIED": "denied",
            "_CONTROLLER_STATE_EXPIRED": "expired",
            "_CONTROLLER_STATE_PAUSED": "paused",
        }
        seen: dict = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign) and isinstance(
                node.target, ast.Name,
            ):
                if node.target.id in expected:
                    if isinstance(node.value, ast.Constant):
                        seen[node.target.id] = node.value.value
            elif isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and tgt.id in expected:
                        if isinstance(node.value, ast.Constant):
                            seen[tgt.id] = node.value.value
        for name, expected_value in expected.items():
            if name not in seen:
                violations.append(
                    f"missing controller-state constant {name!r}"
                )
            elif seen[name] != expected_value:
                violations.append(
                    f"controller-state constant {name!r} drifted: "
                    f"expected {expected_value!r}, got "
                    f"{seen[name]!r}"
                )
        return tuple(violations)

    target = (
        "backend/core/ouroboros/governance/inline_prompt_gate.py"
    )
    return [
        ShippedCodeInvariant(
            invariant_name="inline_prompt_gate_pure_stdlib",
            target_file=target,
            description=(
                "Slice 1 primitive stays pure-stdlib at hot path: "
                "no governance imports outside register_flags / "
                "register_shipped_invariants, no async, no "
                "exec/eval/compile."
            ),
            validate=_validate_pure_stdlib,
        ),
        ShippedCodeInvariant(
            invariant_name="inline_prompt_gate_taxonomy_5_values",
            target_file=target,
            description=(
                "PhaseInlineVerdict is a 5-value closed taxonomy "
                "(ALLOW / DENY / PAUSE_OP / EXPIRED / DISABLED). "
                "New values require explicit scope-doc + bridge "
                "update."
            ),
            validate=_validate_taxonomy_5_values,
        ),
        ShippedCodeInvariant(
            invariant_name="inline_prompt_gate_state_byte_parity",
            target_file=target,
            description=(
                "Controller STATE_* constants redefined verbatim "
                "with literal byte-parity values (allowed / denied "
                "/ expired / paused) so Slice 1 stays pure-stdlib."
            ),
            validate=_validate_state_byte_parity,
        ),
    ]
