"""Slice 116 — Adaptive Immune Synthesis Engine (SAFE / bounded).

An immune system that only logs an infection is incomplete — it should synthesize
antibodies. But the antibody target here is the **safety cage itself**, so the
synthesis is bounded by the §1 invariant (the operator is the recursion bound):

  * **Deterministic, not LLM.** A new cage rule is derived by *analyzing the
    escape's AST signature* and emitting a pure structural matcher — never by an
    LLM. The safety substrate must not be LLM-written: an LLM rule could be
    subverted by a crafted Red payload (the adversary writing its own antibody)
    or carry a silent false-negative. Deterministic synthesis is precise +
    not prompt-injectable.
  * **Synthesize → validate → SHADOW → operator-promote.** A synthesized
    antibody is a *proposal*: it must (a) block the exact escape, (b) cause
    ZERO clean-control false-positives, (c) only ever ADD a block (monotonic
    tightening — it can never loosen the cage). It is written to a shadow
    proposals ledger; it becomes ACTIVE only via :func:`promote_antibody`, an
    OPERATOR-gated action (fail-closed — exactly the Slice-102/103 graduation
    discipline). The system never auto-arms a rule into its own cage.
  * **Hot-swap.** Approved antibodies load into :class:`AntibodyRegistry` live —
    the cage consults it ADDITIVELY (on top of the built-in SemanticGuardian
    patterns), no restart, no GLS interruption. The registry never *removes*
    a built-in rule; it can only add blocks.

This is bounded, human-actuated adaptive immunity — the dissertation's theorem,
not its refutation. Master ``JARVIS_ADAPTIVE_IMMUNITY_ENABLED`` §33.1 default-FALSE.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger("ouroboros.antibody_synthesizer")

_TRUTHY = ("1", "true", "yes", "on")
ANTIBODY_SCHEMA_VERSION = "antibody.v1"

# High-signal introspection attribute names — the escape vectors the cage's
# static rules are designed around. An antibody keyed on these is precise.
_INTROSPECTION_NAMES = frozenset({
    "__mro__", "__subclasses__", "__bases__", "__class__", "__globals__",
    "__builtins__", "__dict__", "__code__", "__closure__", "__func__",
    "__self__", "__module__", "__getattribute__", "func_globals",
})


def adaptive_immunity_enabled() -> bool:
    """§33.1 master — default FALSE. NEVER raises."""
    try:
        return (os.environ.get("JARVIS_ADAPTIVE_IMMUNITY_ENABLED", "") or "").strip().lower() in _TRUTHY
    except Exception:  # noqa: BLE001
        return False


def proposals_path() -> Path:
    raw = os.environ.get("JARVIS_ANTIBODY_PROPOSALS_PATH")
    return Path(raw) if raw else Path(".jarvis") / "antibody_proposals.jsonl"


def active_path() -> Path:
    raw = os.environ.get("JARVIS_ANTIBODY_ACTIVE_PATH")
    return Path(raw) if raw else Path(".jarvis") / "antibodies_active.jsonl"


# ===========================================================================
# Deterministic AST-signature extraction
# ===========================================================================


def extract_signature(source: str) -> Dict[str, Any]:
    """Parse *source* and extract the distinctive structural features an escape
    exploits — the high-signal introspection attribute accesses + getattr
    string-literal targets. PURE; never executes the source; NEVER raises
    (unparseable → empty signature)."""
    attrs: Set[str] = set()
    getattr_literals: Set[str] = set()
    try:
        tree = ast.parse(source or "")
    except Exception:  # noqa: BLE001
        return {"attrs": [], "getattr_literals": []}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if node.attr in _INTROSPECTION_NAMES:
                attrs.add(node.attr)
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id == "getattr" and len(node.args) >= 2:
                a1 = node.args[1]
                if isinstance(a1, ast.Constant) and isinstance(a1.value, str):
                    if a1.value in _INTROSPECTION_NAMES:
                        getattr_literals.add(a1.value)
    return {"attrs": sorted(attrs), "getattr_literals": sorted(getattr_literals)}


# ===========================================================================
# Antibody — a deterministic, monotonic-tighten-only structural matcher
# ===========================================================================


@dataclass(frozen=True)
class Antibody:
    antibody_id: str
    escape_sha256: str
    attr_block: Tuple[str, ...]
    getattr_block: Tuple[str, ...]
    status: str           # "proposed" | "approved"
    created_ts: float
    validation: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": ANTIBODY_SCHEMA_VERSION,
            "antibody_id": self.antibody_id, "escape_sha256": self.escape_sha256,
            "attr_block": list(self.attr_block), "getattr_block": list(self.getattr_block),
            "status": self.status, "created_ts": self.created_ts, "validation": self.validation,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Antibody":
        return cls(
            antibody_id=d["antibody_id"], escape_sha256=d.get("escape_sha256", ""),
            attr_block=tuple(d.get("attr_block", ())), getattr_block=tuple(d.get("getattr_block", ())),
            status=d.get("status", "proposed"), created_ts=float(d.get("created_ts", 0.0)),
            validation=d.get("validation", {}),
        )


def antibody_matches(source: str, ab: Antibody) -> bool:
    """Deterministic structural match: does *source*'s AST exhibit ANY blocked
    introspection feature? Pure; never executes; NEVER raises."""
    try:
        tree = ast.parse(source or "")
    except Exception:  # noqa: BLE001
        return False
    block_attrs = set(ab.attr_block)
    block_get = set(ab.getattr_block)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in block_attrs:
            return True
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id == "getattr" and len(node.args) >= 2:
                a1 = node.args[1]
                if isinstance(a1, ast.Constant) and isinstance(a1.value, str) and a1.value in block_get:
                    return True
    return False


def synthesize_antibody(
    escape_source: str,
    clean_controls: Sequence[str],
    *,
    now_unix: Optional[float] = None,
) -> Optional[Antibody]:
    """Derive a candidate antibody from an escape's AST signature, then VALIDATE
    it: it MUST block the escape AND produce ZERO clean-control false-positives.
    Returns the validated proposal, or ``None`` when no zero-FP narrow matcher
    exists (we never propose an FP-prone rule — a false antibody is worse than
    none). Deterministic; NEVER raises."""
    try:
        sig = extract_signature(escape_source)
        attrs = tuple(sig.get("attrs", ()))
        getattrs = tuple(sig.get("getattr_literals", ()))
        if not attrs and not getattrs:
            return None  # no distinctive introspection feature to key on
        ts = time.time() if now_unix is None else float(now_unix)
        escape_sha = hashlib.sha256((escape_source or "").encode("utf-8")).hexdigest()
        ab_id = hashlib.sha256(("|".join(attrs) + "::" + "|".join(getattrs)).encode("utf-8")).hexdigest()[:16]
        candidate = Antibody(
            antibody_id=ab_id, escape_sha256=escape_sha,
            attr_block=attrs, getattr_block=getattrs,
            status="proposed", created_ts=ts,
        )
        # Validate: blocks the escape...
        if not antibody_matches(escape_source, candidate):
            return None
        # ...and ZERO clean-control false-positives (monotonic-tighten safety).
        fp = [i for i, c in enumerate(clean_controls) if antibody_matches(c, candidate)]
        if fp:
            logger.debug("[Antibody] rejected — would FP on clean controls %s", fp)
            return None
        validated = Antibody(
            antibody_id=ab_id, escape_sha256=escape_sha,
            attr_block=attrs, getattr_block=getattrs, status="proposed", created_ts=ts,
            validation={"blocks_escape": True, "clean_fp_count": 0,
                        "clean_controls_checked": len(clean_controls)},
        )
        return validated
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Antibody] synthesis swallowed: %s", exc)
        return None


# ===========================================================================
# Shadow proposals + OPERATOR-gated promotion
# ===========================================================================


def propose_antibody(ab: Antibody, *, path: Optional[Path] = None) -> bool:
    """Write a synthesized antibody to the SHADOW proposals ledger. This does
    NOT arm it — it is evidence + a candidate awaiting operator review. NEVER
    raises."""
    try:
        p = path or proposals_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(ab.to_dict(), separators=(",", ":")) + "\n")
        return True
    except Exception:  # noqa: BLE001
        return False


def promote_antibody(ab: Antibody, *, operator_approved: bool, path: Optional[Path] = None) -> bool:
    """Arm an antibody into the ACTIVE set — the cage will enforce it after the
    next registry reload. **OPERATOR-GATED, fail-closed**: refuses unless
    ``operator_approved=True`` is passed explicitly (the operator's deliberate
    act). The synthesizer NEVER calls this; only an operator surface does.
    NEVER raises."""
    if not operator_approved:
        logger.warning("[Antibody] promote refused — operator approval required (fail-closed)")
        return False
    try:
        p = path or active_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        approved = Antibody(
            antibody_id=ab.antibody_id, escape_sha256=ab.escape_sha256,
            attr_block=ab.attr_block, getattr_block=ab.getattr_block,
            status="approved", created_ts=ab.created_ts, validation=ab.validation,
        )
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(approved.to_dict(), separators=(",", ":")) + "\n")
        return True
    except Exception:  # noqa: BLE001
        return False


# ===========================================================================
# Hot-swappable antibody registry — the cage's ADDITIVE consult
# ===========================================================================


class AntibodyRegistry:
    """Loads APPROVED antibodies and screens candidate source against them —
    the additive layer the cage consults on top of its built-in SemanticGuardian
    patterns. ``reload()`` hot-swaps the active set from disk with NO restart and
    NO GLS interruption. It can only ADD blocks (monotonic tightening); it never
    relaxes a built-in rule."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or active_path()
        self._antibodies: List[Antibody] = []
        self.reload()

    def reload(self) -> int:
        """Hot-load the active antibody set from disk. Returns the count. NEVER
        raises (a corrupt line is skipped, not fatal)."""
        loaded: List[Antibody] = []
        try:
            if self._path.exists():
                for line in self._path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        loaded.append(Antibody.from_dict(json.loads(line)))
                    except Exception:  # noqa: BLE001
                        continue
        except Exception:  # noqa: BLE001
            loaded = []
        self._antibodies = loaded
        return len(loaded)

    def screen(self, source: str) -> List[str]:
        """Return the IDs of every active antibody that matches *source* (a
        non-empty list = the cage blocks). NEVER raises."""
        hits: List[str] = []
        for ab in self._antibodies:
            try:
                if antibody_matches(source, ab):
                    hits.append(ab.antibody_id)
            except Exception:  # noqa: BLE001
                continue
        return hits

    @property
    def count(self) -> int:
        return len(self._antibodies)


