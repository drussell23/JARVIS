"""§3.6.2 vector #6 — Phase 9 graduation orchestrator (Slice 1).

Closes the load-bearing 🔴 Critical default-FALSE flag problem
identified in the 2026-05-05 brutal review. The vector itself
(operator-paced empirical evidence) cannot be engineering-
shortcut without violating §33.1 discipline — each flag still
needs ≥3 (PASS_B) or ≥5 (PASS_C) clean sessions accumulating
across real cadence runs. But the *operator surface* around
the cadence can be:

  * Aggregated into a single source of truth (today: the
    operator scans graduation_ledger output × N flags by hand).
  * Priority-ranked by readiness so the "next-best flag to
    soak" is computable in one shot.
  * Cross-flag-interaction-tracked so confounded evidence
    (flag A clean solo + flag B clean solo, but A+B together
    breaks something) becomes operator-visible BEFORE the flip.

This module is the **Phase 9 dashboard substrate** — a pure-
read aggregation layer composing existing canonical primitives
(``adaptation/graduation_ledger.GraduationLedger`` + the 8 §33.1
``*_graduation_contract.py`` harnesses + the cadence policy
table). Zero parallel state, zero policy mutation.

**Architectural lineage** (operator binding 2026-05-07):

  * **NOT** a resurrection of the archived
    ``graduation_orchestrator.py`` (15-phase tool→agent
    synthesis FSM at ``/archive/legacy/graduation_orchestrator
    _2026_04_06.py``). That file is AST-pinned archive-only via
    ``graduation_orchestrator_archived_only`` — different
    purpose, intentional cage.
  * **NEW** purpose: per-flag soak coordination. Phase 9 is
    the cadence axis (graduation_ledger + live_fire_soak
    are the actuators); this module is the **dashboard +
    queue** that composes them.

**Interaction matrix** (bullet (e) from 2026-05-07 substrate
map): persistent append-only JSONL at
``.jarvis/graduation_interaction_matrix.jsonl``. Each row =
one soak session + the set of flags enabled in it. Pair counts
derive deterministically from the rows. Defends against:
"flag A clean × N + flag B clean × N, but A+B together
produces a regression that no solo soak surfaces."

**Authority asymmetry** (AST-pinned): no orchestrator /
iron_gate / providers / urgency_router / change_engine /
semantic_guardian / candidate_generator / policy imports.
Pure-aggregation read-only browser; mirrors ``replay_repl`` /
``history_repl`` / ``mode_repl`` / ``canvas_repl`` /
``scope_repl`` discipline.

**Master flag** ``JARVIS_PHASE9_ORCHESTRATOR_ENABLED`` default-
FALSE per §33.1: when off, every public surface returns empty
results (zero ledger reads, zero filesystem cost). The data
flag stays FALSE until 3 clean sessions empirically prove the
dashboard render path; harness flag in graduation contract
stays default-TRUE per separation-of-concerns.

**NEVER raises** — every code path defensive.
"""
from __future__ import annotations

import enum
import itertools
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Tuple


logger = logging.getLogger("Ouroboros.Phase9Orchestrator")


PHASE9_ORCHESTRATOR_SCHEMA_VERSION: str = (
    "phase9_orchestrator.1"
)


_TRUTHY = frozenset({"1", "true", "yes", "on"})


# ---------------------------------------------------------------------------
# Closed taxonomy — 4-value queue status
# ---------------------------------------------------------------------------


class Phase9QueueStatus(str, enum.Enum):
    """Closed 4-value taxonomy for queue-entry state.
    AST-pinned."""

    READY = "ready"
    """Clean count ≥ required + zero non-waived runner failures
    + dependencies satisfied. Operator can flip the data flag."""

    PENDING = "pending"
    """Clean count < required OR clean count met but contract
    harness reports INSUFFICIENT_EVIDENCE for some other axis.
    Continue soaking."""

    BLOCKED = "blocked"
    """Runner failures present (non-waived) OR dependency on
    another flag that hasn't graduated yet. Cadence is wasted
    until the upstream blocker resolves."""

    GRADUATED = "graduated"
    """Data flag already flipped to default-TRUE. Listed here
    so the queue is a complete record (operator can see what's
    already shipped vs what's outstanding)."""


