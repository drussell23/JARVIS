"""``/model`` — which brain runs the work, and who decides.

O+V routes across three lanes: DoubleWord's fleet, Anthropic's Claude models,
and the self-hosted J-Prime golden image. The choice is made per-op by
`provider_topology` from `brain_selection_policy.yaml` — deliberately, and
against live-fire calibration — and an operator had no way to see the decision
or take it.

THIS IS A SURFACE, NOT A SECOND ROUTER
----------------------------------------
The override already exists and is already consulted. The "Sovereign
Context-Routing Override Matrix" reads ``JARVIS_DW_PRIMARY_OVERRIDE`` via
`model_pinning_heuristic.model_pin_override()` — **per call**, so "an operator
can flip the pin live" — promotes a healthy pin to Rank 1 in
`_pin_and_fleet_guarded`, soft-locks it on repeated failure, and filters
entitlement LAST so a pin cannot resurrect a model the endpoint refuses.

So this verb sets that pin and reads that topology. It computes no ranking,
keeps no parallel state, and owns no policy. Writing a second selector would
have produced two answers to "which model is running" — which is the defect
this cockpit has spent a day removing from other surfaces.

WHAT AUTO MEANS, AND WHY IT IS THE DEFAULT
--------------------------------------------
The policy is segmented on measured evidence, not preference: DW is excluded
from the Prefrontal Cortex because live-fire (bbpst3ebf) showed DW models time
out on COMPLEX generation; BACKGROUND and SPECULATIVE are sealed because
bt-2026-04-14-182446 produced 0/13 Gemma successes and routing daemons to a
$3/$15-per-M provider "violates the unit economics". The yaml says plainly:
"Do not mutate at runtime."

A pin is SOVEREIGN, and the first draft of this module said the opposite.

I wrote "a pin re-ranks within what the topology already admits; it cannot
open a sealed route", then measured it:

    dw_models_for_route("immediate")  ->  ()                      unpinned
    dw_models_for_route("immediate")  ->  ("Qwen/Qwen3.5-397B…",)  pinned

The Override Matrix injects the pin into an EMPTY ladder, so pinning routes
IMMEDIATE to DoubleWord — the exact lane the policy excludes from the
Prefrontal Cortex because live fire showed DW timing out there. The mechanism
is named "Sovereign" and it means it.

That is real power and it costs something specific, so this verb states it at
the moment of use rather than letting an operator discover it from a timeout.
A control that quietly does MORE than it claims is worse than one that does
less.

Auto-discovered by the ``*_repl.py`` naming cage; the palette row, the
description and the attach-cockpit mirroring all come from that.
"""
from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

#: The env var the Override Matrix already reads. Named once, here, so this
#: verb and the router cannot disagree about where the pin lives.
PIN_ENV = "JARVIS_DW_PRIMARY_OVERRIDE"

#: Routes worth showing. Read from the topology when it can answer, so a route
#: added to the policy appears here without an edit.
_FALLBACK_ROUTES = ("immediate", "standard", "complex", "background",
                    "speculative")

__verb_args__ = {"model": "[list|auto|<model-id>|help]"}


@dataclass(frozen=True)
class ModelReplDispatchResult:
    ok: bool
    text: str
    matched: bool = True


def _matches(line: str) -> bool:
    try:
        head = (line or "").strip().split(None, 1)[0].lower()
    except IndexError:
        return False
    return head in ("/model", "model", "/models", "models")


# ---------------------------------------------------------------------------
# Enumeration — derived from the policy and the providers, never a list here
# ---------------------------------------------------------------------------


def _routes() -> Tuple[str, ...]:
    try:
        from backend.core.ouroboros.governance.provider_topology import (
            get_topology,
        )
        topo = get_topology()
        found = tuple(sorted(getattr(topo, "routes", {}) or ()))
        return found or _FALLBACK_ROUTES
    except Exception:  # noqa: BLE001
        return _FALLBACK_ROUTES


def _dw_models() -> Dict[str, Tuple[str, ...]]:
    """``route -> ranked DW model ids``, straight from the topology.

    The policy owns the ranking and the segmentation; asking it per route is
    what keeps this verb honest about a sealed lane instead of listing models
    the router will never choose there.
    """
    out: Dict[str, Tuple[str, ...]] = {}
    try:
        from backend.core.ouroboros.governance.provider_topology import (
            get_topology,
        )
        topo = get_topology()
        for route in _routes():
            try:
                models = tuple(topo.dw_models_for_route(route) or ())
            except Exception:  # noqa: BLE001
                models = ()
            if models:
                out[route] = models
    except Exception:  # noqa: BLE001
        return out
    return out