# ===========================================================================
# Ledger listener — on a verified Escape, synthesize + PROPOSE (shadow only)
# ===========================================================================


def on_escape(
    escape_source: str,
    clean_controls: Sequence[str],
    *,
    proposals: Optional[Path] = None,
) -> Optional[Antibody]:
    """Adaptive listener: a verified Red-Team escape arrives → synthesize a
    candidate antibody and write it to the SHADOW proposals ledger. Does NOT
    arm it (operator promotes). Inert when the master is off. NEVER raises.
    Returns the proposal (for telemetry), or None."""
    if not adaptive_immunity_enabled():
        return None
    ab = synthesize_antibody(escape_source, clean_controls)
    if ab is None:
        return None
    propose_antibody(ab, path=proposals)
    logger.warning(
        "[IMMUNE SYSTEM ALERT] Antibody Proposal Generated — Awaiting Operator "
        "Approval (id=%s blocks introspection %s; SHADOW, cage NOT armed)",
        ab.antibody_id, list(ab.attr_block) + list(ab.getattr_block),
    )
    return ab


def recent_proposals(limit: int = 25, *, path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Read the most recent SHADOW antibody proposals (newest last) for the
    operator-approval UI. Read-only; NEVER raises."""
    out: List[Dict[str, Any]] = []
    try:
        p = path or proposals_path()
        if not p.exists():
            return []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    continue
    except Exception:  # noqa: BLE001
        return []
    return out[-max(0, int(limit)):]