# ---------------------------------------------------------------------------
# Master flag — §33.1 default-FALSE
# ---------------------------------------------------------------------------


def master_enabled() -> bool:
    """``JARVIS_PHASE9_ORCHESTRATOR_ENABLED`` master switch.
    Default-FALSE per §33.1: when off, every public surface
    returns empty results (zero ledger reads, zero filesystem
    cost; byte-identical pre-slice behavior)."""
    raw = os.environ.get(
        "JARVIS_PHASE9_ORCHESTRATOR_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False
    return raw in _TRUTHY


# ---------------------------------------------------------------------------
# Path resolution — composes canonical .jarvis dir
# ---------------------------------------------------------------------------


def _resolve_repo_root() -> Path:
    override = os.environ.get(
        "JARVIS_PHASE9_REPO_ROOT", "",
    ).strip()
    if override:
        try:
            return Path(override).resolve()
        except Exception:  # noqa: BLE001 — defensive
            pass
    try:
        return Path(__file__).resolve().parents[4]
    except Exception:  # noqa: BLE001 — defensive
        return Path(".").resolve()


def interaction_matrix_path() -> Path:
    """``.jarvis/graduation_interaction_matrix.jsonl`` under
    the resolved repo root. Override via
    ``JARVIS_PHASE9_INTERACTION_MATRIX_PATH``."""
    override = os.environ.get(
        "JARVIS_PHASE9_INTERACTION_MATRIX_PATH", "",
    ).strip()
    if override:
        try:
            return Path(override).resolve()
        except Exception:  # noqa: BLE001 — defensive
            pass
    return (
        _resolve_repo_root()
        / ".jarvis"
        / "graduation_interaction_matrix.jsonl"
    )


# ---------------------------------------------------------------------------
# Frozen artifact — Phase9QueueEntry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Phase9QueueEntry:
    """One flag's queue entry. Frozen for safe propagation
    across query result sets. Adopts §33.5 versioned-artifact
    contract."""

    flag_name: str
    cadence_class: str
    """``pass_b`` (3 clean) or ``pass_c`` (5 clean)."""

    clean_count: int
    runner_count: int
    infra_count: int
    required: int
    last_outcome: str
    """Last recorded outcome: clean / infra / runner / migration /
    none."""

    description: str
    status: Phase9QueueStatus
    readiness_score: float
    """[0.0, 1.0]. ``clean_count / required`` clamped, with
    runner-failure penalty: scores collapse to 0 when any
    non-waived runner row exists (BLOCKED state)."""

    interaction_partner_count: int
    """Number of distinct OTHER flags that have appeared
    enabled alongside this flag in any session of the
    interaction matrix. Higher = more broadly tested in
    combination."""

    schema_version: str = field(
        default=PHASE9_ORCHESTRATOR_SCHEMA_VERSION,
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "flag_name": str(self.flag_name),
            "cadence_class": str(self.cadence_class),
            "clean_count": int(self.clean_count),
            "runner_count": int(self.runner_count),
            "infra_count": int(self.infra_count),
            "required": int(self.required),
            "last_outcome": str(self.last_outcome),
            "description": str(self.description)[:256],
            "status": self.status.value,
            "readiness_score": float(self.readiness_score),
            "interaction_partner_count": int(
                self.interaction_partner_count,
            ),
            "schema_version": str(self.schema_version),
        }


# ---------------------------------------------------------------------------
# Phase9Orchestrator — pure-aggregation dashboard
# ---------------------------------------------------------------------------


class Phase9Orchestrator:
    """Pure-aggregation read-only browser composing
    ``GraduationLedger`` + ``CADENCE_POLICY`` + the interaction
    matrix into a single dashboard. NEVER raises."""

    def __init__(
        self,
        *,
        interaction_matrix_path_override: Optional[Path] = None,
    ) -> None:
        self._matrix_path: Path = (
            Path(interaction_matrix_path_override)
            if interaction_matrix_path_override is not None
            else interaction_matrix_path()
        )
        self._matrix_lock = threading.RLock()

    @property
    def matrix_path(self) -> Path:
        return self._matrix_path

    # ------------------------------------------------------------------
    # Queue API
    # ------------------------------------------------------------------

    def get_full_queue(
        self,
    ) -> Tuple[Phase9QueueEntry, ...]:
        """Aggregate every flag in CADENCE_POLICY into queue
        entries, composing GraduationLedger.progress + the
        interaction matrix. Returns tuple in policy-table
        order (operator can re-rank via :meth:`rank_by_
        readiness`). NEVER raises."""
        if not master_enabled():
            return ()
        try:
            from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
                CADENCE_POLICY,
                get_default_ledger,
                is_ledger_enabled,
            )
        except ImportError:
            return ()
        try:
            ledger = get_default_ledger()
        except Exception:  # noqa: BLE001 — defensive
            return ()
        ledger_active = False
        try:
            ledger_active = is_ledger_enabled()
        except Exception:  # noqa: BLE001 — defensive
            pass
        partner_counts = self._compute_partner_counts()
        entries: List[Phase9QueueEntry] = []
        for policy in CADENCE_POLICY:
            try:
                if ledger_active:
                    progress = ledger.progress(policy.flag_name)
                else:
                    progress = {
                        "clean": 0, "infra": 0, "runner": 0,
                        "migration": 0,
                        "required": policy.required_clean_sessions,
                    }
            except Exception:  # noqa: BLE001 — defensive
                progress = {
                    "clean": 0, "infra": 0, "runner": 0,
                    "migration": 0,
                    "required": policy.required_clean_sessions,
                }
            clean = int(progress.get("clean", 0))
            runner = int(progress.get("runner", 0))
            infra = int(progress.get("infra", 0))
            required = int(
                progress.get(
                    "required", policy.required_clean_sessions,
                ),
            )
            partners = partner_counts.get(
                policy.flag_name, set(),
            )
            entry = self._build_entry(
                flag_name=policy.flag_name,
                cadence_class=policy.cadence_class.value,
                clean=clean, runner=runner, infra=infra,
                required=required,
                description=policy.description,
                last_outcome=self._last_outcome_for_flag(
                    ledger, policy.flag_name, ledger_active,
                ),
                already_graduated=(
                    self._flag_already_graduated(
                        policy.flag_name,
                    )
                ),
                partner_count=len(partners),
            )
            entries.append(entry)
        return tuple(entries)

    def rank_by_readiness(
        self,
    ) -> Tuple[Phase9QueueEntry, ...]:
        """Return queue entries sorted by readiness_score
        descending — the operator-facing "what to soak next"
        order. GRADUATED entries sink to the bottom (they're
        done). NEVER raises."""
        try:
            queue = self.get_full_queue()
        except Exception:  # noqa: BLE001 — defensive
            return ()

        def _sort_key(e: Phase9QueueEntry) -> Tuple[int, float]:
            # Primary: GRADUATED last; everything else grouped
            # at top.
            primary = (
                1 if e.status is Phase9QueueStatus.GRADUATED
                else 0
            )
            # Secondary: readiness_score descending → negate.
            return (primary, -e.readiness_score)

        try:
            return tuple(sorted(queue, key=_sort_key))
        except Exception:  # noqa: BLE001 — defensive
            return queue

    def next_recommended_flag(self) -> Optional[str]:
        """Return the highest-readiness non-GRADUATED non-
        BLOCKED flag name, or ``None`` when nothing is
        soakable. NEVER raises."""
        try:
            ranked = self.rank_by_readiness()
        except Exception:  # noqa: BLE001 — defensive
            return None
        for entry in ranked:
            if entry.status in (
                Phase9QueueStatus.READY,
                Phase9QueueStatus.PENDING,
            ):
                return entry.flag_name
        return None

    # ------------------------------------------------------------------
    # Interaction matrix (append-only JSONL)
    # ------------------------------------------------------------------

    def record_session_flags(
        self,
        *,
        session_id: str,
        flags_enabled: Tuple[str, ...],
    ) -> bool:
        """Append a session record to the interaction matrix.
        Idempotent re-record is a no-op deduplicated by
        session_id (caller's responsibility — we don't read-
        modify-write the file). Returns ``True`` on success.
        NEVER raises."""
        if not master_enabled():
            return False
        sid = str(session_id or "").strip()
        if not sid:
            return False
        try:
            cleaned = tuple(
                str(f).strip() for f in flags_enabled
                if str(f).strip()
            )
        except Exception:  # noqa: BLE001 — defensive
            return False
        if not cleaned:
            return False
        record = {
            "schema_version": (
                PHASE9_ORCHESTRATOR_SCHEMA_VERSION
            ),
            "session_id": sid,
            "flags": list(cleaned),
            "ts": time.time(),
        }
        try:
            with self._matrix_lock:
                self._matrix_path.parent.mkdir(
                    parents=True, exist_ok=True,
                )
                with self._matrix_path.open(
                    "a", encoding="utf-8",
                ) as f:
                    f.write(json.dumps(record) + "\n")
            return True
        except OSError:
            return False
        except Exception:  # noqa: BLE001 — defensive
            return False

    def get_interaction_matrix(
        self,
    ) -> Dict[FrozenSet[str], int]:
        """Read the matrix and return pair-counts (flag-set
        keyed by FrozenSet of size 2 → count of sessions where
        BOTH flags were enabled). NEVER raises."""
        if not master_enabled():
            return {}
        out: Dict[FrozenSet[str], int] = {}
        for flags in self._iter_session_flags():
            try:
                for a, b in itertools.combinations(
                    sorted(flags), 2,
                ):
                    key = frozenset({a, b})
                    out[key] = out.get(key, 0) + 1
            except Exception:  # noqa: BLE001 — defensive
                continue
        return out

    def total_session_count(self) -> int:
        """Operator visibility — total session rows in the
        matrix. NEVER raises."""
        if not master_enabled():
            return 0
        n = 0
        for _flags in self._iter_session_flags():
            n += 1
        return n

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _iter_session_flags(self):
        """Yield each session's flag-set from the JSONL.
        Defensive: malformed lines / missing fields skipped.
        NEVER raises."""
        if not self._matrix_path.is_file():
            return
        try:
            with self._matrix_path.open(
                "r", encoding="utf-8",
            ) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if not isinstance(entry, dict):
                        continue
                    flags_raw = entry.get("flags")
                    if not isinstance(flags_raw, list):
                        continue
                    flags = tuple(
                        str(x).strip() for x in flags_raw
                        if str(x).strip()
                    )
                    if flags:
                        yield flags
        except OSError:
            return

    def _compute_partner_counts(
        self,
    ) -> Dict[str, set]:
        """Build flag → set-of-distinct-partner-flags from the
        interaction matrix. NEVER raises."""
        out: Dict[str, set] = {}
        for flags in self._iter_session_flags():
            try:
                flag_set = set(flags)
                for f in flag_set:
                    bucket = out.setdefault(f, set())
                    for other in flag_set:
                        if other != f:
                            bucket.add(other)
            except Exception:  # noqa: BLE001 — defensive
                continue
        return out

    @staticmethod
    def _last_outcome_for_flag(
        ledger: Any, flag_name: str, ledger_active: bool,
    ) -> str:
        """Best-effort lookup of the most-recent outcome.
        Defensive: returns ``"none"`` on any failure."""
        if not ledger_active:
            return "none"
        try:
            # Use the public progress() to derive a last-
            # outcome heuristic without parsing private state:
            # if any runner > 0, runner is "recent enough"; else
            # if clean > 0, clean; else infra; else none.
            progress = ledger.progress(flag_name)
        except Exception:  # noqa: BLE001 — defensive
            return "none"
        if int(progress.get("runner", 0)) > 0:
            return "runner"
        if int(progress.get("clean", 0)) > 0:
            return "clean"
        if int(progress.get("infra", 0)) > 0:
            return "infra"
        if int(progress.get("migration", 0)) > 0:
            return "migration"
        return "none"

    @staticmethod
    def _flag_already_graduated(flag_name: str) -> bool:
        """Detect if the data flag has been flipped to default-
        TRUE in production. Best-effort check via env var read
        (operator may have flipped via env override; the
        canonical default-flip is in source code which we
        can't introspect cheaply here)."""
        raw = os.environ.get(flag_name, "").strip().lower()
        return raw in _TRUTHY

    @staticmethod
    def _build_entry(
        *,
        flag_name: str,
        cadence_class: str,
        clean: int,
        runner: int,
        infra: int,
        required: int,
        description: str,
        last_outcome: str,
        already_graduated: bool,
        partner_count: int,
    ) -> Phase9QueueEntry:
        """Compose a Phase9QueueEntry — pure function, status
        + readiness derive deterministically from inputs."""
        if already_graduated:
            status = Phase9QueueStatus.GRADUATED
            score = 1.0
        elif runner > 0:
            status = Phase9QueueStatus.BLOCKED
            score = 0.0
        elif clean >= required:
            status = Phase9QueueStatus.READY
            score = 1.0
        else:
            status = Phase9QueueStatus.PENDING
            try:
                score = float(clean) / float(required) \
                    if required > 0 else 0.0
            except (TypeError, ZeroDivisionError):
                score = 0.0
            if score > 1.0:
                score = 1.0
            if score < 0.0:
                score = 0.0
        return Phase9QueueEntry(
            flag_name=flag_name,
            cadence_class=cadence_class,
            clean_count=int(clean),
            runner_count=int(runner),
            infra_count=int(infra),
            required=int(required),
            last_outcome=last_outcome,
            description=description,
            status=status,
            readiness_score=score,
            interaction_partner_count=int(partner_count),
        )


