"""Refuse a plan that depends on an API this repository does not have.

Why here and not in VALIDATE
----------------------------

Of validation failures carrying an identifiable exception, ~63% are
``AttributeError`` / ``ModuleNotFoundError`` / ``ImportError`` -- the model
calling something that does not exist -- against **6** ``SyntaxError`` in the
same corpus. Every one of those is discovered after GENERATE has been paid
for, after the sandbox worktree, after pytest collection. The question "does
``module.symbol`` exist" is answerable from the repository's own AST in
milliseconds, and answering it earlier costs a generation instead of a
validation.

What it will and will not claim
-------------------------------

The gate only speaks about modules **this repository defines**. A reference
into the standard library or a third-party package resolves to no file here,
and a gate that failed those would refuse every correct plan that imports
``pathlib``. Absence of evidence is not evidence of absence: the verdict for
an unresolvable module is ``UNKNOWN``, not ``MISSING``, and only ``MISSING``
sheds a candidate.

That asymmetry is the whole design. A false refusal costs a correct plan and
teaches the loop that grounding checks are noise; a missed reference costs
one validation cycle, which is what happens today anyway. So the gate is
deliberately quiet: it fires only when it can point at the file that should
contain the symbol and show that it does not.
"""
from __future__ import annotations

import ast
import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger("Ouroboros.APIGrounding")

# A dotted reference with at least one dot: ``mod.symbol``, ``a.b.c``.
# Bare names are excluded -- they are locals far more often than API, and a
# gate that guesses at them is a gate that cries wolf.
_DOTTED = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)\b")

# Fenced blocks and inline spans: where a plan states code rather than prose.
_FENCE = re.compile(r"```[a-zA-Z0-9_-]*\n(.*?)```", re.DOTALL)
_INLINE = re.compile(r"`([^`\n]+)`")


class Verdict(str, Enum):
    GROUNDED = "grounded"
    MISSING = "missing"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Reference:
    """One dotted reference a plan intends to use."""

    dotted: str
    module: str
    symbol: str


@dataclass
class GroundingReport:
    """What the gate could and could not establish."""

    checked: Tuple[Reference, ...] = ()
    missing: Tuple[Reference, ...] = ()
    unknown: Tuple[Reference, ...] = ()

    @property
    def verdict(self) -> Verdict:
        if self.missing:
            return Verdict.MISSING
        return Verdict.GROUNDED if self.checked else Verdict.UNKNOWN

    @property
    def grounded(self) -> bool:
        return not self.missing

    def render(self) -> str:
        return (
            f"verdict={self.verdict.value} checked={len(self.checked)} "
            f"missing={len(self.missing)} unknown={len(self.unknown)}"
            + (
                " -> " + ", ".join(
                    f"{r.module}.{r.symbol}" for r in self.missing[:5]
                ) if self.missing else ""
            )
        )


def rejection_constraint(report: "GroundingReport") -> str:
    """The refusal, written as an instruction the planner can act on.

    A gate that only says "no" makes the planner guess again from the same
    prior, and it guesses the same way -- which is how a refusal becomes a
    loop instead of a correction. Naming the exact symbol turns the refusal
    into information: the model is told what does not exist, not merely
    that something did not.

    Deliberately states the negative only. Proposing a replacement symbol
    would be this gate inventing API, which is the failure it exists to
    catch. NEVER raises.
    """
    try:
        if not report.missing:
            return ""
        lines = [
            "GROUNDING CONSTRAINT — the previous plan referenced APIs that "
            "do not exist in this repository:",
        ]
        for ref in report.missing[:12]:
            lines.append(
                f"  - `{ref.dotted}` — module `{ref.module}` defines no "
                f"`{ref.symbol}`."
            )
        lines.append(
            "Do NOT reference these symbols again. Read the module's actual "
            "surface and choose a symbol it defines, or state that the "
            "capability is absent and must be created."
        )
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        return ""


def missing_signature(report: "GroundingReport") -> str:
    """A stable identity for one set of missing symbols.

    Order-independent, so the same hallucination re-stated in a different
    order is recognised as the same hallucination.
    """
    try:
        return "|".join(sorted(f"{r.module}.{r.symbol}" for r in report.missing))
    except Exception:  # noqa: BLE001
        return ""


