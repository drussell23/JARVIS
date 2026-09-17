"""Capability assurance — no op runs weaker than the envelope promised.

Every capability in this organism degrades QUIETLY. `single_file_diff_schema_
enabled()` returns False and the model is asked for a whole file instead of a
diff; `ctx.telemetry` is None and `_ctx_schema_capability` answers
`full_content_only`; `ctx.target_symbols` is empty and the declared-symbol
refusal has nothing to refuse. In every one of those cases the pipeline still
runs, still produces a candidate, and still reports success — it just produces
the WEAKER thing, and the only evidence is a mangled docstring in a landed
diff, days later.

That is the failure this module exists to make impossible. The envelope
(`production_envelope`) states what the organism can do; this asserts the
running op actually has it, and REFUSES rather than silently delivering less.

## Two layers, because the questions are different

**Preflight** (`preflight_verdict`) runs before an op is dispatched — at
`/goal sanction` and `/goal inject`. There is no `OperationContext` yet, so it
asks what can be known statically: are the capability flags armed, does the
signed goal declare symbols, is the scope single-file. A failure here means
"do not dispatch", and the operator sees why in the TUI before anything runs.

**Runtime** (`assert_generation_capability`) runs at the seam where the schema
decision is actually made, with the real context in hand. It answers the one
question preflight cannot: *given this op's served model and telemetry, is the
organism about to ask for the schema it promised?* Full content is legitimate
for a multi-file op, or a model that cannot produce diffs — those are not
degradation. Degradation is: the flag is on, the served model IS diff-capable,
the scope IS single-file, and the decision still came out `full_content`. That
combination means something upstream lost the capability, and it is the exact
shape of `capability=? brain=-` (`ctx.telemetry is None`, 89a9166e05 /
ec6bb92c9a) — a routing admission that never happened.

Both return a verdict; neither raises. The CALLER decides what a failure means,
because "refuse to dispatch" and "abort a running op" are different acts with
different blast radii, and a module that made that choice for both would be
wrong for one of them.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.CapabilityAssurance")

__all__ = [
    "CapabilityVerdict",
    "FATAL",
    "RECOVERABLE",
    "assert_generation_capability",
    "clear_degradations_for_tests",
    "degradation_for",
    "degradation_lesson_kwargs",
    "degradation_stats",
    "enforcement_enabled",
    "mark_degraded",
    "preflight_verdict",
]

_ENV_ENFORCE = "JARVIS_CAPABILITY_ASSURANCE_ENFORCE"
#: Bounded registry of ops that generated under a degraded capability.
_ENV_DEGRADED_REGISTRY_MAX = "JARVIS_CAPABILITY_DEGRADED_REGISTRY_MAX"
_DEFAULT_DEGRADED_REGISTRY_MAX = 512

# ---------------------------------------------------------------------------
# Severity — the distinction the first version of this module did not draw
#
# The check runs at PROMPT BUILD time, before a single token exists. So it can
# never ask "is the output parseable" — there is no output. What it can ask is
# whether the schema we are about to request still produces a USABLE candidate.
#
# `full_content` (2b.1) is the historic, always-supported schema. Asking for it
# when a diff was promised is a real loss of fidelity -- it re-emits whole files
# and risks the docstring mangle on large ones -- but it is not an inability to
# generate. Aborting turned that fidelity loss into `tokens=0`, which is
# strictly worse than the thing it was protecting against: measured live in
# bt-2026-09-08-202025, the Sentinel's own sanctioned op died in 64.79s having
# produced nothing at all, and every self-directed op after Phase 2 delivered
# them to a worker would have died the same way.
#
# FATAL is reserved for a schema decision that cannot yield a candidate at all.
# No CURRENT failure mode is fatal, and the honest thing is to say so rather
# than to keep an abort alive for a case that has never occurred.
# ---------------------------------------------------------------------------

#: Weaker than promised, still produces a usable candidate. Report and proceed.
RECOVERABLE = "recoverable"
#: Cannot produce a usable candidate at all. Abort a sanctioned op.
FATAL = "fatal"


def enforcement_enabled() -> bool:
    """Whether a failed assurance ABORTS, or is merely reported.

    Default ON. The whole point is that degradation must not pass silently;
    an operator who wants to run degraded on purpose says so explicitly.
    NEVER raises.
    """
    raw = (os.environ.get(_ENV_ENFORCE, "") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


@dataclass(frozen=True)
class CapabilityVerdict:
    """What the organism can do right now, and what it was promised."""

    ok: bool
    reason: str = ""
    checks: Dict[str, bool] = field(default_factory=dict)
    detail: Dict[str, str] = field(default_factory=dict)
    #: Whether a failure here may ABORT the op, as opposed to being reported.
    #:
    #: The envelope promises capability for the operator's SANCTIONED work. It
    #: says nothing about the many legitimate contexts that build a prompt with
    #: no routing admission — an ambient tool call, a probe, a unit test. The
    #: first version of this module aborted on all of them, which turned a
    #: diagnostic into a hard failure on paths that were previously fine: seven
    #: prompt-building tests began raising ``capability_degraded`` the moment
    #: another test left the diff flag armed in the environment.
    #:
    #: An op is enforceable when it carries a signed-goal pointer — i.e. it is
    #: exactly the work the operator sanctioned and for which the capability
    #: was actually promised.
    enforceable: bool = False
    #: RECOVERABLE (weaker schema, still usable) or FATAL (no usable candidate).
    #: Defaults to RECOVERABLE because that is what every observed degradation
    #: has been: an abort must be argued for, not assumed.
    severity: str = RECOVERABLE

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.ok

    @property
    def is_fatal(self) -> bool:
        """Whether this failure may end the op.

        Enforceability and severity are DIFFERENT questions and both must hold.
        `enforceable` asks *whose* work this is — only the operator's sanctioned
        work was ever promised a capability. `severity` asks whether the loss
        actually prevents a candidate. The first version conflated them, so a
        sanctioned op was aborted for a degradation it could have generated
        through.
        """
        return (not self.ok) and self.enforceable and self.severity == FATAL

    @property
    def degraded_but_usable(self) -> bool:
        """A real loss of fidelity the pipeline should proceed through."""
        return (not self.ok) and self.severity == RECOVERABLE

    def render(self) -> str:
        """One operator-facing line per check — the TUI's telemetry error."""
        marks = ", ".join(
            f"{'ok' if v else 'FAIL'}:{k}" for k, v in sorted(self.checks.items())
        )
        head = "capability assurance PASSED" if self.ok else (
            f"capability assurance FAILED — {self.reason}"
        )
        return f"{head} [{marks}]" if marks else head


