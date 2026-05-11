"""
Antivenom Self-Immunization
============================

Closes §40 Wave 2 #7 — the third Wave 2 recursion-bounding arc
+ the structural close of the antivenom-completeness gap that
Wave 1 #8 (Adversarial Autobiography) audited retrospectively.

The framing (from §40 Wave 2 #7 operator binding):

  "When SemanticGuardian flags a pattern, generate K mutations
   of that exact pattern and re-test. If guardian can't catch
   the mutations, auto-file a hardening op against itself."

This substrate is the **deterministic probe loop** that exercises
guardian's pattern-detectors against systematically-mutated
versions of the canonical P9.4 corpus. Mutations are semantically-
equivalent variations of known-adversarial inputs (whitespace
drift, comment insertion, variable rename, string-split-concat,
paren-wrap); guardian SHOULD catch them all. The substrate
records every (entry × mutation) probe and surfaces any
immunization gap — patterns the guardian's static-string AST
detectors miss when the adversary varies trivial form.

Auto-filing the hardening op against itself is **out of scope
for this ship** — it would compose downstream M10
ArchitectureProposer machinery. This substrate produces the
**evidence ledger** that downstream tooling (or the operator)
consumes; closing the loop end-to-end is a follow-on slice.

Composition contract — thin pure-function composer over canonical
sources, zero parallel state, zero hardcoded patterns:

* :mod:`p9_4_adversarial_corpus` — canonical 25-entry CORPUS +
  ``materialize_pattern()`` builder. The baseline adversarial
  inputs are the truth set; mutations derive from these.
* :mod:`semantic_guardian` — canonical 12-pattern detector. The
  audit composes :func:`SemanticGuardian().inspect()` verbatim;
  no parallel detector.
* :mod:`cross_process_jsonl.flock_append_line` — §33.4 audit
  ledger persistence (optional, sub-flag opt-in).
* :mod:`ide_observability_stream.publish_task_event` — canonical
  SSE broker for ``antivenom_immunization_audited`` events.

Closed 4-value :class:`ImmunizationFinding` taxonomy
(per-probe):

* ``IMMUNIZED`` — guardian caught both the original baseline AND
  the mutated variant. Cage holds against this kind of mutation.
* ``GAP`` — guardian caught the baseline but the mutation
  slipped through. **This is a real immunization gap** the
  operator needs to investigate (and downstream M10 may
  auto-file a hardening op).
* ``BASELINE_MISS`` — guardian didn't catch the baseline either.
  Indicates corpus drift OR a missing detector altogether (not
  a "self-immunization" question; the static cage is already
  failing). Surfaced separately so operators distinguish.
* ``DISABLED`` — master flag off OR substrate unavailable.

Closed 5-value :class:`MutationKind` taxonomy:

* ``WHITESPACE_DRIFT`` — adds/removes leading whitespace on
  each line. Catches detectors that string-match exact
  indentation.
* ``COMMENT_INSERTION`` — inserts a deterministic `# noqa`-
  style comment between statements.
* ``RENAME_VARIABLE`` — renames the first identifier that
  looks like a simple variable assignment.
* ``STRING_SPLIT_CONCAT`` — splits string literals into
  ``+``-joined chunks. Catches naive substring detectors.
* ``PARENS_WRAP`` — wraps any expression in extra parens.
  Catches AST-shape detectors that pin too tight.

§33.1 master flag ``JARVIS_ANTIVENOM_IMMUNIZATION_ENABLED``
default-**FALSE** per the cognitive-substrate convention.
Operator flips when running the audit; substrate is dormant
otherwise to keep the boot path free.

Authority asymmetry (AST-pinned): substrate imports stdlib +
p9_4_adversarial_corpus + semantic_guardian + cross_process_jsonl
ONLY. Does NOT import orchestrator / iron_gate / policy /
providers / candidate_generator / urgency_router / change_engine.

Pure substrate — NEVER raises. A failed mutation, missing
detector, or unparseable input degrades to ``BASELINE_MISS`` or
``GAP``, not exception.
"""
from __future__ import annotations

import ast
import enum
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

logger = logging.getLogger(__name__)


ANTIVENOM_IMMUNIZATION_SCHEMA_VERSION: str = (
    "antivenom_immunization.1"
)


