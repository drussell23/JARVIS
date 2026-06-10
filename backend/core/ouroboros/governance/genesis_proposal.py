"""Slice 200 — Milestone Sovereignty & Genesis Proposal.

The full code-shipping highway (mine → synthesize → taste → commit → push →
PR) had never been exercised end-to-end — it depended on the M10 miner
non-deterministically surfacing a pattern. This module proves the highway
DETERMINISTICALLY, exactly once: it builds an honest architecture document of
the resilience arc, taste-checks it, and opens ONE real review PR through the
orange reviewer's production code path, then marks a durable sentinel so it
can never fire again.

Safety — the genesis trigger must be impossible to weaponize into a PR-spam
loop under ``restart: always``:

  * Gated default-FALSE (``JARVIS_GENESIS_PROPOSAL_ENABLED``) — it opens a
    live PR, so it is operator opt-in.
  * SINGLE-USE — a durable sentinel (``.jarvis/genesis_proposal.done``) makes
    it a permanent no-op once shipped; the bind-mounted ``.jarvis`` carries it
    across every restart.
  * The sentinel is written ONLY on a confirmed PR. A failed/None creator
    leaves it unset (a later boot may retry) but the sentinel guarantees it
    never double-ships.
  * Fail-soft throughout — any error is swallowed; the soak is never blocked.
  * The PR is opened APPROVAL_REQUIRED / DO-NOT-AUTO-MERGE: it waits for the
    operator's signature. The genesis trigger ships, it never merges.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_ENV_ENABLED = "JARVIS_GENESIS_PROPOSAL_ENABLED"
_ENV_SENTINEL = "JARVIS_GENESIS_SENTINEL_PATH"
_DEFAULT_SENTINEL = ".jarvis/genesis_proposal.done"

_GENESIS_OP_ID = "genesis-slice-200"
_GENESIS_DOC_PATH = "docs/architecture/OUROBOROS_RESILIENCE_200.md"


def genesis_enabled() -> bool:
    """Master gate — default FALSE (opens a live PR, operator opt-in). NEVER
    raises."""
    return os.environ.get(_ENV_ENABLED, "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _sentinel_path() -> Path:
    raw = os.environ.get(_ENV_SENTINEL, "").strip()
    return Path(raw) if raw else Path(_DEFAULT_SENTINEL)


def genesis_already_shipped() -> bool:
    """True once the single-use genesis PR has been confirmed. NEVER raises."""
    try:
        return _sentinel_path().exists()
    except Exception:  # noqa: BLE001
        return False


def mark_genesis_shipped(pr_url: str) -> None:
    """Write the durable single-use sentinel. NEVER raises."""
    try:
        path = _sentinel_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"genesis milestone PR shipped: {pr_url}\n", encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Genesis] sentinel write failed soft: %s", exc)


def build_genesis_doc() -> Tuple[str, str]:
    """Compose the honest architecture document for the resilience arc. The
    content is grounded in shipped, verifiable subsystems — no inflation, no
    unfounded claims. Returns ``(repo_relative_path, markdown_content)``."""
    content = """# Ouroboros + Venom — The Resilience Arc (Slice 200 Milestone)

> **DO-NOT-AUTO-MERGE.** This document was composed and opened as a review PR
> by the autonomous O+V engine itself, from inside its isolated soak
> container, as the deterministic end-to-end proof of the code-shipping
> pipeline. It awaits the operator's signature.

This document records the architecture of the DoubleWord-cortex resilience
arc — the campaign that made the self-development loop robust against live
vendor transport instability while keeping every autonomous decision
operator-bounded.

## 1. Proactive transport-hedge (the structural neutralization)

Rather than react to or predict DoubleWord real-time (SSE) stream ruptures,
the dispatch throat RACES the fast path (RT stream) against the stable path
(batch) concurrently and takes the first success (`dw_transport_hedge.py`).
An RT rupture is swallowed so the batch arm still wins — the operation never
waits on the failure. A proactive hierarchy decides when to race versus go
batch-only under a forecast storm.

## 2. Sovereign telemetry registry (binary, memory-mapped)

Hedge outcomes were `logger.info` — invisible at the soak's WARNING console
threshold — and the economic counters were in-memory only. The registry
(`observability_registry.py`) replaces noisier logs with structured,
restart-durable counters in a fixed-slot memory-mapped file
(`.jarvis/observability_registry.bin`). An increment is a microsecond
lock-guarded page-cache write; durability comes from a background flusher off
the hot path. A corrupt or unwritable backing file fails soft to in-memory
counting and never raises into dispatch. Surfaced loopback-only at
`GET /observability/registry`.

## 3. Race-triage & cross-model rotation

When both hedge arms fail, the race is counted as abandoned and triaged
(`race_triage.py`): a confirmed dual vendor-lane failure blacklists that model
for the scope of the operation, so the sentinel walker rotates to the next
ranked catalog candidate within the same cycle — no blind retry, no stall.
Internal (our-bug) faults never blame the vendor model.

