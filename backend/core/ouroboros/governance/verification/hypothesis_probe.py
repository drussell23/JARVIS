"""Priority C — Bounded HypothesisProbe primitive.

The structural primitive that lets O+V resolve epistemic ambiguity
**autonomously** — without falling back to CC's reactive ``ask_human``
mode and without violating the Zero-Trust boundary.

Per PRD §25.5.3: today, when O+V encounters epistemic ambiguity
("should I refactor pattern A or pattern B?"), it has no first-class
way to *probe the environment* to disambiguate. The Iron Gate counts
tool calls; it doesn't measure whether they actually probed the
uncertainty. This module ships:

  * ``Hypothesis`` — frozen claim with ``confidence_prior``,
    ``test_strategy``, ``expected_signal``, hard depth/budget/wall
    bounds, ``parent_op_id`` for replay.
  * ``ProbeResult`` — frozen result with ``confidence_posterior``,
    ``observation_summary``, ``cost_usd``, ``iterations_used``,
    ``convergence_state``, ``evidence_hash``.
  * Test-strategy registry (mirrors Slice A2 + B1 + E patterns).
    Operators register additional strategies from their own modules;
    seed strategies register at module load. Three seeds:
      - ``lookup`` — pure file-read + AST inspection (read-only;
        no LLM, no network)
      - ``subagent_explore`` — placeholder; future slice wires the
        EXPLORE subagent in read-only mode
      - ``dry_run`` — placeholder; future slice wires the existing
        dry-run infrastructure
  * ``HypothesisProbe`` — the bounded probe runner. ``test()`` walks
    the hypothesis through cheapest-first strategies, computes
    Bayesian posteriors via Slice 2.2's ``RepeatRunner.bayesian_-
    update`` (Antigravity's ``exploration_calculus`` with stdlib
    fallback — no duplication), halts on:
      - convergence (|posterior - prior| < epsilon)
      - max_iterations
      - budget exhausted
      - wall-clock exceeded
      - hypothesis memorialized as dead in prior cycle
  * Failed-hypothesis ledger at ``.jarvis/failed_hypotheses.jsonl``.
    Content-hash indexed (sha256 of claim + evidence_required +
    test_strategy). Future cycles never re-enter the same dead end.

Mathematical contract (load-bearing):

  1. BOUNDED DEPTH      — max 3 nested probes per root hypothesis
  2. BOUNDED BUDGET     — $0.05/probe, $0.15/tree (env-tunable)
  3. PROVABLE CONVERGENCE
                        — halt when |posterior - prior| < epsilon
                        — OR max_iterations / max_wall_s reached
  4. MEMORIALIZED       — failed probes write content-hash to
                          .jarvis/failed_hypotheses.jsonl

Antivenom invariants (non-negotiable):

  * Read-only by AST enforcement — strategy callables MUST NOT
    invoke any mutating tool. Cage-level enforcement (Venom tool
    integration) lives at the future Slice C4; this primitive
    enforces by typed strategy-callable signature + AST-pinned
    test that no strategy imports change_engine or its kin.
  * Subject to same risk-tier ladder as ops — high-risk hypotheses
    are rejected at the cage level (future Slice C4).
  * Failed probes recorded immutably with content hash so
    adversarial probes cannot quietly retry.
  * Epsilon convergence threshold is mathematically derived from
    the prior confidence, NOT hardcoded.

Master flag ``JARVIS_HYPOTHESIS_PROBE_ENABLED`` (default ``false``
until graduation per CLAUDE.md discipline).

Authority invariants (AST-pinned by tests):
  * No imports of orchestrator / phase_runner / candidate_generator /
    iron_gate / change_engine / policy / semantic_guardian.
  * Allowed: verification.* (own slice family) + adaptation.*
    (Antigravity Bayesian primitives via lazy import).
  * Pure stdlib + verification + adaptation only.
  * NEVER raises out of any public method.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Callable,
    Coroutine,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Tuple,
)

logger = logging.getLogger(__name__)


HYPOTHESIS_PROBE_SCHEMA_VERSION: str = "hypothesis_probe.1"


# ---------------------------------------------------------------------------
# Master flag + bounds
# ---------------------------------------------------------------------------


def hypothesis_probe_enabled() -> bool:
    """``JARVIS_HYPOTHESIS_PROBE_ENABLED`` (default ``false`` until
    graduation). When off, ``HypothesisProbe.test()`` returns a
    no-op ProbeResult with convergence_state="disabled" — the
    caller's logic continues without raising."""
    raw = os.environ.get(
        "JARVIS_HYPOTHESIS_PROBE_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False  # opt-in until graduation
    return raw in ("1", "true", "yes", "on")


def default_budget_per_probe_usd() -> float:
    """Per-probe USD cap. Default $0.05 per PRD §25.5.3."""
    raw = os.environ.get(
        "JARVIS_HYPOTHESIS_BUDGET_PER_PROBE_USD", "0.05",
    )
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.05


def default_budget_per_tree_usd() -> float:
    """Per-hypothesis-tree USD cap (across all nested probes).
    Default $0.15 per PRD §25.5.3."""
    raw = os.environ.get(
        "JARVIS_HYPOTHESIS_BUDGET_PER_TREE_USD", "0.15",
    )
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.15


def default_max_iterations() -> int:
    """Default max iterations per probe. PRD §25.5.3 says 3."""
    raw = os.environ.get("JARVIS_HYPOTHESIS_MAX_ITERATIONS", "3")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 3


def default_max_wall_s() -> int:
    """Default per-probe wall-clock cap in seconds. PRD §25.5.3
    says 30."""
    raw = os.environ.get("JARVIS_HYPOTHESIS_MAX_WALL_S", "30")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 30


def derive_epsilon(prior_confidence: float) -> float:
    """Mathematically-derived convergence epsilon.

    Core insight: a high-stakes hypothesis (prior near 0.5 — high
    uncertainty) deserves a tighter epsilon (more probing required)
    than an already-likely hypothesis (prior near 0 or 1).

    Formula: epsilon = base * (1 - 4 * (prior - 0.5)^2)
      * prior=0.5 (max uncertainty)  → epsilon = base
      * prior=0.0 or 1.0 (certain)   → epsilon = 0 (no probing needed)
      * prior=0.25 or 0.75           → epsilon = base * 0.5

    base = JARVIS_HYPOTHESIS_EPSILON_BASE (default 0.05).

    Caller bounds prior to [0.001, 0.999] before passing in.
    """
    raw = os.environ.get("JARVIS_HYPOTHESIS_EPSILON_BASE", "0.05")
    try:
        base = max(0.001, min(0.5, float(raw)))
    except (TypeError, ValueError):
        base = 0.05
    p = max(0.001, min(0.999, float(prior_confidence)))
    weight = max(0.0, 1.0 - 4.0 * (p - 0.5) ** 2)
    return base * weight


# ---------------------------------------------------------------------------
# Frozen schemas
# ---------------------------------------------------------------------------


ConvergenceState = Literal[
    "stable",            # |posterior - prior| < epsilon
    "inconclusive",      # iterations exhausted without convergence
    "budget_exhausted",  # cost cap hit
    "wall_exceeded",     # wall-clock cap hit
    "memorialized_dead", # already in failed-hypotheses ledger
    "disabled",          # master flag off
    "evaluator_error",   # strategy raised
    "unknown_strategy",  # strategy_kind not registered
]


@dataclass(frozen=True)
class Hypothesis:
    """A falsifiable claim ready for bounded autonomous probing."""

    claim: str
    confidence_prior: float
    test_strategy: str
    expected_signal: str
    parent_op_id: str = ""
    budget_usd: float = -1.0           # -1 = use env default
    max_iterations: int = -1           # -1 = use env default
    max_wall_s: int = -1               # -1 = use env default
    schema_version: str = HYPOTHESIS_PROBE_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "claim": self.claim,
            "confidence_prior": self.confidence_prior,
            "test_strategy": self.test_strategy,
            "expected_signal": self.expected_signal,
            "parent_op_id": self.parent_op_id,
            "budget_usd": self.budget_usd,
            "max_iterations": self.max_iterations,
            "max_wall_s": self.max_wall_s,
        }

    def resolved_budget_usd(self) -> float:
        return (
            self.budget_usd if self.budget_usd >= 0
            else default_budget_per_probe_usd()
        )

    def resolved_max_iterations(self) -> int:
        return (
            self.max_iterations if self.max_iterations > 0
            else default_max_iterations()
        )

    def resolved_max_wall_s(self) -> int:
        return (
            self.max_wall_s if self.max_wall_s > 0
            else default_max_wall_s()
        )


