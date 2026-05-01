"""Move 5 Slice 2 — Read-only EvidenceProber + canonical allowlist.

Phase 7.6's ``EvidenceProber`` Protocol explicitly reserved
allowlist enforcement to implementations:

  > "Production implementations call a read-only Venom subset
  >  (allowlist enforced at the implementation, not here — this
  >  primitive is Venom-agnostic)."

Move 5 Slice 2 is the first such implementation. This module
defines THE canonical read-only-tool allowlist that future
probers (and the Move 5 runner in Slice 3) consume.

Slice scope:

  * ``READONLY_TOOL_ALLOWLIST`` — frozenset of 9 tool names from
    the Iron Gate exploration tool set (read_file, search_code,
    get_callers, glob_files, list_dir, list_symbols, git_blame,
    git_log, git_diff). AST-pinned by Slice 5 graduation so a
    refactor cannot silently add a mutation tool.

  * ``QuestionResolver`` Protocol — Slice 2's question→answer
    cognitive primitive. Different shape from Phase 7.6's
    ``EvidenceProber`` (which is per-round-evidence-for-claim);
    this one resolves a single ``ProbeQuestion`` to a single
    ``ProbeAnswer`` via tool calls bounded by the allowlist.

  * ``ReadonlyToolBackend`` Protocol — per-tool execution
    surface. Slice 3's runner injects the production backend
    (Venom's ``tool_executor`` filtered to the allowlist).
    Tests inject capturing fakes. Default is ``_NullToolBackend``
    (returns empty string) so a misconfigured caller cannot
    accidentally hit a paid model.

  * ``ReadonlyEvidenceProber`` — concrete ``QuestionResolver``
    implementation. Per question:
      1. Compute candidate tool sequence from
         ``ProbeQuestion.resolution_method`` hint
      2. Verify EACH tool name is in the allowlist (defense in
         depth — even if generator hint is corrupted, prober
         refuses to call non-allowlisted tools)
      3. Execute up to ``max_tool_rounds`` tool calls via backend
      4. Concatenate results into ``answer_text``
      5. Compute ``evidence_fingerprint`` via Slice 1's
         ``canonical_fingerprint``
      6. Return ``ProbeAnswer``
    NEVER raises.

Direct-solve principles (per the operator directive):

  * **Asynchronous-ready** — the QuestionResolver Protocol can be
    sync or async; Slice 2's reference implementation is sync
    (Slice 3's runner wraps in ``asyncio.to_thread`` for the
    streaming hot path).

  * **Dynamic** — backend injection means production wires Venom;
    tests inject capturing fakes; default is null. Allowlist is
    a frozenset (immutable, AST-pinned).

  * **Adaptive** — prober honors per-question
    ``max_tool_rounds`` from ``ProbeQuestion``; falls back to
    bridge env knob when 0.

  * **Intelligent** — fingerprint computed via Slice 1's shared
    canonicalization function; consistent with the runner's
    convergence detector.

  * **Robust** — never raises. Backend exception → empty answer
    text + diminished fingerprint (won't converge with anything,
    appropriately). Non-allowlisted tool name → silently skipped
    + logged.

  * **No hardcoding** — allowlist is a module-level frozenset
    constant; max_tool_rounds env-tunable; backend injectable.

Authority invariants (AST-pinned by companion tests):

  * Module imports stdlib + verification.confidence_probe_bridge
    (Slice 1 types) ONLY.
  * NEVER imports orchestrator / phase_runners /
    candidate_generator / iron_gate / change_engine / policy /
    semantic_guardian / semantic_firewall / providers /
    doubleword_provider / urgency_router / auto_action_router /
    subagent_scheduler / tool_executor.
  * NEVER references mutation tool names (edit_file / write_file
    / delete_file / run_tests / bash) in code — pinned by AST
    walk over Name + Attribute nodes; allowlist constant is
    only place these names can appear (and they MUST NOT).
  * Read-only tool allowlist is a module-level frozenset
    constant — Slice 5 graduation pin verifies its contents.

Master flag default-false until Slice 5 graduation:
``JARVIS_READONLY_EVIDENCE_PROBER_ENABLED``. Asymmetric env
semantics: when off, prober resolves to empty ``ProbeAnswer``
(zero cost; matches the null-backend safe default).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import (
    Any,
    Dict,
    FrozenSet,
    List,
    Optional,
    Tuple,
)

try:
    from typing import Protocol  # Py 3.8+
except ImportError:  # pragma: no cover
    from typing_extensions import Protocol  # type: ignore[assignment]

from backend.core.ouroboros.governance.verification.confidence_probe_bridge import (  # noqa: E501
    ProbeAnswer,
    ProbeQuestion,
    canonical_fingerprint,
    make_probe_answer,
    max_tool_rounds_per_question,
)

logger = logging.getLogger(__name__)


READONLY_EVIDENCE_PROBER_SCHEMA_VERSION: str = (
    "readonly_evidence_prober.1"
)


# ---------------------------------------------------------------------------
# THE canonical read-only tool allowlist
# ---------------------------------------------------------------------------
#
# 9 tools drawn from the Iron Gate exploration tool set. NO mutation
# tools (edit_file / write_file / delete_file / run_tests / bash) —
# pinned by Slice 5 graduation AST test which verifies (a) every name
# in this frozenset is read-only by classification AND (b) no
# mutation-tool-name string literal appears anywhere else in this
# module or in confidence_probe_bridge.py.


READONLY_TOOL_ALLOWLIST: FrozenSet[str] = frozenset({
    "read_file",
    "search_code",
    "get_callers",
    "glob_files",
    "list_dir",
    "list_symbols",
    "git_blame",
    "git_log",
    "git_diff",
})


def is_tool_allowlisted(tool_name: str) -> bool:
    """True iff ``tool_name`` is in the canonical read-only
    allowlist. Defense-in-depth: callers should ALWAYS check before
    invoking a tool. NEVER raises."""
    try:
        return str(tool_name) in READONLY_TOOL_ALLOWLIST
    except Exception:  # noqa: BLE001 — defensive
        return False


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def prober_enabled() -> bool:
    """``JARVIS_READONLY_EVIDENCE_PROBER_ENABLED`` (default ``false``
    until Slice 5 graduation). Asymmetric env semantics: empty/
    whitespace = unset = current default; explicit truthy/falsy
    overrides at call time."""
    raw = os.environ.get(
        "JARVIS_READONLY_EVIDENCE_PROBER_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Backend Protocol — per-tool execution surface (injectable)
# ---------------------------------------------------------------------------


class ReadonlyToolBackend(Protocol):
    """Per-tool execution surface. Production callers wire Venom's
    ``tool_executor`` filtered to ``READONLY_TOOL_ALLOWLIST``;
    tests inject capturing fakes; default is ``_NullToolBackend``.

    Implementations MUST NOT raise — but if they do, the prober
    catches and continues with empty evidence. Implementations
    MUST honor the allowlist filter at THEIR layer (defense in
    depth — the prober also checks)."""

    def execute(
        self,
        *,
        tool_name: str,
        args: Dict[str, Any],
    ) -> str:
        """Execute one tool call. Return raw result text (or empty
        string on tool failure / refusal). NEVER raises."""
        ...


class _NullToolBackend:
    """Safe-default backend that returns empty string on every
    call. Mirrors Phase 7.6's ``_NullEvidenceProber`` discipline:
    a misconfigured caller cannot accidentally hit a paid model
    (every probe terminates with empty fingerprints, which match
    no other answer, which fails to converge — appropriate
    safety default)."""

    def execute(
        self,
        *,
        tool_name: str,
        args: Dict[str, Any],
    ) -> str:
        del tool_name, args  # null backend ignores
        return ""


# ---------------------------------------------------------------------------
# QuestionResolver Protocol — Slice 2's cognitive primitive
# ---------------------------------------------------------------------------


class QuestionResolver(Protocol):
    """Slice 2's question→answer cognitive primitive.

    Different shape from Phase 7.6's ``EvidenceProber`` Protocol
    (which is per-round-evidence-for-claim). This Protocol resolves
    one ``ProbeQuestion`` to one ``ProbeAnswer`` via bounded tool
    calls.

    Implementations MUST NOT raise. Implementations MUST enforce
    the read-only allowlist at their boundary."""

    def resolve(
        self,
        question: ProbeQuestion,
        *,
        max_tool_rounds: Optional[int] = None,
    ) -> ProbeAnswer:
        ...


# ---------------------------------------------------------------------------
# Concrete ReadonlyEvidenceProber
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ToolPlan:
    """Internal — bounded sequence of (tool_name, args) tuples
    derived from a ProbeQuestion's resolution_method hint."""

    steps: Tuple[Tuple[str, Dict[str, Any]], ...]