def _claude_models() -> Tuple[str, ...]:
    """Claude ids the runtime is configured to use.

    Read from the env the providers themselves read, so the list cannot claim
    a model the next generation will not request. De-duplicated and ordered
    by declaration, because two names here is a config smell worth SEEING —
    `.env` currently declares both `CLAUDE_MODEL` and
    `JARVIS_GOVERNED_CLAUDE_MODEL`, and they disagree.
    """
    out: List[str] = []
    for key in ("JARVIS_GOVERNED_CLAUDE_MODEL", "CLAUDE_MODEL",
                "JARVIS_CLAUDE_MODEL"):
        try:
            value = (os.environ.get(key) or "").strip()
        except Exception:  # noqa: BLE001
            continue
        if value and value not in out:
            out.append(value)
    # The cheap tier, which the env never names. `economic_router` already
    # resolves it — operator env first, then
    # `cost_optimization.claude_low_cost_model` in the policy — and reusing
    # that accessor is the difference between a catalogue and a second,
    # quietly-disagreeing opinion about what Anthropic can be asked for.
    # Without it the ONE model the economic failover path actually reaches
    # was the one model an operator could not pin.
    try:
        from backend.core.ouroboros.governance.economic_router import (
            economic_failover_model,
        )
        cheap = (economic_failover_model() or "").strip()
        if cheap and cheap not in out:
            out.append(cheap)
    except Exception:  # noqa: BLE001 — a catalogue is never load-bearing
        pass
    return tuple(out)


def _prime_models() -> Tuple[str, ...]:
    """The J-Prime golden-image tier's active model label.

    Sourced from `failover_lifecycle.active_jprime_model()` — the accessor
    that already drives model-aware compaction — rather than a second reading
    of the tier config.
    """
    try:
        from backend.core.ouroboros.governance.failover_lifecycle import (
            get_failover_controller,
        )
        label = (get_failover_controller().active_jprime_model() or "").strip()
        return (label,) if label else ()
    except Exception:  # noqa: BLE001
        return ()


def routes_opened_by_pin() -> Tuple[str, ...]:
    """Routes that have a DW lane ONLY because of the pin. NEVER raises.

    Asked by toggling the env the topology reads and diffing the answer,
    rather than by re-deriving the policy's segmentation here — the topology
    stays the single authority on what a route admits, and this only reports
    the difference the operator caused.
    """
    pin = current_pin()
    if not pin:
        return ()
    try:
        from backend.core.ouroboros.governance.provider_topology import (
            get_topology,
        )
        topo = get_topology()
        opened: List[str] = []
        saved = os.environ.get(PIN_ENV)
        try:
            os.environ.pop(PIN_ENV, None)
            sealed = {r for r in _routes()
                      if not (topo.dw_models_for_route(r) or ())}
        finally:
            # Restored unconditionally: leaving the operator's pin cleared
            # because a STATUS READ raised would be a display that silently
            # changes routing.
            if saved is None:
                os.environ.pop(PIN_ENV, None)
            else:
                os.environ[PIN_ENV] = saved
        for route in sorted(sealed):
            if topo.dw_models_for_route(route):
                opened.append(route)
        return tuple(opened)
    except Exception:  # noqa: BLE001
        return ()


def available_models() -> Dict[str, Tuple[str, ...]]:
    """``lane -> model ids`` across all three lanes. NEVER raises.

    A lane that cannot be interrogated is OMITTED rather than shown empty:
    "doubleword: (none)" reads as "DW has no models", which is a claim about
    the fleet rather than about this process's ability to ask.
    """
    lanes: Dict[str, Tuple[str, ...]] = {}
    dw = _dw_models()
    if dw:
        merged: List[str] = []
        for models in dw.values():
            for model in models:
                if model not in merged:
                    merged.append(model)
        lanes["doubleword"] = tuple(merged)
    claude = _claude_models()
    if claude:
        lanes["claude"] = claude
    prime = _prime_models()
    if prime:
        lanes["j-prime"] = prime
    return lanes


# ---------------------------------------------------------------------------
# The pin
# ---------------------------------------------------------------------------