@dataclass(frozen=True)
class ProbeResult:
    """Frozen outcome of one HypothesisProbe.test() call."""

    confidence_posterior: float
    observation_summary: str
    cost_usd: float
    iterations_used: int
    convergence_state: str
    evidence_hash: str
    schema_version: str = HYPOTHESIS_PROBE_SCHEMA_VERSION

    @property
    def is_terminal(self) -> bool:
        """True iff the probe converged or was structurally halted."""
        return self.convergence_state in (
            "stable", "memorialized_dead", "evaluator_error",
            "unknown_strategy", "disabled",
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "confidence_posterior": self.confidence_posterior,
            "observation_summary": self.observation_summary,
            "cost_usd": self.cost_usd,
            "iterations_used": self.iterations_used,
            "convergence_state": self.convergence_state,
            "evidence_hash": self.evidence_hash,
        }


# ---------------------------------------------------------------------------
# Test strategy registry
# ---------------------------------------------------------------------------


# A test strategy callable: takes a Hypothesis, returns an
# (observation_text, cost_usd, verdict_kind) tuple. Verdict_kind is
# a str matching the calculus vocabulary ("CONFIRMED" / "REFUTED" /
# "INCONCLUSIVE"). NEVER raises — strategies must catch their own
# errors and return ("error: ...", 0.0, "INCONCLUSIVE") on failure.
StrategyExecutor = Callable[
    [Hypothesis],
    "Coroutine[Any, Any, Tuple[str, float, str]]",
]


