"""Route a tool call to a machine capability — and never hold the loop open.

`capability_registry` derives 42 macOS capabilities and answers, for each one,
whether it needs the Iron Gate. This is what a tool loop does with that answer.

THE DEADLOCK THIS EXISTS TO AVOID
-----------------------------------
The obvious implementation awaits the operator inside the tool call:

    if gated: result = await provider.await_decision(rid, timeout_s)   # NO

`await_decision` is documented as "block until a decision is made or timeout
expires". Awaiting it inside a generation turn holds the LLM's task open for as
long as a human takes to read a prompt — which outlives the provider timeout,
the route's generation budget (`orchestrator.py:6337`, 120s on IMMEDIATE), and
the operator's patience, in that order. The turn dies of a timeout that had
nothing to do with the model.

So the gate is a SUSPENSION, not a wait. The router requests a decision and
returns immediately. The generation task ends cleanly, the event loop is free,
and the operator answers on their own clock through the surfaces that already
exist. Re-entry is a NEW turn carrying the outcome as context.

    call → registry → gated? → request() → Outcome.SUSPENDED → turn ends
                                              ↓ (out of band)
    operator answers → resume(rid) → APPROVED → execute → re-trigger
                                   → DENIED   → synthetic result → re-trigger

WHY A DENIAL IS A RESULT AND NOT AN EXCEPTION
-----------------------------------------------
Raising on denial makes the model see an error, and a model that sees an error
retries — often with a slightly reworded call, which is the same request wearing
a hat. `OPERATOR_DENIED_EXECUTION` is fed back as a normal tool RESULT so the
refusal enters the context as a fact the agent must reason about. The same
reason `ask_human` is a tool rather than an interrupt.

DRY
-----
No new prompt UI. `ApprovalProvider` (request / approve / reject /
await_decision) and `inline_approval`'s bounded queue already exist and already
render; this only decides WHEN to ask and what to do with the answer. The
registry decides IF.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import enum
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger("JARVIS.CapabilityRouter")

CAPABILITY_ROUTER_SCHEMA_VERSION: str = "capability_router.v1"

#: Fed back to the model verbatim when the operator refuses. A stable, greppable
#: token rather than prose: the agent must be able to recognise this exact
#: condition across turns, and a reworded sentence would read as a new failure.
DENIED_PAYLOAD: str = "[SYSTEM: OPERATOR_DENIED_EXECUTION]"
SUSPENDED_NOTE: str = "[SYSTEM: Awaiting operator consent]"
EXPIRED_PAYLOAD: str = "[SYSTEM: OPERATOR_CONSENT_TIMED_OUT]"


def router_enabled() -> bool:
    """Master gate. Default TRUE. NEVER raises."""
    return (os.environ.get("JARVIS_CAPABILITY_ROUTER_ENABLED", "true")
            or "").strip().lower() not in ("0", "false", "no", "off")


def consent_ttl_s() -> float:
    """How long a suspended call stays resumable. Clamped. NEVER raises.

    Not a wait — nothing blocks for this. It bounds how long a pending consent
    remains meaningful, so an operator answering tomorrow does not execute a
    command whose world has moved on.
    """
    try:
        v = float(os.environ.get("JARVIS_CAPABILITY_CONSENT_TTL_S", "900"))
    except (TypeError, ValueError):
        v = 900.0
    return max(30.0, min(v, 24 * 3600.0))


class Outcome(str, enum.Enum):
    """What happened to a tool call."""

    EXECUTED = "executed"
    SUSPENDED = "suspended"      # awaiting consent — the turn must END here
    DENIED = "denied"
    EXPIRED = "expired"
    UNKNOWN_CAPABILITY = "unknown_capability"
    FAILED = "failed"


@dataclass
class RoutedCall:
    """The result of routing one tool call. Transport-safe."""

    outcome: str
    capability: str = ""
    request_id: str = ""
    #: The challenge the signed HUD must echo back. Never logged in full and
    #: never reused — see `_mint_nonce`.
    nonce: str = ""
    result: Any = None
    #: What to append to the LLM context. ALWAYS populated — a turn that ends
    #: without telling the model why is a turn the model will simply repeat.
    context_note: str = ""
    detail: str = ""
    schema_version: str = CAPABILITY_ROUTER_SCHEMA_VERSION

    @property
    def suspended(self) -> bool:
        return self.outcome == Outcome.SUSPENDED.value

    @property
    def should_retrigger(self) -> bool:
        """Whether the orchestrator should start a new turn with this result.

        True for every RESOLVED outcome, denial included. A denial that does not
        re-trigger leaves the agent mid-plan with no idea its plan was refused.
        """
        return self.outcome in (
            Outcome.EXECUTED.value, Outcome.DENIED.value,
            Outcome.EXPIRED.value, Outcome.FAILED.value,
            Outcome.UNKNOWN_CAPABILITY.value,
        )


@dataclass
class _Suspended:
    """A call parked awaiting consent. Holds no coroutine and no lock."""

    request_id: str
    capability: str
    args: Dict[str, Any] = field(default_factory=dict)
    created: float = field(default_factory=time.time)
    #: One-time challenge. The verdict must echo THIS value or it is not an
    #: answer to THIS question — see `_verify_nonce`.
    nonce: str = ""

    def expired(self) -> bool:
        return (time.time() - self.created) > consent_ttl_s()


class CapabilityRouter:
    """Registry + gate + executor, with a suspension boundary. NEVER raises."""

    def __init__(self, *, registry: Any = None, provider: Any = None,
                 target: Any = None) -> None:
        self._registry = registry
        self._provider = provider
        self._target = target
        self._parked: Dict[str, _Suspended] = {}
        self._stats: Dict[str, int] = {
            "executed": 0, "suspended": 0, "denied": 0, "expired": 0,
            "unknown": 0, "failed": 0,
        }

    # -- resolution ------------------------------------------------------

    def _reg(self) -> Any:
        if self._registry is None:
            from backend.system_control.capability_registry import (
                get_capability_registry,
            )
            self._registry = get_capability_registry()
        return self._registry

    def _instance(self) -> Any:
        """The controller INSTANCE — needed to execute, unlike describing.

        The registry deliberately reflects over the class so describing costs
        nothing. Executing genuinely needs an object, so it is constructed here
        and only here, lazily, and a failure to construct is a FAILED call
        rather than an import-time explosion.
        """
        if self._target is None:
            from backend.system_control.macos_controller import MacOSController
            get = getattr(MacOSController, "get_instance", None)
            self._target = get() if callable(get) else MacOSController()
        return self._target

    # -- the boundary ----------------------------------------------------

    async def route(self, name: str, args: Optional[Dict[str, Any]] = None,
                    *, op_id: str = "") -> RoutedCall:
        """Route one tool call. Returns immediately — NEVER awaits an operator.

        The whole contract is in that sentence. A gated capability yields
        SUSPENDED with a request id; the caller ends its turn and resumes later
        through :meth:`resume`.
        """
        args = dict(args or {})
        try:
            if not router_enabled():
                return await self._execute(name, args)
            reg = self._reg()
            cap = reg.get(name) if reg is not None else None
            if cap is None:
                self._stats["unknown"] += 1
                return RoutedCall(
                    outcome=Outcome.UNKNOWN_CAPABILITY.value, capability=name,
                    context_note=f"[SYSTEM: UNKNOWN_CAPABILITY {name}]",
                    detail="not in the derived registry")
            if not cap.iron_gate_required:
                return await self._execute(name, args)

            # GATED. Request consent and RETURN — do not await the human.
            rid = await self._request_consent(name, args, op_id=op_id)
            if not rid:
                # No provider wired. Fail CLOSED: a gated capability with no way
                # to ask is not permission to proceed.
                self._stats["denied"] += 1
                return RoutedCall(
                    outcome=Outcome.DENIED.value, capability=name,
                    context_note=DENIED_PAYLOAD,
                    detail="no approval provider available — failing closed")
            nonce = _mint_nonce()
            self._parked[rid] = _Suspended(rid, name, args, nonce=nonce)
            self._stats["suspended"] += 1
            logger.info("[CapabilityRouter] '%s' SUSPENDED awaiting consent "
                        "(request=%s) — turn released", name, rid[:12])
            return RoutedCall(
                outcome=Outcome.SUSPENDED.value, capability=name,
                request_id=rid, nonce=nonce, context_note=SUSPENDED_NOTE,
                detail="operator consent required")
        except Exception as exc:  # noqa: BLE001 — a router never kills a turn
            self._stats["failed"] += 1
            logger.debug("[CapabilityRouter] route degraded", exc_info=True)
            return RoutedCall(outcome=Outcome.FAILED.value, capability=name,
                              context_note=f"[SYSTEM: TOOL_FAILED {name}]",
                              detail=f"{type(exc).__name__}: {exc}")

    async def resume(self, request_id: str,
                     decision: Optional[Any] = None) -> RoutedCall:
        """Re-enter a suspended call once the operator has answered.

        *decision* may be an `ApprovalResult`, an `ApprovalStatus`, or a plain
        string — the surfaces that answer differ and none of them should have to
        learn this module's vocabulary.
        """
        try:
            parked = self._parked.pop(request_id, None)
            if parked is None:
                return RoutedCall(
                    outcome=Outcome.FAILED.value, request_id=request_id,
                    context_note="[SYSTEM: UNKNOWN_CONSENT_REQUEST]",
                    detail="no suspended call for that id")
            if parked.expired():
                self._stats["expired"] += 1
                return RoutedCall(
                    outcome=Outcome.EXPIRED.value, capability=parked.capability,
                    request_id=request_id, context_note=EXPIRED_PAYLOAD,
                    detail=f"consent not given within {consent_ttl_s():.0f}s")
            # CHALLENGE-RESPONSE. A verdict is only an answer to THIS question
            # if it carries THIS question's nonce. Without it, anything that can
            # write to the IPC socket can replay a captured approval — the
            # attack the signed-bundle boundary exists to stop, defeated one
            # layer below it.
            if parked.nonce and not _verify_nonce(parked.nonce, decision):
                self._stats["denied"] += 1
                logger.warning(
                    "[CapabilityRouter] verdict for '%s' failed the nonce "
                    "challenge — treating as DENIED", parked.capability)
                return RoutedCall(
                    outcome=Outcome.DENIED.value, capability=parked.capability,
                    request_id=request_id, context_note=DENIED_PAYLOAD,
                    detail="verdict did not echo the challenge nonce")
            if _approved(decision):
                out = await self._execute(parked.capability, parked.args)
                out.request_id = request_id
                return out
            self._stats["denied"] += 1
            logger.info("[CapabilityRouter] '%s' DENIED by operator",
                        parked.capability)
            # A RESULT, not an exception: the agent must reason about the
            # refusal rather than retry a reworded version of the same call.
            return RoutedCall(
                outcome=Outcome.DENIED.value, capability=parked.capability,
                request_id=request_id, context_note=DENIED_PAYLOAD,
                detail="operator declined")
        except Exception as exc:  # noqa: BLE001
            self._stats["failed"] += 1
            return RoutedCall(outcome=Outcome.FAILED.value,
                              request_id=request_id,
                              context_note="[SYSTEM: CONSENT_RESUME_FAILED]",
                              detail=f"{type(exc).__name__}: {exc}")

    # -- internals -------------------------------------------------------

    async def _request_consent(self, name: str, args: Dict[str, Any], *,
                               op_id: str) -> str:
        """Ask, and return the request id. NEVER waits. NEVER raises."""
        try:
            provider = self._provider
            if provider is None:
                return ""
            ctx = _ConsentContext(op_id=op_id or f"cap:{name}",
                                  capability=name, args=args)
            rid = await provider.request(ctx)
            return str(rid or "")
        except Exception:  # noqa: BLE001
            logger.debug("[CapabilityRouter] consent request degraded",
                         exc_info=True)
            return ""

    async def _execute(self, name: str, args: Dict[str, Any]) -> RoutedCall:
        """Invoke the capability. NEVER raises."""
        try:
            import inspect
            fn = getattr(self._instance(), name, None)
            if not callable(fn):
                self._stats["unknown"] += 1
                return RoutedCall(
                    outcome=Outcome.UNKNOWN_CAPABILITY.value, capability=name,
                    context_note=f"[SYSTEM: UNKNOWN_CAPABILITY {name}]")
            result = fn(**args)
            if inspect.isawaitable(result):
                result = await result
            self._stats["executed"] += 1
            return RoutedCall(outcome=Outcome.EXECUTED.value, capability=name,
                              result=result, context_note=str(result)[:2000])
        except Exception as exc:  # noqa: BLE001
            self._stats["failed"] += 1
            logger.debug("[CapabilityRouter] execute(%s) failed", name,
                         exc_info=True)
            return RoutedCall(
                outcome=Outcome.FAILED.value, capability=name,
                context_note=f"[SYSTEM: TOOL_FAILED {name}: "
                             f"{type(exc).__name__}]",
                detail=f"{type(exc).__name__}: {exc}")

    # -- observability ---------------------------------------------------

    def pending(self) -> Dict[str, str]:
        """Suspended calls, id → capability. NEVER raises."""
        try:
            return {rid: s.capability for rid, s in self._parked.items()}
        except Exception:  # noqa: BLE001
            return {}

    def stats(self) -> Dict[str, Any]:
        return {"schema_version": CAPABILITY_ROUTER_SCHEMA_VERSION,
                "enabled": router_enabled(), "pending": len(self._parked),
                **self._stats}


@dataclass
class _ConsentContext:
    """Minimal shape an `ApprovalProvider.request` needs. Frozen in spirit."""

    op_id: str
    capability: str
    args: Dict[str, Any] = field(default_factory=dict)

    @property
    def description(self) -> str:
        return f"Run macOS capability '{self.capability}' with {self.args or '{}'}"


def _mint_nonce() -> str:
    """A one-time challenge. NEVER raises.

    `secrets.token_urlsafe` rather than uuid4: this is an anti-replay token, so
    it wants a CSPRNG, and uuid4's guarantee is uniqueness rather than
    unpredictability. 32 bytes because the cost is nothing and the value is a
    socket anyone on the box can write to.
    """
    try:
        import secrets
        return secrets.token_urlsafe(32)
    except Exception:  # noqa: BLE001
        return ""


def _verify_nonce(expected: str, decision: Any) -> bool:
    """Does this verdict answer THIS challenge? NEVER raises.

    Constant-time compare — a verdict arriving over a local socket is attacker-
    influenced input, and an early-exit `==` leaks the prefix one byte at a
    time to anything that can time it.

    Fails CLOSED on a missing or unreadable nonce: a verdict that cannot prove
    which question it answers is not an answer.
    """
    try:
        import hmac
        got = ""
        if isinstance(decision, dict):
            got = str(decision.get("nonce") or "")
        else:
            got = str(getattr(decision, "nonce", "") or "")
        if not got or not expected:
            return False
        return hmac.compare_digest(str(expected), got)
    except Exception:  # noqa: BLE001
        return False


def _approved(decision: Any) -> bool:
    """Did the operator say yes? NEVER raises.

    Fails CLOSED on anything unrecognisable. A decision object this module does
    not understand is not consent — the same inversion the registry makes about
    unclassified capabilities.
    """
    try:
        if decision is None:
            return False
        # The IPC delivers JSON, so a verdict from the signed HUD arrives as a
        # dict. Reading only `.status` would have made every real verdict from
        # Swift fall through to denial — safe, and completely broken.
        if isinstance(decision, dict):
            status = decision.get("status", decision.get("approved"))
            if isinstance(status, bool):
                return status
        else:
            status = getattr(decision, "status", decision)
        name = getattr(status, "name", None) or str(status)
        return str(name).strip().upper() in ("APPROVED", "APPROVE", "YES", "Y",
                                             "TRUE")
    except Exception:  # noqa: BLE001
        return False


_ROUTER: Optional[CapabilityRouter] = None


def get_capability_router() -> CapabilityRouter:
    """Process-wide router. NEVER raises."""
    global _ROUTER
    if _ROUTER is None:
        _ROUTER = CapabilityRouter()
    return _ROUTER


def reset_capability_router() -> None:
    """Testing seam. NEVER raises."""
    global _ROUTER
    _ROUTER = None
