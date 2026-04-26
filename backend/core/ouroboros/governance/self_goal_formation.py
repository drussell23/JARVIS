"""P1 Slice 2 — SelfGoalFormationEngine (Curiosity Engine v2).

Orchestrates the model writing self-formed backlog entries when POSTMORTEM
clusters reveal a recurring pattern. **The line between automation
("does what the operator wrote") and autonomy ("forms its own intent")**
per OUROBOROS_VENOM_PRD.md §9 Phase 2 P1.

Decision tree (every check is a hard gate; failure short-circuits to None):

  1. Master flag — ``JARVIS_SELF_GOAL_FORMATION_ENABLED`` (default false).
  2. Posture veto — only EXPLORE / CONSOLIDATE proceed. HARDEN / MAINTAIN
     return None (DirectionInferrer veto, PRD §9 P1 edge case).
  3. Per-session cap — ``JARVIS_SELF_GOAL_PER_SESSION_CAP`` (default 1).
     Strict cap on proposals emitted per process lifetime.
  4. Cost cap — ``JARVIS_SELF_GOAL_COST_CAP_USD`` (default 0.10).
     Engine refuses to call the model if accumulated session cost would
     exceed the cap. Per-call cost is reported by the injected ``model_caller``.
  5. Cluster discovery — ``cluster_postmortems`` (Slice 1) with
     configurable min_cluster_size (default 3).
  6. Blocklist dedup — drops any cluster whose signature_hash is already
     in the operator-provided blocklist. Prevents the "infinite loop"
     failure mode from PRD §9 P1.
  7. Model call — best-effort. Any exception is swallowed; engine returns
     None. The model_caller is dependency-injected so this module never
     imports providers — preserves authority-free contract.
  8. Persist — append a ``ProposalDraft`` row to the JSONL ledger at
     ``.jarvis/self_goal_formation_proposals.jsonl`` for operator audit
     BEFORE any backlog write happens (Slice 3 wires the BacklogSensor).

Authority invariants (PRD §12.2 / Manifesto §1 Boundary):

  * **No authority imports** — orchestrator / policy / iron_gate /
    risk_tier / change_engine / candidate_generator / gate /
    semantic_guardian. The model_caller is a callable, never an
    imported provider class.
  * **No backlog write** — this slice only emits ``ProposalDraft`` and
    persists to the read-only audit ledger. Slice 3 wires the backlog
    side; that gives operators a chance to inspect the audit trail
    BEFORE the backlog cycle absorbs proposals.
  * **No FSM mutation** — engine is a pure side-effect-free decision
    layer modulo the JSONL append + the in-process counter.
  * **Best-effort everywhere** — any failure (model exception, ledger
    write failure, parse error) returns None and emits a DEBUG breadcrumb.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

from backend.core.ouroboros.governance.postmortem_clusterer import (
    DEFAULT_MIN_CLUSTER_SIZE,
    ProposalCandidate,
    cluster_postmortems,
    is_signature_in_blocklist,
)
from backend.core.ouroboros.governance.postmortem_recall import PostmortemRecord
from backend.core.ouroboros.governance.posture import Posture

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


_TRUTHY = ("1", "true", "yes", "on")

# Default configuration constants — pinned by tests so changes are
# explicit + reviewable. Per PRD §9 P1 edge case constraints.
DEFAULT_PER_SESSION_CAP: int = 1
DEFAULT_COST_CAP_USD: float = 0.10
DEFAULT_LEDGER_FILENAME: str = "self_goal_formation_proposals.jsonl"
PROPOSAL_SCHEMA_VERSION: str = "self_goal_formation.1"


def is_enabled() -> bool:
    """Master flag — ``JARVIS_SELF_GOAL_FORMATION_ENABLED`` (default ``true``).

    GRADUATED 2026-04-26 (Slice 5). Default: **``true``** post-graduation.
    Layered evidence on the graduation PR:
      * Slice 1 — postmortem_clusterer (28 tests, deterministic + dedup
        + signature_hash stability)
      * Slice 2 — engine (40 tests, every gate of the 9-gate decision
        tree pinned positive + negative)
      * Slice 3 — BacklogSensor consumer (26 tests, default-off second
        source + bounded ≤5/scan + dedup)
      * Slice 4 — REPL operator-review surface (35 tests, idempotent
        approve/reject + audit trail)
      * Slice 5 — graduation pin suite + in-process live-fire smoke +
        end-to-end reachability supplement
      * 130+ deterministic regression tests across the P1 stack

    Hot-revert: ``export JARVIS_SELF_GOAL_FORMATION_ENABLED=false`` makes
    ``evaluate`` return None immediately — byte-for-byte pre-graduation
    behavior. Bounded-by-construction safety stack (per-session cap=1,
    cost cap=$0.10, posture veto, blocklist dedup, operator-review tier)
    remains in force regardless of flag state — the flag controls only
    whether the engine attempts to evaluate at all."""
    return os.environ.get(
        "JARVIS_SELF_GOAL_FORMATION_ENABLED", "true",
    ).strip().lower() in _TRUTHY


def per_session_cap() -> int:
    """Strict cap on proposals per process lifetime.

    Defaults to 1 (PRD §9 P1: "compared to 3 ask_human in W2(4)").
    Negative values clamp to 0 (effectively disable). Invalid values
    fall back to default."""
    raw = os.environ.get("JARVIS_SELF_GOAL_PER_SESSION_CAP")
    if raw is None:
        return DEFAULT_PER_SESSION_CAP
    try:
        v = int(raw)
        return max(0, v)
    except (TypeError, ValueError):
        return DEFAULT_PER_SESSION_CAP


def cost_cap_usd() -> float:
    """Per-session cumulative cost cap in USD.

    Default 0.10 per PRD §9 P1. Negative values clamp to 0.0 (effectively
    disable). Invalid values fall back to default."""
    raw = os.environ.get("JARVIS_SELF_GOAL_COST_CAP_USD")
    if raw is None:
        return DEFAULT_COST_CAP_USD
    try:
        v = float(raw)
        return max(0.0, v)
    except (TypeError, ValueError):
        return DEFAULT_COST_CAP_USD


def min_cluster_size_override() -> int:
    """Engine-side override for the clusterer's min_cluster_size.

    Defaults to the clusterer's own DEFAULT_MIN_CLUSTER_SIZE (3) per
    PRD §9 P1 ("3+ similar failures"). Operators can tighten or relax
    via env without a code change."""
    raw = os.environ.get("JARVIS_SELF_GOAL_MIN_CLUSTER_SIZE")
    if raw is None:
        return DEFAULT_MIN_CLUSTER_SIZE
    try:
        v = int(raw)
        return max(2, v)
    except (TypeError, ValueError):
        return DEFAULT_MIN_CLUSTER_SIZE


# Postures that are PERMITTED to propose self-formed goals. Per PRD §9
# P1: "Posture must be EXPLORE or CONSOLIDATE". HARDEN / MAINTAIN veto.
_PERMITTED_POSTURES: Tuple[Posture, ...] = (Posture.EXPLORE, Posture.CONSOLIDATE)


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProposalDraft:
    """A self-formed backlog entry proposed by the engine.

    Persists to the JSONL ledger. Slice 3 will wire BacklogSensor to
    consume this shape and emit a real backlog.json entry tagged
    ``auto_proposed=true`` for operator review.

    Attributes
    ----------
    schema_version:
        Frozen at ``"self_goal_formation.1"``. Future bumps require
        explicit migration semantics + ledger-format pin update.
    signature_hash:
        sha256[:12] of the ``ClusterSignature`` that triggered this
        proposal. Used by the engine itself to dedup against recently
        proposed signatures (in-process), and by Slice 3+ as the
        cross-session blocklist key.
    cluster_member_count:
        How many distinct ops the triggering cluster contained.
    target_files:
        Files most recurrently mentioned across cluster members. Capped
        at 30 by the clusterer.
    dominant_next_safe_action:
        Plurality-vote action from cluster members. May be empty.
    description:
        Model-written one-line summary of the proposed investigation.
    rationale:
        Model-written multi-sentence justification + linked evidence.
    posture_at_proposal:
        Posture value that was active when the engine proceeded
        (always one of EXPLORE / CONSOLIDATE).
    cost_usd_spent:
        Cost of the model call that produced this draft. Bounded by
        ``cost_cap_usd()``.
    timestamp_unix:
        When the engine emitted this draft.
    auto_proposed:
        Always ``True`` for engine output. Carried through to
        ``backlog.json`` in Slice 3 so BacklogSensor can flag the entry
        as awaiting operator review.
    """

    schema_version: str
    signature_hash: str
    cluster_member_count: int
    target_files: Tuple[str, ...]
    dominant_next_safe_action: str
    description: str
    rationale: str
    posture_at_proposal: str
    cost_usd_spent: float
    timestamp_unix: float
    auto_proposed: bool = True

    def to_ledger_dict(self) -> dict:
        d = asdict(self)
        d["target_files"] = list(self.target_files)
        return d


# Type alias: a callable that takes (prompt: str, max_cost_usd: float)
# and returns (response_text: str, cost_usd: float). Engine never imports
# a provider directly — keeps authority-free contract.
ModelCaller = Callable[[str, float], Tuple[str, float]]


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class SelfGoalFormationEngine:
    """Orchestrates self-formed-goal proposal generation.

    Per-process state (counter + cumulative cost) is held on the instance,
    so callers should reuse one engine for the lifetime of a session and
    call ``reset_session_state()`` only at session boundaries.

    Parameters
    ----------
    project_root:
        Repository root. Used to locate the JSONL ledger directory.
    ledger_path:
        Optional explicit path override for the proposals ledger.
        Defaults to ``project_root/.jarvis/self_goal_formation_proposals.jsonl``.
    """

    def __init__(
        self,
        project_root: Path,
        ledger_path: Optional[Path] = None,
    ) -> None:
        self._root = Path(project_root).resolve()
        self._ledger_path = (
            Path(ledger_path).resolve()
            if ledger_path is not None
            else self._root / ".jarvis" / DEFAULT_LEDGER_FILENAME
        )
        self._lock = threading.Lock()
        self._proposals_emitted: int = 0
        self._cost_spent_usd: float = 0.0
        # In-process blocklist of signatures we've already proposed this
        # session — a belt-and-suspenders complement to whatever
        # cross-session blocklist the caller passes.
        self._inprocess_blocklist: List[str] = []

    # ---- session state ----

    def reset_session_state(self) -> None:
        """Reset per-session counters + in-process blocklist.

        Intended for explicit session-boundary use (and tests). Does not
        truncate the JSONL ledger — that is the persistent audit trail."""
        with self._lock:
            self._proposals_emitted = 0
            self._cost_spent_usd = 0.0
            self._inprocess_blocklist = []

    @property
    def proposals_emitted(self) -> int:
        return self._proposals_emitted

    @property
    def cost_spent_usd(self) -> float:
        return self._cost_spent_usd

    @property
    def ledger_path(self) -> Path:
        return self._ledger_path

    # ---- public API ----

    def evaluate(
        self,
        *,
        postmortems: Iterable[PostmortemRecord],
        posture: Posture,
        model_caller: ModelCaller,
        blocklist_hashes: Sequence[str] = (),
    ) -> Optional[ProposalDraft]:
        """Run the full decision tree. Returns a ``ProposalDraft`` on
        success, ``None`` on any short-circuit (gate failure, no clusters,
        all clusters blocklisted, model error, persist failure).

        Always best-effort: never raises. All short-circuits emit a
        DEBUG breadcrumb so live cadence can audit decisions per cycle."""
        if not is_enabled():
            logger.debug("[SelfGoalFormation] short_circuit reason=master_flag_off")
            return None

        if posture not in _PERMITTED_POSTURES:
            logger.debug(
                "[SelfGoalFormation] short_circuit reason=posture_veto posture=%s",
                getattr(posture, "value", str(posture)),
            )
            return None

        cap = per_session_cap()
        if self._proposals_emitted >= cap:
            logger.debug(
                "[SelfGoalFormation] short_circuit reason=per_session_cap "
                "emitted=%d cap=%d",
                self._proposals_emitted, cap,
            )
            return None

        cost_cap = cost_cap_usd()
        if self._cost_spent_usd >= cost_cap:
            logger.debug(
                "[SelfGoalFormation] short_circuit reason=cost_cap "
                "spent=%.4f cap=%.4f",
                self._cost_spent_usd, cost_cap,
            )
            return None
        budget_remaining = max(0.0, cost_cap - self._cost_spent_usd)

        clusters = cluster_postmortems(
            postmortems,
            min_cluster_size=min_cluster_size_override(),
        )
        if not clusters:
            logger.debug(
                "[SelfGoalFormation] short_circuit reason=no_clusters",
            )
            return None

        # Filter: drop blocklisted signatures (cross-session + in-process).
        full_blocklist = list(blocklist_hashes) + list(self._inprocess_blocklist)
        eligible: List[ProposalCandidate] = []
        for c in clusters:
            if is_signature_in_blocklist(c.signature, full_blocklist):
                continue
            eligible.append(c)
        if not eligible:
            logger.debug(
                "[SelfGoalFormation] short_circuit reason=all_clusters_blocklisted "
                "n_clusters=%d",
                len(clusters),
            )
            return None

        # Highest-recurrence cluster first (clusterer already sorted).
        chosen = eligible[0]

        prompt = self._build_prompt(chosen, posture)

        try:
            response_text, call_cost_usd = model_caller(prompt, budget_remaining)
        except Exception:  # noqa: BLE001 — engine must never raise
            logger.debug(
                "[SelfGoalFormation] short_circuit reason=model_caller_exception",
                exc_info=True,
            )
            return None

        # Defensive: clamp cost to non-negative + accumulate.
        call_cost_usd = max(0.0, float(call_cost_usd or 0.0))
        with self._lock:
            self._cost_spent_usd += call_cost_usd

        description, rationale = self._parse_proposal(response_text)
        if not description:
            logger.debug(
                "[SelfGoalFormation] short_circuit reason=model_returned_empty "
                "cost_usd=%.4f",
                call_cost_usd,
            )
            return None

        draft = ProposalDraft(
            schema_version=PROPOSAL_SCHEMA_VERSION,
            signature_hash=chosen.signature.signature_hash(),
            cluster_member_count=chosen.member_count,
            target_files=chosen.target_files_union,
            dominant_next_safe_action=chosen.dominant_next_safe_action,
            description=description,
            rationale=rationale,
            posture_at_proposal=posture.value,
            cost_usd_spent=call_cost_usd,
            timestamp_unix=time.time(),
            auto_proposed=True,
        )

        # Persist to ledger (best-effort, never blocks). Doing this
        # BEFORE counter increment so a ledger-write failure still leaves
        # the budget available for retry.
        if not self._persist(draft):
            logger.debug(
                "[SelfGoalFormation] short_circuit reason=ledger_persist_failed "
                "signature_hash=%s",
                draft.signature_hash,
            )
            return None

        with self._lock:
            self._proposals_emitted += 1
            self._inprocess_blocklist.append(draft.signature_hash)

        # Telemetry per PRD §9 P1 contract.
        logger.info(
            "[SelfGoalFormation] op=engine analyzed=%d clusters → "
            "proposed entry signature=%s description=%r cost=$%.4f "
            "posture=%s emitted_so_far=%d/%d",
            len(clusters), draft.signature_hash, draft.description[:80],
            draft.cost_usd_spent, draft.posture_at_proposal,
            self._proposals_emitted, cap,
        )
        return draft

    # ---- internals ----

    def _build_prompt(
        self, candidate: ProposalCandidate, posture: Posture,
    ) -> str:
        """Render the LLM prompt for one proposal candidate.

        Pure-string shape — no model invocation here. Pinned by source-grep
        so the prompt structure stays reviewable + reproducible."""
        files = ", ".join(candidate.target_files_union[:8])
        if len(candidate.target_files_union) > 8:
            files += f" (+{len(candidate.target_files_union) - 8} more)"
        action_line = (
            f"\n  Suggested next-safe-action (from cluster vote): "
            f"{candidate.dominant_next_safe_action}"
            if candidate.dominant_next_safe_action
            else ""
        )
        return (
            "You are the SelfGoalFormationEngine of an autonomous "
            "self-development engine. A POSTMORTEM cluster has surfaced "
            "a recurring failure pattern. Propose ONE backlog entry to "
            "investigate the root cause.\n\n"
            f"Posture: {posture.value} (system is in a {posture.value} posture; "
            "your proposal must align with that direction).\n\n"
            "Cluster summary:\n"
            f"  Recurrence: {candidate.member_count} similar failed ops\n"
            f"  Phase: {candidate.signature.failed_phase}\n"
            f"  Representative root_cause: {candidate.representative_root_cause[:200]}\n"
            f"  Target files: {files}"
            f"{action_line}\n\n"
            "Respond as STRICT JSON with two fields:\n"
            '  {"description": "<one-line backlog entry>", '
            '"rationale": "<2-3 sentences citing the recurrence count + '
            'root cause + suggested investigation>"}\n\n'
            "Constraints:\n"
            "  - description MUST be ≤ 120 characters.\n"
            "  - rationale MUST cite the specific recurrence count.\n"
            "  - DO NOT propose a destructive action — investigation only.\n"
            "  - DO NOT include backticks or markdown fences in the JSON.\n"
        )

    def _parse_proposal(self, response_text: str) -> Tuple[str, str]:
        """Parse the model's JSON response into (description, rationale).

        Tolerates surrounding whitespace + accidental markdown fences.
        Returns ("", "") on any parse failure — engine treats that as a
        short-circuit + records zero proposal."""
        if not response_text:
            return ("", "")
        text = response_text.strip()
        # Tolerate accidental ```json ... ``` fences.
        if text.startswith("```"):
            lines = text.splitlines()
            if len(lines) >= 3:
                text = "\n".join(lines[1:-1]).strip()
        # Tolerate leading/trailing prose (find first { and last })
        first_brace = text.find("{")
        last_brace = text.rfind("}")
        if first_brace == -1 or last_brace == -1 or last_brace <= first_brace:
            return ("", "")
        candidate_json = text[first_brace : last_brace + 1]
        try:
            data = json.loads(candidate_json)
        except (json.JSONDecodeError, ValueError):
            return ("", "")
        if not isinstance(data, dict):
            return ("", "")
        description = str(data.get("description", "")).strip()[:200]
        rationale = str(data.get("rationale", "")).strip()[:1000]
        if not description:
            return ("", "")
        return (description, rationale)

    def _persist(self, draft: ProposalDraft) -> bool:
        """Append draft to the JSONL ledger. Best-effort, never raises.

        Returns True on successful write, False otherwise."""
        try:
            self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with self._ledger_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(draft.to_ledger_dict()) + "\n")
            return True
        except OSError:
            return False


# ---------------------------------------------------------------------------
# Default-singleton accessor
# ---------------------------------------------------------------------------


_default_engine: Optional[SelfGoalFormationEngine] = None
_default_engine_lock = threading.Lock()


def get_default_engine(
    project_root: Optional[Path] = None,
) -> Optional[SelfGoalFormationEngine]:
    """Return the process-wide ``SelfGoalFormationEngine``.

    Lazily constructs on first call. Returns ``None`` when the master
    flag is off so callers can short-circuit cleanly (mirrors the
    PostmortemRecallService accessor pattern)."""
    if not is_enabled():
        return None
    global _default_engine
    with _default_engine_lock:
        if _default_engine is None:
            root = Path(project_root) if project_root else Path.cwd()
            _default_engine = SelfGoalFormationEngine(project_root=root)
    return _default_engine


def reset_default_engine() -> None:
    """Reset the singleton — for tests and config reload."""
    global _default_engine
    with _default_engine_lock:
        _default_engine = None


__all__ = [
    "DEFAULT_PER_SESSION_CAP",
    "DEFAULT_COST_CAP_USD",
    "DEFAULT_LEDGER_FILENAME",
    "PROPOSAL_SCHEMA_VERSION",
    "ModelCaller",
    "ProposalDraft",
    "SelfGoalFormationEngine",
    "is_enabled",
    "per_session_cap",
    "cost_cap_usd",
    "min_cluster_size_override",
    "get_default_engine",
    "reset_default_engine",
]
