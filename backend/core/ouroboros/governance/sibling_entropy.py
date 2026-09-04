# backend/core/ouroboros/governance/sibling_entropy.py
"""Structural diversity for sibling candidate draws.

## The measurement that produced this module

A GRPO group needs siblings that differ in LOGIC. Measured on the live
trajectory corpus (2026-09-01, 3 groups with 2+ responses):

* every sibling pair shared >=95.9% of its AST (``difflib`` on
  ``ast.dump``, ``autojunk=False``), and 0.9978-1.0000 of its node-type
  multiset;
* identifier counts (84/84), call vocabulary (jaccard 1.0000, 13 calls)
  and ``defs``/``stmt`` counts were IDENTICAL across siblings;
* the actual differences were docstring prose ("Display summary
  *including* total count" vs "*with* total count"), one unused
  ``, Optional`` import, and a deleted comment;
* under ``_python_ast_fingerprint`` every group lost at least one sibling
  to exact structural duplication, and one group collapsed to a SINGLE
  fingerprint -- two responses carrying one answer.

The reward was therefore right to score them within 0.0003 and drop the
group. Nothing downstream can recover information the draw never
contained, which is why this is fixed at the DRAW and not in the reward.

## Why the old dedup missed it

``_extend_with_siblings`` deduplicated on ``candidate_hash`` -- exact
byte equality. Two candidates differing only in a docstring word have
different hashes, so they were counted as distinct and shipped. Exact
equality is the wrong predicate: the corpus is full of pairs that are
byte-different and structurally identical.

## The two causes, both addressed here

1. **Entropy.** Every sibling was drawn at the same hardcoded
   ``temperature=0.2`` (``PrimeProvider._generate_impl``). Siblings drawn
   from a near-deterministic distribution are near-identical by
   construction. ``sampling_for`` gives each draw its own point in
   sampling space. The FIRST draw is deliberately left at the legacy
   settings, so the candidate an op would have produced anyway is
   byte-identical and only the bonus draws explore.
2. **Acceptance.** ``is_structurally_redundant`` rejects a sibling that
   adds no logic, so a redundant draw is re-taken at higher entropy
   rather than persisted.

## A note on measuring similarity

``difflib.SequenceMatcher`` defaults to ``autojunk=True``, which treats
any character occurring in >1% of a string longer than 200 chars as
junk. On ``ast.dump`` output -- thousands of chars of highly repetitive
tokens -- that is catastrophic: the same pairs measured 0.3186 with
autojunk and 0.9595 without. An earlier session read those numbers as
"the siblings are 67% different" and nearly refactored a correct reward
function on the strength of it. **Every comparison here passes
``autojunk=False``.**

Pure, stdlib-only, no LLM. NEVER raises: a diversity check that throws
would turn a bonus draw into a failed op.
"""
from __future__ import annotations

import difflib
import hashlib
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: Master switch. False restores byte-identical pre-entropy behaviour:
#: every draw at the legacy sampling point, dedup by hash only.
_ENV_ENABLED = "JARVIS_SIBLING_ENTROPY_ENABLED"

#: Above this structural similarity two candidates carry the same logic.
#: 0.95 is not arbitrary: the measured near-duplicates sat at 0.959-1.000.
_ENV_THRESHOLD = "JARVIS_SIBLING_DIVERSITY_THRESHOLD"

#: How many times ONE draw may be re-taken at elevated entropy before the
#: loop accepts what it has. Bounded because a re-draw costs a full
#: generation out of the op's slack.
_ENV_MAX_RESAMPLE = "JARVIS_SIBLING_MAX_RESAMPLE"

#: The legacy sampling point. Draw 1 uses exactly this, so an op that
#: never draws a sibling is byte-identical to the pre-entropy pipeline.
_LEGACY_TEMPERATURE = 0.2

#: Ceiling on temperature. Past ~1.2 a mid-size coder model stops
#: producing parseable Python, and an unparseable sibling is worth less
#: than a redundant one -- it cannot even reach the substance tier.
_ENV_TEMP_CEILING = "JARVIS_SIBLING_TEMP_CEILING"

