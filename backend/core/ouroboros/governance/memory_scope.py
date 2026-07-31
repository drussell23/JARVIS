"""What a subagent is allowed to remember, decided on purpose.

Claude Code makes an explicit choice not to inherit conversation memory into
subagents. O+V had never made that choice either way — which does not mean
the question went unanswered. It means it was answered by omission, at four
call sites, by whoever wrote each one, and nobody could read the answer back.

That is the defect. Not "subagents lack memory" — a boundary whose crossing
rule exists only as the absence of code.

Why one global switch would be wrong
-------------------------------------
The four subagent types have genuinely different epistemic needs, and a
single on/off would have to be wrong for at least three of them.

``EXPLORE`` — its value is INDEPENDENT evidence. The parent's memory is the
hypothesis; EXPLORE is supposed to be the test. Feeding the hypothesis to the
test is how a search finds what it was told to expect. Default ``NONE``.

``REVIEW`` — adversarial, and the interesting case. Architecture memory is
exactly what a reviewer wants ("we tried this, it failed"). But a reviewer
handed the SAME topics the author had inherits the author's blind spot: it
cannot catch a mistake the memory itself caused. Default ``COMPLEMENT`` —
route independently, then EXCLUDE what the parent was shown, so the reviewer
reads different evidence by construction. The admission ledger makes this
possible: it records exactly what the parent saw.

``PLAN`` — needs the architectural constraints the parent is working under,
and shares its goal rather than adversarially checking it. Default
``INHERIT``: reuse the parent's already-routed section, which costs nothing
and cannot disagree with it.

``GENERAL`` — infrastructure-only, under the Semantic Firewall, the most
prompt-injection-vulnerable surface in the system. Every additional token of
context is additional attack surface, and architecture memory has no bearing
on the mechanical tasks GENERAL exists for. Default ``NONE``, and that is a
security position rather than a preference.

Scope is DECLARED, then recorded
---------------------------------
Every dispatch files an admission record under its own
:class:`MemoryConsumer`, including — especially — the ones that received
nothing. A ``NONE`` scope produces a record saying *withheld by policy*, so
``/memory context`` can show ``review · 0 admitted · scope=none`` instead of
rendering identically to "the ledger was off". A boundary you cannot observe
is a boundary you are trusting, not enforcing.

Honest about reach
------------------
Three of the four subagents are deterministic today — EXPLORE greps, PLAN
partitions, REVIEW scores — so they have no model prompt for a rendered
section to enter. Exactly one (GENERAL) drives a model, and its policy is
``NONE``. So this module's SECTION currently has one potential consumer and
is deliberately denied to it; the POLICY and its audit trail are live now.

That is stated rather than glossed because shipping a rendered section into
three subagents that cannot read one is precisely the wired-but-inert failure
this codebase keeps rediscovering. When a subagent gains a prompt, the
crossing rule is already written, already enforced, and already visible.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.MemoryScope")

MEMORY_SCOPE_SCHEMA_VERSION: str = "memory_scope.1"

__all__ = [
    "MEMORY_SCOPE_SCHEMA_VERSION",
    "MemoryScope",
    "ScopeResolution",
    "resolve_scope",
    "scope_for",
    "scoping_enabled",
]


class MemoryScope(str, enum.Enum):
    """How memory crosses the parent→subagent boundary.

    ``COMPLEMENT`` is the one that does not exist in Claude Code, because it
    requires knowing what the parent was actually shown — which is what the
    admission ledger records.
    """

    #: Nothing crosses. The subagent reasons from its own goal alone.
    NONE = "none"
    #: Reuse the parent's already-rendered section verbatim. Free, and
    #: guaranteed consistent with what the parent reasoned from.
    INHERIT = "inherit"
    #: Route fresh against the SUBAGENT's goal and target files.
    INDEPENDENT = "independent"
    #: Route fresh, then withhold every topic the parent was shown.
    COMPLEMENT = "complement"


#: Per-type defaults. Each is a judgement about that subagent's epistemic
#: role, argued in the module docstring — not a tuning constant.
_DEFAULTS: Dict[str, MemoryScope] = {
    "explore": MemoryScope.NONE,
    "review": MemoryScope.COMPLEMENT,
    "plan": MemoryScope.INHERIT,
    "general": MemoryScope.NONE,
}

#: Scopes an operator may NOT select for a given type, with the reason.
#:
#: A policy that can be flipped to anything by an env var is a suggestion.
#: GENERAL's exclusion is load-bearing: it sits behind the Semantic Firewall
#: precisely because it is the most injection-vulnerable surface, and letting
#: a deployment widen its context by setting a string would route around that
#: reasoning without ever touching the firewall.
_FORBIDDEN: Dict[str, Tuple[Tuple[MemoryScope, ...], str]] = {
    "general": (
        (MemoryScope.INHERIT, MemoryScope.INDEPENDENT, MemoryScope.COMPLEMENT),
        "GENERAL runs behind the Semantic Firewall on infrastructure-only "
        "tasks; architecture memory is attack surface with no bearing on "
        "its work",
    ),
}


def scoping_enabled() -> bool:
    """``JARVIS_SUBAGENT_MEMORY_SCOPE_ENABLED`` (default true).

    OFF resolves every type to ``NONE`` — the behaviour before this module,
    stated explicitly instead of by omission.
    """
    try:
        return os.environ.get(
            "JARVIS_SUBAGENT_MEMORY_SCOPE_ENABLED", "1",
        ).strip().lower() not in ("0", "false", "no", "off", "")
    except Exception:  # noqa: BLE001
        return True


def _env_key(subagent_type: str) -> str:
    return f"JARVIS_SUBAGENT_MEMORY_SCOPE_{subagent_type.upper()}"


def scope_for(subagent_type: Any) -> MemoryScope:
    """The declared scope for *subagent_type*. NEVER raises.

    Resolution order: master gate → env override (rejected if forbidden for
    this type) → per-type default → ``NONE``.

    An unknown type resolves to ``NONE``. A subagent this module has never
    heard of is exactly the one whose memory needs are undecided, and the
    safe answer to an undecided boundary question is "nothing crosses".
    """
    try:
        key = str(getattr(subagent_type, "value", subagent_type) or "").strip().lower()
        if not scoping_enabled():
            return MemoryScope.NONE

        raw = os.environ.get(_env_key(key), "").strip().lower()
        if raw:
            for member in MemoryScope:
                if member.value == raw:
                    forbidden, reason = _FORBIDDEN.get(key, ((), ""))
                    if member in forbidden:
                        logger.warning(
                            "[MemoryScope] %s=%s refused — %s; falling back "
                            "to the declared default",
                            _env_key(key), raw, reason,
                        )
                        break
                    return member
            else:
                logger.warning(
                    "[MemoryScope] %s=%r is not a scope; using the default",
                    _env_key(key), raw,
                )
        return _DEFAULTS.get(key, MemoryScope.NONE)
    except Exception:  # noqa: BLE001
        return MemoryScope.NONE


@dataclass(frozen=True)
class ScopeResolution:
    """What one subagent dispatch is permitted to remember, and why."""

    scope: MemoryScope
    section: str
    topic_count: int
    excluded_parent_topics: int
    detail: str

    @property
    def carries_memory(self) -> bool:
        return bool(self.section)

    @classmethod
    def denied(cls, scope: MemoryScope, detail: str) -> "ScopeResolution":
        return cls(scope=scope, section="", topic_count=0,
                   excluded_parent_topics=0, detail=detail)


def _parent_admitted_hashes(parent_op_id: str) -> Tuple[str, ...]:
    """Content hashes the PARENT was shown. NEVER raises.

    The input to ``COMPLEMENT``, and only obtainable because the admission
    ledger records withheld and admitted rows per op. Without it, "review
    something the author did not read" is not expressible.
    """
    try:
        from backend.core.ouroboros.governance.memory_admission import (
            ledger_for,
        )
        record = ledger_for(parent_op_id).latest()
        if record is None:
            return ()
        return tuple(r.content_hash for r in record.rows if r.admitted)
    except Exception:  # noqa: BLE001
        logger.debug("[MemoryScope] parent admissions unavailable", exc_info=True)
        return ()


def _parent_section(parent_ctx: Any) -> str:
    """The parent's already-rendered memory block, or ``""``. NEVER raises."""
    try:
        return str(getattr(parent_ctx, "strategic_memory_prompt", "") or "")
    except Exception:  # noqa: BLE001
        return ""


