"""Proposal inbox — model-proposed work, sanctioned the way signed goals are.

The organism has three producers of work it thought of itself: the Epistemic
Planner (north-star gaps), the DreamEngine (blueprints, through the conception
bridge) and the IntentDiscoverySensor. Each routed its proposals into the
intake router with ``requires_human_ack=True``, where they parked in
``pending_ack`` -- and in an unattended organism, parked forever (the bridge
even counted ``pending_ack`` as routed and never offered them again).

The operator's decision (2026-10-09): such work counts as sanctioned the same
way a signed roadmap goal does. "The same way" is the whole design. There is
no second sanction path here:

* a producer deposits a :class:`Proposal` (:func:`route_proposal`);
* Goal Discovery reads the inbox as one more SOURCE of ``DiscoveredWork``
  (``kind="model_proposal"``), so every refusal the cage already applies to
  discovered work applies unchanged -- the governance substrate is never a
  target, cooling and settled targets are skipped, undispatchable work is held
  out, and proposals rank BELOW evidence-backed work (a red test or an
  uncovered module) because a proposal is a hypothesis, not a fact;
* the Sentinel signs the chosen work through ``synthesize_and_sign`` -- the
  one signer the CLI, ``/goal`` and discovery already use -- and from there
  the op meets the same Iron Gate, VALIDATE, SemanticGuardian and risk tier
  as any other.

With ``JARVIS_AUTONOMOUS_PROPOSAL_SANCTION_ENABLED`` off, :func:`route_proposal`
takes the legacy human-ack route, unchanged.

Durable (one JSONL file, cross-process locked), bounded, deduplicated and
expiring: a proposal nobody acted on within its TTL was about a tree that has
since moved on. NEVER raises.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("Ouroboros.ProposalInbox")

_ENV_SANCTION = "JARVIS_AUTONOMOUS_PROPOSAL_SANCTION_ENABLED"
_ENV_PATH = "JARVIS_PROPOSAL_INBOX_PATH"
_ENV_TTL_S = "JARVIS_PROPOSAL_TTL_S"
_ENV_MAX = "JARVIS_PROPOSAL_INBOX_MAX"
_DEFAULT_TTL_S = 86400.0
_DEFAULT_MAX = 64

#: Results that mean "the proposal is in the organism's hands".
INBOXED = "inboxed"
DUPLICATE = "duplicate"


def sanction_enabled() -> bool:
    raw = (os.environ.get(_ENV_SANCTION, "") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _positive(env: str, default: float) -> float:
    try:
        value = float(os.environ.get(env, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


def ttl_s() -> float:
    return _positive(_ENV_TTL_S, _DEFAULT_TTL_S)


def max_entries() -> int:
    return int(_positive(_ENV_MAX, _DEFAULT_MAX))


def inbox_path(repo_root: Any) -> Path:
    raw = (os.environ.get(_ENV_PATH, "") or "").strip()
    return Path(raw) if raw else Path(repo_root) / ".jarvis" / "autonomy" / "proposal_inbox.jsonl"


def _resolved(path: Path) -> Path:
    try:
        from backend.core.ouroboros.governance.workspace_resolver import resolve_durable_path
        return Path(resolve_durable_path(path))
    except Exception:  # noqa: BLE001
        return path


@dataclass(frozen=True)
class Proposal:
    """One unit of model-proposed work and the evidence it was made from."""

    producer: str                     # intent_discovery | epistemic_planner | dream_engine | ...
    target_file: str
    description: str
    rationale: str = ""
    score: float = 0.5                # the producer's grounded value, 0..1
    evidence: Dict[str, Any] = field(default_factory=dict)
    created_unix: float = 0.0

    @property
    def key(self) -> str:
        """Same target + same task = same proposal, whoever proposed it."""
        basis = f"{self.target_file.strip()}\n{' '.join(self.description.split()).lower()}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _read(path: Path) -> List[Proposal]:
    out: List[Proposal] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return out
    for line in lines:
        try:
            raw = json.loads(line)
            out.append(Proposal(**{k: raw[k] for k in raw if k in Proposal.__dataclass_fields__}))
        except Exception:  # noqa: BLE001 -- one bad line never poisons the inbox
            continue
    return out


def _live(items: Sequence[Proposal], now: float) -> List[Proposal]:
    """Unexpired, newest first, one per key, bounded."""
    horizon = now - ttl_s()
    seen: Dict[str, Proposal] = {}
    for p in sorted(items, key=lambda p: -p.created_unix):
        if p.created_unix >= horizon and p.key not in seen:
            seen[p.key] = p
    return list(seen.values())[: max_entries()]


def pending(repo_root: Any, *, now: Optional[float] = None) -> List[Proposal]:
    """The proposals still awaiting the organism's decision."""
    return _live(_read(_resolved(inbox_path(repo_root))), now if now is not None else time.time())