def repeated_hallucination(op_id: str, report: "GroundingReport") -> bool:
    """Has the planner produced this exact missing-symbol set before?

    Composed on ``ForwardProgressDetector`` -- the same primitive the
    GENERATE retry loop, the micro-fix governor and the super-agent ReAct
    loop use -- rather than a fourth counter. Keyed per op so two
    operations hallucinating alike are not read as one looping.
    NEVER raises.
    """
    signature = missing_signature(report)
    if not signature:
        return False
    try:
        import hashlib

        from backend.core.ouroboros.governance.forward_progress import (
            ForwardProgressDetector,
        )
        global _GROUNDING_DETECTOR  # noqa: PLW0603
        if _GROUNDING_DETECTOR is None:
            _GROUNDING_DETECTOR = ForwardProgressDetector()
        digest = hashlib.sha256(signature.encode("utf-8", "replace")).hexdigest()
        return bool(_GROUNDING_DETECTOR.observe(f"grounding::{op_id}", digest))
    except Exception:  # noqa: BLE001
        return False


_GROUNDING_DETECTOR = None


class APIGroundingFault(Exception):
    """A plan referenced a symbol its own repository does not define."""

    def __init__(self, report: GroundingReport) -> None:
        super().__init__(
            "plan references APIs that do not exist: "
            + ", ".join(f"{r.module}.{r.symbol}" for r in report.missing)
        )
        self.report = report