async def resolve_scope(
    *,
    subagent_type: Any,
    parent_op_id: str,
    parent_ctx: Any,
    subagent_id: str,
    goal: str,
    target_files: Sequence[str],
    project_root: Any,
    max_topics: int = 3,
    token_budget: int = 2000,
) -> ScopeResolution:
    """Resolve and RECORD what this dispatch may remember. NEVER raises.

    Always files an admission record under the subagent's own consumer —
    including for ``NONE``, where the record is the only evidence that a
    boundary decision was made at all rather than forgotten.
    """
    scope = scope_for(subagent_type)
    consumer = str(getattr(subagent_type, "value", subagent_type) or "unknown")

    try:
        if scope is MemoryScope.NONE:
            _record_denied(parent_op_id, subagent_id, consumer, scope,
                           token_budget)
            return ScopeResolution.denied(
                scope, "no memory crosses this boundary by policy")

        if scope is MemoryScope.INHERIT:
            section = _parent_section(parent_ctx)
            _record_inherited(parent_op_id, subagent_id, consumer, scope,
                              token_budget, bool(section))
            return ScopeResolution(
                scope=scope, section=section,
                topic_count=len(_parent_admitted_hashes(parent_op_id)),
                excluded_parent_topics=0,
                detail=("inherited the parent's routed section" if section
                        else "parent carried no memory to inherit"),
            )

        # INDEPENDENT / COMPLEMENT — route fresh against the SUBAGENT's own
        # goal, which is the whole point: a different question deserves a
        # different answer, even over the same corpus.
        from backend.core.ouroboros.governance.module_routing import (
            ModuleContextRouter, routing_enabled,
        )
        if not routing_enabled():
            return ScopeResolution.denied(scope, "routing disabled")

        exclude: Tuple[str, ...] = ()
        if scope is MemoryScope.COMPLEMENT:
            exclude = _parent_admitted_hashes(parent_op_id)

        router = ModuleContextRouter(project_root)
        routed = await router.route(
            list(target_files), str(goal or ""),
            max_topics=max_topics, token_budget=token_budget,
            op_id=subagent_id, consumer=consumer,
            exclude_hashes=exclude,
        )
        return ScopeResolution(
            scope=scope, section=routed.section,
            topic_count=len(routed.topics),
            excluded_parent_topics=len(exclude),
            detail=(f"routed independently, withholding {len(exclude)} topic(s) "
                    f"the parent already saw" if exclude
                    else "routed independently"),
        )
    except Exception:  # noqa: BLE001
        logger.debug("[MemoryScope] resolution degraded", exc_info=True)
        return ScopeResolution.denied(scope, "resolution failed")