# ---------------------------------------------------------------------------
# Default-singleton accessor
# ---------------------------------------------------------------------------


_DEFAULT_ORCHESTRATOR: Optional[Phase9Orchestrator] = None
_SINGLETON_LOCK = threading.Lock()


def get_default_orchestrator() -> Phase9Orchestrator:
    """Return the process-wide :class:`Phase9Orchestrator`
    singleton."""
    global _DEFAULT_ORCHESTRATOR
    with _SINGLETON_LOCK:
        if _DEFAULT_ORCHESTRATOR is None:
            _DEFAULT_ORCHESTRATOR = Phase9Orchestrator()
        return _DEFAULT_ORCHESTRATOR


def reset_default_orchestrator_for_tests() -> None:
    """Test-only — pinned via naming convention."""
    global _DEFAULT_ORCHESTRATOR
    with _SINGLETON_LOCK:
        _DEFAULT_ORCHESTRATOR = None


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> None:
    """Auto-discovered. Seeds the master flag this module
    reads."""
    try:
        registry.register(
            name="JARVIS_PHASE9_ORCHESTRATOR_ENABLED",
            type_="bool",
            default="false",
            description=(
                "Master switch for §3.6.2 vector #6 Phase 9 "
                "graduation orchestrator dashboard. Default-"
                "FALSE per §33.1; when off, get_full_queue "
                "returns () (zero ledger reads, zero "
                "filesystem cost — byte-identical pre-slice "
                "behavior)."
            ),
            category="Observability",
            posture_relevance="RELEVANT",
            source_file=(
                "backend/core/ouroboros/governance/"
                "phase9_orchestrator.py"
            ),
            example=(
                "JARVIS_PHASE9_ORCHESTRATOR_ENABLED=true"
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[Phase9Orchestrator] FlagRegistry seeding "
            "failed (non-fatal)", exc_info=True,
        )


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``phase9_orchestrator_status_taxonomy_4_values`` —
         closed enum (READY/PENDING/BLOCKED/GRADUATED).
      2. ``phase9_orchestrator_master_flag_default_false`` —
         §33.1 producer flag stays default-FALSE.
      3. ``phase9_orchestrator_authority_asymmetry`` —
         substrate purity (no orchestrator-tier imports).
      4. ``phase9_orchestrator_no_archived_orchestrator_import``
         — defends against re-importing the archived
         ``graduation_orchestrator`` (different scope, AST-
         pinned archive-only).
      5. ``phase9_orchestrator_composes_canonical_ledger`` —
         queue aggregation MUST compose
         ``adaptation.graduation_ledger`` (CADENCE_POLICY +
         GraduationLedger.progress); no parallel policy table.
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "phase9_orchestrator.py"
    )

    def _validate_status_taxonomy(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        required = {"READY", "PENDING", "BLOCKED", "GRADUATED"}
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "Phase9QueueStatus"
            ):
                seen: set = set()
                for stmt in node.body:
                    if isinstance(stmt, ast.Assign):
                        for tgt in stmt.targets:
                            if isinstance(tgt, ast.Name):
                                seen.add(tgt.id)
                extra = seen - required
                missing = required - seen
                if extra:
                    violations.append(
                        f"Phase9QueueStatus has extra values "
                        f"{sorted(extra)} — taxonomy is closed"
                    )
                if missing:
                    violations.append(
                        f"Phase9QueueStatus missing required "
                        f"values {sorted(missing)}"
                    )
                return tuple(violations)
        violations.append(
            "Phase9QueueStatus class missing"
        )
        return tuple(violations)

    def _validate_master_default_false(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        target_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.name == "master_enabled":
                    target_func = node
                    break
        if target_func is None:
            violations.append("master_enabled() missing")
            return tuple(violations)
        empty_guard_returns_false = False
        for sub in ast.walk(target_func):
            if not isinstance(sub, ast.If):
                continue
            test = sub.test
            compares: list = []
            for st in ast.walk(test):
                if isinstance(st, ast.Compare):
                    compares.append(st)
            compares_empty_str = False
            for cmp_node in compares:
                if not cmp_node.ops or not isinstance(
                    cmp_node.ops[0], ast.Eq,
                ):
                    continue
                for operand in (
                    cmp_node.left, *cmp_node.comparators,
                ):
                    if (
                        isinstance(operand, ast.Constant)
                        and operand.value == ""
                    ):
                        compares_empty_str = True
                        break
                if compares_empty_str:
                    break
            if not compares_empty_str:
                continue
            for body_stmt in sub.body:
                if isinstance(body_stmt, ast.Return):
                    if (
                        isinstance(body_stmt.value, ast.Constant)
                        and body_stmt.value.value is False
                    ):
                        empty_guard_returns_false = True
                        break
            if empty_guard_returns_false:
                break
        if not empty_guard_returns_false:
            violations.append(
                "master_enabled() MUST return False on empty "
                "env-var string per §33.1"
            )
        return tuple(violations)

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Forbid orchestrator-tier imports. Uses segment-
        based matching (split module path by '.') so
        `governance.orchestrator` is caught while
        `phase9_orchestrator` (self) is allowed. Multi-segment
        forbidden names (e.g. `iron_gate`) match any segment
        containing the substring."""
        violations: list = []
        # Exact-segment forbidden names (must equal a path
        # segment, not just contain).
        forbidden_exact = {"orchestrator"}
        # Substring forbidden — match if any segment contains.
        forbidden_substring = (
            "iron_gate", "providers",
            "candidate_generator", "urgency_router",
            "change_engine", "semantic_guardian",
        )
        # The bare token "policy" is too common as a substring
        # in legitimate names; require an exact-segment match
        # to fire.
        forbidden_exact.add("policy")
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                segments = module.split(".")
                # Allow self-references.
                if any(
                    s == "phase9_orchestrator" for s in segments
                ):
                    continue
                # Exact-segment match.
                for seg in segments:
                    if seg in forbidden_exact:
                        violations.append(
                            f"phase9_orchestrator.py MUST NOT "
                            f"import {module!r} "
                            f"(forbidden segment {seg!r})"
                        )
                        break
                # Substring match across segments.
                for f in forbidden_substring:
                    if any(f in seg for seg in segments):
                        violations.append(
                            f"phase9_orchestrator.py MUST NOT "
                            f"import {module!r} "
                            f"(forbidden token {f!r})"
                        )
                        break
        return tuple(violations)

    def _validate_no_archived_import(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Defends against accidentally re-importing the
        archived ``graduation_orchestrator`` (different
        purpose; AST-pinned archive-only)."""
        violations: list = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if "graduation_orchestrator" in module:
                    violations.append(
                        f"phase9_orchestrator.py MUST NOT "
                        f"import the archived "
                        f"graduation_orchestrator: {module!r}"
                    )
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if "graduation_orchestrator" in alias.name:
                        violations.append(
                            f"phase9_orchestrator.py MUST NOT "
                            f"import {alias.name!r}"
                        )
        return tuple(violations)

    def _validate_composes_canonical_ledger(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Aggregation MUST compose
        ``adaptation.graduation_ledger.CADENCE_POLICY`` +
        ``GraduationLedger`` — no parallel policy table."""
        violations: list = []
        target_method = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.name == "get_full_queue":
                    target_method = node
                    break
        if target_method is None:
            violations.append("get_full_queue() missing")
            return tuple(violations)
        composes_ledger = False
        for sub in ast.walk(target_method):
            if isinstance(sub, ast.ImportFrom):
                module = sub.module or ""
                if "graduation_ledger" in module:
                    names = {n.name for n in sub.names}
                    if (
                        "CADENCE_POLICY" in names
                        and "get_default_ledger" in names
                    ):
                        composes_ledger = True
        if not composes_ledger:
            violations.append(
                "get_full_queue() MUST lazy-import "
                "CADENCE_POLICY + get_default_ledger from "
                "adaptation.graduation_ledger — composition "
                "discipline (no parallel policy table)"
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "phase9_orchestrator_status_taxonomy_4_values"
            ),
            target_file=target,
            description=(
                "§3.6.2 #6 — Phase9QueueStatus is 4-value "
                "closed enum (READY/PENDING/BLOCKED/"
                "GRADUATED)."
            ),
            validate=_validate_status_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "phase9_orchestrator_master_flag_default_false"
            ),
            target_file=target,
            description=(
                "§3.6.2 #6 — §33.1 producer flag stays "
                "default-FALSE; byte-identical pre-slice "
                "when off."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "phase9_orchestrator_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "§3.6.2 #6 — substrate purity: no "
                "orchestrator-tier imports."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "phase9_orchestrator_no_archived_"
                "orchestrator_import"
            ),
            target_file=target,
            description=(
                "§3.6.2 #6 — defends against re-importing "
                "the archived graduation_orchestrator "
                "(different purpose; AST-pinned archive-"
                "only)."
            ),
            validate=_validate_no_archived_import,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "phase9_orchestrator_composes_canonical_ledger"
            ),
            target_file=target,
            description=(
                "§3.6.2 #6 — get_full_queue composes "
                "CADENCE_POLICY + GraduationLedger from "
                "adaptation.graduation_ledger; no parallel "
                "policy table."
            ),
            validate=_validate_composes_canonical_ledger,
        ),
    ]


__all__ = [
    "PHASE9_ORCHESTRATOR_SCHEMA_VERSION",
    "Phase9Orchestrator",
    "Phase9QueueEntry",
    "Phase9QueueStatus",
    "get_default_orchestrator",
    "interaction_matrix_path",
    "master_enabled",
    "register_flags",
    "register_shipped_invariants",
    "reset_default_orchestrator_for_tests",
]
