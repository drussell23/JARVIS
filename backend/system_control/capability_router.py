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
    #: Set when this call OPENED a session. Surfaced rather than kept internal
    #: because a model that started a stream and was never told it holds
    #: something open has no reason to ever stop it.
    lease_id: str = ""
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
                 target: Any = None, federation: Any = None,
                 leases: Any = None) -> None:
        self._registry = registry
        self._provider = provider
        self._target = target
        self._federation = federation
        self._leases = leases
        # Distinct "we already tried and could not" flags rather than parking a
        # False in the slot itself. Overloading the value would make `None` mean
        # both "not looked up yet" and "unavailable", and every reader would
        # have to know which — the same conflation `Readiness.UNHYDRATED` exists
        # to prevent one layer up.
        self._federation_tried = federation is not None
        self._leases_tried = leases is not None
        self._releaser_installed = False
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

    def _fed(self) -> Any:
        """The federated vocabulary. NEVER raises.

        Separate from `_reg` rather than merged behind one resolver because the
        two answer different questions. The registry describes ONE controller
        and needs no hydration; the federation spans subsystems whose imports
        measured 8.4 s, and a call that silently blocked on that inside `route`
        would stall the IPC loop the HUD reads from. Keeping them distinct is
        what makes `get()` honestly synchronous on both.
        """
        if self._federation is None and not self._federation_tried:
            self._federation_tried = True
            try:
                from backend.system_control.capability_federation import (
                    get_federation,
                )
                self._federation = get_federation()
            except Exception:  # noqa: BLE001 — macOS capabilities still work
                logger.debug("[CapabilityRouter] federation unavailable",
                             exc_info=True)
        return self._federation

    def _book(self) -> Any:
        """The lease book, with this router installed as its releaser.

        Installed HERE rather than at import: the book is constructed before any
        router exists, and a releaser bound at import time would capture whatever
        singleton happened to exist first — which in tests is never the one under
        test.
        """
        if self._leases is None and not self._leases_tried:
            self._leases_tried = True
            try:
                from backend.system_control.capability_leases import (
                    get_lease_book,
                )
                self._leases = get_lease_book()
            except Exception:  # noqa: BLE001
                logger.debug("[CapabilityRouter] lease book unavailable",
                             exc_info=True)
        book = self._leases
        if book is not None and not self._releaser_installed:
            try:
                book.set_releaser(self._release)
                self._releaser_installed = True
            except Exception:  # noqa: BLE001
                pass
        return book

    def _lookup(self, name: str) -> Any:
        """The CapabilityDef for a name, local or federated. NEVER raises.

        A dot decides. `lock_screen` is the derived macOS vocabulary;
        `video.start_streaming` is a federated namespace. Trying both for every
        name would let a federated capability shadow a controller method that
        happened to share its bare name, which is exactly the ambiguity
        namespacing was introduced to end.
        """
        try:
            if "." in (name or ""):
                fed = self._fed()
                return fed.get(name) if fed is not None else None
            reg = self._reg()
            return reg.get(name) if reg is not None else None
        except Exception:  # noqa: BLE001
            return None

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
            cap = self._lookup(name)
            if cap is None:
                self._stats["unknown"] += 1
                return RoutedCall(
                    outcome=Outcome.UNKNOWN_CAPABILITY.value, capability=name,
                    context_note=f"[SYSTEM: UNKNOWN_CAPABILITY {name}]",
                    detail=self._why_unknown(name))
            if not cap.iron_gate_required:
                return await self._execute(name, args)

            # GATED. Request consent and RETURN — do not await the human.
            #
            # The nonce is minted BEFORE the request, not after. The signed HUD
            # must ECHO this exact challenge, so it has to travel WITH the
            # question; minting it afterwards meant the prompt went out with no
            # nonce, `SecureConsent.Challenge` failed closed on the empty field,
            # and the request vanished without the operator ever seeing it.
            nonce = _mint_nonce()
            rid = await self._request_consent(name, args, op_id=op_id,
                                              nonce=nonce)
            if not rid:
                # No provider wired, or nothing connected to ask. Fail CLOSED: a
                # gated capability with no way to ask is not permission to
                # proceed.
                self._stats["denied"] += 1
                return RoutedCall(
                    outcome=Outcome.DENIED.value, capability=name,
                    context_note=DENIED_PAYLOAD,
                    detail="no approval provider available — failing closed")
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
                               op_id: str, nonce: str = "") -> str:
        """Ask, and return the request id. NEVER waits. NEVER raises."""
        try:
            provider = self._provider
            if provider is None:
                return ""
            ctx = _ConsentContext(op_id=op_id or f"cap:{name}",
                                  capability=name, args=args, nonce=nonce,
                                  session=self._session_kind(name))
            rid = await provider.request(ctx)
            return str(rid or "")
        except Exception:  # noqa: BLE001
            logger.debug("[CapabilityRouter] consent request degraded",
                         exc_info=True)
            return ""

    def _session_kind(self, name: str) -> str:
        """"start" if approving this opens something continuous. NEVER raises."""
        try:
            cap = self._lookup(name)
            return "start" if getattr(cap, "starts_session", False) else ""
        except Exception:  # noqa: BLE001
            return ""

    def _why_unknown(self, name: str) -> str:
        """Explain an unknown name. NEVER raises.

        "Not in the derived registry" is true of a typo AND of a real capability
        whose namespace has not finished importing — and a model told the same
        sentence for both will abandon a tool that would have worked in another
        second. UNHYDRATED and ABSENT are different facts, so they get different
        words, the same distinction `Readiness` refuses to collapse.
        """
        try:
            if "." not in (name or ""):
                return "not in the derived registry"
            fed = self._fed()
            if fed is None:
                return "federation unavailable; namespaced capabilities are off"
            from backend.system_control.capability_federation import (
                Readiness, split,
            )
            ns, _ = split(name)
            if ns not in fed.namespaces():
                return (f"no namespace '{ns}' — known: "
                        f"{', '.join(fed.namespaces()) or 'none'}")
            st = fed.stats().get("namespaces", {}).get(ns, {})
            readiness = st.get("readiness")
            if readiness == Readiness.UNHYDRATED.value:
                return (f"namespace '{ns}' has not hydrated yet — retry, or "
                        f"call warm() at boot")
            if readiness == Readiness.DEGRADED.value:
                return (f"namespace '{ns}' is DEGRADED: "
                        f"{str(st.get('detail') or 'no detail')[:160]}")
            return f"namespace '{ns}' is ready but exports no '{name}'"
        except Exception:  # noqa: BLE001
            return "not in the derived registry"

    def _resolve_call(self, name: str) -> Any:
        """(callable, capability_def) for a name. (None, def) if uncallable.

        The one place a federated EXPORT name becomes a METHOD. `stop_actuator`
        is exported under an alias and implemented as `stop`; invoking the
        export name on the instance would raise `AttributeError` — the collision
        fix breaking the very calls it was added to make possible.
        """
        cap = self._lookup(name)
        if "." not in (name or ""):
            return (getattr(self._instance(), name, None), cap)
        fed = self._fed()
        if fed is None:
            return (None, cap)
        target = fed.resolve_target(name)
        if target is None:
            return (None, cap)
        return (getattr(target, fed.method_for(name) or "", None), cap)

    async def _execute(self, name: str, args: Dict[str, Any]) -> RoutedCall:
        """Invoke the capability, and book its session if it opened one.

        NEVER raises. This is the ONE seam both entry paths funnel through —
        an ungated direct call and a post-consent `resume` — which is why the
        lease bookkeeping lives here and not in `route`. Putting it in `route`
        would have left every approved session unbooked, and an approved session
        is precisely the long-running one.
        """
        try:
            import inspect
            fn, cap = self._resolve_call(name)
            if not callable(fn):
                self._stats["unknown"] += 1
                return RoutedCall(
                    outcome=Outcome.UNKNOWN_CAPABILITY.value, capability=name,
                    context_note=f"[SYSTEM: UNKNOWN_CAPABILITY {name}]",
                    detail=self._unbuildable_detail(name))
            result = fn(**args)
            if inspect.isawaitable(result):
                result = await result
            self._stats["executed"] += 1
            out = RoutedCall(outcome=Outcome.EXECUTED.value, capability=name,
                             result=result, context_note=str(result)[:2000])
            self._book_session(name, args, cap, result, out)
            return out
        except TypeError as exc:
            # Separated from the general case because it is almost always the
            # model inventing an argument, and a note that says so is worth more
            # to the next turn than a bare type name.
            self._stats["failed"] += 1
            return RoutedCall(
                outcome=Outcome.FAILED.value, capability=name,
                context_note=f"[SYSTEM: TOOL_FAILED {name}: bad arguments]",
                detail=f"{exc} — check the tool's declared parameters")
        except Exception as exc:  # noqa: BLE001
            self._stats["failed"] += 1
            logger.debug("[CapabilityRouter] execute(%s) failed", name,
                         exc_info=True)
            return RoutedCall(
                outcome=Outcome.FAILED.value, capability=name,
                context_note=f"[SYSTEM: TOOL_FAILED {name}: "
                             f"{type(exc).__name__}]",
                detail=f"{type(exc).__name__}: {exc}")

    def _unbuildable_detail(self, name: str) -> str:
        """Why a KNOWN federated capability had no instance. NEVER raises."""
        try:
            fed = self._fed()
            if fed is None or "." not in name:
                return "capability is not callable on its target"
            from backend.system_control.capability_federation import split
            ns, _ = split(name)
            for key, why in fed.unbuildable().items():
                if any(s.key == key for s in fed.providers(ns)):
                    return f"provider {key} could not be constructed: {why}"
            return "capability is not callable on its target"
        except Exception:  # noqa: BLE001
            return "capability is not callable on its target"

    def _book_session(self, name: str, args: Dict[str, Any], cap: Any,
                      result: Any, out: RoutedCall) -> None:
        """Open or discharge a lease for a call that just succeeded. NEVER raises.

        A START that RETURNED FALSE opened nothing, and booking it would have
        the reaper later call `stop_streaming` on a stream that never started —
        harmless here, but it is the shape of bug that makes a lease book stop
        being believed.
        """
        try:
            if cap is None:
                return
            book = self._book()
            if book is None:
                return
            from backend.system_control.capability_leases import (
                current_principal,
            )
            if getattr(cap, "starts_session", False):
                if result is False:
                    logger.info("[CapabilityRouter] '%s' reported failure — "
                                "no lease opened", name)
                    return
                lease = book.open(
                    name, _qualify(name, cap.release),
                    owner=current_principal(), args=dict(args or {}))
                if lease is not None:
                    out.lease_id = lease.lease_id
                    out.context_note = (
                        f"{out.context_note}\n[SYSTEM: SESSION OPEN — '{name}' "
                        f"is now running continuously. Call "
                        f"'{_qualify(name, cap.release)}' to stop it.]")[:2400]
            elif getattr(cap, "ends_session", False):
                # The release already ran; this only records it. See
                # `LeaseBook.discharge` on why this must not call anything.
                book.discharge(name, owner=current_principal())
        except Exception:  # noqa: BLE001
            logger.debug("[CapabilityRouter] session bookkeeping degraded",
                         exc_info=True)

    async def _release(self, name: str, args: Dict[str, Any]) -> bool:
        """Stop a session on the reaper's behalf. NEVER raises.

        Goes through `route`, not around it. An END is forced SAFE_AUTO at
        classification time, so this executes without consent by the RULE rather
        than by a bypass — and if that rule is ever violated the call SUSPENDS,
        which is reported here as a failure the operator can see instead of a
        silent success the book would believe.
        """
        try:
            from backend.system_control.capability_leases import (
                REAPER_PRINCIPAL, set_principal,
            )
            set_principal(REAPER_PRINCIPAL)
            routed = await self.route(name, args)
            if routed.outcome != Outcome.EXECUTED.value:
                logger.error("[CapabilityRouter] release '%s' did not execute "
                             "(%s: %s) — the session is still open", name,
                             routed.outcome, routed.detail or "-")
                return False
            # A stop method returning None is the normal Python spelling of
            # "done". Only an explicit False is a refusal.
            return routed.result is not False
        except Exception as exc:  # noqa: BLE001
            logger.error("[CapabilityRouter] release '%s' raised: %s", name, exc)
            return False

    # -- observability ---------------------------------------------------

    def pending(self) -> Dict[str, str]:
        """Suspended calls, id → capability. NEVER raises."""
        try:
            return {rid: s.capability for rid, s in self._parked.items()}
        except Exception:  # noqa: BLE001
            return {}

    def stats(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "schema_version": CAPABILITY_ROUTER_SCHEMA_VERSION,
            "enabled": router_enabled(), "pending": len(self._parked),
            **self._stats,
        }
        # Folded in rather than left to a second endpoint: "what is running"
        # and "what did the router do" are one question when the answer is a
        # session somebody has to go and stop.
        try:
            book = self._leases
            if book is not None:
                ls = book.stats()
                out["sessions_active"] = ls.get("active", 0)
                out["sessions_orphaned"] = ls.get("orphaned", 0)
                out["sessions"] = ls.get("capabilities", [])
        except Exception:  # noqa: BLE001
            pass
        return out


