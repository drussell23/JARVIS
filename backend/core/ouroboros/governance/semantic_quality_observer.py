"""What the served model actually produced, measured at GENERATE.

## Why this exists

Three soaks have produced zero landings and taught us nothing about the model,
because every op was shed at VALIDATE for reasons that have nothing to do with
the candidate: no covering test, work already done, a schema forced to
whole-file by a size gate. Judging the model on landings under those conditions
measures the harness.

Candidate quality is observable BEFORE any of that. A candidate is a text, and
the questions worth asking about it — did it obey the schema it was given, does
its change carry meaning, did its patch parse — are answerable the moment it
arrives, whatever the pipeline later decides to do with the op.

## What it measures, and why each is derived rather than judged

* **schema adherence** — the schema the op negotiated vs the shape the model
  returned. The only fully objective number here: the request is recorded, the
  reply is inspectable.

* **AST delta efficiency** — canonical AST nodes changed per changed line. A
  surgical edit scores near 1. A whole-file re-emission that alters three lines
  of behaviour scores near 0, because the denominator is the whole file. This
  is the number that would have shown the 800-line threshold's damage as a
  measurement instead of a forensic reading of three commits.

* **null churn** — lines the model changed that survive a text diff and vanish
  under :func:`declared_symbols.canonical_ast_dump`. **This is how a semantic
  tic is detected without anyone naming it in advance.** ``exc_info=True`` on a
  call that already defaults it, re-quoting a string, reflowing a docstring: no
  rule lists these, and no rule has to. They are exactly the changes that
  cannot survive canonicalisation, so the canonicaliser finds them by
  construction — including the ones nobody has met yet. A tic that a future
  model invents will be caught by the same subtraction.

* **malformed diffs** — patches that did not parse or place.

Nothing here scores the model against a threshold or a grade. It reports
distributions; the operator draws the conclusion. A "quality bar" baked in here
would be the same mistake as the 800-line gate: a constant deciding a question
it cannot see.

## Composition

Reuses the telemetry seams already in the generation path rather than adding a
parallel logger: ``_note_diff_outcome`` for diff application evidence, the op
ledger for identity, and ``canonical_ast_dump`` for normalisation. The observer
never touches the apply path and never raises into it.
"""
from __future__ import annotations

import ast
import difflib
import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.SemanticQuality")

__all__ = [
    "CandidateObservation",
    "observe_candidate",
    "observer_enabled",
    "snapshot",
    "render_report",
    "reset_for_tests",
]

_ENV_ENABLED = "JARVIS_SEMANTIC_QUALITY_OBSERVER_ENABLED"
_ENV_PATH = "JARVIS_SEMANTIC_QUALITY_PATH"
_DEFAULT_PATH = ".ouroboros/semantic_quality.jsonl"

_lock = threading.Lock()
_observations: List["CandidateObservation"] = []


def observer_enabled() -> bool:
    """Default ON. It reads what is already flowing and writes one JSONL line
    per candidate; the cost of not measuring has been three blind soaks."""
    return os.environ.get(_ENV_ENABLED, "true").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _path() -> Path:
    raw = (os.environ.get(_ENV_PATH, "") or "").strip()
    return Path(raw or _DEFAULT_PATH)


@dataclass
class CandidateObservation:
    """One candidate, measured. Every field is a count or a ratio — no verdicts."""

    op_id: str = ""
    served_model: str = ""
    target_file: str = ""
    #: the schema the op NEGOTIATED ("diff" / "full_content")
    schema_requested: str = ""
    #: the shape the model actually RETURNED
    schema_returned: str = ""
    adhered: bool = False
    #: lines that differ textually between original and candidate
    changed_lines: int = 0
    #: canonical AST nodes that differ — meaning that survived normalisation
    ast_delta_nodes: int = 0
    #: changed_lines that produce NO canonical AST change
    null_churn_lines: int = 0
    malformed_diff: bool = False
    detail: str = ""

    @property
    def delta_efficiency(self) -> float:
        """Meaning per changed line. 1.0 = every touched line carried one."""
        if self.changed_lines <= 0:
            return 0.0
        return round(min(1.0, self.ast_delta_nodes / self.changed_lines), 4)

    @property
    def null_churn_ratio(self) -> float:
        """Share of the change that survives text diff and dies under the AST."""
        if self.changed_lines <= 0:
            return 0.0
        return round(self.null_churn_lines / self.changed_lines, 4)


def _ast_node_count(source: str) -> int:
    try:
        return sum(1 for _ in ast.walk(ast.parse(source)))
    except (SyntaxError, ValueError):
        return 0