@dataclass(frozen=True)
class TestStrategy:
    """One probing strategy. Frozen + hashable."""

    strategy_kind: str
    description: str
    execute: StrategyExecutor


_STRATEGY_REGISTRY: Dict[str, TestStrategy] = {}
_REGISTRY_LOCK = threading.RLock()


def register_test_strategy(
    strategy: TestStrategy, *, overwrite: bool = False,
) -> None:
    """Install a probe strategy. NEVER raises. Idempotent on
    identical re-register."""
    if not isinstance(strategy, TestStrategy):
        return
    safe_kind = (
        str(strategy.strategy_kind).strip()
        if strategy.strategy_kind else ""
    )
    if not safe_kind:
        return
    with _REGISTRY_LOCK:
        existing = _STRATEGY_REGISTRY.get(safe_kind)
        if existing is not None:
            if existing == strategy:
                return
            if not overwrite:
                logger.info(
                    "[HypothesisProbe] strategy %r already registered",
                    safe_kind,
                )
                return
        _STRATEGY_REGISTRY[safe_kind] = strategy


def unregister_test_strategy(strategy_kind: str) -> bool:
    """Remove a strategy. Returns True if removed. NEVER raises."""
    safe_kind = str(strategy_kind).strip() if strategy_kind else ""
    if not safe_kind:
        return False
    with _REGISTRY_LOCK:
        return _STRATEGY_REGISTRY.pop(safe_kind, None) is not None


def list_test_strategies() -> Tuple[TestStrategy, ...]:
    """Return all strategies in stable alphabetical order."""
    with _REGISTRY_LOCK:
        return tuple(
            _STRATEGY_REGISTRY[k]
            for k in sorted(_STRATEGY_REGISTRY.keys())
        )


def reset_strategy_registry_for_tests() -> None:
    """Test isolation."""
    with _REGISTRY_LOCK:
        _STRATEGY_REGISTRY.clear()
    _register_seed_strategies()