def _flag(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Degraded-but-usable policy: report, remember, proceed
#
# Three things have to happen when an op generates under a lost capability, and
# exactly none of them is "stop":
#
#   1. It is LOGGED, so an operator watching telemetry sees it.
#   2. It is REMEMBERED, as a lesson keyed to the modules involved, so the
#      degradation informs future work instead of evaporating with the session.
#   3. It is FLAGGED, so every later phase can tell that this op's candidate
#      was produced under a weaker schema than it was promised -- which is
#      exactly the context VALIDATE needs when it judges the output.
#
# The flag lives in a bounded module registry keyed by op_id rather than on the
# context, because OperationContext is frozen and this seam builds a prompt: it
# has no way to hand a new context back to its caller. Same shape as
# `recorder_lease`, for the same reason.
# ---------------------------------------------------------------------------

_degraded: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_degraded_lock = threading.Lock()
_degraded_stats: Dict[str, int] = {"marked": 0, "evicted": 0, "lessons": 0}


def _registry_max() -> int:
    try:
        raw = (os.environ.get(_ENV_DEGRADED_REGISTRY_MAX, "") or "").strip()
        val = int(raw) if raw else _DEFAULT_DEGRADED_REGISTRY_MAX
        return val if val >= 1 else _DEFAULT_DEGRADED_REGISTRY_MAX
    except (TypeError, ValueError):
        return _DEFAULT_DEGRADED_REGISTRY_MAX


def mark_degraded(op_id: Any, verdict: "CapabilityVerdict") -> None:
    """Flag *op_id* as having generated under a degraded capability.

    Non-fatal telemetry: later phases read it to know the candidate they are
    judging was produced from a weaker schema than the envelope promised.
    NEVER raises.
    """
    try:
        oid = str(op_id or "").strip()
        if not oid:
            return
        entry = {
            "reason": str(getattr(verdict, "reason", "") or ""),
            "severity": str(getattr(verdict, "severity", RECOVERABLE)),
            "enforceable": bool(getattr(verdict, "enforceable", False)),
            "detail": dict(getattr(verdict, "detail", {}) or {}),
            "at": time.time(),
        }
        with _degraded_lock:
            _degraded[oid] = entry
            _degraded.move_to_end(oid)
            _degraded_stats["marked"] += 1
            while len(_degraded) > _registry_max():
                _degraded.popitem(last=False)
                _degraded_stats["evicted"] += 1
    except Exception:  # noqa: BLE001 — a telemetry flag may never fail a prompt
        logger.debug("[CapabilityAssurance] degradation flag degraded", exc_info=True)


def degradation_for(op_id: Any) -> Optional[Dict[str, Any]]:
    """The degradation record for *op_id*, or ``None``. NEVER raises."""
    try:
        oid = str(op_id or "").strip()
        if not oid:
            return None
        with _degraded_lock:
            entry = _degraded.get(oid)
            return dict(entry) if entry is not None else None
    except Exception:  # noqa: BLE001
        return None


def degradation_stats() -> Dict[str, Any]:
    """Counters plus live registry size. NEVER raises."""
    try:
        with _degraded_lock:
            out: Dict[str, Any] = dict(_degraded_stats)
            out["open"] = len(_degraded)
            return out
    except Exception:  # noqa: BLE001
        return {}


def clear_degradations_for_tests() -> None:
    """Test seam."""
    with _degraded_lock:
        _degraded.clear()
        for key in _degraded_stats:
            _degraded_stats[key] = 0


# ---------------------------------------------------------------------------
# Execution attestation — did the capability we armed actually get USED?
# ---------------------------------------------------------------------------
#
# The generalisation of `CapabilityExecutionDrift`, which guards exactly one
# decision today and had to be written by hand for it. Thirteen defects in one
# session shared a single shape: a capability built, tested, armed — and the
# thing that consumes it never asked. The load-shed latch armed nowhere. The
# promotion machinery never called. `target_symbols` read by three consumers
# and populated by none. The diff schema armed, capability-resolved, and
# overridden by a threshold; and in the lane that actually runs, the branch did
# not exist. Every one passed its unit tests, because the defect was never in a
# component — it was in whether anything consulted it, and seams have no owner.
#
# So a component that arms a capability DECLARES it for the op, and the
# component that uses it REDEEMS it. A declaration with no redemption at the
# end of the op is a capability that was promised and never delivered.
#
# ## Deliberately not cryptographic
#
# The threat is a developer forgetting to wire a seam, not an adversary forging
# state. The roadmap is HMAC-signed because an unsigned goal authorises
# arbitrary file writes; a voucher authorises nothing and crosses no trust
# boundary. Signing it would be ceremony that buys no safety and costs a key to
# manage.
#
# ## Checked at op TERMINAL, not at VALIDATE
#
# Many ops are shed at GENERATE and never reach VALIDATE, and capabilities like
# promotion are exercised after it. Redeeming at VALIDATE would turn every
# early-shed op into an attestation failure — a deadlock manufactured by the
# checker rather than found by it.

_attest_lock = threading.Lock()
_attested: "OrderedDict[str, Dict[str, Dict[str, Any]]]" = OrderedDict()


def _attest_registry_max() -> int:
    return _registry_max()


def declare_capability(op_id: Any, capability: str, detail: str = "") -> None:
    """A component announces it armed *capability* for this op. NEVER raises."""
    try:
        oid, cap = str(op_id or "").strip(), str(capability or "").strip()
        if not oid or not cap:
            return
        with _attest_lock:
            caps = _attested.setdefault(oid, {})
            caps.setdefault(cap, {
                "state": "declared", "detail": str(detail)[:200],
                "ts": time.time(),
            })
            _attested.move_to_end(oid)
            while len(_attested) > _attest_registry_max():
                _attested.popitem(last=False)
    except Exception:  # noqa: BLE001
        pass


def redeem_capability(op_id: Any, capability: str, detail: str = "") -> None:
    """The consumer announces it actually used *capability*. NEVER raises.

    Redeeming something never declared is recorded rather than dropped: it
    means the USE is wired and the declaration is not, which is the same seam
    defect seen from the other end.
    """
    try:
        oid, cap = str(op_id or "").strip(), str(capability or "").strip()
        if not oid or not cap:
            return
        with _attest_lock:
            caps = _attested.setdefault(oid, {})
            entry = caps.setdefault(cap, {"state": "undeclared_use", "ts": time.time()})
            if entry.get("state") in ("declared", "undeclared_use"):
                entry["state"] = "redeemed" if entry.get("state") == "declared" else "undeclared_use"
            entry["redeemed_detail"] = str(detail)[:200]
            _attested.move_to_end(oid)
    except Exception:  # noqa: BLE001
        pass


def revoke_capability(op_id: Any, capability: str, reason: str) -> None:
    """A downstream state legitimately removed the need for *capability*.

    The escape hatch that keeps attestation from deadlocking honest work: a
    file-deletion goal has no AST to scope, a multi-file op cannot emit a
    single-file diff. Revocation is RECORDED, not silent — an unbroken chain of
    "declared → revoked, because X" is what separates a legitimate branch from
    a capability quietly going missing.
    """
    try:
        oid, cap = str(op_id or "").strip(), str(capability or "").strip()
        if not oid or not cap:
            return
        with _attest_lock:
            caps = _attested.setdefault(oid, {})
            entry = caps.setdefault(cap, {"state": "declared", "ts": time.time()})
            entry["state"] = "revoked"
            entry["reason"] = str(reason)[:200]
            _attested.move_to_end(oid)
        logger.info(
            "[CapabilityRevocationEvent] op=%s capability=%s — %s",
            oid[:20], cap, str(reason)[:160],
        )
    except Exception:  # noqa: BLE001
        pass


def unredeemed_capabilities(op_id: Any) -> Tuple[str, ...]:
    """Capabilities declared for this op that were neither used nor revoked."""
    try:
        oid = str(op_id or "").strip()
        with _attest_lock:
            caps = _attested.get(oid) or {}
            return tuple(
                sorted(c for c, e in caps.items() if e.get("state") == "declared")
            )
    except Exception:  # noqa: BLE001
        return ()


def attest_execution(op_id: Any, *, enforced: Optional[bool] = None) -> "CapabilityVerdict":
    """Was every capability armed for this op actually exercised?

    Report-only unless ``JARVIS_CAPABILITY_ATTESTATION_ENFORCE`` is set. A new
    fatal path spanning every capability is exactly the change that should not
    be armed before a soak has shown what it would have refused — the same
    discipline the surgical scope validator shipped under, for the same reason.
    """
    try:
        pending = unredeemed_capabilities(op_id)
        if not pending:
            return CapabilityVerdict(True, "all declared capabilities redeemed")
        armed = (
            enforced if enforced is not None
            else _flag("JARVIS_CAPABILITY_ATTESTATION_ENFORCE", False)
        )
        reason = (
            "CapabilityAttestationDrift: declared but never exercised: "
            + ", ".join(pending)
        )
        logger.warning(
            "[CapabilityAttestation] op=%s %s — %s", str(op_id)[:20], reason,
            "FAILING" if armed else "reporting only "
            "(JARVIS_CAPABILITY_ATTESTATION_ENFORCE is off)",
        )
        return CapabilityVerdict(
            False, reason, {}, "", enforceable=bool(armed),
            severity=FATAL if armed else RECOVERABLE,
        )
    except Exception:  # noqa: BLE001
        return CapabilityVerdict(True, "attestation unavailable")


def attestation_snapshot(op_id: Any) -> Dict[str, Any]:
    """The full declared/redeemed/revoked chain for one op. Tests + operators."""
    try:
        with _attest_lock:
            return {k: dict(v) for k, v in (_attested.get(str(op_id or "")) or {}).items()}
    except Exception:  # noqa: BLE001
        return {}


def clear_attestations_for_tests() -> None:
    with _attest_lock:
        _attested.clear()


def degradation_lesson_kwargs(
    ctx: Any, verdict: "CapabilityVerdict",
) -> Optional[Dict[str, Any]]:
    """``lesson_memory.record_lesson`` arguments for a degraded generation.

    Built as a pure function, exactly as ``subagent_diagnostic_interceptor``
    builds its own — so the payload is testable without touching the store, and
    the recording seam stays a one-liner. Returns ``None`` when there is
    nothing worth recording. NEVER raises.
    """
    try:
        if verdict is None or getattr(verdict, "ok", True):
            return None
        files = tuple(str(f) for f in (getattr(ctx, "target_files", ()) or ()))
        detail = dict(getattr(verdict, "detail", {}) or {})
        return {
            "op_id": str(getattr(ctx, "op_id", "") or "?"),
            "target_files": files,
            "phase": "GENERATE",
            "failure_class": "capability_degraded",
            # The taxonomy is overridden explicitly because this is not a
            # failure the regex classifier could ever name: the op SUCCEEDS,
            # having quietly been asked for less than it was promised.
            "error_class": "capability_degraded",
            "error_text": str(getattr(verdict, "reason", "") or ""),
            "summary": (
                "CapabilityDegradedWarning: generated under a weaker schema "
                f"than promised (capability={detail.get('capability', '?')} "
                f"served={detail.get('served_model', '-')} "
                f"severity={getattr(verdict, 'severity', RECOVERABLE)}) — "
                "candidate is usable; fidelity was lost, not the generation"
            ),
        }
    except Exception:  # noqa: BLE001
        logger.debug("[CapabilityAssurance] lesson kwargs degraded", exc_info=True)
        return None


def record_degradation(ctx: Any, verdict: "CapabilityVerdict") -> None:
    """Flag the op and persist a CapabilityDegradedWarning lesson.

    Fire-and-forget: ``record_lesson`` is async and this seam is the synchronous
    prompt build, so the write is scheduled on the running loop when there is
    one and skipped when there is not. A lesson that cannot be written must
    never hold up a generation -- the whole point of this change is that
    telemetry does not get to stop work. NEVER raises.
    """
    try:
        mark_degraded(getattr(ctx, "op_id", ""), verdict)
        kwargs = degradation_lesson_kwargs(ctx, verdict)
        if kwargs is None:
            return
        import asyncio  # noqa: PLC0415

        from backend.core.ouroboros.governance.lesson_memory import (  # noqa: PLC0415
            record_lesson,
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop: the flag stands, the lesson is skipped
        loop.create_task(record_lesson(**kwargs))
        with _degraded_lock:
            _degraded_stats["lessons"] += 1
    except Exception:  # noqa: BLE001
        logger.debug("[CapabilityAssurance] degradation record degraded", exc_info=True)


# ---------------------------------------------------------------------------
# Preflight — before an op exists
# ---------------------------------------------------------------------------


def preflight_verdict(
    *,
    target_files: Sequence[str] = (),
    target_symbols: Sequence[str] = (),
    require_symbols: bool = True,
) -> CapabilityVerdict:
    """Can a goal with this shape run at full capability? NEVER raises.

    Checked before dispatch, so a degraded run is refused at the keystroke
    rather than discovered in the diff.
    """
    try:
        files = tuple(str(f) for f in (target_files or ()) if str(f).strip())
        symbols = tuple(str(s) for s in (target_symbols or ()) if str(s).strip())

        checks: Dict[str, bool] = {}
        detail: Dict[str, str] = {}

        # 1. The output schema. FALSE means every candidate is a whole-file
        #    re-emission — the fidelity ceiling, and where the mangle lives.
        from backend.core.ouroboros.governance.providers import (  # noqa: PLC0415
            single_file_diff_schema_enabled,
        )
        checks["diff_schema"] = bool(single_file_diff_schema_enabled())
        detail["diff_schema"] = "JARVIS_SINGLE_FILE_DIFF_SCHEMA_ENABLED"

        # 2. The L3 fan-out. Off, the parent-inheritance path engages only
        #    when an authoritative multi-node PLAN DAG drives it.
        checks["fanout"] = _flag("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED")
        detail["fanout"] = "JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED"

        # 3. The AST-signature anchor: a goal that declares no symbol cannot
        #    have a hallucinated no-op refused, because there is nothing the
        #    candidate is required to have CHANGED.
        checks["declared_symbols"] = bool(symbols) or not require_symbols
        detail["declared_symbols"] = (
            f"{len(symbols)} declared" if symbols else "NONE declared"
        )

        # 4. Single-file scope is a PRECONDITION of the diff schema
        #    (single_file_diff_requested refuses len(target_files) != 1).
        checks["single_file_scope"] = len(files) == 1
        detail["single_file_scope"] = f"{len(files)} target file(s)"

        # 5. The air-gap is a capability too — an op that can reach origin
        #    from an interactive session is not the op the operator asked for.
        from backend.core.ouroboros.governance.remote_push_guard import (  # noqa: PLC0415
            airgap_engaged,
        )
        checks["airgap"] = bool(airgap_engaged())
        detail["airgap"] = "JARVIS_REMOTE_PUSH_AIRGAP"

        failed = sorted(k for k, v in checks.items() if not v)
        if failed:
            return CapabilityVerdict(
                False,
                "degraded: " + ", ".join(
                    f"{k} ({detail.get(k, '')})" for k in failed
                ),
                checks, detail,
            )
        return CapabilityVerdict(True, "full capability", checks, detail)
    except Exception as exc:  # noqa: BLE001
        # An assurance that cannot run is itself a failure — fail CLOSED.
        logger.warning("[CapabilityAssurance] preflight degraded: %r", exc)
        return CapabilityVerdict(
            False, f"assurance_unavailable:{type(exc).__name__}",
        )


# ---------------------------------------------------------------------------
# Runtime — with the real context, at the schema decision
# ---------------------------------------------------------------------------


def assert_generation_capability(
    ctx: Any, *, force_full_content: bool,
) -> CapabilityVerdict:
    """Is this op about to be asked for the schema it was promised?

    Returns OK for every case where full content is the CORRECT answer —
    multi-file scope, a model that cannot produce diffs, the flag deliberately
    off. It fails only on genuine degradation: the flag on, the served model
    diff-capable, the scope single-file, and the decision still full_content.

    NEVER raises.
    """
    try:
        from backend.core.ouroboros.governance.providers import (  # noqa: PLC0415
            _ctx_schema_capability,
            single_file_diff_requested,
            single_file_diff_schema_enabled,
        )

        # Only a SANCTIONED op may be aborted — see CapabilityVerdict.enforceable.
        sanctioned = False
        try:
            from backend.core.ouroboros.governance.autonomy.parent_inheritance import (  # noqa: E501,PLC0415
                goal_pointer_for,
            )
            sanctioned = bool(goal_pointer_for(ctx))
        except Exception:  # noqa: BLE001
            sanctioned = False

        files = tuple(getattr(ctx, "target_files", ()) or ())
        capability = str(_ctx_schema_capability(ctx) or "")
        served = ""
        try:
            ri = getattr(getattr(ctx, "telemetry", None), "routing_intent", None)
            served = str(getattr(ri, "served_model", "") or "")
        except Exception:  # noqa: BLE001
            served = ""

        checks = {
            "diff_schema_flag": bool(single_file_diff_schema_enabled()),
            "telemetry_present": getattr(ctx, "telemetry", None) is not None,
            "single_file_scope": len(files) == 1,
        }
        detail = {
            "capability": capability or "?",
            "served_model": served or "-",
            "target_files": str(len(files)),
        }

        # Not applicable: these are correct outcomes, not degradation.
        if len(files) != 1:
            return CapabilityVerdict(
                True, "multi-file scope — full content is correct",
                checks, detail, enforceable=sanctioned,
            )
        if not checks["diff_schema_flag"]:
            return CapabilityVerdict(
                True, "diff schema deliberately off",
                checks, detail, enforceable=sanctioned,
            )
        if capability != "full_content_and_diff":
            # The served model genuinely cannot produce diffs -> correct.
            # But a MISSING telemetry is not the model saying no; it is nobody
            # having asked, and that IS the degradation — for a SANCTIONED op.
            # For anything else (an ambient tool call, a probe, a unit test)
            # a bare context is ordinary and must not be treated as a fault.
            if not checks["telemetry_present"]:
                return CapabilityVerdict(
                    False,
                    "routing admission missing: ctx.telemetry is None, so the "
                    "served model's schema capability was never resolved and "
                    "the 2b.1-diff schema is structurally unreachable "
                    "(capability=? brain=-)",
                    checks, detail, enforceable=sanctioned,
                    # RECOVERABLE: the admission is missing, but full_content is
                    # still requested and still produces a usable candidate.
                    severity=RECOVERABLE,
                )
            return CapabilityVerdict(
                True, f"served model is {capability} — full content is correct",
                checks, detail, enforceable=sanctioned,
            )

        if single_file_diff_requested(ctx, force_full_content=force_full_content):
            return CapabilityVerdict(
                True, "2b.1-diff in play", checks, detail, enforceable=sanctioned,
            )

        return CapabilityVerdict(
            False,
            "CapabilityExecutionDrift: the diff schema is armed and "
            f"{served or 'the served model'} is diff-capable on a single-file "
            "op, but the schema decision came out full_content with no "
            "explicit fallback trigger",
            checks, detail, enforceable=sanctioned,
            # FATAL now, and the reason it was not before has been removed.
            #
            # This verdict fired six times in soak bt-2026-09-17-184946 and
            # nothing acted on it, because at the time a full_content decision
            # on a diff-capable single-file op was LEGITIMATE: the size gate
            # forced it for any file under 800 lines. Refusing to generate
            # could not be the response to a deliberate, documented heuristic,
            # so RECOVERABLE was the honest severity.
            #
            # With the size gate gone, `single_file_diff_requested` returns
            # True for exactly the conditions this branch has already
            # confirmed. Reaching here therefore means the schema decision
            # disagrees with the capability that was negotiated for it — a
            # silent downgrade with no stated cause, which is the defect class
            # that produced three damaged commits while every precondition
            # reported ok.
            #
            # Still bounded by `enforceable`: only an op carrying a signed-goal
            # pointer is failed. An ambient tool call or a probe reports and
            # proceeds, because a diagnostic must not become the thing that
            # breaks unsanctioned work.
            severity=FATAL,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[CapabilityAssurance] runtime check degraded: %r", exc)
        return CapabilityVerdict(
            False, f"assurance_unavailable:{type(exc).__name__}",
            # The CHECK broke, which is no evidence at all about the schema.
            # Aborting here would let a bug in the diagnostic kill the work it
            # was written to observe.
            severity=RECOVERABLE,
        )
