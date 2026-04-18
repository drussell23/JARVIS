"""MutationGate — APPLY-phase execution boundary for mutation testing.

Closes the asymmetry exposed by the Session W calibration (Grade F,
28.6% — see ``project_session_w_mutation_calibration_baseline.md``):
a test suite that passes is not the same as a test suite that tests.
For allowlisted critical paths, the gate runs a cached mutation score
before APPLY writes land on disk, and upgrades or blocks the operation
when the score falls below the operator-configured floor.

**Authority model — deliberate split**:

  * ``mutation_tester.py`` is a pure measurer. It computes a
    ``MutationResult`` dataclass. It has no authority over the
    pipeline. Its docstring-level authority invariant is preserved.
  * ``mutation_gate.py`` (this module) is the *decision maker*. It
    consumes the measurement, reads operator policy, and emits a
    verdict. The orchestrator consumes the verdict and is still the
    sole party that modifies risk tier or blocks APPLY.
  * The split means this module's authority is explicit, scoped, and
    revocable via env var. Manifesto §1 Boundary Principle: friction
    at a threshold, not replacement of deterministic proof.

**What the gate does NOT do**:

  * It does not auto-improve tests. That's the autonomous loop's job,
    with the survivor list as input signal.
  * It does not auto-retry with different seeds. A sampled-low score
    may improve on a different seed, but the default seed=0 run is
    what the operator sees — stable per-op reproducibility beats
    score-chasing.
  * It does not gate non-critical paths. The allowlist is deliberately
    narrow — Session W's APPLY was ~5 minutes; that cost only pays off
    on paths where the blast radius justifies it.

**Allowlist sources (highest precedence first)**:

  1. Explicit env var: ``JARVIS_MUTATION_GATE_CRITICAL_PATHS`` —
     comma-separated relative paths or path prefixes.
  2. YAML config: ``config/mutation_critical_paths.yml`` (optional,
     skipped when missing).

A path matches if it equals an allowlist entry OR starts with one
of the entries (prefix match on the path component boundary — so
``backend/core/foo`` matches ``backend/core/`` but not ``backend/core_test``).

**Verdict tiers**:

    decision = "allow"                 — score >= JARVIS_MUTATION_GATE_ALLOW_THRESHOLD (default 0.75)
    decision = "upgrade_to_approval"   — score in [block_threshold, allow_threshold)
    decision = "block"                 — score < JARVIS_MUTATION_GATE_BLOCK_THRESHOLD (default 0.40)
    decision = "skip"                  — gate disabled / path not critical /
                                          no test files / tester errored

The orchestrator maps:
    allow              → leave risk tier unchanged
    upgrade_to_approval → force ``APPROVAL_REQUIRED`` (even for SAFE_AUTO)
    block              → fail the op outright with ``mutation_gate_block``
    skip               → proceed normally

**Rollout modes** (separate knob from master enable/disable):

    shadow    → gate runs, verdict is recorded to ledger + logged to
                stderr, but the orchestrator NEVER upgrades the risk
                tier based on the result. Default mode — safe to flip
                the master switch on with confidence.
    enforce   → full boundary behavior: upgrade_to_approval /
                block decisions DO modify risk_tier. Flip to this only
                after shadow-mode data confirms the gate agrees with
                operator judgment.

Recommended rollout:
  1. Enable master: ``JARVIS_MUTATION_GATE_ENABLED=1``.
  2. Leave mode at default (``shadow``). Ship one or two paths in the
     allowlist. Let the harness run for several battle-test sessions.
  3. Inspect the ledger (``.jarvis/mutation_gate_ledger.jsonl``) —
     confirm scores look reasonable, no surprises.
  4. Flip ``JARVIS_MUTATION_GATE_MODE=enforce``. From now on low-score
     ops get upgraded or blocked.
  5. Widen the allowlist as confidence grows.

Env gates (all fail-closed: default master=OFF so existing sessions
don't start running expensive mutation runs until explicitly enabled):

    JARVIS_MUTATION_GATE_ENABLED           default 0
    JARVIS_MUTATION_GATE_MODE              shadow|enforce, default shadow
    JARVIS_MUTATION_GATE_CRITICAL_PATHS    comma-separated allowlist
    JARVIS_MUTATION_GATE_ALLOW_THRESHOLD   default 0.75 (Grade B)
    JARVIS_MUTATION_GATE_BLOCK_THRESHOLD   default 0.40 (below Grade D)
    JARVIS_MUTATION_GATE_MAX_MUTANTS       default 25 (matches tester default)
    JARVIS_MUTATION_GATE_PER_TIMEOUT_S     default 30
    JARVIS_MUTATION_GATE_GLOBAL_TIMEOUT_S  default 600 (10min hard cap)
    JARVIS_MUTATION_GATE_LEDGER_PATH       default .jarvis/mutation_gate_ledger.jsonl
    JARVIS_MUTATION_GATE_LEDGER_MAX_LINES  default 5000 (soft rotate)
    JARVIS_MUTATION_GATE_LEDGER_DISABLED   default 0 (kill switch)
    JARVIS_MUTATION_GATE_PREWARM           default 1 — enumerate catalogs at boot
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backend.core.ouroboros.governance import mutation_cache as MC
from backend.core.ouroboros.governance.mutation_tester import (
    MutantOutcome,
    MutationResult,
    enumerate_mutants,
    run_mutant,
    sample_mutants,
)


logger = logging.getLogger("Ouroboros.MutationGate")

_ENV_ENABLED = "JARVIS_MUTATION_GATE_ENABLED"
_ENV_MODE = "JARVIS_MUTATION_GATE_MODE"
_ENV_PATHS = "JARVIS_MUTATION_GATE_CRITICAL_PATHS"
_ENV_ALLOW = "JARVIS_MUTATION_GATE_ALLOW_THRESHOLD"
_ENV_BLOCK = "JARVIS_MUTATION_GATE_BLOCK_THRESHOLD"
_ENV_MAX = "JARVIS_MUTATION_GATE_MAX_MUTANTS"
_ENV_PER_TO = "JARVIS_MUTATION_GATE_PER_TIMEOUT_S"
_ENV_GLOB_TO = "JARVIS_MUTATION_GATE_GLOBAL_TIMEOUT_S"
_ENV_LEDGER_PATH = "JARVIS_MUTATION_GATE_LEDGER_PATH"
_ENV_LEDGER_MAX = "JARVIS_MUTATION_GATE_LEDGER_MAX_LINES"
_ENV_LEDGER_OFF = "JARVIS_MUTATION_GATE_LEDGER_DISABLED"
_ENV_PREWARM = "JARVIS_MUTATION_GATE_PREWARM"

_TRUTHY = frozenset({"1", "true", "yes", "on"})

MODE_SHADOW = "shadow"
MODE_ENFORCE = "enforce"
_VALID_MODES = frozenset({MODE_SHADOW, MODE_ENFORCE})

_YAML_CONFIG_PATH = Path("config/mutation_critical_paths.yml")


def gate_enabled() -> bool:
    return os.environ.get(_ENV_ENABLED, "0").strip().lower() in _TRUTHY


def gate_mode() -> str:
    """Return 'shadow' or 'enforce'. Invalid values fall back to shadow
    — refusing to enforce when misconfigured is the safer failure mode.
    """
    raw = os.environ.get(_ENV_MODE, MODE_SHADOW).strip().lower()
    if raw in _VALID_MODES:
        return raw
    return MODE_SHADOW


def ledger_enabled() -> bool:
    return os.environ.get(_ENV_LEDGER_OFF, "0").strip().lower() not in _TRUTHY


def ledger_path() -> Path:
    return Path(os.environ.get(
        _ENV_LEDGER_PATH, ".jarvis/mutation_gate_ledger.jsonl",
    ))


def ledger_max_lines() -> int:
    try:
        return max(100, min(1_000_000, int(
            os.environ.get(_ENV_LEDGER_MAX, "5000"),
        )))
    except (TypeError, ValueError):
        return 5000


def prewarm_enabled() -> bool:
    return os.environ.get(_ENV_PREWARM, "1").strip().lower() in _TRUTHY


def _float_env(key: str, default: float, *, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(os.environ.get(key, str(default)))))
    except (TypeError, ValueError):
        return default


def _int_env(key: str, default: int, *, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(os.environ.get(key, str(default)))))
    except (TypeError, ValueError):
        return default


def allow_threshold() -> float:
    return _float_env(_ENV_ALLOW, 0.75, lo=0.0, hi=1.0)


def block_threshold() -> float:
    return _float_env(_ENV_BLOCK, 0.40, lo=0.0, hi=1.0)


def max_mutants() -> int:
    return _int_env(_ENV_MAX, 25, lo=1, hi=500)


def per_timeout_s() -> float:
    return float(_int_env(_ENV_PER_TO, 30, lo=5, hi=600))


def global_timeout_s() -> float:
    return float(_int_env(_ENV_GLOB_TO, 600, lo=30, hi=7200))


# ---------------------------------------------------------------------------
# Allowlist loading
# ---------------------------------------------------------------------------


def _env_allowlist() -> List[str]:
    raw = os.environ.get(_ENV_PATHS, "").strip()
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def _yaml_allowlist() -> List[str]:
    if not _YAML_CONFIG_PATH.is_file():
        return []
    try:
        import yaml  # type: ignore
    except ImportError:
        logger.debug(
            "[MutationGate] yaml not installed — skipping YAML allowlist",
        )
        return []
    try:
        doc = yaml.safe_load(_YAML_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        logger.debug(
            "[MutationGate] YAML parse failed %s", _YAML_CONFIG_PATH,
            exc_info=True,
        )
        return []
    if not isinstance(doc, dict):
        return []
    entries = doc.get("critical_paths") or []
    return [str(e).strip() for e in entries if str(e).strip()]


def load_allowlist() -> List[str]:
    """Combined allowlist — env takes precedence, YAML extends it."""
    env_list = _env_allowlist()
    yaml_list = _yaml_allowlist()
    combined = list(dict.fromkeys(env_list + yaml_list))  # preserve order, dedup
    return combined


def is_path_critical(path: Path, *, allowlist: Optional[Sequence[str]] = None) -> bool:
    """Prefix-match with path-component boundary safety."""
    if allowlist is None:
        allowlist = load_allowlist()
    if not allowlist:
        return False
    norm = str(path).replace("\\", "/")
    for entry in allowlist:
        entry_norm = entry.replace("\\", "/").rstrip("/")
        if not entry_norm:
            continue
        if norm == entry_norm:
            return True
        if norm.startswith(entry_norm + "/"):
            return True
    return False


# ---------------------------------------------------------------------------
# Verdict data type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateVerdict:
    decision: str                    # "allow" | "upgrade_to_approval" | "block" | "skip"
    score: float                     # 0.0-1.0 (NaN-equivalent: 0.0 when no mutants)
    grade: str                       # letter grade from MutationResult
    allow_threshold: float
    block_threshold: float
    total_mutants: int
    caught: int
    survived: int
    reason: str                      # short human-readable justification
    survivors: Tuple[MutantOutcome, ...] = ()
    cache_hits: int = 0              # outcome-cache hits
    cache_misses: int = 0            # outcome-cache misses
    duration_s: float = 0.0
    sut_path: str = ""

    def to_log_payload(self) -> Dict[str, Any]:
        return {
            "decision": self.decision,
            "score": round(self.score, 4),
            "grade": self.grade,
            "allow_threshold": self.allow_threshold,
            "block_threshold": self.block_threshold,
            "total_mutants": self.total_mutants,
            "caught": self.caught,
            "survived": self.survived,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "duration_s": round(self.duration_s, 2),
            "sut_path": self.sut_path,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Core gate evaluation — with cache
# ---------------------------------------------------------------------------


def _map_score_to_decision(score: float) -> str:
    if score >= allow_threshold():
        return "allow"
    if score >= block_threshold():
        return "upgrade_to_approval"
    return "block"


def _run_with_cache(
    sut_path: Path,
    test_files: Sequence[Path],
) -> Tuple[MutationResult, int, int]:
    """Run the full mutation test using the two-tier cache.

    Returns (result, cache_hits, cache_misses).
    """
    started = time.time()
    # Catalog — parse + enumerate or pull from cache.
    sut_hash, cached_mutants = MC.get_catalog(sut_path)
    if cached_mutants is None:
        cached_mutants = enumerate_mutants(sut_path)
        MC.put_catalog(sut_hash, cached_mutants)
    # Sample deterministically — seed=0 for per-op reproducibility.
    mutants = sample_mutants(
        cached_mutants, limit=max_mutants(), seed=0,
    )
    # Outcomes — check cache; only run uncached mutants.
    tests_hash = MC.files_composite_hash(test_files)
    cached_outcomes: Dict[str, str] = MC.get_outcomes(sut_hash, tests_hash) or {}
    hits = 0
    misses = 0
    fresh_outcomes: Dict[str, str] = dict(cached_outcomes)
    outcomes: List[MutantOutcome] = []
    per_to = per_timeout_s()
    global_to = global_timeout_s()
    for idx, m in enumerate(mutants):
        elapsed = time.time() - started
        if elapsed >= global_to:
            logger.warning(
                "[MutationGate] global timeout reached at mutant %d/%d",
                idx, len(mutants),
            )
            break
        cached_key = cached_outcomes.get(m.key)
        if cached_key is not None:
            outcomes.append(MutantOutcome(
                mutant=m, caught=(cached_key == "caught"),
                reason=cached_key, duration_s=0.0,
                stderr_excerpt="<cache hit>",
            ))
            hits += 1
            continue
        outcome = run_mutant(m, test_files=test_files, timeout_s=per_to)
        outcomes.append(outcome)
        fresh_outcomes[m.key] = (
            "caught" if outcome.caught else "survived"
        )
        misses += 1
    if misses:
        MC.put_outcomes(sut_hash, tests_hash, fresh_outcomes)
    caught = sum(1 for o in outcomes if o.caught)
    total = len(outcomes)
    survivors = tuple(o for o in outcomes if not o.caught)
    score = caught / total if total else 0.0
    # Reuse the tester's own grade helper for consistency.
    from backend.core.ouroboros.governance.mutation_tester import (
        _grade_from_score, _MUTATION_OPS,
    )
    coverage_by_op = {op: 0 for op in _MUTATION_OPS}
    for m in cached_mutants:
        coverage_by_op[m.op] = coverage_by_op.get(m.op, 0) + 1
    result = MutationResult(
        source_file=str(sut_path),
        total_mutants=total,
        caught=caught,
        survived=total - caught,
        score=score,
        grade=_grade_from_score(score, total),
        survivors=survivors,
        coverage_by_op=coverage_by_op,
        skipped_by_op={},
        duration_s=time.time() - started,
    )
    return result, hits, misses


_ledger_lock = threading.Lock()


def _ledger_rotate_if_needed(path: Path, max_lines: int) -> None:
    """If the ledger exceeds ``max_lines * 1.2``, keep only the most
    recent ``max_lines`` entries. The rotation is lazy — called on every
    append — so a crash after the truncate but before the new append
    can lose the truncation work but never old verdicts.
    """
    try:
        if not path.is_file():
            return
        with path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= int(max_lines * 1.2):
            return
        keep = lines[-max_lines:]
        tmp = path.with_suffix(path.suffix + ".rotating")
        with tmp.open("w", encoding="utf-8") as f:
            f.writelines(keep)
        tmp.replace(path)
    except Exception:  # noqa: BLE001
        logger.debug(
            "[MutationGate] ledger rotate failed", exc_info=True,
        )


def append_ledger(
    *,
    op_id: str,
    verdict: "GateVerdict",
    mode: str,
    enforced: bool,
    applied_tier_change: Optional[str] = None,
) -> None:
    """Persist one verdict line to the JSONL ledger. Best-effort — any
    disk error is swallowed so ledger issues never break the pipeline.
    Manifesto §8 observability, not a governance surface.
    """
    if not ledger_enabled():
        return
    path = ledger_path()
    entry = {
        "ts": int(time.time()),
        "op_id": op_id,
        "mode": mode,
        "enforced": enforced,
        "decision": verdict.decision,
        "score": round(verdict.score, 4),
        "grade": verdict.grade,
        "total_mutants": verdict.total_mutants,
        "caught": verdict.caught,
        "survived": verdict.survived,
        "cache_hits": verdict.cache_hits,
        "cache_misses": verdict.cache_misses,
        "duration_s": round(verdict.duration_s, 3),
        "sut_path": verdict.sut_path,
        "reason": verdict.reason,
        "applied_tier_change": applied_tier_change or "",
    }
    try:
        with _ledger_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, sort_keys=True) + "\n")
            _ledger_rotate_if_needed(path, ledger_max_lines())
    except Exception:  # noqa: BLE001
        logger.debug("[MutationGate] ledger append failed", exc_info=True)


def read_ledger(*, last_n: int = 10) -> List[Dict[str, Any]]:
    """Read the tail of the ledger for status-command display."""
    path = ledger_path()
    if not path.is_file():
        return []
    try:
        with path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:  # noqa: BLE001
        return []
    out: List[Dict[str, Any]] = []
    for raw in lines[-last_n:]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except Exception:  # noqa: BLE001
            continue
    return out


def _skip_verdict(sut_path: Path, reason: str) -> GateVerdict:
    return GateVerdict(
        decision="skip",
        score=0.0, grade="N/A",
        allow_threshold=allow_threshold(),
        block_threshold=block_threshold(),
        total_mutants=0, caught=0, survived=0,
        reason=reason,
        sut_path=str(sut_path),
    )


def evaluate_file(
    sut_path: Path,
    test_files: Sequence[Path],
    *,
    force: bool = False,
) -> GateVerdict:
    """Run the gate for one file. ``force=True`` bypasses the master switch
    and the allowlist — used by the /mutation REPL command to drive the
    same cached path without changing env.

    Returns a ``GateVerdict``. Callers (orchestrator) map decisions to
    risk-tier upgrades or block-outcomes.
    """
    if not force and not gate_enabled():
        return _skip_verdict(sut_path, "gate_disabled")
    if not force and not is_path_critical(sut_path):
        return _skip_verdict(sut_path, "path_not_critical")
    if not sut_path.is_file():
        return _skip_verdict(sut_path, "sut_missing")
    if not test_files:
        return _skip_verdict(sut_path, "no_test_files")
    try:
        result, hits, misses = _run_with_cache(sut_path, test_files)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[MutationGate] evaluate failed for %s — fallback skip",
            sut_path,
        )
        return _skip_verdict(sut_path, f"gate_error:{type(exc).__name__}")
    if result.total_mutants == 0:
        return _skip_verdict(sut_path, "no_mutation_sites")
    decision = _map_score_to_decision(result.score)
    reason_map = {
        "allow": "score_above_allow_threshold",
        "upgrade_to_approval": "score_in_approval_band",
        "block": "score_below_block_threshold",
    }
    verdict = GateVerdict(
        decision=decision,
        score=result.score,
        grade=result.grade,
        allow_threshold=allow_threshold(),
        block_threshold=block_threshold(),
        total_mutants=result.total_mutants,
        caught=result.caught,
        survived=result.survived,
        reason=reason_map[decision],
        survivors=result.survivors,
        cache_hits=hits,
        cache_misses=misses,
        duration_s=result.duration_s,
        sut_path=str(sut_path),
    )
    logger.info(
        "[MutationGate] file=%s decision=%s score=%.2f grade=%s "
        "caught=%d/%d cache_hits=%d cache_misses=%d duration=%.1fs",
        sut_path, verdict.decision, verdict.score, verdict.grade,
        verdict.caught, verdict.total_mutants,
        verdict.cache_hits, verdict.cache_misses, verdict.duration_s,
    )
    return verdict


def evaluate_files(
    sut_paths: Sequence[Path],
    test_file_map: Dict[Path, Sequence[Path]],
    *,
    force: bool = False,
) -> List[GateVerdict]:
    """Evaluate a batch. One verdict per SUT. The orchestrator aggregates."""
    out: List[GateVerdict] = []
    for sp in sut_paths:
        tests = test_file_map.get(sp, ())
        out.append(evaluate_file(sp, tests, force=force))
    return out


def merge_verdicts(verdicts: Sequence[GateVerdict]) -> GateVerdict:
    """Merge a list of per-file verdicts into one. Worst decision wins:
    ``block`` > ``upgrade_to_approval`` > ``allow`` > ``skip``.
    """
    order = {"block": 3, "upgrade_to_approval": 2, "allow": 1, "skip": 0}
    if not verdicts:
        return _skip_verdict(Path(""), "no_verdicts")
    worst = max(verdicts, key=lambda v: order.get(v.decision, 0))
    total_caught = sum(v.caught for v in verdicts)
    total_muts = sum(v.total_mutants for v in verdicts)
    score = total_caught / total_muts if total_muts else 0.0
    all_survivors: List[MutantOutcome] = []
    for v in verdicts:
        all_survivors.extend(v.survivors)
    return GateVerdict(
        decision=worst.decision,
        score=score,
        grade=worst.grade,
        allow_threshold=worst.allow_threshold,
        block_threshold=worst.block_threshold,
        total_mutants=total_muts,
        caught=total_caught,
        survived=total_muts - total_caught,
        reason=f"merged:{worst.reason}",
        survivors=tuple(all_survivors),
        cache_hits=sum(v.cache_hits for v in verdicts),
        cache_misses=sum(v.cache_misses for v in verdicts),
        duration_s=sum(v.duration_s for v in verdicts),
        sut_path=";".join(v.sut_path for v in verdicts),
    )


def prewarm_allowlist(
    *,
    project_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Enumerate catalogs for every allowlisted file so the first APPLY
    doesn't pay the ``ast.unparse`` cost. Idempotent — re-runs are cheap
    when the cache is already warm. Returns a summary dict for logging.
    """
    if not gate_enabled():
        return {"skipped": "gate_disabled"}
    if not prewarm_enabled():
        return {"skipped": "prewarm_disabled"}
    root = project_root or Path.cwd()
    allowlist = load_allowlist()
    if not allowlist:
        return {"skipped": "empty_allowlist"}
    from backend.core.ouroboros.governance.mutation_tester import (
        enumerate_mutants,
    )
    warmed: List[str] = []
    failed: List[str] = []
    total_mutants = 0
    t0 = time.time()
    for entry in allowlist:
        abs_path = root / entry
        if not abs_path.is_file():
            # Entry is a prefix — glob for .py files under it.
            if abs_path.is_dir():
                for py in abs_path.rglob("*.py"):
                    if py.is_file():
                        try:
                            sut_hash, cached = MC.get_catalog(py)
                            if cached is None:
                                muts = enumerate_mutants(py)
                                MC.put_catalog(sut_hash, muts)
                                total_mutants += len(muts)
                            warmed.append(str(py.relative_to(root)))
                        except Exception:  # noqa: BLE001
                            failed.append(str(py))
            continue
        try:
            sut_hash, cached = MC.get_catalog(abs_path)
            if cached is None:
                muts = enumerate_mutants(abs_path)
                MC.put_catalog(sut_hash, muts)
                total_mutants += len(muts)
            warmed.append(entry)
        except Exception:  # noqa: BLE001
            failed.append(entry)
    duration = time.time() - t0
    summary = {
        "warmed_files": len(warmed),
        "failed_files": len(failed),
        "total_mutants_enumerated": total_mutants,
        "duration_s": round(duration, 2),
    }
    logger.info(
        "[MutationGate] prewarm complete files=%d mutants=%d duration=%.2fs",
        len(warmed), total_mutants, duration,
    )
    return summary


__all__ = [
    "GateVerdict",
    "MODE_ENFORCE",
    "MODE_SHADOW",
    "allow_threshold",
    "append_ledger",
    "block_threshold",
    "evaluate_file",
    "evaluate_files",
    "gate_enabled",
    "gate_mode",
    "is_path_critical",
    "ledger_enabled",
    "ledger_path",
    "load_allowlist",
    "merge_verdicts",
    "prewarm_allowlist",
    "prewarm_enabled",
    "read_ledger",
]