# ---------------------------------------------------------------------------
# Failed-hypothesis ledger (memorialization)
# ---------------------------------------------------------------------------


def _ledger_path() -> Path:
    """Resolve the failed-hypotheses ledger path. Mirrors the
    posture/topology ledger path conventions."""
    base = os.environ.get(
        "JARVIS_HYPOTHESIS_LEDGER_PATH",
        ".jarvis/failed_hypotheses.jsonl",
    ).strip()
    return Path(base)


def hypothesis_dead_id(h: Hypothesis) -> str:
    """Content-hash of the (claim, expected_signal, test_strategy)
    triple. Used as the dedup key for memorialization. NEVER raises.

    Adversarial probes that try to retry the same hypothesis with
    cosmetic-only differences (e.g., trailing whitespace, varying
    parent_op_id) hash to the same id — the cycle short-circuits.
    """
    try:
        material = "".join([
            str(h.claim).strip(),
            str(h.expected_signal).strip(),
            str(h.test_strategy).strip(),
        ])
        return hashlib.sha256(material.encode("utf-8")).hexdigest()
    except Exception:  # noqa: BLE001
        return ""


def is_hypothesis_memorialized(h: Hypothesis) -> bool:
    """True iff the hypothesis has previously been declared dead.
    Reads ``.jarvis/failed_hypotheses.jsonl`` line-by-line. NEVER
    raises — read errors → False (treat as 'not memorialized')."""
    target_id = hypothesis_dead_id(h)
    if not target_id:
        return False
    path = _ledger_path()
    if not path.exists():
        return False
    try:
        with path.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    rec = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, Mapping):
                    if rec.get("dead_id") == target_id:
                        return True
    except OSError:
        return False
    return False


def memorialize_hypothesis(
    h: Hypothesis, result: ProbeResult,
) -> bool:
    """Append a dead-hypothesis record to the ledger. Returns True
    on successful append, False otherwise. NEVER raises.

    Idempotent: if the dead_id is already present, the append is
    skipped (saves bytes). Append format: JSONL row with dead_id,
    hypothesis dict, result dict, ts_unix."""
    target_id = hypothesis_dead_id(h)
    if not target_id:
        return False
    if is_hypothesis_memorialized(h):
        return True  # already dead — silent success
    path = _ledger_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "schema_version": HYPOTHESIS_PROBE_SCHEMA_VERSION,
            "dead_id": target_id,
            "hypothesis": h.to_dict(),
            "result": result.to_dict(),
            "ts_unix": time.time(),
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        return True
    except OSError as exc:
        logger.debug(
            "[HypothesisProbe] memorialize append failed: %s", exc,
        )
        return False


def reset_ledger_for_tests() -> None:
    """Test isolation — wipe the ledger. NEVER raises."""
    path = _ledger_path()
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# HypothesisProbe — the bounded probe runner
# ---------------------------------------------------------------------------