def current_pin() -> str:
    """The active pin, read through the router's own accessor. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.model_pinning_heuristic import (
            model_pin_override,
        )
        return model_pin_override() or ""
    except Exception:  # noqa: BLE001
        try:
            return (os.environ.get(PIN_ENV) or "").strip()
        except Exception:  # noqa: BLE001
            return ""


def _resolve(token: str) -> Tuple[str, str]:
    """``(model_id, lane)`` for an operator's token, or ``("", "")``.

    Case-insensitive and suffix-tolerant, because a model id is long and an
    operator types what they can see: ``397b`` finds
    ``Qwen/Qwen3.5-397B-A17B-FP8``. Ambiguity is NOT resolved by picking the
    first — two matches means the operator meant something this cannot know,
    and guessing pins the wrong brain silently.
    """
    needle = (token or "").strip().lower()
    if not needle:
        return ("", "")
    hits: List[Tuple[str, str]] = []
    for lane, models in available_models().items():
        for model in models:
            low = model.lower()
            if low == needle:
                return (model, lane)          # exact wins outright
            if needle in low:
                hits.append((model, lane))
    if len(hits) == 1:
        return hits[0]
    return ("", "")


def _ambiguous(token: str) -> List[str]:
    needle = (token or "").strip().lower()
    return [m for models in available_models().values() for m in models
            if needle and needle in m.lower()]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


_HELP = (
    "  [bold cyan]/model — which brain runs the work[/bold cyan]\n"
    "\n"
    "    /model                    what is selected now, and per route\n"
    "    /model list               every model the policy can reach\n"
    "    /model <id>               pin one (partial ids work: 397b)\n"
    "    /model auto               hand the choice back to the policy\n"
    "\n"
    "  [dim]auto is calibrated, not arbitrary: DW is excluded from the\n"
    "  Prefrontal Cortex (IMMEDIATE/COMPLEX) because live fire showed it\n"
    "  timing out there, and BACKGROUND is sealed on unit economics.[/dim]\n"
    "\n"
    "  [yellow]A pin is SOVEREIGN.[/yellow] [dim]It outranks the policy and\n"
    "  will open a route the policy sealed — measured: pinning puts DW on\n"
    "  IMMEDIATE, which has no DW lane by design. That is the point of an\n"
    "  override; it is also how you inherit the timeouts the policy was\n"
    "  written to avoid. /model auto gives the choice back.[/dim]\n"
)


def _render_status() -> str:
    pin = current_pin()
    lanes = available_models()
    out: List[str] = []
    if pin:
        model, lane = _resolve(pin)
        where = f" [dim]({lane})[/dim]" if lane else ""
        out.append(f"  [bold cyan]model[/bold cyan] [yellow]pinned[/yellow] "
                   f"→ [white]{model or pin}[/white]{where}")
        if not model:
            out.append(f"    [yellow]{pin!r} matches no model this policy "
                       f"can reach — the router falls back to auto[/yellow]")
        opened = routes_opened_by_pin()
        if opened:
            out.append(
                f"    [yellow]sovereign: this pin OPENS {', '.join(opened)}"
                f"[/yellow] [dim]— the policy leaves those to Claude on "
                f"live-fire evidence; /model auto restores it[/dim]")
    else:
        out.append("  [bold cyan]model[/bold cyan] [green]auto[/green] "
                   "[dim]— the policy chooses per route[/dim]")

    dw = _dw_models()
    if dw:
        out += ["", "    [dim]DoubleWord, per route (rank 1 first)[/dim]"]
        for route in sorted(dw):
            head = dw[route][0]
            more = len(dw[route]) - 1
            tail = f" [dim]+{more}[/dim]" if more > 0 else ""
            out.append(f"      [cyan]{route:<12}[/cyan] {head}{tail}")
    sealed = [r for r in _routes() if r not in dw]
    if sealed:
        out.append(f"      [dim]{', '.join(sealed)}: no DW lane "
                   f"(sealed or Claude-only by policy)[/dim]")
    for lane in ("claude", "j-prime"):
        if lanes.get(lane):
            out.append(f"    [dim]{lane}[/dim] {', '.join(lanes[lane])}")
    out += ["", "  [dim]/model list · /model <id> · /model auto[/dim]"]
    return "\n".join(out)


def _render_list() -> str:
    lanes = available_models()
    if not lanes:
        return ("  [yellow]no lane could be interrogated[/yellow] [dim]— the "
                "topology and provider config are both unreadable from this "
                "process[/dim]")
    pin = current_pin().lower()
    out = ["  [bold cyan]models the policy can reach[/bold cyan]", ""]
    for lane, models in lanes.items():
        out.append(f"    [dim]{lane}[/dim]")
        for model in models:
            mark = " [yellow]← pinned[/yellow]" if model.lower() == pin else ""
            out.append(f"      {model}{mark}")
    out += ["", "  [dim]/model <id> to pin · /model auto to release[/dim]"]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def dispatch_model_command(line: str) -> ModelReplDispatchResult:
    """Pick the brain, or hand the choice back to the policy.

    Operator: Show or pin which model runs the work, or return it to auto.

    NEVER raises.
    """
    if not _matches(line):
        return ModelReplDispatchResult(ok=False, text="", matched=False)
    try:
        tokens = shlex.split(line or "")
    except ValueError as exc:
        return ModelReplDispatchResult(
            ok=False, text=f"  /model parse error: {exc}")
    args = tokens[1:]
    head = (args[0].lower() if args else "")

    try:
        if head in ("help", "?", "--help", "-h"):
            return ModelReplDispatchResult(ok=True, text=_HELP)
        if head in ("", "status"):
            return ModelReplDispatchResult(ok=True, text=_render_status())
        if head in ("list", "ls"):
            return ModelReplDispatchResult(ok=True, text=_render_list())
        if head in ("auto", "clear", "none", "off"):
            had = current_pin()
            os.environ.pop(PIN_ENV, None)
            if had:
                return ModelReplDispatchResult(
                    ok=True,
                    text=f"  [green]auto[/green] — released [white]{had}"
                         f"[/white]; the policy chooses per route again")
            return ModelReplDispatchResult(
                ok=True, text="  [green]auto[/green] — nothing was pinned")

        token = " ".join(args)
        model, lane = _resolve(token)
        if not model:
            candidates = _ambiguous(token)
            if len(candidates) > 1:
                shown = "\n".join(f"      {c}" for c in candidates[:8])
                return ModelReplDispatchResult(
                    ok=False,
                    text=(f"  [yellow]{token!r} matches "
                          f"{len(candidates)} models[/yellow] [dim]— name one "
                          f"exactly:[/dim]\n{shown}"))
            return ModelReplDispatchResult(
                ok=False,
                text=(f"  [yellow]no model matches {token!r}[/yellow] "
                      f"[dim]— /model list shows what this policy can "
                      f"reach[/dim]"))

        # THE WRITE, through the one interface every lane consults.
        #
        # This used to write the DW variable unconditionally and tell the
        # operator that a Claude or J-Prime id was "recorded but selected by
        # route" — an honest description of a control that did nothing. The
        # Claude lane bound its model ONCE in `GovernedLoopConfig.from_env`,
        # so the pin could not have taken effect before the next restart.
        # `sovereign_override` is read per REQUEST by each lane instead.
        from backend.core.ouroboros.governance.sovereign_override import (
            set_pin,
        )
        if not set_pin(lane, model):
            return ModelReplDispatchResult(
                ok=False,
                text=f"  [yellow]{lane} cannot be pinned from here[/yellow]")
        note = ""
        if lane == "j-prime":
            # The one lane a pin cannot fully own: switching the J-Prime tier
            # PROVISIONS a VM, which is a spend action the failover controller
            # owns. Recorded and honoured where it is read; it does not
            # conjure a node.
            note = ("\n    [dim]note: recorded for the J-Prime lane. "
                    "Switching the active tier provisions a node, which the "
                    "failover controller owns — this does not spin one "
                    "up[/dim]")
        opened = routes_opened_by_pin()
        if opened:
            note += (f"\n    [yellow]sovereign: opens {', '.join(opened)}"
                     f"[/yellow] [dim]— the policy seals those against DW "
                     f"because live fire showed it timing out there[/dim]")
        return ModelReplDispatchResult(
            ok=True,
            text=(f"  [yellow]pinned[/yellow] → [white]{model}[/white] "
                  f"[dim]({lane})[/dim]{note}"))
    except Exception as exc:  # noqa: BLE001 — a verb must not kill the REPL
        return ModelReplDispatchResult(
            ok=False, text=f"  /model unavailable: {type(exc).__name__}: {exc}")
