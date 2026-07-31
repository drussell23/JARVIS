"""Rules the operator wrote, delivered only where they apply.

Claude Code scopes human-written rules with ``paths:`` frontmatter so a rule
about ``src/api/**`` reaches Claude when it touches the API and stays out of
the way otherwise. O+V has the stronger substrate for this and never shipped
the delivery.

What was already true
---------------------
``UserPreferenceStore`` persists typed memories with ``paths``, ``tags``,
``why``, and ``how_to_apply``. Two of its three documented integrations are
live: ``FORBIDDEN_PATH`` reaches ``tool_executor`` through
``register_protected_path_provider``, and the orchestrator writes FEEDBACK
memories on approval rejection.

The third was not
-----------------
The module docstring describes ``StrategicDirectionService`` accepting a
``user_prefs`` param and appending a "User Preferences" section "filtered by
relevance to the op", scored by "path overlap + tag match + type weight".
None of it exists. ``StrategicDirectionService.__init__`` takes
``project_root`` alone, ``user_prefs`` appears nowhere in that module, and no
relevance function was ever written.

So operator rules could BLOCK a write and could be LEARNED from a rejection,
but could never GUIDE a generation. The organism was told things it had no
path to act on — the same shape as every other finding in this arc, one layer
further out: not a value dropped before the eye, but a value the operator
supplied that never reached the model at all.

Scoping is the point, not a refinement
---------------------------------------
Injecting every rule into every prompt would be the easy version and the
wrong one. A rule about the voice bridge, in the prompt for an orchestrator
edit, is noise indistinguishable from the ghost topics this arc removed —
and it costs the same budget the relevant rules need. A rule that fires
everywhere trains the model to skim rules.

Specificity, not just matching
-------------------------------
A rule with no ``paths:`` is GLOBAL and always eligible — that is Claude
Code's semantics and the right default for "never use force-push". But a
rule pinned to the directory under edit outranks it, because the budget
should go to the rule that knows something about this particular work.
Specificity is measured from the pattern's own shape (how many literal path
segments it commits to), so it needs no table of importance.

Widening, never narrowing
--------------------------
``UserMemory.matches_path`` is a SECURITY path — ``FORBIDDEN_PATH`` consults
it before every mutating tool call. Its legacy semantics are substring. This
module adds glob matching as a UNION with that, never a replacement: a path
matches if the substring test OR the glob test says so. That direction can
only ever protect more paths than before, never fewer, which is the only
acceptable direction for a change to a guard.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.OperatorRules")

OPERATOR_RULES_SCHEMA_VERSION: str = "operator_rules.1"

__all__ = [
    "OPERATOR_RULES_SCHEMA_VERSION",
    "RuleMatch",
    "RuleSelection",
    "match_pattern",
    "rules_enabled",
    "score_rule",
    "select_rules",
]


def _flag(name: str, default: str = "1") -> bool:
    try:
        return os.environ.get(name, default).strip().lower() not in (
            "0", "false", "no", "off", "")
    except Exception:  # noqa: BLE001
        return True


def _num(name: str, default: float, lo: float, hi: float) -> float:
    try:
        return min(hi, max(lo, float(os.environ.get(name, "").strip() or default)))
    except Exception:  # noqa: BLE001
        return default


def rules_enabled() -> bool:
    """``JARVIS_OPERATOR_RULES_ENABLED`` (default true)."""
    return _flag("JARVIS_OPERATOR_RULES_ENABLED", "1")


def _max_rules() -> int:
    return int(_num("JARVIS_OPERATOR_RULES_MAX", 6, 1, 64))


def _char_budget() -> int:
    return int(_num("JARVIS_OPERATOR_RULES_CHAR_BUDGET", 1500, 200, 40000))


def _global_rules_enabled() -> bool:
    """Whether unscoped rules are eligible. ``JARVIS_OPERATOR_RULES_GLOBAL``.

    On by default: a rule the operator wrote without a path scope is a rule
    they meant everywhere, and silently dropping it would be the surface
    deciding it knew better than the author.
    """
    return _flag("JARVIS_OPERATOR_RULES_GLOBAL", "1")


#: Per-type weights. FEEDBACK and STYLE lead because they are the two kinds
#: that describe HOW to do the work — the actionable half. REFERENCE is a
#: pointer to something external and rarely changes a patch.
def _type_weight(memory_type: Any) -> float:
    name = str(getattr(memory_type, "value", memory_type) or "").lower()
    return _num(f"JARVIS_OPERATOR_RULES_W_{name.upper()}", {
        "feedback": 1.0,
        "style": 0.95,
        "forbidden_path": 0.9,
        "project": 0.7,
        "user": 0.6,
        "reference": 0.35,
        "forbidden_app": 0.1,
    }.get(name, 0.5), 0.0, 2.0)


# ---------------------------------------------------------------------------
# Pattern matching
# ---------------------------------------------------------------------------


def _normalise(path: str, project_root: Optional[Path] = None) -> str:
    """A repo-relative POSIX path. NEVER raises.

    Absolute target files are relativised against *project_root* so a rule
    written as ``backend/voice/**`` matches whether the op reported
    ``/Users/.../backend/voice/x.py`` or ``backend/voice/x.py``. Without
    this the same rule fires or does not depending on how a caller happened
    to spell a path.
    """
    try:
        raw = str(path or "").replace("\\", "/").strip()
        if not raw:
            return ""
        if project_root is not None:
            try:
                candidate = Path(raw)
                if candidate.is_absolute():
                    return PurePosixPath(
                        candidate.resolve().relative_to(
                            Path(project_root).resolve())).as_posix()
            except Exception:  # noqa: BLE001
                pass
        return raw.lstrip("./")
    except Exception:  # noqa: BLE001
        return ""


def match_pattern(pattern: str, rel_path: str) -> bool:
    """Whether *rel_path* matches *pattern*. Pure. NEVER raises.

    Supports three shapes, chosen because they are what operators actually
    write:

    * ``**`` recursive globs — ``backend/**/*.py``
    * a bare DIRECTORY prefix — ``backend/voice`` matches everything beneath
      it. Operators write directories far more often than they write
      ``backend/voice/**``, and treating the shorthand as a literal filename
      would silently match nothing.
    * everything else through :func:`_glob_re`, whose wildcards are
      PATH-AWARE — ``*`` stops at a separator, unlike ``fnmatch``'s.

    Case-SENSITIVE, because git is: on a case-insensitive macOS filesystem a
    tolerant match would fire on paths the repository considers different,
    and the repository is the authority everywhere else in this arc.
    """
    try:
        pat = str(pattern or "").replace("\\", "/").strip().lstrip("./")
        target = str(rel_path or "").replace("\\", "/").strip().lstrip("./")
        if not pat or not target:
            return False

        # Directory-prefix shorthand. Checked before fnmatch so a pattern with
        # no metacharacters behaves the way an operator expects.
        if not any(ch in pat for ch in "*?["):
            if target == pat:
                return True
            return target.startswith(pat.rstrip("/") + "/")

        return _glob_re(pat).match(target) is not None
    except Exception:  # noqa: BLE001
        return False


@lru_cache(maxsize=512)
def _glob_re(pattern: str) -> "re.Pattern[str]":
    """Compile a glob to a regex with PATH-AWARE wildcards. Cached.

    ``fnmatch`` cannot express this and quietly gets it wrong: its ``*``
    matches ``/`` as happily as any other character, so ``*.md`` matches
    ``docs/README.md``. An operator scoping a rule to top-level markdown
    would silently have it fire on every nested file in the repo — the
    over-matching twin of the under-matching substring problem, and just as
    invisible.

    So the translation is explicit:

    * ``**`` spans directories (``.*``)
    * ``*`` stops at a separator (``[^/]*``)
    * ``?`` matches one non-separator character
    * ``[...]`` character classes pass through, with the negation form
      ``[!...]`` rewritten to the regex ``[^...]`` spelling
    * every other character is escaped, so a rule containing ``.`` or ``+``
      cannot become an accidental wildcard
    """
    out: List[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            if pattern.startswith("**", i):
                # `a/**/b` must also match `a/b` — a recursive wildcard that
                # refuses to match zero directories surprises everyone.
                if pattern.startswith("**/", i):
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
            continue
        if ch == "?":
            out.append("[^/]")
            i += 1
            continue
        if ch == "[":
            close = pattern.find("]", i + 1)
            if close == -1:
                out.append(re.escape(ch))
                i += 1
                continue
            body = pattern[i + 1:close]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[" + body + "]")
            i = close + 1
            continue
        out.append(re.escape(ch))
        i += 1
    return re.compile("".join(out) + r"\Z")


def _split_patterns(paths: Sequence[str]) -> Tuple[List[str], List[str]]:
    """``(include, exclude)`` — leading ``!`` marks an exclusion.

    Exclusions exist because the useful real rule is "everything under src,
    except the vendored tree", and without negation an operator has to
    enumerate the complement by hand and keep it current.
    """
    include: List[str] = []
    exclude: List[str] = []
    for raw in paths or ():
        pat = str(raw or "").strip()
        if not pat:
            continue
        (exclude if pat.startswith("!") else include).append(pat.lstrip("!"))
    return include, exclude


def _pattern_specificity(pattern: str) -> float:
    """How much this pattern COMMITS to, in [0, 1]. Pure.

    Counted from the pattern's own shape — literal path segments before any
    wildcard — so ``backend/core/ouroboros/governance`` outranks ``backend``
    outranks ``**``. Derived rather than declared: an importance table would
    have to be maintained against a directory tree that moves weekly.
    """
    try:
        pat = str(pattern or "").replace("\\", "/").strip().lstrip("!./")
        if not pat:
            return 0.0
        literal = 0
        for seg in pat.split("/"):
            if not seg or any(ch in seg for ch in "*?["):
                break
            literal += 1
        # Saturating rather than linear: the difference between one and three
        # committed segments matters far more than between eight and ten.
        return 1.0 - (0.5 ** literal)
    except Exception:  # noqa: BLE001
        return 0.0


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleMatch:
    """One rule's standing against one operation."""

    memory: Any
    score: float
    matched_files: Tuple[str, ...]
    specificity: float
    is_global: bool
    #: True when the rule declares paths and NONE of them exist in the repo —
    #: a rule about a directory that has since been deleted or renamed. The
    #: same fact ``Drift.ORPHANED`` reports for a topic, and it earns the same
    #: treatment: surfaced, not silently obeyed forever.
    orphaned: bool = False

    @property
    def chars(self) -> int:
        return len(render_rule(self.memory))


def score_rule(
    memory: Any,
    target_files: Sequence[str],
    query: str = "",
    *,
    project_root: Optional[Path] = None,
    repo_has: Optional[Any] = None,
) -> Optional[RuleMatch]:
    """Score one rule against an op, or None when it does not apply.

    NEVER raises. Returning None rather than a zero score keeps "this rule is
    irrelevant here" distinct from "this rule applies but ranked last", which
    are different rows in the admission ledger.
    """
    try:
        paths = tuple(getattr(memory, "paths", ()) or ())
        include, exclude = _split_patterns(paths)
        normalised = [_normalise(f, project_root) for f in target_files or ()]
        normalised = [f for f in normalised if f]

        matched: List[str] = []
        best_specificity = 0.0
        is_global = not include

        if include:
            for rel in normalised:
                if any(match_pattern(p, rel) for p in exclude):
                    continue
                hits = [p for p in include if match_pattern(p, rel)]
                if hits:
                    matched.append(rel)
                    best_specificity = max(
                        best_specificity,
                        max(_pattern_specificity(p) for p in hits))
            if not matched:
                return None
        elif not _global_rules_enabled():
            return None

        # Tag overlap against the op description — a weak, additive signal.
        tags = {str(t).lower() for t in (getattr(memory, "tags", ()) or ())}
        words = {w for w in str(query or "").lower().replace(
            "/", " ").replace("_", " ").split() if len(w) > 2}
        tag_score = (len(tags & words) / len(tags)) if tags else 0.0

        coverage = (len(matched) / len(normalised)) if normalised else 0.0
        weight = _type_weight(getattr(memory, "type", None))

        # A scoped rule that matched beats a global rule of the same type,
        # because it demonstrably knows something about THIS op. A global
        # rule still scores — it is eligible, just outranked.
        base = (0.6 * best_specificity + 0.25 * coverage + 0.15 * tag_score
                if not is_global else 0.15 * tag_score + 0.10)

        orphaned = False
        if include and repo_has is not None:
            try:
                orphaned = not any(repo_has(p) for p in include)
            except Exception:  # noqa: BLE001
                orphaned = False

        return RuleMatch(
            memory=memory, score=float(base * weight),
            matched_files=tuple(matched), specificity=best_specificity,
            is_global=is_global, orphaned=orphaned,
        )
    except Exception:  # noqa: BLE001
        logger.debug("[OperatorRules] scoring degraded", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Rendering + selection
# ---------------------------------------------------------------------------


def render_rule(memory: Any) -> str:
    """One rule as prompt text. Pure. NEVER raises.

    Renders ``why`` and ``how_to_apply`` when present because a rule without
    its reason is an order, and an order the model cannot evaluate is one it
    will misapply at the first edge case the operator did not foresee.
    """
    try:
        name = str(getattr(memory, "name", "") or "rule")
        desc = str(getattr(memory, "description", "") or "").strip()
        why = str(getattr(memory, "why", "") or "").strip()
        how = str(getattr(memory, "how_to_apply", "") or "").strip()
        paths = tuple(getattr(memory, "paths", ()) or ())

        lines = [f"- **{name}**" + (f" — {desc}" if desc else "")]
        if paths:
            lines.append(f"  - scope: {', '.join(str(p) for p in paths)}")
        if why:
            lines.append(f"  - why: {why}")
        if how:
            lines.append(f"  - apply: {how}")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        return ""


@dataclass(frozen=True)
class RuleSelection:
    """What the operator's rules contribute to one prompt."""

    section: str
    selected: Tuple[RuleMatch, ...]
    considered: int
    withheld: Tuple[Tuple[RuleMatch, str], ...]
    char_budget: int

    @property
    def chars(self) -> int:
        return len(self.section)

    @classmethod
    def empty(cls) -> "RuleSelection":
        return cls(section="", selected=(), considered=0, withheld=(),
                   char_budget=0)


def select_rules(
    memories: Iterable[Any],
    target_files: Sequence[str],
    query: str = "",
    *,
    project_root: Optional[Path] = None,
    max_rules: Optional[int] = None,
    char_budget: Optional[int] = None,
    repo_has: Optional[Any] = None,
) -> RuleSelection:
    """Select and render the rules that apply to this op. NEVER raises."""
    if not rules_enabled():
        return RuleSelection.empty()
    try:
        cap = int(max_rules if max_rules is not None else _max_rules())
        budget = int(char_budget if char_budget is not None else _char_budget())

        scored: List[RuleMatch] = []
        considered = 0
        for memory in memories or ():
            considered += 1
            match = score_rule(memory, target_files, query,
                               project_root=project_root, repo_has=repo_has)
            if match is not None:
                scored.append(match)

        # Deterministic order: score, then specificity, then name — so the
        # same op with the same rules produces the same prompt on every run.
        scored.sort(key=lambda m: (
            -m.score, -m.specificity,
            str(getattr(m.memory, "name", ""))))

        selected: List[RuleMatch] = []
        withheld: List[Tuple[RuleMatch, str]] = []
        used = 0
        for match in scored:
            if match.orphaned:
                # A rule scoped to paths that no longer exist describes a
                # shape of the repo that is gone. Obeying it silently is how
                # a codebase inherits constraints nobody can locate.
                withheld.append((match, "orphaned"))
                continue
            if len(selected) >= cap:
                withheld.append((match, "max_rules"))
                continue
            size = match.chars + 1
            if used + size > budget and selected:
                withheld.append((match, "budget"))
                continue
            selected.append(match)
            used += size

        if not selected:
            return RuleSelection(section="", selected=(), considered=considered,
                                 withheld=tuple(withheld), char_budget=budget)

        body = "\n".join(render_rule(m.memory) for m in selected)
        section = ("## Operator Rules\n\n"
                   "Rules the operator wrote for this codebase. They are "
                   "scoped to the files under edit; follow them unless they "
                   "contradict a correctness requirement, and say so if they "
                   "do.\n\n" + body)
        return RuleSelection(section=section, selected=tuple(selected),
                             considered=considered, withheld=tuple(withheld),
                             char_budget=budget)
    except Exception:  # noqa: BLE001
        logger.debug("[OperatorRules] selection degraded", exc_info=True)
        return RuleSelection.empty()


# ---------------------------------------------------------------------------
# Admission recording — rules ride the ledger topics already use
# ---------------------------------------------------------------------------


def record_selection(
    selection: RuleSelection,
    *,
    op_id: str,
    consumer: str = "main",
    query: str = "",
    target_files: Sequence[str] = (),
) -> Any:
    """File *selection* on the admission ledger. NEVER raises.

    The SAME ledger topics use, deliberately. An operator asking "what was in
    that prompt" wants one answer, and a second parallel ledger for rules
    would make the honest answer require knowing which of two surfaces to
    consult.
    """
    try:
        from backend.core.ouroboros.governance.memory_admission import (
            AdmissionDecision, AdmissionReason, AdmissionRecord, AdmissionRow,
            MemoryConsumer, record_admission,
        )
        import hashlib

        def _row(match: RuleMatch, admitted: bool, why: str) -> AdmissionRow:
            name = str(getattr(match.memory, "name", "") or "rule")
            mem_id = str(getattr(match.memory, "id", "") or name)
            if admitted:
                reason = (AdmissionReason.STRUCTURAL_TARGET
                          if match.matched_files else AdmissionReason.SEMANTIC)
            elif why == "orphaned":
                reason = AdmissionReason.ORPHANED_SUBJECT
            elif why == "budget":
                reason = AdmissionReason.BUDGET_EXHAUSTED
            elif why == "max_rules":
                reason = AdmissionReason.MAX_TOPICS_REACHED
            else:
                reason = AdmissionReason.RANK_BELOW_CUTOFF
            return AdmissionRow(
                source_id=f"operator_rule:{mem_id}",
                uri=f"rule:{name}",
                content_hash=hashlib.sha256(
                    mem_id.encode("utf-8", "replace")).hexdigest()[:16],
                decision=(AdmissionDecision.ADMITTED if admitted
                          else AdmissionDecision.WITHHELD),
                reason=reason, score=match.score, chars=match.chars,
                drift="orphaned" if match.orphaned else "unbound",
                breakdown=(("specificity", round(match.specificity, 4)),
                           ("global", 1.0 if match.is_global else 0.0)),
                note=(", ".join(match.matched_files[:3])
                      if match.matched_files else "global rule"),
            )

        rows = [_row(m, True, "") for m in selection.selected]
        rows += [_row(m, False, why) for m, why in selection.withheld]
        return record_admission(AdmissionRecord.of(
            op_id=op_id or "unattributed",
            consumer=MemoryConsumer.coerce(consumer),
            rows=rows, corpus_size=selection.considered,
            corpus_provenance="operator_rules",
            corpus_excluded=0, char_budget=selection.char_budget,
            query=query, target_files=target_files,
            extra={"schema": OPERATOR_RULES_SCHEMA_VERSION,
                   "source": "operator_rules"},
        ))
    except Exception:  # noqa: BLE001
        logger.debug("[OperatorRules] record skipped", exc_info=True)
        return None


def compose_for_op(
    project_root: Path,
    target_files: Sequence[str],
    query: str = "",
    *,
    op_id: str = "",
    consumer: str = "main",
) -> str:
    """The ``## Operator Rules`` block for this op, recorded. NEVER raises.

    The one function the pipeline calls. Resolves the store, scores, renders,
    files the admission record, and returns prompt text — so a caller cannot
    accidentally inject rules without recording that it did.
    """
    if not rules_enabled():
        return ""
    try:
        from backend.core.ouroboros.governance.user_preference_memory import (
            get_default_store,
        )
        memories = list(get_default_store(project_root).list_all() or ())
        if not memories:
            return ""

        root = Path(project_root)

        def _repo_has(pattern: str) -> bool:
            """Does anything in the repo match *pattern*? NEVER raises.

            Used only to mark a rule ORPHANED. Failing OPEN (returning True)
            on any error is deliberate: an unreadable tree must not cause a
            live rule to be discarded as stale.
            """
            try:
                pat = str(pattern).replace("\\", "/").lstrip("!./")
                if not any(ch in pat for ch in "*?["):
                    return (root / pat).exists()
                return any(root.glob(pat))
            except Exception:  # noqa: BLE001
                return True

        selection = select_rules(
            memories, target_files, query,
            project_root=root, repo_has=_repo_has)
        record_selection(selection, op_id=op_id, consumer=consumer,
                         query=query, target_files=target_files)
        if selection.section:
            logger.info(
                "[OperatorRules] op=%s selected=%d/%d chars=%d withheld=%d",
                op_id, len(selection.selected), selection.considered,
                selection.chars, len(selection.withheld),
            )
        return selection.section
    except Exception:  # noqa: BLE001
        logger.debug("[OperatorRules] composition degraded", exc_info=True)
        return ""


def render_rules_lines(limit: int = 12) -> List[str]:
    """Markup lines for ``/memory rules``. NEVER raises."""
    if not rules_enabled():
        return ["  [dim]operator rules disabled "
                "(JARVIS_OPERATOR_RULES_ENABLED=0)[/dim]"]
    try:
        from backend.core.ouroboros.governance.user_preference_memory import (
            get_default_store,
        )
        memories = list(get_default_store().list_all() or ())
        if not memories:
            return ["  [bold]memory · operator rules[/bold]",
                    "    [dim](none written yet)[/dim]"]
        scoped = [m for m in memories if getattr(m, "paths", ())]
        out = [f"  [bold]memory · operator rules[/bold]  "
               f"[dim]{len(memories)} total · {len(scoped)} path-scoped[/dim]"]
        for mem in memories[:limit]:
            kind = str(getattr(getattr(mem, "type", None), "value", "?"))
            name = str(getattr(mem, "name", "?"))
            paths = tuple(getattr(mem, "paths", ()) or ())
            scope = (", ".join(str(p) for p in paths[:3]) if paths
                     else "[dim]global[/dim]")
            out.append(f"    [{kind}] {name} → {scope}")
        if len(memories) > limit:
            out.append(f"    [dim]… {len(memories) - limit} more[/dim]")
        return out
    except Exception as exc:  # noqa: BLE001
        return [f"  [dim]rules surface degraded: "
                f"{type(exc).__name__}: {exc}[/dim]"]