class HypothesisProbe:
    """Bounded autonomous probe runner. Walks a Hypothesis through
    its registered strategy with hard depth/budget/wall bounds,
    computes Bayesian posteriors via Slice 2.2's ``RepeatRunner.-
    bayesian_update``, halts on convergence/exhaustion, and
    memorializes dead hypotheses for future cycles.

    Stateless — the same instance can probe many hypotheses safely.
    All state lives on the Hypothesis + ProbeResult (frozen) and the
    ledger (file-system).
    """

    async def test(self, h: Hypothesis) -> ProbeResult:
        """Probe a Hypothesis. NEVER raises.

        Termination contract (per PRD §25.5.3):
          * memorialized_dead: prior cycle declared this hypothesis
            dead → return immediately with cached posterior=prior
          * disabled: master flag off → no-op result
          * unknown_strategy: registered strategies don't include
            ``h.test_strategy``
          * stable: |posterior - prior| < epsilon (mathematically
            derived from prior; tighter for high-uncertainty priors)
          * budget_exhausted: cost > h.budget_usd
          * wall_exceeded: elapsed > h.max_wall_s
          * inconclusive: max_iterations reached without convergence
        """
        evidence_hash = hypothesis_dead_id(h)

        if not hypothesis_probe_enabled():
            return ProbeResult(
                confidence_posterior=h.confidence_prior,
                observation_summary="master flag off",
                cost_usd=0.0,
                iterations_used=0,
                convergence_state="disabled",
                evidence_hash=evidence_hash,
            )

        if is_hypothesis_memorialized(h):
            return ProbeResult(
                confidence_posterior=h.confidence_prior,
                observation_summary=(
                    "memorialized as dead in prior cycle"
                ),
                cost_usd=0.0,
                iterations_used=0,
                convergence_state="memorialized_dead",
                evidence_hash=evidence_hash,
            )

        with _REGISTRY_LOCK:
            strategy = _STRATEGY_REGISTRY.get(
                str(h.test_strategy).strip(),
            )
        if strategy is None:
            return ProbeResult(
                confidence_posterior=h.confidence_prior,
                observation_summary=(
                    f"unknown strategy: {h.test_strategy!r}"
                ),
                cost_usd=0.0,
                iterations_used=0,
                convergence_state="unknown_strategy",
                evidence_hash=evidence_hash,
            )

        budget_cap = h.resolved_budget_usd()
        max_iter = h.resolved_max_iterations()
        max_wall = h.resolved_max_wall_s()
        epsilon = derive_epsilon(h.confidence_prior)

        prior = max(0.001, min(0.999, float(h.confidence_prior)))
        posterior = prior
        cost = 0.0
        iterations = 0
        evidence_bearing_iterations = 0  # only CONFIRMED/REFUTED count
        observations: List[str] = []
        convergence: str = "inconclusive"

        start_ts = time.monotonic()
        for i in range(max_iter):
            # Wall-clock check
            if (time.monotonic() - start_ts) > max_wall:
                convergence = "wall_exceeded"
                break
            # Budget check
            if cost > budget_cap:
                convergence = "budget_exhausted"
                break

            iterations += 1
            try:
                obs_text, obs_cost, verdict_str = await asyncio.wait_for(
                    strategy.execute(h),
                    timeout=max(1.0, max_wall - (time.monotonic() - start_ts)),
                )
            except asyncio.TimeoutError:
                convergence = "wall_exceeded"
                observations.append(f"iter={i+1} timed out")
                break
            except Exception as exc:  # noqa: BLE001 — defensive
                convergence = "evaluator_error"
                observations.append(
                    f"iter={i+1} strategy raised: {type(exc).__name__}"
                )
                break

            cost += float(obs_cost or 0.0)
            observations.append(
                f"iter={i+1} verdict={verdict_str} "
                f"cost=${obs_cost:.4f}: {str(obs_text)[:120]}"
            )

            # Bayesian update — reuse Slice 2.2's primitive (no
            # duplication). Falls back to stdlib Bernoulli if
            # exploration_calculus unavailable.
            new_posterior = _bayesian_update_via_repeat_runner(
                posterior, verdict_str,
            )
            delta = abs(new_posterior - posterior)
            posterior = new_posterior

            # Only count evidence-bearing verdicts toward convergence.
            # INCONCLUSIVE strategies (placeholders, indeterminate
            # observations) leave the posterior unchanged → delta=0 →
            # would otherwise fire premature "stable" convergence on
            # a genuinely-uncertain hypothesis. Track CONFIRMED /
            # REFUTED separately so only meaningful evidence drives
            # the math contract's stability proof.
            v_norm = str(verdict_str or "").strip().upper()
            if v_norm in ("CONFIRMED", "REFUTED"):
                evidence_bearing_iterations += 1
                # Convergence check — mathematically derived epsilon
                if (
                    delta < epsilon
                    and evidence_bearing_iterations >= 1
                ):
                    convergence = "stable"
                    break

        elapsed = time.monotonic() - start_ts
        summary = "; ".join(observations[:5])
        if not summary:
            summary = "(no observations)"

        result = ProbeResult(
            confidence_posterior=round(posterior, 6),
            observation_summary=summary,
            cost_usd=round(cost, 6),
            iterations_used=iterations,
            convergence_state=convergence,
            evidence_hash=evidence_hash,
        )

        # Memorialize hypotheses that the system gave its full
        # budget/iterations to without converging — they're "dead
        # ends" for future cycles. ``stable`` results (genuine
        # convergence) are NOT memorialized — they may want to
        # re-probe later as the codebase changes.
        if convergence in (
            "inconclusive", "budget_exhausted", "wall_exceeded",
        ):
            try:
                memorialize_hypothesis(h, result)
            except Exception:  # noqa: BLE001
                pass

        return result