@dataclass
class _ConsentContext:
    """Minimal shape an `ApprovalProvider.request` needs. Frozen in spirit."""

    op_id: str
    capability: str
    args: Dict[str, Any] = field(default_factory=dict)
    #: The one-time challenge the verdict must echo. Carried WITH the question
    #: because a challenge that arrives after the prompt is not a challenge.
    nonce: str = ""
    #: "start" when approving this opens something that keeps running.
    session: str = ""

    @property
    def description(self) -> str:
        """What the operator is actually being asked. This becomes the text of
        the Touch ID dialog, so it says what will happen rather than naming a
        method — and it says out loud when a session is about to be opened,
        because approving a continuous observer is a different decision from
        approving one action that ends when it returns.
        """
        detail = f"Run '{self.capability}'"
        if self.args:
            detail += f" with {self.args}"
        if self.session == "start":
            detail += " — this will KEEP RUNNING until it is stopped"
        return detail


def _qualify(start_name: str, release: str) -> str:
    """Put a release in the same namespace as the start that named it.

    A tag says ``release=stop_streaming`` because that is what the author of
    `video.start_streaming` calls it. The reaper routes by FULL name, so an
    unqualified release would be looked up as a bare macOS capability, miss, and
    leave the session open — a leak whose only symptom is an
    `UNKNOWN_CAPABILITY` line in a log nobody is reading at the time.
    """
    if not release or "." in release:
        return release
    ns, _, bare = (start_name or "").partition(".")
    return f"{ns}.{release}" if bare else release


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