def _canonical(source: str) -> Optional[str]:
    try:
        from backend.core.ouroboros.governance.declared_symbols import (  # noqa: E501,PLC0415
            canonical_ast_dump,
        )
        return canonical_ast_dump(source)
    except Exception:  # noqa: BLE001
        return None


def _changed_lines(before: str, after: str) -> int:
    diff = difflib.unified_diff(
        before.splitlines(), after.splitlines(), lineterm="", n=0,
    )
    return sum(
        1 for ln in diff
        if (ln.startswith("+") or ln.startswith("-"))
        and not ln.startswith(("+++", "---"))
    )


def _null_churn_lines(before: str, after: str) -> int:
    """Changed lines that carry no canonical meaning.

    Measured by SUBTRACTION, not by pattern: re-render both sides through the
    canonicaliser and diff those. Whatever the text diff saw that this one does
    not is churn — and it never had to be enumerated. A tic nobody has met yet
    lands in this number on the day the model invents it.
    """
    cb, ca = _canonical(before), _canonical(after)
    if cb is None or ca is None:
        return 0
    if cb == ca:
        return _changed_lines(before, after)     # the whole change was null
    textual = _changed_lines(before, after)
    semantic = _changed_lines(cb, ca)
    return max(0, textual - semantic)


def observe_candidate(
    *,
    ctx: Any,
    target_file: str,
    original: Optional[str],
    candidate_content: Optional[str],
    schema_requested: str,
    schema_returned: str,
    malformed_diff: bool = False,
    detail: str = "",
) -> Optional[CandidateObservation]:
    """Measure one candidate. NEVER raises; returns ``None`` when disabled.

    Called from the generation path, which must be unaffected by anything that
    happens here — an observer that can break the thing it observes is worse
    than no observer.
    """
    if not observer_enabled():
        return None
    try:
        ri = getattr(getattr(ctx, "telemetry", None), "routing_intent", None)
        obs = CandidateObservation(
            op_id=str(getattr(ctx, "op_id", "") or "")[:64],
            served_model=str(getattr(ri, "served_model", "") or ""),
            target_file=str(target_file or ""),
            schema_requested=str(schema_requested or ""),
            schema_returned=str(schema_returned or ""),
            adhered=bool(schema_requested) and schema_requested == schema_returned,
            malformed_diff=bool(malformed_diff),
            detail=str(detail or "")[:200],
        )
        if isinstance(original, str) and isinstance(candidate_content, str) and original:
            obs.changed_lines = _changed_lines(original, candidate_content)
            cb, ca = _canonical(original), _canonical(candidate_content)
            if cb is not None and ca is not None:
                obs.ast_delta_nodes = abs(
                    _ast_node_count(candidate_content) - _ast_node_count(original),
                ) or (0 if cb == ca else 1)
            obs.null_churn_lines = _null_churn_lines(original, candidate_content)
        with _lock:
            _observations.append(obs)
        _append_jsonl(obs)
        return obs
    except Exception:  # noqa: BLE001 — an observer never perturbs generation
        logger.debug("[SemanticQuality] observation degraded", exc_info=True)
        return None


def _append_jsonl(obs: "CandidateObservation") -> None:
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        row = asdict(obs)
        row["delta_efficiency"] = obs.delta_efficiency
        row["null_churn_ratio"] = obs.null_churn_ratio
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")
    except Exception:  # noqa: BLE001
        pass


def snapshot() -> Tuple["CandidateObservation", ...]:
    with _lock:
        return tuple(_observations)


def render_report() -> str:
    """The distributions, as one operator-readable block. No grades."""
    obs = snapshot()
    if not obs:
        return "[SemanticQuality] no candidates observed"
    n = len(obs)
    with_delta = [o for o in obs if o.changed_lines > 0]
    asked = [o for o in obs if o.schema_requested]
    adhered = [o for o in asked if o.adhered]
    malformed = [o for o in obs if o.malformed_diff]
    lines = [
        f"[SemanticQuality] {n} candidate(s) observed",
        f"  schema adherence : {len(adhered)}/{len(asked)}"
        + (f" ({100.0 * len(adhered) / len(asked):.0f}%)" if asked else " (none asked)"),
        f"  malformed diffs  : {len(malformed)}/{n}",
    ]
    if with_delta:
        eff = sorted(o.delta_efficiency for o in with_delta)
        churn = sorted(o.null_churn_ratio for o in with_delta)
        mid = len(eff) // 2
        lines += [
            f"  changed lines    : min={min(o.changed_lines for o in with_delta)} "
            f"median={sorted(o.changed_lines for o in with_delta)[mid]} "
            f"max={max(o.changed_lines for o in with_delta)}",
            f"  delta efficiency : median={eff[mid]:.3f} "
            f"(1.0 = every touched line carried meaning)",
            f"  null churn ratio : median={churn[mid]:.3f} "
            f"(share of the change that dies under canonicalisation)",
        ]
        worst = max(with_delta, key=lambda o: o.null_churn_ratio)
        if worst.null_churn_ratio > 0:
            lines.append(
                f"  worst churn      : {worst.target_file} "
                f"{worst.null_churn_lines}/{worst.changed_lines} lines carried "
                f"no meaning — inspect for a semantic tic",
            )
    return "\n".join(lines)