# ===========================================================================
# Env knobs
# ===========================================================================


_ENV_MASTER = "JARVIS_ANTIVENOM_IMMUNIZATION_ENABLED"
_ENV_PERSISTENCE = (
    "JARVIS_ANTIVENOM_IMMUNIZATION_PERSISTENCE_ENABLED"
)
_ENV_LEDGER_PATH = "JARVIS_ANTIVENOM_IMMUNIZATION_LEDGER_PATH"
_ENV_MAX_PROBES = "JARVIS_ANTIVENOM_IMMUNIZATION_MAX_PROBES"

_DEFAULT_LEDGER_RELATIVE = (
    ".jarvis/antivenom_immunization_ledger.jsonl"
)
_DEFAULT_MAX_PROBES = 1000
_MIN_MAX_PROBES = 10
_MAX_MAX_PROBES = 100_000


_TRUTHY: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 cognitive substrate — default-FALSE.

    Operator-paced audit. Flip to true to run the probe loop;
    substrate is dormant otherwise."""
    return _flag(_ENV_MASTER, default=False)


def persistence_enabled() -> bool:
    """Sub-flag — gate the §33.4 JSONL ledger writes. Default-
    TRUE when master on; master-off short-circuits."""
    if not master_enabled():
        return False
    return _flag(_ENV_PERSISTENCE, default=True)


def _read_clamped_int(
    name: str, default: int, lo: int, hi: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def max_probes() -> int:
    """Defensive ceiling on probes per audit run. Clamped to
    [10, 100_000]. Defaults to 1000."""
    return _read_clamped_int(
        _ENV_MAX_PROBES,
        _DEFAULT_MAX_PROBES,
        _MIN_MAX_PROBES,
        _MAX_MAX_PROBES,
    )


def _resolve_repo_root() -> Optional[Path]:
    try:
        here = Path(__file__).resolve()
        for ancestor in (here, *here.parents):
            try:
                if (ancestor / ".git").exists():
                    return ancestor
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return None
    return None


def ledger_path() -> Path:
    """Canonical §33.4 audit ledger. Operator override via
    ``JARVIS_ANTIVENOM_IMMUNIZATION_LEDGER_PATH``. NEVER raises."""
    raw = os.environ.get(_ENV_LEDGER_PATH, "").strip()
    if raw:
        try:
            return Path(raw).expanduser().resolve()
        except Exception:  # noqa: BLE001
            pass
    root = _resolve_repo_root()
    if root is None:
        return Path(_DEFAULT_LEDGER_RELATIVE)
    return root / _DEFAULT_LEDGER_RELATIVE


# ===========================================================================
# Closed taxonomies
# ===========================================================================


class ImmunizationFinding(str, enum.Enum):
    """Closed 4-value per-probe finding. Bytes-pinned via AST."""

    IMMUNIZED = "immunized"
    GAP = "gap"
    BASELINE_MISS = "baseline_miss"
    DISABLED = "disabled"


class MutationKind(str, enum.Enum):
    """Closed 5-value deterministic mutation taxonomy."""

    WHITESPACE_DRIFT = "whitespace_drift"
    COMMENT_INSERTION = "comment_insertion"
    RENAME_VARIABLE = "rename_variable"
    STRING_SPLIT_CONCAT = "string_split_concat"
    PARENS_WRAP = "parens_wrap"


# ===========================================================================
# Deterministic mutation functions (pure)
# ===========================================================================


# Bounded length so a pathological corpus entry doesn't bloat the
# audit log or stress the mutators.
_MAX_PATTERN_BYTES = 8 * 1024


def _mutate_whitespace_drift(text: str) -> str:
    """Add 2 spaces to the start of each non-empty line.

    Detectors that pin exact indentation will miss this; correct
    semantic detectors (AST-based / token-based) will still fire.
    """
    lines = text.split("\n")
    return "\n".join(
        ("  " + ln) if ln.strip() else ln for ln in lines
    )


def _mutate_comment_insertion(text: str) -> str:
    """Insert a deterministic comment between line 1 and line 2.

    Catches detectors that pin exact line offsets."""
    lines = text.split("\n")
    if len(lines) < 2:
        return text + "  # antivenom_probe"
    return "\n".join(
        [lines[0], "    # antivenom_probe_insertion", *lines[1:]]
    )


_IDENT_RE = re.compile(r"^(\s*)([a-zA-Z_][a-zA-Z0-9_]*)(\s*=)")


def _mutate_rename_variable(text: str) -> str:
    """Rename the first variable assignment's LHS to a
    deterministic alias.

    Pattern: ``foo = ...`` → ``av_alias = ...`` on first match.
    No-op when no simple assignment is found.
    """
    out_lines: List[str] = []
    renamed = False
    for line in text.split("\n"):
        if renamed:
            out_lines.append(line)
            continue
        m = _IDENT_RE.match(line)
        if m is not None:
            # Don't rename keywords or magic dunders.
            ident = m.group(2)
            if ident.startswith("__") or ident in (
                "def", "class", "if", "while", "for", "return",
            ):
                out_lines.append(line)
                continue
            new_line = (
                m.group(1) + "av_alias" + m.group(3)
                + line[m.end():]
            )
            out_lines.append(new_line)
            renamed = True
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


_STRING_LITERAL_RE = re.compile(r'"([^"\\]{4,40})"')


def _mutate_string_split_concat(text: str) -> str:
    """Find the FIRST double-quoted string literal of moderate
    length and split it into two ``+``-joined parts.

    Catches detectors that substring-match on full literals.
    No-op when no qualifying literal is found.
    """
    def split(m: re.Match) -> str:
        inner = m.group(1)
        mid = len(inner) // 2
        return f'"{inner[:mid]}" + "{inner[mid:]}"'
    # only do first occurrence
    return _STRING_LITERAL_RE.sub(split, text, count=1)


def _mutate_parens_wrap(text: str) -> str:
    """Wrap the right-hand side of the first simple assignment
    in extra parens.

    ``x = a + b`` → ``x = (a + b)``. Detectors that pin AST shape
    too tight (e.g., ``ast.BinOp`` direct child of ``ast.Assign``)
    will miss this; semantic detectors will still fire.
    """
    out_lines: List[str] = []
    wrapped = False
    for line in text.split("\n"):
        if wrapped:
            out_lines.append(line)
            continue
        if "=" in line and not line.lstrip().startswith("#"):
            # naive split at first `=` (skipping ==, !=, etc.)
            idx = -1
            for i, ch in enumerate(line):
                if ch != "=":
                    continue
                # peek prev/next to avoid relational ops
                prev = line[i - 1] if i > 0 else ""
                nxt = line[i + 1] if i + 1 < len(line) else ""
                if prev in "<>!=" or nxt == "=":
                    continue
                idx = i
                break
            if idx > 0:
                left = line[:idx + 1]
                right = line[idx + 1:].strip()
                if right and not right.startswith("("):
                    out_lines.append(
                        f"{left} ({right})",
                    )
                    wrapped = True
                    continue
        out_lines.append(line)
    return "\n".join(out_lines)


_MUTATORS: Dict[MutationKind, Any] = {
    MutationKind.WHITESPACE_DRIFT: _mutate_whitespace_drift,
    MutationKind.COMMENT_INSERTION: _mutate_comment_insertion,
    MutationKind.RENAME_VARIABLE: _mutate_rename_variable,
    MutationKind.STRING_SPLIT_CONCAT: _mutate_string_split_concat,
    MutationKind.PARENS_WRAP: _mutate_parens_wrap,
}


def mutate_pattern(text: str, kind: MutationKind) -> str:
    """Apply the named mutation to ``text``. NEVER raises.

    Returns the original text on any failure or when the mutation
    has no applicable site. Idempotent in the no-applicable-site
    case (caller can detect "no-op" via identity comparison).
    """
    if not text or not isinstance(text, str):
        return text or ""
    if len(text) > _MAX_PATTERN_BYTES:
        text = text[:_MAX_PATTERN_BYTES]
    mutator = _MUTATORS.get(kind)
    if mutator is None:
        return text
    try:
        return mutator(text)
    except Exception:  # noqa: BLE001
        return text


# ===========================================================================
# §33.5 frozen versioned artifacts
# ===========================================================================


@dataclass(frozen=True)
class ImmunizationProbe:
    """One (corpus_entry × mutation_kind) probe — frozen."""

    entry_id: str            # canonical p9.4.NNN
    category: str            # AdversarialCategory.value
    mutation_kind: str       # MutationKind.value
    finding: ImmunizationFinding
    original_caught: bool
    mutation_caught: bool
    baseline_patterns: Tuple[str, ...]
    """Pattern names that fired on the original baseline. Empty
    means BASELINE_MISS (the cage already missed the baseline)."""
    mutation_patterns: Tuple[str, ...]
    """Pattern names that fired on the mutated variant. Empty
    + non-empty baseline = GAP (immunization failure)."""
    diagnostic: str
    schema_version: str = ANTIVENOM_IMMUNIZATION_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "category": self.category,
            "mutation_kind": self.mutation_kind,
            "finding": self.finding.value,
            "original_caught": bool(self.original_caught),
            "mutation_caught": bool(self.mutation_caught),
            "baseline_patterns": list(self.baseline_patterns),
            "mutation_patterns": list(self.mutation_patterns),
            "diagnostic": self.diagnostic[:256],
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class ImmunizationReport:
    """Aggregate audit report across all probes."""

    audited_at_unix: float
    master_enabled: bool
    finding: ImmunizationFinding
    probes_run: int
    immunized_count: int
    gap_count: int
    baseline_miss_count: int
    per_kind_gap: Mapping[str, int]
    per_entry_gap: Mapping[str, int]
    coverage_ratio: float
    elapsed_s: float
    diagnostic: str
    schema_version: str = ANTIVENOM_IMMUNIZATION_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "audited_at_unix": self.audited_at_unix,
            "master_enabled": self.master_enabled,
            "finding": self.finding.value,
            "probes_run": int(self.probes_run),
            "immunized_count": int(self.immunized_count),
            "gap_count": int(self.gap_count),
            "baseline_miss_count": int(self.baseline_miss_count),
            "per_kind_gap": dict(self.per_kind_gap),
            "per_entry_gap": dict(self.per_entry_gap),
            "coverage_ratio": float(self.coverage_ratio),
            "elapsed_s": float(self.elapsed_s),
            "diagnostic": self.diagnostic[:512],
            "schema_version": self.schema_version,
        }


# ===========================================================================
# Canonical source composition
# ===========================================================================


def _canonical_corpus() -> Tuple[Any, ...]:
    """Compose canonical CORPUS. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.p9_4_adversarial_corpus import (  # noqa: E501
            CORPUS,
        )
        return tuple(CORPUS)
    except Exception:  # noqa: BLE001
        return ()