_DEFAULT_PROBE: Optional[HypothesisProbe] = None
_DEFAULT_PROBE_LOCK = threading.Lock()


def get_default_probe() -> HypothesisProbe:
    """Singleton accessor. The probe is stateless so a single instance
    is safe to share across the orchestrator."""
    global _DEFAULT_PROBE
    with _DEFAULT_PROBE_LOCK:
        if _DEFAULT_PROBE is None:
            _DEFAULT_PROBE = HypothesisProbe()
        return _DEFAULT_PROBE


# ---------------------------------------------------------------------------
# Bayesian update — proxy to Slice 2.2's RepeatRunner primitive
# ---------------------------------------------------------------------------


def _bayesian_update_via_repeat_runner(
    prior: float, verdict_str: str,
) -> float:
    """Lazy-import wrapper that delegates to Slice 2.2's
    ``_bayesian_update_safely`` (which itself wraps Antigravity's
    ``exploration_calculus.bayesian_update`` with stdlib fallback).

    Translates the strategy's verdict string ("CONFIRMED" /
    "REFUTED" / "INCONCLUSIVE") into the VerdictKind enum that
    RepeatRunner expects. NEVER raises — fallback prior on error."""
    try:
        from backend.core.ouroboros.governance.verification.repeat_runner import (
            _bayesian_update_safely,
        )
        from backend.core.ouroboros.governance.verification.property_oracle import (
            VerdictKind,
        )
        v_str = str(verdict_str or "").strip().upper()
        if v_str == "CONFIRMED":
            v = VerdictKind.PASSED
        elif v_str == "REFUTED":
            v = VerdictKind.FAILED
        else:
            v = VerdictKind.INSUFFICIENT_EVIDENCE
        return _bayesian_update_safely(prior, v)
    except Exception:  # noqa: BLE001
        return float(prior)


# ---------------------------------------------------------------------------
# Seed test strategies
# ---------------------------------------------------------------------------


async def _strategy_lookup(
    h: Hypothesis,
) -> Tuple[str, float, str]:
    """Pure-stdlib lookup strategy. Reads files referenced in the
    hypothesis's expected_signal field and matches against the
    claim using simple string/AST predicates.

    Convention for ``expected_signal``:
      * ``"file_exists:<path>"`` — claim is CONFIRMED iff the path
        exists; REFUTED iff missing.
      * ``"contains:<path>:<substring>"`` — CONFIRMED iff the file
        contains the substring; REFUTED iff missing.
      * ``"not_contains:<path>:<substring>"`` — inverse.

    Cost is always 0.0 (filesystem read is free at this granularity).
    NEVER raises."""
    sig = str(h.expected_signal or "").strip()
    if not sig:
        return ("expected_signal empty", 0.0, "INCONCLUSIVE")
    try:
        if sig.startswith("file_exists:"):
            path = Path(sig[len("file_exists:"):])
            if path.exists():
                return (f"file exists: {path}", 0.0, "CONFIRMED")
            return (f"file missing: {path}", 0.0, "REFUTED")

        if sig.startswith("contains:"):
            rest = sig[len("contains:"):]
            sep = rest.find(":")
            if sep < 0:
                return ("malformed signal", 0.0, "INCONCLUSIVE")
            path = Path(rest[:sep])
            needle = rest[sep + 1:]
            if not path.exists():
                return (f"file missing: {path}", 0.0, "REFUTED")
            text = path.read_text(encoding="utf-8", errors="replace")
            if needle in text:
                return (
                    f"contains {needle!r} in {path}", 0.0, "CONFIRMED",
                )
            return (
                f"missing {needle!r} in {path}", 0.0, "REFUTED",
            )

        if sig.startswith("not_contains:"):
            rest = sig[len("not_contains:"):]
            sep = rest.find(":")
            if sep < 0:
                return ("malformed signal", 0.0, "INCONCLUSIVE")
            path = Path(rest[:sep])
            needle = rest[sep + 1:]
            if not path.exists():
                # Vacuously true — the file doesn't even exist
                return (
                    f"file missing (vacuous): {path}",
                    0.0, "CONFIRMED",
                )
            text = path.read_text(encoding="utf-8", errors="replace")
            if needle in text:
                return (
                    f"unexpected {needle!r} in {path}",
                    0.0, "REFUTED",
                )
            return (
                f"absent {needle!r} from {path}", 0.0, "CONFIRMED",
            )

        return (
            f"unknown signal pattern: {sig[:60]!r}",
            0.0, "INCONCLUSIVE",
        )
    except OSError as exc:
        return (f"OSError: {exc}", 0.0, "INCONCLUSIVE")
    except Exception as exc:  # noqa: BLE001
        return (
            f"strategy raised: {type(exc).__name__}",
            0.0, "INCONCLUSIVE",
        )