class ReadonlyEvidenceProber:
    """Concrete ``QuestionResolver`` that calls a
    ``ReadonlyToolBackend`` filtered to ``READONLY_TOOL_ALLOWLIST``.

    Per question:
      1. Compute tool plan from question's resolution_method hint
      2. Defense-in-depth: verify every step is in the allowlist;
         skip non-allowlisted with WARNING (should never happen if
         generator is correct, but defends against corrupted hints)
      3. Execute up to max_tool_rounds tool calls via backend
      4. Concatenate results into answer_text (bounded length)
      5. Compute fingerprint via Slice 1 canonical_fingerprint
      6. Return ProbeAnswer

    Master flag off → resolve returns empty answer (zero cost,
    matches null-backend semantics).

    NEVER raises out of resolve(). Backend exceptions caught;
    non-allowlisted tools skipped; malformed args coerced
    defensively."""

    # Per-call answer text cap — operator-tunable would be
    # premature; if needed Slice 5 graduation can env-knob this.
    _MAX_ANSWER_CHARS: int = 4096

    def __init__(
        self,
        *,
        backend: Optional[ReadonlyToolBackend] = None,
        allowlist: Optional[FrozenSet[str]] = None,
    ) -> None:
        self._backend = (
            backend if backend is not None else _NullToolBackend()
        )
        self._allowlist = (
            allowlist if allowlist is not None
            else READONLY_TOOL_ALLOWLIST
        )
        # Counter for diagnostics
        self._calls_total: int = 0
        self._calls_blocked_by_allowlist: int = 0
        self._calls_failed: int = 0

    @property
    def allowlist(self) -> FrozenSet[str]:
        return self._allowlist

    def stats(self) -> Dict[str, Any]:
        return {
            "schema_version": (
                READONLY_EVIDENCE_PROBER_SCHEMA_VERSION
            ),
            "calls_total": self._calls_total,
            "calls_blocked_by_allowlist": (
                self._calls_blocked_by_allowlist
            ),
            "calls_failed": self._calls_failed,
            "allowlist_size": len(self._allowlist),
        }

    def resolve(
        self,
        question: ProbeQuestion,
        *,
        max_tool_rounds: Optional[int] = None,
    ) -> ProbeAnswer:
        """Resolve one question. NEVER raises. Returns a
        ``ProbeAnswer`` (with empty answer_text on master-off /
        empty-question / null-backend / total backend failure)."""
        # Master flag off → empty answer (zero cost)
        if not prober_enabled():
            return make_probe_answer(
                question=question.question if question else "",
                answer_text="",
                tool_rounds_used=0,
            )

        if not isinstance(question, ProbeQuestion):
            return make_probe_answer(
                question="", answer_text="", tool_rounds_used=0,
            )
        if not (question.question or "").strip():
            return make_probe_answer(
                question="", answer_text="", tool_rounds_used=0,
            )

        # Compute effective per-question round cap
        question_cap = (
            int(question.max_tool_rounds)
            if question.max_tool_rounds and question.max_tool_rounds > 0
            else 0
        )
        if max_tool_rounds is not None and max_tool_rounds > 0:
            effective_cap = max_tool_rounds
        elif question_cap > 0:
            effective_cap = question_cap
        else:
            effective_cap = max_tool_rounds_per_question()
        # Floor at 1 (defensive)
        effective_cap = max(1, int(effective_cap))

        # Compute tool plan from resolution_method hint
        plan = self._plan_for_question(question)

        rounds_used = 0
        result_chunks: List[str] = []
        for tool_name, args in plan.steps:
            if rounds_used >= effective_cap:
                break
            self._calls_total += 1
            # Defense-in-depth allowlist check
            if not is_tool_allowlisted(tool_name):
                self._calls_blocked_by_allowlist += 1
                logger.warning(
                    "[ReadonlyEvidenceProber] tool %r blocked by "
                    "allowlist (plan generator should not produce "
                    "this)", tool_name,
                )
                continue
            try:
                result_text = self._backend.execute(
                    tool_name=tool_name, args=args,
                )
            except Exception as exc:  # noqa: BLE001 — defensive
                self._calls_failed += 1
                logger.debug(
                    "[ReadonlyEvidenceProber] backend.execute "
                    "raised on %r: %s", tool_name, exc,
                )
                continue
            rounds_used += 1
            if isinstance(result_text, str) and result_text:
                result_chunks.append(result_text)
            elif result_text:
                # Coerce non-string defensively
                result_chunks.append(str(result_text))

        # Concatenate + bound length
        joined = "\n".join(result_chunks)
        if len(joined) > self._MAX_ANSWER_CHARS:
            joined = (
                joined[: self._MAX_ANSWER_CHARS - 3] + "..."
            )

        return make_probe_answer(
            question=question.question,
            answer_text=joined,
            tool_rounds_used=rounds_used,
        )

    # ---- tool plan compute ----------------------------------------------

    def _plan_for_question(
        self, question: ProbeQuestion,
    ) -> _ToolPlan:
        """Translate a ProbeQuestion's resolution_method hint into a
        bounded tool sequence. Pure data — no I/O. NEVER raises.

        Resolution_method values map to tool sequences. Unknown
        method → empty plan (returns empty answer; safe)."""
        try:
            method = (question.resolution_method or "").strip().lower()
            q = question.question or ""
            # Templates are deterministic per (method, question)
            # so probes for the same context produce the same plan.
            if method == "read_file":
                # The generator should embed the file path in the
                # question. We don't parse it here — pass the
                # question text as a hint to the backend, which
                # is responsible for the actual file lookup.
                return _ToolPlan(
                    steps=((
                        "read_file",
                        {"hint": q, "method": method},
                    ),),
                )
            if method == "search_code":
                return _ToolPlan(
                    steps=((
                        "search_code",
                        {"query": q, "method": method},
                    ),),
                )
            if method == "get_callers":
                return _ToolPlan(
                    steps=((
                        "get_callers",
                        {"target": q, "method": method},
                    ),),
                )
            if method == "list_symbols":
                return _ToolPlan(
                    steps=((
                        "list_symbols",
                        {"target": q, "method": method},
                    ),),
                )
            if method == "list_dir":
                return _ToolPlan(
                    steps=((
                        "list_dir",
                        {"path": q, "method": method},
                    ),),
                )
            if method == "glob_files":
                return _ToolPlan(
                    steps=((
                        "glob_files",
                        {"pattern": q, "method": method},
                    ),),
                )
            if method in ("git_blame", "git_log", "git_diff"):
                return _ToolPlan(
                    steps=((
                        method,
                        {"target": q, "method": method},
                    ),),
                )
            # Unknown method — fallback compose: search_code +
            # list_symbols. Safe because both are read-only.
            return _ToolPlan(
                steps=(
                    ("search_code", {"query": q, "method": method}),
                ),
            )
        except Exception:  # noqa: BLE001 — defensive
            return _ToolPlan(steps=())


# ---------------------------------------------------------------------------
# Default singleton (mirrors Slice 1 style; tests use direct
# construction with capturing backends)
# ---------------------------------------------------------------------------


_default_prober: Optional[ReadonlyEvidenceProber] = None


def get_default_prober() -> ReadonlyEvidenceProber:
    """Singleton default with null backend. Production callers
    construct with their own Venom-filtered backend. NEVER
    raises."""
    global _default_prober
    if _default_prober is None:
        _default_prober = ReadonlyEvidenceProber()
    return _default_prober


def reset_default_prober_for_tests() -> None:
    """Test isolation."""
    global _default_prober
    _default_prober = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "READONLY_EVIDENCE_PROBER_SCHEMA_VERSION",
    "READONLY_TOOL_ALLOWLIST",
    "QuestionResolver",
    "ReadonlyEvidenceProber",
    "ReadonlyToolBackend",
    "get_default_prober",
    "is_tool_allowlisted",
    "prober_enabled",
    "reset_default_prober_for_tests",
]