def _materialize_canonical(entry: Any) -> Optional[str]:
    """Compose canonical materialize_pattern. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.p9_4_adversarial_corpus import (  # noqa: E501
            materialize_pattern,
        )
        result = materialize_pattern(entry)
        if not isinstance(result, str) or not result:
            return None
        if result.startswith("<") and result.endswith(">"):
            return None
        return result
    except Exception:  # noqa: BLE001
        return None


def _run_guardian(text: str) -> Tuple[str, ...]:
    """Compose canonical SemanticGuardian.inspect(). Returns the
    tuple of pattern names that fired. NEVER raises.

    Uses ``old_content=""`` so removed/diff-only patterns can
    operate. Synthetic file_path under a temp prefix so tests/
    detectors that gate on file_path semantics behave normally
    (most detectors are content-only)."""
    try:
        from backend.core.ouroboros.governance.semantic_guardian import (  # noqa: E501
            SemanticGuardian,
        )
        guardian = SemanticGuardian()
        results = guardian.inspect(
            file_path="antivenom_probe.py",
            old_content="",
            new_content=text or "",
        )
    except Exception:  # noqa: BLE001
        return ()
    try:
        return tuple(d.pattern for d in results)
    except Exception:  # noqa: BLE001
        return ()


# ===========================================================================
# Probe loop — pure function
# ===========================================================================


def _classify_probe(
    baseline_patterns: Tuple[str, ...],
    mutation_patterns: Tuple[str, ...],
) -> Tuple[ImmunizationFinding, str]:
    """Pure-function probe classifier. Returns (finding, diagnostic).
    NEVER raises."""
    original_caught = bool(baseline_patterns)
    mutation_caught = bool(mutation_patterns)
    if not original_caught:
        return (
            ImmunizationFinding.BASELINE_MISS,
            (
                "cage didn't catch the baseline pattern — "
                "static detector missing or corpus drifted"
            ),
        )
    if not mutation_caught:
        return (
            ImmunizationFinding.GAP,
            (
                f"cage caught baseline ({','.join(baseline_patterns)})"
                " but missed mutation — immunization gap"
            ),
        )
    return (
        ImmunizationFinding.IMMUNIZED,
        (
            f"cage holds: baseline={','.join(baseline_patterns)}"
            f" mutation={','.join(mutation_patterns)}"
        ),
    )


def probe_entry(
    entry: Any,
    mutation_kind: MutationKind,
) -> Optional[ImmunizationProbe]:
    """Run one (entry × mutation_kind) probe. NEVER raises.

    Returns None when the entry's pattern can't be materialized.
    """
    materialized = _materialize_canonical(entry)
    if materialized is None:
        return None
    try:
        entry_id = str(getattr(entry, "entry_id", ""))
        category = ""
        cat_obj = getattr(entry, "category", None)
        if hasattr(cat_obj, "value"):
            category = str(cat_obj.value)
        else:
            category = str(cat_obj or "")
    except Exception:  # noqa: BLE001
        entry_id = ""
        category = ""

    baseline_patterns = _run_guardian(materialized)
    mutated = mutate_pattern(materialized, mutation_kind)
    if mutated == materialized:
        # No-op mutation — record as IMMUNIZED if baseline caught
        # (the cage state is identical) OR BASELINE_MISS if not.
        finding, diagnostic = _classify_probe(
            baseline_patterns, baseline_patterns,
        )
        return ImmunizationProbe(
            entry_id=entry_id,
            category=category,
            mutation_kind=mutation_kind.value,
            finding=finding,
            original_caught=bool(baseline_patterns),
            mutation_caught=bool(baseline_patterns),
            baseline_patterns=baseline_patterns,
            mutation_patterns=baseline_patterns,
            diagnostic=(
                "mutation_no_op_no_applicable_site: "
                + diagnostic
            ),
        )
    mutation_patterns = _run_guardian(mutated)
    finding, diagnostic = _classify_probe(
        baseline_patterns, mutation_patterns,
    )
    return ImmunizationProbe(
        entry_id=entry_id,
        category=category,
        mutation_kind=mutation_kind.value,
        finding=finding,
        original_caught=bool(baseline_patterns),
        mutation_caught=bool(mutation_patterns),
        baseline_patterns=baseline_patterns,
        mutation_patterns=mutation_patterns,
        diagnostic=diagnostic,
    )


# ===========================================================================
# Aggregator
# ===========================================================================


def audit_self_immunization(
    *,
    mutation_kinds: Optional[Sequence[MutationKind]] = None,
    corpus_override: Optional[Sequence[Any]] = None,
) -> ImmunizationReport:
    """Run the full probe loop and return an aggregate report.

    NEVER raises. Master-flag-gated: returns ``DISABLED`` report
    when off. ``mutation_kinds`` defaults to the full enum;
    ``corpus_override`` allows tests to inject a synthetic
    corpus subset.
    """
    started = time.time()
    if not master_enabled():
        return ImmunizationReport(
            audited_at_unix=started,
            master_enabled=False,
            finding=ImmunizationFinding.DISABLED,
            probes_run=0,
            immunized_count=0,
            gap_count=0,
            baseline_miss_count=0,
            per_kind_gap={},
            per_entry_gap={},
            coverage_ratio=0.0,
            elapsed_s=0.0,
            diagnostic=(
                f"master flag {_ENV_MASTER}=false — flip "
                "to true to run self-immunization audit"
            ),
        )

    corpus = corpus_override if corpus_override is not None else (
        _canonical_corpus()
    )
    if not corpus:
        return ImmunizationReport(
            audited_at_unix=started,
            master_enabled=True,
            finding=ImmunizationFinding.DISABLED,
            probes_run=0,
            immunized_count=0,
            gap_count=0,
            baseline_miss_count=0,
            per_kind_gap={},
            per_entry_gap={},
            coverage_ratio=0.0,
            elapsed_s=time.time() - started,
            diagnostic=(
                "canonical p9_4_adversarial_corpus.CORPUS "
                "unavailable or empty"
            ),
        )

    kinds = (
        tuple(mutation_kinds)
        if mutation_kinds is not None
        else tuple(MutationKind)
    )
    if not kinds:
        kinds = tuple(MutationKind)

    cap = max_probes()
    probes: List[ImmunizationProbe] = []
    for entry in corpus:
        if len(probes) >= cap:
            break
        for kind in kinds:
            if len(probes) >= cap:
                break
            probe = probe_entry(entry, kind)
            if probe is None:
                continue
            probes.append(probe)

    immunized = sum(
        1 for p in probes
        if p.finding is ImmunizationFinding.IMMUNIZED
    )
    gaps = sum(
        1 for p in probes
        if p.finding is ImmunizationFinding.GAP
    )
    baseline_misses = sum(
        1 for p in probes
        if p.finding is ImmunizationFinding.BASELINE_MISS
    )

    per_kind_gap: Dict[str, int] = {}
    per_entry_gap: Dict[str, int] = {}
    for p in probes:
        if p.finding is ImmunizationFinding.GAP:
            per_kind_gap[p.mutation_kind] = (
                per_kind_gap.get(p.mutation_kind, 0) + 1
            )
            per_entry_gap[p.entry_id] = (
                per_entry_gap.get(p.entry_id, 0) + 1
            )

    total_meaningful = immunized + gaps
    coverage = (
        immunized / total_meaningful
        if total_meaningful > 0 else 0.0
    )

    aggregate_finding = (
        ImmunizationFinding.GAP if gaps > 0
        else ImmunizationFinding.IMMUNIZED
    )

    report = ImmunizationReport(
        audited_at_unix=started,
        master_enabled=True,
        finding=aggregate_finding,
        probes_run=len(probes),
        immunized_count=immunized,
        gap_count=gaps,
        baseline_miss_count=baseline_misses,
        per_kind_gap=per_kind_gap,
        per_entry_gap=per_entry_gap,
        coverage_ratio=coverage,
        elapsed_s=time.time() - started,
        diagnostic=(
            f"ran {len(probes)} probes ({len(corpus)} entries "
            f"× {len(kinds)} mutations); immunized={immunized} "
            f"gaps={gaps} baseline_miss={baseline_misses} "
            f"coverage={coverage:.3f}"
        ),
    )
    _maybe_persist_report(report, tuple(probes))
    _publish_audit_event(report)
    return report


# ===========================================================================
# §33.4 flock'd JSONL persistence (sub-flag opt-in)
# ===========================================================================


def _maybe_persist_report(
    report: ImmunizationReport,
    probes: Tuple[ImmunizationProbe, ...],
) -> None:
    """Compose canonical flock_append_line. Best-effort — NEVER
    raises into the audit path. Persists one summary row +
    one per-GAP probe row (immunized probes not persisted to
    avoid ledger bloat — operators reproduce via re-audit).
    """
    if not persistence_enabled():
        return
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
            flock_append_line,
        )
    except ImportError:
        return
    try:
        target = ledger_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        flock_append_line(
            target,
            json.dumps({
                "kind": "summary",
                "payload": report.to_dict(),
            }),
        )
        for p in probes:
            if p.finding is ImmunizationFinding.GAP:
                flock_append_line(
                    target,
                    json.dumps({
                        "kind": "gap",
                        "payload": p.to_dict(),
                    }),
                )
    except Exception:  # noqa: BLE001
        return


# ===========================================================================
# SSE publisher
# ===========================================================================


def _publish_audit_event(report: ImmunizationReport) -> None:
    """Best-effort SSE publish. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_ANTIVENOM_IMMUNIZATION_AUDITED,
            publish_task_event,
        )
        payload = {
            "audited_at_unix": report.audited_at_unix,
            "finding": report.finding.value,
            "probes_run": report.probes_run,
            "immunized_count": report.immunized_count,
            "gap_count": report.gap_count,
            "baseline_miss_count": report.baseline_miss_count,
            "coverage_ratio": report.coverage_ratio,
            "per_kind_gap": dict(report.per_kind_gap),
            "schema_version": report.schema_version,
        }
        publish_task_event(
            EVENT_TYPE_ANTIVENOM_IMMUNIZATION_AUDITED,
            (
                f"system::antivenom_immunization::"
                f"{report.schema_version}"
            ),
            payload,
        )
    except Exception:  # noqa: BLE001
        return