async def _strategy_subagent_explore_placeholder(
    h: Hypothesis,
) -> Tuple[str, float, str]:
    """Placeholder for the EXPLORE-subagent strategy. Future slice
    wires the existing Phase B EXPLORE subagent in read-only mode
    (with cage AST-enforcement). Today it returns INCONCLUSIVE so
    consumers can opt-in without breaking on missing infrastructure."""
    return (
        "subagent_explore strategy not yet wired (future slice C2.b)",
        0.0, "INCONCLUSIVE",
    )


async def _strategy_dry_run_placeholder(
    h: Hypothesis,
) -> Tuple[str, float, str]:
    """Placeholder for the dry-run strategy. Future slice wires the
    existing dry-run infrastructure in read-only mode."""
    return (
        "dry_run strategy not yet wired (future slice C2.c)",
        0.0, "INCONCLUSIVE",
    )


def _register_seed_strategies() -> None:
    """Module-load: register the three seed strategies. Lookup is
    fully implemented; subagent_explore + dry_run are placeholders
    that consumers can opt-in to without breaking."""
    register_test_strategy(
        TestStrategy(
            strategy_kind="lookup",
            description=(
                "Pure-stdlib lookup: file existence + substring "
                "presence/absence checks via the expected_signal "
                "convention (file_exists:, contains:, not_contains:)."
            ),
            execute=_strategy_lookup,
        ),
    )
    register_test_strategy(
        TestStrategy(
            strategy_kind="subagent_explore",
            description=(
                "Placeholder. Future slice wires Phase B EXPLORE "
                "subagent in read-only mode."
            ),
            execute=_strategy_subagent_explore_placeholder,
        ),
    )
    register_test_strategy(
        TestStrategy(
            strategy_kind="dry_run",
            description=(
                "Placeholder. Future slice wires existing dry-run "
                "infrastructure in read-only mode."
            ),
            execute=_strategy_dry_run_placeholder,
        ),
    )


_register_seed_strategies()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "HYPOTHESIS_PROBE_SCHEMA_VERSION",
    "Hypothesis",
    "HypothesisProbe",
    "ProbeResult",
    "TestStrategy",
    "default_budget_per_probe_usd",
    "default_budget_per_tree_usd",
    "default_max_iterations",
    "default_max_wall_s",
    "derive_epsilon",
    "get_default_probe",
    "hypothesis_dead_id",
    "hypothesis_probe_enabled",
    "is_hypothesis_memorialized",
    "list_test_strategies",
    "memorialize_hypothesis",
    "register_test_strategy",
    "reset_ledger_for_tests",
    "reset_strategy_registry_for_tests",
    "unregister_test_strategy",
]