#: Per-op tally of malformed diffs, for the cascade ceiling.
_malformed_by_op: Dict[str, int] = {}

#: Ops that have already been handed the reason their patch was rejected.
#: A malformed diff from one of these is the feedback loop failing, which is
#: what a cascade IS — no counter, no budget, no threshold.
_realigned_ops: set = set()


def note_malformed(op_id: str) -> int:
    """Count a malformed diff for *op_id* and return the running total."""
    key = str(op_id or "")[:64]
    with _lock:
        _malformed_by_op[key] = _malformed_by_op.get(key, 0) + 1
        return _malformed_by_op[key]


def note_realignment_armed(op_id: str) -> None:
    """Record that this op has been handed the reason its patch was rejected."""
    with _lock:
        _realigned_ops.add(str(op_id or "")[:64])


def diff_cascade_exhausted_after_feedback(op_id: str) -> bool:
    """Whether this op produced a malformed diff AFTER being told why.

    ## The budget read was the bug, so there is no budget read

    The previous version took ``retries_remaining`` and got it from ``ctx``,
    which does not carry it — the counter lives on the validate runner. Every
    call therefore saw ``0``, the ceiling collapsed to 1, and a SINGLE
    malformed diff shed the op: ``TerminalDiffCascade`` fired 7 times while the
    realignment retry ran 0 times. Threading the real counter down to a
    provider would mean plumbing validate-phase state into the generation path
    to answer a question that does not need it.

    The question this actually wants to ask is not "how many tries are left"
    but "is the feedback working". That is answerable from state this module
    already owns: a first malformed diff arms
    :func:`note_realignment_armed`, and a malformed diff arriving when the flag
    is already set means the model was shown the exact locator it got wrong and
    got it wrong again.

    So there is no threshold, no budget and no constant — the cascade is
    defined by the failure of its own correction. An op that has never been
    given feedback is never shed by this; an op whose feedback did not land is
    shed immediately, because the next attempt has nothing new to work with.
    """
    return str(op_id or "")[:64] in _realigned_ops


def diff_cascade_exhausted(op_id: str, *, retries_remaining: int) -> bool:
    """Whether this op has spent its diff attempts on patches that never parsed.

    ## The ceiling is DERIVED, not chosen

    The budget is the op's OWN remaining retries — the number the FSM is
    already counting down and already honours. A second constant here would be
    a second thing to keep in agreement with it, and the first time they
    disagreed the op would either die early or loop past the FSM's own limit.

    So: a cascade is exhausted when the malformed count has consumed the
    retries the FSM has left. An op with a generous budget gets more attempts;
    a nearly-spent one gets fewer. Nothing to tune.

    The failure this guards is narrow and real: with the schema now strictly
    diff for every single-file op, a model that cannot produce a parseable
    patch for a particular file would retry until the wall clock ends the
    session, holding a worker the whole time. The lessons recorded on each
    attempt are the correction; this is the point at which we stop waiting for
    it to land.

    ## An unreadable budget never cascades

    Measured the hard way in soak bt-2026-09-17-205140: ``retries_remaining``
    was read off ``ctx``, which does not carry it — the counter lives on the
    validate runner — so every call got ``0``, the ceiling collapsed to 1, and
    a SINGLE malformed diff terminated the op. ``TerminalDiffCascade`` fired 7
    times and the realignment retry never ran once, because there was no retry
    left to carry it.

    A budget of zero or less is now "unknown", not "exhausted". Shedding an op
    on a signal that could not be read is the aggressive answer to an
    unanswerable question, and this file has argued against that three times
    already.
    """
    try:
        budget = int(retries_remaining)
        if budget <= 0:
            return False
        with _lock:
            seen = _malformed_by_op.get(str(op_id or "")[:64], 0)
        return seen > 0 and seen >= budget
    except Exception:  # noqa: BLE001
        return False


def reset_for_tests() -> None:
    with _lock:
        _observations.clear()
        _malformed_by_op.clear()
        _realigned_ops.clear()