# ===========================================================================
# Renderer
# ===========================================================================


def format_immunization_panel(
    report: Optional[ImmunizationReport] = None,
) -> str:
    """Operator-facing panel. NEVER raises."""
    if report is None:
        if not master_enabled():
            return (
                f"antivenom self-immunization: disabled "
                f"({_ENV_MASTER}=false)"
            )
        report = audit_self_immunization()
    if not report.master_enabled:
        return (
            f"antivenom self-immunization: disabled "
            f"({_ENV_MASTER}=false)"
        )
    lines: List[str] = [
        f"💉 Antivenom Self-Immunization  "
        f"({report.finding.value})",
        f"  probes_run            : {report.probes_run}",
        f"  immunized             : {report.immunized_count}",
        f"  gaps                  : {report.gap_count}",
        f"  baseline_miss         : {report.baseline_miss_count}",
        f"  coverage_ratio        : {report.coverage_ratio:.3f}",
    ]
    if report.per_kind_gap:
        lines.append("  gaps by mutation:")
        for k, c in sorted(report.per_kind_gap.items()):
            lines.append(f"    {k:<24} : {c}")
    if report.per_entry_gap:
        lines.append("  gaps by entry:")
        for e, c in sorted(report.per_entry_gap.items()):
            lines.append(f"    {e:<12} : {c}")
    lines.append(f"  diagnostic            : {report.diagnostic}")
    return "\n".join(lines)