def _is_governance(target: str) -> bool:
    from backend.core.ouroboros.governance.autonomy.goal_discovery import _is_governance as rule
    return rule(target)


def submit(proposal: Proposal, repo_root: Any, *, now: Optional[float] = None) -> str:
    """Deposit *proposal*. Returns ``inboxed``, ``duplicate`` or a
    ``refused_*`` reason. The cage's un-signable floor applies at the door
    too: a governance target is refused here, not merely later."""
    try:
        target = (proposal.target_file or "").replace("\\", "/").strip()
        if not target:
            return "refused_unscoped"
        if not (proposal.description or "").strip():
            return "refused_empty"
        if _is_governance(target):
            return "refused_governance"
        when = now if now is not None else time.time()
        item = Proposal(
            producer=proposal.producer, target_file=target,
            description=proposal.description.strip(),
            rationale=(proposal.rationale or "").strip(),
            score=max(0.0, min(1.0, float(proposal.score))),
            evidence=dict(proposal.evidence or {}), created_unix=when,
        )
        path = _resolved(inbox_path(repo_root))
        from backend.core.ouroboros.governance.cross_process_jsonl import flock_critical_section
        with flock_critical_section(path) as locked:
            if not locked:
                return "refused_lock_timeout"
            current = _live(_read(path), when)
            if any(p.key == item.key for p in current):
                return DUPLICATE
            kept = [item] + current
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                "".join(json.dumps(asdict(p), sort_keys=True) + "\n"
                        for p in kept[: max_entries()]),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        logger.info(
            "[ProposalInbox] %s proposed %s (score=%.2f): %s",
            item.producer, target, item.score, item.description[:160],
        )
        return INBOXED
    except Exception:  # noqa: BLE001
        logger.debug("[ProposalInbox] submit degraded", exc_info=True)
        return "refused_error"


async def route_proposal(
    *,
    router: Any,
    repo_root: Any,
    proposal: Proposal,
    envelope_factory: Any,
) -> str:
    """THE routing decision for every producer of model-proposed work.

    Sanction on: the proposal joins the inbox and is signed through the same
    path as any discovered work. Sanction off: ``envelope_factory()`` builds
    the producer's legacy ``requires_human_ack`` envelope and the router gets
    it, unchanged. With no known repository there is no inbox to file into,
    and the legacy route is taken rather than a guessed location."""
    if sanction_enabled() and repo_root is not None:
        import asyncio
        result = await asyncio.to_thread(submit, proposal, repo_root)
    else:
        result = str(await router.ingest(envelope_factory()))
    from backend.core.ouroboros.governance.proof_of_work import emit_evidence
    emit_evidence(
        f"proposal:{proposal.producer}", result,
        detail=f"{proposal.target_file}: {proposal.description[:120]}",
    )
    return result


__all__ = [
    "DUPLICATE", "INBOXED", "Proposal", "inbox_path", "max_entries",
    "pending", "route_proposal", "sanction_enabled", "submit", "ttl_s",
]