def gate_enabled() -> bool:
    """Default ON. ``JARVIS_API_GROUNDING_GATE_ENABLED`` kills it.

    Safe to default on because the gate is advisory unless
    :func:`shed_enabled`; it reports without shedding until an operator
    arms the shed.
    """
    raw = (os.environ.get("JARVIS_API_GROUNDING_GATE_ENABLED", "") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def shed_enabled() -> bool:
    """Whether a MISSING verdict actually sheds the candidate. Default OFF.

    The gate earns the authority to shed by first proving, in the
    reachability ledger, that it fires on real plans and not on correct
    ones. Arming a refusal before it has that evidence is how a grounding
    check becomes the thing everyone disables.
    """
    raw = (os.environ.get("JARVIS_API_GROUNDING_SHED_ENABLED", "") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def code_spans(text: str) -> List[str]:
    """The parts of a plan that state code rather than describe it.

    Prose is excluded deliberately: "we will update the manager's state"
    contains no API claim, and mining it for dotted names produces noise
    that a gate cannot act on.
    """
    if not text:
        return []
    spans: List[str] = []
    try:
        for match in _FENCE.finditer(text):
            spans.append(match.group(1))
        stripped = _FENCE.sub(" ", text)
        for match in _INLINE.finditer(stripped):
            spans.append(match.group(1))
    except Exception:  # noqa: BLE001
        return []
    return spans


def extract_references(text: str) -> Tuple[Reference, ...]:
    """Dotted references a plan states in code. NEVER raises.

    The last segment is the symbol, everything before it the module path --
    which is a claim about where it lives, and exactly the claim the
    repository can adjudicate.
    """
    out: List[Reference] = []
    seen: Set[str] = set()

    def _add(module: str, symbol: str) -> None:
        dotted = f"{module}.{symbol}"
        if dotted in seen or not module or not symbol:
            return
        seen.add(dotted)
        out.append(Reference(dotted=dotted, module=module, symbol=symbol))

    for span in code_spans(text):
        # An import statement states exactly which names a module is
        # expected to export, which is the strongest API claim a plan can
        # make -- and the one a regex cannot read, because the names sit
        # after ``import`` with no dot to find them by. The parser gets
        # them precisely; the regex below still covers attribute access and
        # prose-embedded references in spans that do not parse.
        try:
            tree = ast.parse(span)
        except (SyntaxError, ValueError):
            tree = None
        if tree is not None:
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    for alias in node.names:
                        if alias.name != "*":
                            _add(node.module, alias.name)
        try:
            for dotted in _DOTTED.findall(span):
                module, _, symbol = dotted.rpartition(".")
                _add(module, symbol)
        except Exception:  # noqa: BLE001
            continue
    return tuple(out)


def _module_candidates(module: str, project_root: Path) -> List[Path]:
    """Files that could define *module*, dotted or path-shaped.

    Both spellings appear in real plans -- ``backend.core.x`` from an import
    line and ``backend/core/x.py`` from a file reference -- and a resolver
    that understood only one would call the other unknown.
    """
    parts = module.replace("/", ".").split(".")
    if not parts:
        return []
    rel = Path(*parts)
    out = [project_root / rel.with_suffix(".py"), project_root / rel / "__init__.py"]
    # A plan often names only the tail (``jarvis_reload_manager.Manager``);
    # the last segment alone is a weaker but real signal.
    if len(parts) > 1:
        out.append(project_root / f"{parts[-1]}.py")
    return [p for p in out if p.is_file()]


def _defined_in(path: Path) -> frozenset:
    """Every def/class name in *path*, via the existing declared-symbol
    reader rather than a second AST walk."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return frozenset()
    try:
        from backend.core.ouroboros.governance.declared_symbols import (  # noqa: PLC0415
            defined_names,
        )
        names = set(defined_names(source))
    except Exception:  # noqa: BLE001
        names = set()
    # Module-level assignments are API too: constants, singletons, and the
    # ``logger`` every module in this tree exposes.
    try:
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, ast.Assign):
                names.update(
                    t.id for t in node.targets if isinstance(t, ast.Name)
                )
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    names.add(alias.asname or alias.name.split(".")[0])
    except (SyntaxError, ValueError):
        pass
    return frozenset(names)


def _check(reference: Reference, project_root: Path) -> Verdict:
    # A dotted path that resolves to a MODULE is a module reference, not a
    # missing symbol. ``pkg.sub.leaf`` splits into module ``pkg.sub`` and
    # symbol ``leaf``, and ``pkg/sub/__init__.py`` has no name ``leaf`` --
    # it has a submodule by that name. Checking the whole path first is what
    # stops the gate refusing every correct plan that names a module, which
    # is the false positive that would make it the first thing disabled.
    if _module_candidates(reference.dotted, project_root):
        return Verdict.GROUNDED

    paths = _module_candidates(reference.module, project_root)
    if not paths:
        return Verdict.UNKNOWN          # stdlib, third-party, or not a module
    for path in paths:
        if reference.symbol in _defined_in(path):
            return Verdict.GROUNDED
    return Verdict.MISSING


async def ground_plan(
    plan_text: str,
    *,
    project_root: Path,
    extra_sources: Iterable[str] = (),
) -> GroundingReport:
    """Adjudicate every dotted reference a plan states.

    Async because resolution reads and parses source files -- on a plan with
    many references that is real blocking I/O, and this runs inside the
    orchestrator's event loop beside the heartbeats and the control plane.
    The adjudication itself is pure and deterministic; no model is consulted
    about whether a symbol exists, because the repository already knows.
    """
    if not gate_enabled():
        return GroundingReport()
    references = extract_references(plan_text)
    for source in extra_sources:
        references = references + extract_references(str(source or ""))
    if not references:
        return GroundingReport()

    unique: Dict[str, Reference] = {r.dotted: r for r in references}

    def _resolve_all() -> Tuple[List[Reference], List[Reference], List[Reference]]:
        checked, missing, unknown = [], [], []
        for ref in unique.values():
            verdict = _check(ref, project_root)
            if verdict is Verdict.MISSING:
                missing.append(ref)
                checked.append(ref)
            elif verdict is Verdict.GROUNDED:
                checked.append(ref)
            else:
                unknown.append(ref)
        return checked, missing, unknown

    try:
        checked, missing, unknown = await asyncio.to_thread(_resolve_all)
    except Exception:  # noqa: BLE001 — grounding never blocks the pipeline
        logger.debug("[APIGrounding] resolution degraded", exc_info=True)
        return GroundingReport()

    report = GroundingReport(
        checked=tuple(checked), missing=tuple(missing), unknown=tuple(unknown),
    )
    if report.missing:
        logger.warning(
            "[APIGrounding] plan references %d symbol(s) this repository does "
            "not define: %s — %s",
            len(report.missing),
            ", ".join(f"{r.module}.{r.symbol}" for r in report.missing[:8]),
            "shedding" if shed_enabled() else "advisory only (shed disarmed)",
        )
    else:
        logger.info("[APIGrounding] %s", report.render())
    return report


__all__ = [
    "APIGroundingFault",
    "GroundingReport",
    "Reference",
    "Verdict",
    "code_spans",
    "extract_references",
    "gate_enabled",
    "ground_plan",
    "shed_enabled",
]