# ===========================================================================
# AST pins
# ===========================================================================


def register_shipped_invariants() -> list:
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "antivenom_self_immunization.py"
    )

    _EXPECTED_FINDINGS = {
        "immunized", "gap", "baseline_miss", "disabled",
    }
    _EXPECTED_MUTATIONS = {
        "whitespace_drift",
        "comment_insertion",
        "rename_variable",
        "string_split_concat",
        "parens_wrap",
    }

    def _validate_finding_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "ImmunizationFinding"
            ):
                found = set()
                for sub in node.body:
                    if (
                        isinstance(sub, ast.Assign)
                        and len(sub.targets) == 1
                        and isinstance(sub.targets[0], ast.Name)
                        and isinstance(sub.value, ast.Constant)
                        and isinstance(sub.value.value, str)
                    ):
                        found.add(sub.value.value)
                missing = _EXPECTED_FINDINGS - found
                extra = found - _EXPECTED_FINDINGS
                if missing:
                    return (
                        f"ImmunizationFinding missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"ImmunizationFinding drift: "
                        f"{sorted(extra)}",
                    )
                return ()
        return ("ImmunizationFinding class not found",)

    def _validate_mutation_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "MutationKind"
            ):
                found = set()
                for sub in node.body:
                    if (
                        isinstance(sub, ast.Assign)
                        and len(sub.targets) == 1
                        and isinstance(sub.targets[0], ast.Name)
                        and isinstance(sub.value, ast.Constant)
                        and isinstance(sub.value.value, str)
                    ):
                        found.add(sub.value.value)
                missing = _EXPECTED_MUTATIONS - found
                extra = found - _EXPECTED_MUTATIONS
                if missing:
                    return (
                        f"MutationKind missing: {sorted(missing)}",
                    )
                if extra:
                    return (
                        f"MutationKind drift: {sorted(extra)}",
                    )
                return ()
        return ("MutationKind class not found",)

    def _validate_authority_asymmetry(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        forbidden = (
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.iron_gate",
            "backend.core.ouroboros.governance.policy",
            "backend.core.ouroboros.governance.providers",
            "backend.core.ouroboros.governance.candidate_generator",
            "backend.core.ouroboros.governance.urgency_router",
            "backend.core.ouroboros.governance.change_engine",
            "backend.core.ouroboros.governance.auto_committer",
            "backend.core.ouroboros.governance.risk_tier_floor",
        )
        violations: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(mod == f for f in forbidden):
                    violations.append(
                        f"forbidden authority import: {mod}",
                    )
        return tuple(violations)

    def _validate_master_default_false(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "_flag"
                    ):
                        for kw in sub.keywords:
                            if (
                                kw.arg == "default"
                                and isinstance(kw.value, ast.Constant)
                                and kw.value.value is False
                            ):
                                return ()
                return (
                    "master_enabled() must call _flag(...) "
                    "with default=False per §33.1",
                )
        return ("master_enabled() not found",)

    def _validate_composes_canonical(
        tree: ast.AST, source: str,
    ) -> tuple:
        violations: List[str] = []
        if "p9_4_adversarial_corpus" not in source:
            violations.append(
                "must compose canonical "
                "p9_4_adversarial_corpus (no parallel "
                "adversarial-input catalog)",
            )
        if "materialize_pattern" not in source:
            violations.append(
                "must compose materialize_pattern (no "
                "parallel pattern builder)",
            )
        if "SemanticGuardian" not in source:
            violations.append(
                "must compose SemanticGuardian.inspect() "
                "(no parallel detector)",
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "antivenom_immunization_finding_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "ImmunizationFinding 4-value taxonomy "
                "bytes-pinned."
            ),
            validate=_validate_finding_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "antivenom_immunization_mutation_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "MutationKind 5-value taxonomy bytes-pinned. "
                "Adding/removing requires updating "
                "_MUTATORS dispatch + tests."
            ),
            validate=_validate_mutation_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "antivenom_immunization_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Substrate purity — antivenom self-immunization "
                "MUST NOT import orchestrator / iron_gate / "
                "policy / providers / candidate_generator / "
                "urgency_router / change_engine / "
                "auto_committer / risk_tier_floor. "
                "p9_4_adversarial_corpus + semantic_guardian "
                "are allowed (canonical composition)."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "antivenom_immunization_master_default_false"
            ),
            target_file=target,
            description=(
                "§33.1 cognitive-substrate default-FALSE."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "antivenom_immunization_composes_canonical"
            ),
            target_file=target,
            description=(
                "Substrate composes canonical "
                "p9_4_adversarial_corpus + "
                "materialize_pattern + SemanticGuardian — "
                "no parallel detector, no parallel pattern "
                "catalog."
            ),
            validate=_validate_composes_canonical,
        ),
    ]