def _blank_record(parent_op_id: str, subagent_id: str, consumer: str,
                  scope: MemoryScope, token_budget: int,
                  detail: str) -> None:
    """File a zero-admission record so the decision is observable."""
    try:
        from backend.core.ouroboros.governance.memory_admission import (
            AdmissionRecord, MemoryConsumer, record_admission,
        )
        record_admission(AdmissionRecord.of(
            op_id=subagent_id or parent_op_id,
            consumer=MemoryConsumer.coerce(consumer),
            rows=[], corpus_size=0, corpus_provenance="not_consulted",
            corpus_excluded=0, char_budget=int(token_budget),
            query=detail,
            extra={"scope": scope.value, "parent_op_id": parent_op_id,
                   "schema": MEMORY_SCOPE_SCHEMA_VERSION},
        ))
    except Exception:  # noqa: BLE001
        logger.debug("[MemoryScope] record skipped", exc_info=True)


def _record_denied(parent_op_id: str, subagent_id: str, consumer: str,
                   scope: MemoryScope, token_budget: int) -> None:
    _blank_record(parent_op_id, subagent_id, consumer, scope, token_budget,
                  "withheld by policy — scope=none")


def _record_inherited(parent_op_id: str, subagent_id: str, consumer: str,
                      scope: MemoryScope, token_budget: int,
                      had_section: bool) -> None:
    _blank_record(
        parent_op_id, subagent_id, consumer, scope, token_budget,
        "inherited the parent's section" if had_section
        else "parent had no section to inherit")


def render_scope_lines() -> List[str]:
    """Markup lines for ``/memory scope``. NEVER raises."""
    try:
        out = ["  [bold]memory · subagent scope[/bold]"]
        if not scoping_enabled():
            out.append("    [dim]scoping disabled — every subagent resolves "
                       "to none[/dim]")
        for name in sorted(_DEFAULTS):
            resolved = scope_for(name)
            default = _DEFAULTS[name]
            marker = "" if resolved is default else "  [dim](overridden)[/dim]"
            note = ""
            if name in _FORBIDDEN:
                note = "  [dim]— widening refused by policy[/dim]"
            out.append(f"    {name:<9} {resolved.value}{marker}{note}")
        out.append("  [dim]3 of 4 subagents are deterministic today and have "
                   "no prompt; the policy is enforced regardless[/dim]")
        return out
    except Exception as exc:  # noqa: BLE001
        return [f"  [dim]scope surface degraded: {type(exc).__name__}: {exc}[/dim]"]