#: Rung multiplier applied per CONSECUTIVE collapsed slot. A slot collapses
#: when every draw it was allowed came back redundant, a no-op, or
#: unparseable. One collapse says "this rung is exhausted"; two in a row
#: say the whole region is, and stepping one rung at a time from inside
#: it is how soak 19 re-drew at T=0.95 and got similarity 1.0000 fourteen
#: times out of fifteen. 1.0 is OFF and leaves the ladder byte-identical.
_ENV_ESCALATION_MULT = "JARVIS_SIBLING_ESCALATION_MULTIPLIER"


def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _envi(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def entropy_enabled() -> bool:
    """Master switch, default ON."""
    return (os.environ.get(_ENV_ENABLED, "true") or "").strip().lower() \
        not in ("0", "false", "no", "off")


def diversity_threshold() -> float:
    """Structural similarity at or above which a sibling adds nothing."""
    return max(0.0, min(1.0, _envf(_ENV_THRESHOLD, 0.95)))


def max_resample_attempts() -> int:
    """Re-draws allowed per sibling slot. Clamped to [0, 3]."""
    return max(0, min(3, _envi(_ENV_MAX_RESAMPLE, 1)))


def temperature_ceiling() -> float:
    return max(_LEGACY_TEMPERATURE, _envf(_ENV_TEMP_CEILING, 1.15))


def escalation_multiplier() -> float:
    """Per-collapse rung multiplier. 1.0 = off. Clamped to [1.0, 4.0].

    Bounded above because the ladder is bounded above: past the last rung
    every step is ``_ESCALATION_STEP`` of temperature toward
    ``temperature_ceiling()``, so a multiplier that overshoots the ceiling
    on the first collapse has nothing left to escalate to on the second.
    """
    return max(1.0, min(4.0, _envf(_ENV_ESCALATION_MULT, 1.0)))


def collapse_bump(collapse_streak: int) -> int:
    """Extra rungs a draw climbs after ``collapse_streak`` consecutive
    collapsed slots: ``mult**streak - 1``, so 0 at streak 0 and 0 whenever
    the multiplier is off. Exponential by design -- a collapse is evidence
    that the current region is exhausted, and the answer is to LEAVE it,
    not to take one more step inside it.
    """
    streak = max(0, int(collapse_streak or 0))
    if streak <= 0:
        return 0
    mult = escalation_multiplier()
    if mult <= 1.0:
        return 0
    # The ladder + overflow saturate at the ceiling within a handful of
    # rungs; cap the bump so an absurd streak cannot overflow an int.
    return int(min(64.0, mult ** streak)) - 1


# ---------------------------------------------------------------------------
# The entropy ladder
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SiblingSampling:
    """One point in sampling space for one draw.

    ``temperature`` rides the parameter ``PrimeProvider.generate`` already
    accepts (the T2 epistemic override RepairEngine threads). The rest
    ride ``LocalConfig`` into the engine's ``options`` block. Nothing here
    builds a request -- that stays in exactly one place.
    """

    temperature: float = _LEGACY_TEMPERATURE
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    repeat_penalty: Optional[float] = None
    seed: Optional[int] = None

    @property
    def is_legacy(self) -> bool:
        """True when this is the untouched pre-entropy sampling point."""
        return (
            abs(self.temperature - _LEGACY_TEMPERATURE) < 1e-9
            and self.top_p is None and self.top_k is None
            and self.repeat_penalty is None and self.seed is None
        )

    def config_overrides(self) -> Dict[str, Any]:
        """The ``LocalConfig`` fields this point sets. Only what is set."""
        out: Dict[str, Any] = {}
        if self.top_p is not None:
            out["top_p"] = float(self.top_p)
        if self.top_k is not None:
            out["top_k"] = int(self.top_k)
        if self.repeat_penalty is not None:
            out["repeat_penalty"] = float(self.repeat_penalty)
        if self.seed is not None:
            out["seed"] = int(self.seed)
        return out

    def describe(self) -> str:
        bits = [f"T={self.temperature:.2f}"]
        if self.top_p is not None:
            bits.append(f"top_p={self.top_p:.2f}")
        if self.top_k is not None:
            bits.append(f"top_k={self.top_k}")
        if self.repeat_penalty is not None:
            bits.append(f"rp={self.repeat_penalty:.2f}")
        if self.seed is not None:
            bits.append(f"seed={self.seed}")
        return " ".join(bits)


#: Progressive rungs for draws 2, 3, 4, ... Draw 1 is never on this ladder.
#:
#: Each rung moves temperature AND the truncation window together. Raising
#: temperature alone re-weights a distribution that ``top_k``/``top_p``
#: have already truncated to the same few tokens, which is why a
#: temperature-only knob produced the near-identical siblings that were
#: measured -- the tail it was re-weighting had been cut off.
_LADDER: Tuple[Tuple[float, float, int, float], ...] = (
    (0.70, 0.95, 60, 1.05),
    (0.95, 0.92, 100, 1.10),
    (1.10, 0.90, 140, 1.15),
)

#: Added to temperature per rung past the ladder's end. A redundant draw
#: means this region of sampling space is exhausted, so the answer is to
#: leave it, not to sit in it.
_ESCALATION_STEP = 0.15


def sampling_for(draw_index: int, *, escalation: int = 0,
                 op_id: str = "", collapse_streak: int = 0) -> SiblingSampling:
    """Sampling point for draw ``draw_index`` (1-based) at ``escalation``.

    Draw 1 at escalation 0 returns the LEGACY point: an op that draws one
    candidate behaves byte-identically to the pre-entropy pipeline.

    ``op_id`` only seeds the RNG stream so two ops working the same prompt
    do not walk identical sampling trajectories; it never changes the
    temperature/top_p schedule, so a rung stays reproducible per op.

    ``collapse_streak`` is how many slots in a row have collapsed before
    this draw. With the multiplier off (the default) it changes nothing;
    on, it climbs ``collapse_bump(streak)`` extra rungs and moves the seed
    with them, so a draw after two dead slots is neither the rung nor the
    trajectory that just produced twins.
    """
    if draw_index <= 1 and escalation <= 0:
        return SiblingSampling()
    if not entropy_enabled():
        return SiblingSampling()

    bump = collapse_bump(collapse_streak)
    rung = max(0, draw_index - 2) + max(0, escalation) + bump
    temp, top_p, top_k, rp = _LADDER[min(rung, len(_LADDER) - 1)]
    # Past the last rung, keep climbing temperature rather than repeating
    # a rung that has already produced a redundant draw.
    overflow = max(0, rung - (len(_LADDER) - 1))
    temp = min(temperature_ceiling(), temp + overflow * _ESCALATION_STEP)
    return SiblingSampling(
        temperature=round(temp, 4),
        top_p=top_p,
        top_k=top_k,
        repeat_penalty=rp,
        seed=_derive_seed(op_id, draw_index, escalation + bump),
    )


def _derive_seed(op_id: str, draw_index: int, escalation: int) -> int:
    """A distinct, reproducible sampler seed per (op, draw, escalation).

    Distinct because an engine that reuses one seed reproduces one
    trajectory however high the temperature -- the knob would look wired
    and change nothing. Reproducible because a soak that cannot be re-run
    to the same candidates cannot be bisected.
    """
    raw = f"{op_id}|{draw_index}|{escalation}".encode("utf-8", "replace")
    # 31 bits: engines vary in whether they accept a full 64-bit seed, and
    # every one of them accepts a positive int32.
    return int(hashlib.sha256(raw).hexdigest()[:8], 16) & 0x7FFFFFFF


# ---------------------------------------------------------------------------
# Structural identity
# ---------------------------------------------------------------------------


def structural_fingerprint(
    source: str, baseline: Optional[str] = None,
) -> Optional[str]:
    """Structure of ``source``; None if it will not parse.

    Without a ``baseline`` this is the docstring-stripped ``ast.dump`` of
    the whole file, delegated to
    ``candidate_value_gate._python_ast_fingerprint`` -- the repo's one
    definition of "same code modulo presentation".

    With a ``baseline`` (the file as it is on disk) it is the fingerprint
    of the CHANGED HUNKS only -- see ``changed_hunks``. Measured on soak
    `bt-2026-09-01-235803`: candidates are whole-file rewrites of
    80-150 line modules, so two genuinely different implementations of
    one small edit still shared 96-99% of the tree and read as redundant
    at any sane threshold. The unchanged file was dominating the ratio.
    Comparing what each sibling actually CHANGED removes that dilution
    without a second similarity function: the hunks are dumped with the
    same stripper and compared with the same ratio.

    A parseable candidate with NO changed hunks fingerprints as ``""`` --
    a real value, not None: it is the answer "change nothing", and two of
    those are rightly one answer.
    """
    if not source or not source.strip():
        return None
    try:
        if baseline is not None:
            hunks = changed_hunks(source, baseline)
            if hunks is not None:
                return "\n".join(h.dump for h in hunks)
        from backend.core.ouroboros.governance.candidate_value_gate import (  # noqa: PLC0415
            _python_ast_fingerprint,
        )
        return _python_ast_fingerprint(source)
    except SyntaxError:
        return None
    except Exception:  # noqa: BLE001 — a diversity check must never break a draw
        logger.debug("structural_fingerprint fault", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Hunk-level structure
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Hunk:
    """One top-level statement the candidate added or changed."""

    dump: str
    kinds: Tuple[str, ...]


#: Node kinds that carry ALGORITHM. Two hunks that differ in these differ
#: in what the code does; two that differ only in the kinds below differ
#: in what it is called or what it is set to.
_FLOW_KINDS = frozenset({
    "If", "For", "AsyncFor", "While", "Try", "TryStar", "With", "AsyncWith",
    "Match", "Return", "Raise", "Call", "Lambda", "ListComp", "DictComp",
    "SetComp", "GeneratorExp", "BoolOp", "Compare", "IfExp",
})
_PLUMBING_KINDS = frozenset({
    "Assign", "AnnAssign", "AugAssign", "Constant", "Import", "ImportFrom",
    "Name", "Attribute", "Expr", "Pass", "Global", "Nonlocal",
})

#: Threshold adjustment, applied to the configured threshold and clamped.
#: A flow-heavy hunk needs to be NEARER to identical before it is called
#: the same answer; a plumbing-only hunk is called the same answer more
#: readily -- renamed constants are not a second algorithm.
_FLOW_ADJUST = +0.02
_PLUMBING_ADJUST = -0.05


def changed_hunks(source: str, baseline: str) -> Optional[Tuple[Hunk, ...]]:
    """Top-level statements of ``source`` that are not in ``baseline``.

    Reuses the value gate's own primitives (``_stripped_tree`` and the
    per-statement ``ast.dump`` walk that ``_python_semantic_weight``
    already performs against the on-disk file) so "what changed" has one
    definition in the repo. ``autojunk=False`` for the same reason it is
    everywhere in this module. Returns None when either side will not
    parse -- the caller then falls back to the whole-file fingerprint
    rather than guessing at a diff.
    """
    try:
        import ast  # noqa: PLC0415

        from backend.core.ouroboros.governance.candidate_value_gate import (  # noqa: PLC0415
            _stripped_tree,
        )
        t_new = _stripped_tree(source)
        t_old = _stripped_tree(baseline)
    except SyntaxError:
        return None
    except Exception:  # noqa: BLE001
        logger.debug("changed_hunks fault", exc_info=True)
        return None
    d_old = [ast.dump(s, include_attributes=False) for s in t_old.body]
    d_new = [ast.dump(s, include_attributes=False) for s in t_new.body]
    out: List[Hunk] = []
    sm = difflib.SequenceMatcher(None, d_old, d_new, autojunk=False)
    for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        for idx in range(j1, j2):
            node = t_new.body[idx]
            kinds = tuple(sorted({type(n).__name__ for n in ast.walk(node)}))
            out.append(Hunk(dump=d_new[idx], kinds=kinds))
    return tuple(out)


def hunk_threshold(base: float, hunks: Optional[Sequence[Hunk]]) -> float:
    """``base`` scaled by what the changed hunks are MADE of.

    Dominant kind decides: more flow nodes than plumbing nodes across the
    hunks raises the bar (harder to call two flow rewrites the same
    answer); plumbing-only lowers it. No hunks, or no baseline, leaves the
    configured threshold exactly as it was. Clamped to [0.5, 1.0].
    """
    if not hunks:
        return base
    flow = sum(1 for h in hunks for k in h.kinds if k in _FLOW_KINDS)
    plumbing = sum(1 for h in hunks for k in h.kinds if k in _PLUMBING_KINDS)
    if flow == 0 and plumbing == 0:
        return base
    adj = _FLOW_ADJUST if flow >= plumbing else _PLUMBING_ADJUST
    return max(0.5, min(1.0, base + adj))


def candidate_baseline(candidate: Any, repo_root: Optional[str]) -> Optional[str]:
    """The on-disk file a candidate proposes to replace, or None.

    Resolved the way the generator resolves every path: ``repo_root`` when
    it has one, else the working directory. A file that does not exist is
    a CREATION -- there is nothing to diff against, and the whole tree is
    the hunk. NEVER raises.
    """
    if not isinstance(candidate, dict):
        return None
    rel = str(candidate.get("file_path") or candidate.get("source_path") or "")
    if not rel:
        return None
    try:
        import os  # noqa: PLC0415

        path = rel if os.path.isabs(rel) else os.path.join(repo_root or os.getcwd(), rel)
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception:  # noqa: BLE001
        return None


def structural_similarity(fp_a: Optional[str], fp_b: Optional[str]) -> float:
    """Similarity of two fingerprints in [0, 1]. Identical -> exactly 1.0.

    ``autojunk=False`` is load-bearing, not a style choice -- see the
    module docstring. Unparseable input (None) is treated as maximally
    DISSIMILAR: a candidate that does not parse is not a duplicate of one
    that does, and must not be suppressed as one.
    """
    if fp_a is None or fp_b is None:
        return 0.0
    if fp_a == fp_b:
        return 1.0
    try:
        return difflib.SequenceMatcher(None, fp_a, fp_b, autojunk=False).ratio()
    except Exception:  # noqa: BLE001
        logger.debug("structural_similarity fault", exc_info=True)
        return 0.0


def candidate_source(candidate: Any) -> str:
    """Best-effort source text out of one candidate mapping.

    Reads the fields the 2b.1 envelope actually carries. Returns "" when
    there is nothing to compare, which callers treat as "cannot judge".
    """
    if not isinstance(candidate, dict):
        return ""
    for key in ("full_content", "content", "new_content", "source", "diff", "patch"):
        val = candidate.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def fingerprint_candidates(
    candidates: Sequence[Any], repo_root: Optional[str] = None,
) -> Tuple[str, ...]:
    """Fingerprints for a candidate LIST, in order, skipping the unreadable.

    With ``repo_root`` each candidate is fingerprinted against the file it
    proposes to replace (hunk-level); without it, or for a file that does
    not exist yet, the whole tree is the fingerprint.
    """
    out: List[str] = []
    for cand in candidates or ():
        base = candidate_baseline(cand, repo_root) if repo_root is not None else None
        fp = structural_fingerprint(candidate_source(cand), base)
        if fp is not None:
            out.append(fp)
    return tuple(out)


def hunks_for_candidates(
    candidates: Sequence[Any], repo_root: Optional[str] = None,
) -> Tuple[Hunk, ...]:
    """Every changed hunk across a candidate list, for threshold scaling."""
    out: List[Hunk] = []
    for cand in candidates or ():
        base = candidate_baseline(cand, repo_root) if repo_root is not None else None
        if base is None:
            continue
        hunks = changed_hunks(candidate_source(cand), base)
        if hunks:
            out.extend(hunks)
    return tuple(out)


def is_structurally_redundant(
    new_fingerprints: Sequence[str],
    seen_fingerprints: Iterable[str],
    *,
    threshold: Optional[float] = None,
    hunks: Optional[Sequence[Hunk]] = None,
) -> Tuple[bool, float]:
    """Does this draw add logic the group does not already have?

    Returns ``(redundant, peak_similarity)``. A draw is redundant when
    EVERY one of its fingerprints is at-or-above ``threshold`` against
    something already accepted: a multi-file sibling that repeats one file
    but rewrites another is genuinely new and must survive.

    A draw with nothing fingerprintable is NOT redundant. Unparseable
    output is a real (bad) answer the verifier grades in its syntax band;
    silently discarding it would hide a failure mode from the corpus.

    The master switch is honoured HERE, not only at the ladder. Gating
    just the sampling point left the acceptance filter live with entropy
    off, so a "disabled" feature still rejected siblings -- and re-drew
    them at an unchanged temperature, which is strictly worse than doing
    nothing because the re-draw is redundant by construction. A master
    switch that disables half a feature is not a rollback, and this is
    the one seam every acceptance decision passes through.
    """
    if not entropy_enabled():
        return False, 0.0
    # `is not None`, not truthiness: at hunk level "" is the fingerprint of
    # a candidate that changes NOTHING -- a real answer, and two of those
    # are the same answer. Filtering falsy strings made every no-op draw
    # invisible to the comparison and therefore never redundant.
    seen = [s for s in seen_fingerprints if s is not None]
    if not new_fingerprints or not seen:
        return False, 0.0
    thr = diversity_threshold() if threshold is None else threshold
    # Scale by what the hunks are made of -- see ``hunk_threshold``. Without
    # hunk information (no baseline, whole-file mode) this is the identity.
    thr = hunk_threshold(thr, hunks)
    peak = 0.0
    all_redundant = True
    for fp in new_fingerprints:
        best = max((structural_similarity(fp, s) for s in seen), default=0.0)
        peak = max(peak, best)
        if best < thr:
            all_redundant = False
    return all_redundant, peak


def distinct_structure_count(candidates: Sequence[Any]) -> int:
    """How many STRUCTURALLY distinct answers a candidate set really holds.

    This is the number that matters to the corpus: a group of three whose
    fingerprints collapse to one carries one answer, and a preference pair
    cannot be built from it however many rows were written.
    """
    return len({fp for fp in fingerprint_candidates(candidates)})


def retract_draw(op_id: str, candidates: Sequence[Any], *, reason: str = "") -> bool:
    """Tell the recorder a draw it already saw has been REJECTED.

    ``record_generation`` fires inside the provider, before the sibling
    loop judges the draw, so without this a redundant twin still reached
    the corpus. One forwarding seam here keeps the generator's import
    surface unchanged and gives the two drop branches a single call.
    NEVER raises: a telemetry retraction must not fail a generation.
    """
    try:
        hashes = tuple(
            str((c or {}).get("candidate_hash", "") or "")
            for c in (candidates or ()) if isinstance(c, dict)
        )
        hashes = tuple(h for h in hashes if h)
        if not op_id or not hashes:
            return False
        from backend.core.ouroboros.governance.observability.trajectory_recorder import (  # noqa: E501,PLC0415
            record_retraction,
        )
        return bool(record_retraction(
            op_id=str(op_id), candidate_hashes=hashes, reason=reason,
        ))
    except Exception:  # noqa: BLE001
        logger.debug("retract_draw fault", exc_info=True)
        return False


__all__ = [
    "SiblingSampling", "candidate_source", "distinct_structure_count",
    "retract_draw", "Hunk", "changed_hunks", "hunk_threshold",
    "candidate_baseline", "hunks_for_candidates",
    "diversity_threshold", "entropy_enabled", "fingerprint_candidates",
    "is_structurally_redundant", "max_resample_attempts", "sampling_for",
    "structural_fingerprint", "structural_similarity", "temperature_ceiling",
    "escalation_multiplier", "collapse_bump",
]


def register_flags(registry: Any) -> int:
    """Module-owned FlagRegistry registration. NEVER raises.

    These four decide whether a farming soak produces a trainable corpus
    or a pile of rows that cannot pair, so an operator needs to find them
    by name without reading this file.
    """
    try:
        from backend.core.ouroboros.governance.flag_registry import (  # noqa: PLC0415
            Category,
            FlagSpec,
            FlagType,
        )
    except ImportError:
        return 0

    src = "backend/core/ouroboros/governance/sibling_entropy.py"
    specs = [
        FlagSpec(
            name=_ENV_ENABLED,
            type=FlagType.BOOL,
            default=True,
            description=(
                "Draw each sibling from its own point in sampling space, and "
                "reject a sibling that adds no logic. OFF restores the "
                "measured pre-entropy behaviour: every draw at temperature "
                "0.2 and dedup by candidate_hash, which produced 8 corpus "
                "rows carrying 3 distinct answers and zero constructible "
                "preference pairs. The switch gates BOTH halves -- gating "
                "only the ladder would leave the filter rejecting siblings "
                "and re-drawing them at an unchanged temperature."
            ),
            category=Category.SAFETY,
            source_file=src,
            example="true",
            since="sibling entropy arc (2026-09-01)",
        ),
        FlagSpec(
            name=_ENV_THRESHOLD,
            type=FlagType.FLOAT,
            default=0.95,
            description=(
                "Structural similarity (docstring-stripped AST, difflib with "
                "autojunk=False) at or above which a sibling is treated as "
                "the same answer. 0.95 sits below the measured near-"
                "duplicates, which ran 0.959-1.000. Raising it toward 1.0 "
                "accepts more near-duplicates; lowering it rejects genuinely "
                "different answers and wastes generation budget re-drawing."
            ),
            category=Category.TUNING,
            source_file=src,
            example="0.95",
            since="sibling entropy arc (2026-09-01)",
        ),
        FlagSpec(
            name=_ENV_MAX_RESAMPLE,
            type=FlagType.CAPACITY if hasattr(Category, "CAPACITY") else Category.CAPACITY,
            default=1,
            description=(
                "Re-draws allowed for ONE sibling slot when the draw came "
                "back redundant. Clamped to [0, 3]: every re-draw is a full "
                "generation out of the op's slack, and the budget test that "
                "guards a first draw guards each re-draw identically. 0 "
                "keeps the rejection but never re-draws."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example="1",
            since="sibling entropy arc (2026-09-01)",
        ),
        FlagSpec(
            name=_ENV_TEMP_CEILING,
            type=FlagType.FLOAT,
            default=1.15,
            description=(
                "Hard cap on the entropy ladder's temperature. Past roughly "
                "1.2 a mid-size coder model stops emitting parseable Python, "
                "and an unparseable sibling is worth less than a redundant "
                "one -- it cannot reach the substance tier at all."
            ),
            category=Category.TUNING,
            source_file=src,
            example="1.15",
            since="sibling entropy arc (2026-09-01)",
        ),
        FlagSpec(
            name=_ENV_ESCALATION_MULT,
            type=FlagType.FLOAT,
            default=1.0,
            description=(
                "Rung multiplier applied per CONSECUTIVE collapsed sibling "
                "slot (every draw redundant, no-op, or unparseable). A draw "
                "after N collapsed slots climbs mult**N - 1 extra rungs and "
                "takes a matching seed, bounded by JARVIS_SIBLING_TEMP_"
                "CEILING. 1.0 is OFF: the ladder is byte-identical. Soak 19 "
                "re-drew inside an exhausted region and measured similarity "
                "1.0000 in 14 of 15 dropped siblings; this is the knob that "
                "leaves the region instead. Clamped to [1.0, 4.0]."
            ),
            category=Category.TUNING,
            source_file=src,
            example="2.0",
            since="sibling fulfillment arc (2026-09-04)",
        ),
    ]
    n = 0
    for spec in specs:
        try:
            registry.register(spec)
            n += 1
        except Exception:  # noqa: BLE001 — registration is descriptive
            continue
    return n