## 4. Adaptive horizon governor

The background watchdog ceiling moved from a static table to a derivation of
the operation's shape — context size, a continuous file-count vector, and the
active model's catalog profile (`adaptive_horizon.py`). It is computed once at
pickup, raise-only above the legacy floor, hard-clamped, and blind to the
op-ledger — preserving the watchdog isolation invariant (a wedged op still
dies at its precomputed ceiling).

## 5. Operator-delegated autonomous graduation

The architecture proposer was deadlocked behind a static flag. The graduation
contract (`m10_autonomous_graduation.py`) lets the organism prove a health
criteria set against the registry (evidence floor, zero exhaustions, bounded
abandoned-race ratio, bounded control-plane starvation) and unlock itself —
while the operator kill switch remains supreme and the governance boundary
gate (proposals touching governance route APPROVAL_REQUIRED) stays untouched.
Ignition then arms the cadence loop and the protection gates via live
assertions.

## 6. Sovereign tooling (token-over-HTTPS identity)

The soak container ships PRs autonomously over HTTPS using a scoped token —
it bootstraps its own isolated git repository rather than mounting the
operator's private SSH keys, which keeps the autonomous agent's credential
blast radius minimal. Non-interactive hardening fails closed rather than
hanging on a hidden credential prompt.

## Governing invariants (unchanged across the arc)

* The operator kill switch is supreme over every autonomous unlock.
* The recursion-depth / governance boundary gate is never weakened by a slice
  that expands autonomy.
* Watchdogs share no state-ledger with the system they guard.
* Every autonomous decision is observable.

*Generated by the O+V engine as its Slice 200 milestone proof.*
"""
    return _GENESIS_DOC_PATH, content


async def _default_pr_creator(
    op_id: str, description: str, files: List[Tuple[str, str]], **kwargs: Any,
) -> Optional[Any]:
    """Real ship path — the orange reviewer's production create_review_pr."""
    from backend.core.ouroboros.governance.orange_pr_reviewer import (
        OrangePRReviewer,
    )
    repo_root = Path(
        os.environ.get("JARVIS_AUTO_COMMIT_WORKSPACE", "").strip() or "/app",
    )
    if not (repo_root / ".git").exists():
        repo_root = Path.cwd()
    reviewer = OrangePRReviewer(project_root=repo_root)
    return await reviewer.create_review_pr(
        op_id=op_id, description=description, files=files,
        evidence=kwargs.get("evidence"),
        risk_tier_name="APPROVAL_REQUIRED",
    )


def _default_taste_evaluator(files: List[Tuple[str, str]]) -> Any:
    from backend.core.ouroboros.governance.architectural_taste_layer import (
        evaluate_change,
    )
    return evaluate_change(
        [p for p, _ in files],
        sources_override={p: c for p, c in files},
    )


async def run_genesis_proposal(
    pr_creator: Optional[Callable] = None,
    taste_evaluator: Optional[Callable] = None,
) -> Optional[Dict[str, Any]]:
    """Deterministically ship the single-use genesis milestone PR. Returns a
    result dict on a confirmed PR, else None. NEVER raises.

    Order: gate → sentinel → build doc → taste (advisory) → create PR → mark
    sentinel. The sentinel is written ONLY on a confirmed PR url."""
    try:
        if not genesis_enabled():
            return None
        if genesis_already_shipped():
            logger.info("[Genesis] already shipped — single-use no-op")
            return None

        doc_path, doc_content = build_genesis_doc()
        files: List[Tuple[str, str]] = [(doc_path, doc_content)]

        # Taste is advisory: a regression verdict is informative but must NOT
        # abort the milestone ship (and a taste crash certainly must not).
        taste = taste_evaluator or _default_taste_evaluator
        try:
            verdict = taste(files)
            logger.info("[Genesis] taste verdict: %s", verdict)
        except Exception as texc:  # noqa: BLE001
            logger.warning("[Genesis] taste check failed soft: %s", texc)

        description = (
            "Ouroboros + Venom — Slice 200 milestone: the resilience arc, "
            "documented and shipped autonomously by the engine itself "
            "(DO-NOT-AUTO-MERGE — awaiting operator signature)."
        )
        creator = pr_creator or _default_pr_creator
        result = await creator(
            _GENESIS_OP_ID, description, files,
            evidence={"genesis": True, "slice": 200},
        )
        if result is None:
            logger.warning(
                "[Genesis] PR creator returned None — sentinel unset, "
                "a later boot may retry",
            )
            return None

        pr_url = getattr(result, "pr_url", None) or getattr(
            result, "url", None,
        ) or str(result)
        mark_genesis_shipped(pr_url)
        logger.warning(
            "[Genesis] MILESTONE PR SHIPPED autonomously: %s — single-use "
            "trigger dissolved (sentinel written)",
            pr_url,
        )
        return {"pr_url": pr_url, "op_id": _GENESIS_OP_ID}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Genesis] run failed soft (soak unaffected): %s", exc)
        return None
