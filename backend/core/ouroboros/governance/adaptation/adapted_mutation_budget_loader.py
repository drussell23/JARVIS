"""Phase 7.3 — ScopedToolBackend per-Order mutation budget adapted loader.

Per `OUROBOROS_VENOM_PRD.md` §3.6 + §9 Phase 7.3:

  > Pass C Slice 4a proposes lowering per-Order mutation budgets
  > (over-budget waste = wasted attack surface). ScopedToolBackend
  > doesn't read `.jarvis/adapted_mutation_budgets.yaml` at boot.
  > Phase 7.3 closes that activation gap.

This module bridges Pass C's AdaptationLedger (operator-approved
budget-lowering proposals from Slice 4a's
`per_order_mutation_budget.py`) into the live mutation-budget
COUNT gate. Default-off + best-effort: when the env flag is off OR
the YAML is missing/malformed, the loader returns an empty mapping
and `compute_effective_max_mutations` returns the env default
unchanged (byte-identical pre-Phase-7.3 behavior).

## Design constraints (load-bearing)

  * **Adapted budgets can only LOWER** (per Pass C §4.1 — Pass C
    is one-way tighten-only; loosening goes through Pass B
    `/order2 amend`). Defense-in-depth: `compute_effective_max_
    mutations(order, env_default)` always returns
    `min(env_default, adapted_budget)` even if a YAML typo
    accidentally proposed a higher number.
  * **Per-Order keying.** YAML entries map an Order int (1 or 2)
    to a non-negative budget int. Order=2 hard-floored at
    MIN_ORDER2_BUDGET=1 (mirrors Slice 4a's miner cage rule —
    Pass C never proposes a non-functional budget).
  * **Stdlib + adaptation.ledger import surface only.** Same cage
    discipline as the rest of `adaptation/`. Does NOT import
    `scoped_tool_backend.py` (one-way: caller imports THIS, not
    the reverse — same pattern as Slice 7.1 + 7.2).
  * **Fail-open**: every error path returns an empty dict and the
    helper falls back to env_default.

## Default-off

`JARVIS_SCOPED_TOOL_BACKEND_LOAD_ADAPTED_BUDGETS` (default false).

## YAML schema

The file `.jarvis/adapted_mutation_budgets.yaml`:

```yaml
schema_version: 1
budgets:
  - order: 2
    budget: 1
    proposal_id: adapt-mb-...
    approved_at: "2026-..."
    approved_by: "alice"
  - order: 1
    budget: 5
    proposal_id: adapt-mb-...
    approved_at: "2026-..."
    approved_by: "alice"
```

Each entry:
  * `order`: must be 1 or 2 (Order-2 = governance-mutating ops);
    unknown orders are SKIPPED (with logged warning).
  * `budget`: non-negative int; floats truncated; negatives
    SKIPPED. Order-2 floor MIN_ORDER2_BUDGET=1 enforced (matches
    Slice 4a miner). Budgets exceeding MAX_BUDGET_VALUE clamped.
  * provenance fields: `proposal_id`, `approved_at`, `approved_by`.

## Effective-budget helper

`compute_effective_max_mutations(order: int, env_default: int) -> int`

  * Loader OFF → returns env_default unchanged.
  * Loader ON, no adapted entry for `order` → returns env_default.
  * Loader ON, adapted entry present → returns
    `min(env_default, adapted_budget)`.
  * env_default < 0 normalized to 0 (defensive).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Soft cap on the number of adapted budgets loaded. Pass C only
# defines two Orders today (1 and 2); 8 is generous future-proofing
# without permitting runaway YAML.
MAX_ADAPTED_BUDGETS: int = 8

# Hard cap on YAML file size we'll attempt to load.
MAX_YAML_BYTES: int = 4 * 1024 * 1024

# Per-Order budget cap. Slice 4a's miner is bounded by max-observed
# usage; cumulative observations across many cycles couldn't in
# theory propose a ridiculous number, but operator-typo runaway
# is defended here.
MAX_BUDGET_VALUE: int = 64

# Order-2 hard floor — matches Slice 4a miner's MIN_ORDER2_BUDGET
# constant. Pass C never proposes a non-functional Order-2 budget
# (0 would prevent any Order-2 op from making any change).
MIN_ORDER2_BUDGET: int = 1

# Known Order values. Kept here as the canonical allowlist so the
# loader can validate without coupling to upstream (one-way
# dependency rule per Slice 7.1 + 7.2 pattern).
_KNOWN_ORDERS: FrozenSet[int] = frozenset({1, 2})


def is_loader_enabled() -> bool:
    """Master flag — ``JARVIS_SCOPED_TOOL_BACKEND_LOAD_ADAPTED_BUDGETS``
    (default false until Phase 7.3 graduation)."""
    return os.environ.get(
        "JARVIS_SCOPED_TOOL_BACKEND_LOAD_ADAPTED_BUDGETS", "",
    ).strip().lower() in _TRUTHY


def adapted_budgets_path() -> Path:
    """Return the YAML path. Env-overridable via
    ``JARVIS_ADAPTED_MUTATION_BUDGETS_PATH``; defaults to
    ``.jarvis/adapted_mutation_budgets.yaml`` under cwd."""
    raw = os.environ.get("JARVIS_ADAPTED_MUTATION_BUDGETS_PATH")
    if raw:
        return Path(raw)
    return Path(".jarvis") / "adapted_mutation_budgets.yaml"


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdaptedBudgetEntry:
    """One adapted per-Order budget record loaded from YAML."""

    order: int
    budget: int
    proposal_id: str
    approved_at: str
    approved_by: str


# ---------------------------------------------------------------------------
# YAML reader
# ---------------------------------------------------------------------------


def _parse_entry(
    raw: Dict[str, Any], idx: int,
) -> Optional[AdaptedBudgetEntry]:
    """Parse one YAML entry. Returns None on missing/invalid fields."""
    raw_order = raw.get("order")
    if raw_order is None:
        logger.debug(
            "[AdaptedMutationBudgetLoader] entry %d missing order — skip",
            idx,
        )
        return None
    try:
        order = int(raw_order)
    except (TypeError, ValueError):
        logger.debug(
            "[AdaptedMutationBudgetLoader] entry %d non-integer order — skip",
            idx,
        )
        return None
    if order not in _KNOWN_ORDERS:
        logger.warning(
            "[AdaptedMutationBudgetLoader] entry %d unknown order=%s "
            "(known: %s) — skip",
            idx, order, sorted(_KNOWN_ORDERS),
        )
        return None
    raw_budget = raw.get("budget")
    if raw_budget is None:
        logger.debug(
            "[AdaptedMutationBudgetLoader] entry %d order=%d missing "
            "budget — skip", idx, order,
        )
        return None
    try:
        budget = int(raw_budget)
    except (TypeError, ValueError):
        logger.debug(
            "[AdaptedMutationBudgetLoader] entry %d order=%d non-integer "
            "budget — skip", idx, order,
        )
        return None
    if budget < 0:
        logger.debug(
            "[AdaptedMutationBudgetLoader] entry %d order=%d budget=%d "
            "< 0 — skip", idx, order, budget,
        )
        return None
    if budget > MAX_BUDGET_VALUE:
        logger.warning(
            "[AdaptedMutationBudgetLoader] entry %d order=%d budget=%d "
            "exceeds MAX_BUDGET_VALUE=%d — clamped",
            idx, order, budget, MAX_BUDGET_VALUE,
        )
        budget = MAX_BUDGET_VALUE
    if order == 2 and budget < MIN_ORDER2_BUDGET:
        logger.warning(
            "[AdaptedMutationBudgetLoader] entry %d order=2 budget=%d "
            "below MIN_ORDER2_BUDGET=%d — raised to floor",
            idx, budget, MIN_ORDER2_BUDGET,
        )
        budget = MIN_ORDER2_BUDGET
    return AdaptedBudgetEntry(
        order=order,
        budget=budget,
        proposal_id=str(raw.get("proposal_id") or ""),
        approved_at=str(raw.get("approved_at") or ""),
        approved_by=str(raw.get("approved_by") or ""),
    )


def load_adapted_budgets(
    yaml_path: Optional[Path] = None,
) -> Dict[int, int]:
    """Read the adapted-budgets YAML and return a `{order: budget}`
    dict.

    Returns empty dict when:
      * Master flag off
      * YAML file missing
      * YAML parse fails (PyYAML import OR `yaml.safe_load`)
      * File exceeds MAX_YAML_BYTES
      * Top-level not a mapping or `budgets` key missing/non-list

    Per-entry SKIP (logged) when:
      * Missing order / unknown order / non-integer budget /
        budget < 0

    Cap: at most MAX_ADAPTED_BUDGETS entries returned. Latest
    occurrence per order wins (operator can supersede an earlier
    proposal by adding a new entry with the same order).

    NEVER raises into the caller.
    """
    if not is_loader_enabled():
        return {}

    path = yaml_path if yaml_path is not None else adapted_budgets_path()
    if not path.exists():
        logger.debug(
            "[AdaptedMutationBudgetLoader] no adapted-budgets yaml at %s — "
            "no budgets to merge", path,
        )
        return {}
    try:
        size = path.stat().st_size
    except OSError as exc:
        logger.warning(
            "[AdaptedMutationBudgetLoader] stat failed for %s: %s", path, exc,
        )
        return {}
    if size > MAX_YAML_BYTES:
        logger.warning(
            "[AdaptedMutationBudgetLoader] %s exceeds MAX_YAML_BYTES=%d "
            "(was %d) — refusing to load",
            path, MAX_YAML_BYTES, size,
        )
        return {}
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "[AdaptedMutationBudgetLoader] read failed for %s: %s",
            path, exc,
        )
        return {}
    if not raw_text.strip():
        return {}
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        logger.warning(
            "[AdaptedMutationBudgetLoader] PyYAML not available — cannot "
            "load adapted budgets",
        )
        return {}
    try:
        doc = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        logger.warning(
            "[AdaptedMutationBudgetLoader] YAML parse failed at %s: %s",
            path, exc,
        )
        return {}
    if not isinstance(doc, dict):
        logger.warning(
            "[AdaptedMutationBudgetLoader] %s top-level is not a mapping "
            "— skip", path,
        )
        return {}
    raw_entries = doc.get("budgets")
    if not isinstance(raw_entries, list):
        return {}

    out: Dict[int, int] = {}
    seen_count = 0
    for i, raw_entry in enumerate(raw_entries):
        if seen_count >= MAX_ADAPTED_BUDGETS:
            logger.warning(
                "[AdaptedMutationBudgetLoader] reached "
                "MAX_ADAPTED_BUDGETS=%d — truncating remaining entries",
                MAX_ADAPTED_BUDGETS,
            )
            break
        if not isinstance(raw_entry, dict):
            continue
        entry = _parse_entry(raw_entry, i)
        if entry is None:
            continue
        # Latest-occurrence-wins per order (operator supersedes
        # earlier proposal by adding new entry).
        out[entry.order] = entry.budget
        seen_count += 1
    if out:
        logger.info(
            "[AdaptedMutationBudgetLoader] loaded %d adapted budget(s) "
            "from %s", len(out), path,
        )
    return out


def compute_effective_max_mutations(
    order: int,
    env_default: int,
    adapted: Optional[Dict[int, int]] = None,
) -> int:
    """Return the effective per-Order ``max_mutations`` budget to
    pass into ``ScopedToolBackend(max_mutations=...)``.

    Cage rule (load-bearing): adapted budgets can only LOWER the
    env default — defense-in-depth via ``min(env_default, adapted)``
    ensures even a doctored YAML cannot loosen the cage.

    Behavior:
      * Loader OFF → returns ``max(0, env_default)`` unchanged.
      * Loader ON, no adapted entry for ``order`` → returns
        ``max(0, env_default)``.
      * Loader ON, adapted entry present → returns
        ``min(max(0, env_default), adapted_budget)``.

    The optional ``adapted`` parameter accepts a pre-loaded mapping
    (callers may load once + reuse to amortize YAML I/O). When None,
    the loader is invoked on every call (acceptable for single-op
    construction sites; cache at the caller for hot paths).

    NEVER raises.
    """
    safe_default = max(0, int(env_default))
    if adapted is None:
        try:
            adapted = load_adapted_budgets()
        except Exception as exc:  # defensive — loader is fail-open
            logger.warning(
                "[AdaptedMutationBudgetLoader] load_adapted_budgets raised "
                "%s — falling back to env_default", exc,
            )
            return safe_default
    proposed = adapted.get(int(order))
    if proposed is None:
        return safe_default
    return min(safe_default, int(proposed))


__all__ = [
    "AdaptedBudgetEntry",
    "MAX_ADAPTED_BUDGETS",
    "MAX_BUDGET_VALUE",
    "MAX_YAML_BYTES",
    "MIN_ORDER2_BUDGET",
    "adapted_budgets_path",
    "compute_effective_max_mutations",
    "is_loader_enabled",
    "load_adapted_budgets",
]