# ===========================================================================
# FlagRegistry seeds
# ===========================================================================


def register_flags(registry: Any) -> int:
    from backend.core.ouroboros.governance.flag_registry import (
        Category,
        FlagSpec,
        FlagType,
    )

    src = (
        "backend/core/ouroboros/governance/"
        "antivenom_self_immunization.py"
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Antivenom self-immunization audit master "
                "switch. §33.1 cognitive substrate default-"
                "FALSE. Flip to true to run the probe loop "
                "that exercises SemanticGuardian against "
                "5 deterministic mutations of every P9.4 "
                "corpus entry; surfaces immunization gaps."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_MASTER}=true",
        ),
        FlagSpec(
            name=_ENV_PERSISTENCE,
            type=FlagType.BOOL,
            default=True,
            description=(
                "Sub-flag — gate the §33.4 JSONL audit "
                "ledger. Default-TRUE when master on; "
                "master-off short-circuits."
            ),
            category=Category.OBSERVABILITY,
            source_file=src,
            example=f"{_ENV_PERSISTENCE}=false",
        ),
        FlagSpec(
            name=_ENV_MAX_PROBES,
            type=FlagType.INT,
            default=_DEFAULT_MAX_PROBES,
            description=(
                "Defensive ceiling on probes per audit. "
                "Clamped [10, 100_000]. Defaults to 1000 "
                "(25 entries × 5 mutations = 125 typical)."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_MAX_PROBES}=500",
        ),
    ]

    count = 0
    for spec in seeds:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001 — fail-open per §33.1
            continue
    return count


__all__ = [
    "ANTIVENOM_IMMUNIZATION_SCHEMA_VERSION",
    "ImmunizationFinding",
    "MutationKind",
    "ImmunizationProbe",
    "ImmunizationReport",
    "master_enabled",
    "persistence_enabled",
    "max_probes",
    "ledger_path",
    "mutate_pattern",
    "probe_entry",
    "audit_self_immunization",
    "format_immunization_panel",
    "register_shipped_invariants",
    "register_flags",
]
