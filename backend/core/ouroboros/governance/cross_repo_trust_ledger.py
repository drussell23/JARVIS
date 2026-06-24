"""Adaptive Trust Ledger — earned cross-repo mutation trust (G3 of the
Sovereign Cross-Repo Mutator).

This is the operator-lock substrate. Graduation is NOT a magic-N count;
it is *earned trust*:

* **Consecutive-streak, zero-rollback.** The ledger tracks a *current
  streak* of cross-repo integrations that landed cleanly. ANY ``rollback``
  OR ``fracture`` resets BOTH the streak and the accumulated trust to ZERO
  (trust is consecutive, not cumulative-forever — a single failure means
  the system must re-earn).

* **AST-complexity-weighted trust.** Each clean merge adds
  ``trust += complexity_weight(pr)``, derived from the Oracle blast-radius
  dependent count + AST node count + boundary-crossing depth +
  ``estimate_body_chars``. A trivial one-liner earns a little trust; a deep
  multi-dependent mutation earns a lot. You CANNOT graduate by merging 100
  trivial PRs.

* **Dynamic threshold.** Graduation requires ``trust >= adaptive_threshold``
  where the threshold scales with the complexity attempted:
  ``JARVIS_TRUST_BASE x max(observed_pr_complexity in the streak, 1.0)`` —
  the bar rises with ambition. PLUS a minimum-streak floor
  (``JARVIS_TRUST_MIN_STREAK``, default 2) so a single huge PR cannot
  graduate alone.

Persistence mirrors ``adaptation/graduation_ledger.py``: an append-only
JSONL at ``.jarvis/cross_repo_trust.jsonl`` (env
``JARVIS_CROSS_REPO_TRUST_PATH``), unique-PR dedup, best-effort/fail-soft on
every write. State is reconstructed by replaying the log.

Authority: this module is the trust *bookkeeper*. It is fail-CLOSED on the
read side: :meth:`is_graduated` returns ``False`` on ANY error — never
graduate on error.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("Ouroboros.CrossRepoTrust")

# Outcomes the ledger understands. clean_merge accrues; rollback / fracture
# reset to zero (consecutive zero-rollback).
_CLEAN = "clean_merge"
_RESET_OUTCOMES = frozenset({"rollback", "fracture"})
_VALID_OUTCOMES = frozenset({_CLEAN, "rollback", "fracture"})

# Defensive caps (mirror graduation_ledger discipline).
MAX_LEDGER_FILE_BYTES: int = 4 * 1024 * 1024
MAX_RECORDS_LOADED: int = 50_000


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def trust_ledger_path() -> Path:
    """Durable JSONL path. Env-overridable via
    ``JARVIS_CROSS_REPO_TRUST_PATH``; defaults to
    ``.jarvis/cross_repo_trust.jsonl`` under cwd."""
    raw = os.environ.get("JARVIS_CROSS_REPO_TRUST_PATH")
    if raw:
        return Path(raw)
    return Path(".jarvis") / "cross_repo_trust.jsonl"


# ---------------------------------------------------------------------------
# complexity_weight — pure, env-weighted
# ---------------------------------------------------------------------------


def complexity_weight(
    *,
    blast_dependents: int,
    ast_node_count: int,
    boundary_depth: int,
    body_chars: int,
) -> float:
    """Pure AST-complexity weight for a single cross-repo change.

    Combines four signals, each env-weighted (so NO hardcoded count drives
    graduation):

      * ``blast_dependents`` — count of cross-repo dependents the Oracle
        blast-radius traced (``JARVIS_TRUST_W_DEPENDENTS``).
      * ``ast_node_count`` — number of AST nodes in the changed source
        (``ast.walk`` of the diff), weighted by ``JARVIS_TRUST_W_AST``.
      * ``boundary_depth`` — how deep the boundary-crossing cone goes
        (``JARVIS_TRUST_W_DEPTH``).
      * ``body_chars`` — size component via ``estimate_body_chars``
        (``JARVIS_TRUST_W_CHARS``).

    A trivial one-liner -> small; a deep multi-dependent mutation -> large.
    Monotonic non-decreasing in every input (all weights >= 0 by default).
    NEVER raises — returns 0.0 on any arithmetic surprise.
    """
    try:
        w_dep = _env_float("JARVIS_TRUST_W_DEPENDENTS", 1.0)
        w_ast = _env_float("JARVIS_TRUST_W_AST", 0.01)
        w_depth = _env_float("JARVIS_TRUST_W_DEPTH", 0.5)
        w_chars = _env_float("JARVIS_TRUST_W_CHARS", 0.0005)
        weight = (
            w_dep * max(0, int(blast_dependents))
            + w_ast * max(0, int(ast_node_count))
            + w_depth * max(0, int(boundary_depth))
            + w_chars * max(0, int(body_chars))
        )
        return float(max(0.0, weight))
    except Exception:  # noqa: BLE001 — pure helper, never raise
        logger.debug("[CrossRepoTrust] complexity_weight failed", exc_info=True)
        return 0.0


# ---------------------------------------------------------------------------
# TrustState
# ---------------------------------------------------------------------------


@dataclass
class TrustState:
    repo: str
    streak: int
    trust: float
    threshold: float
    graduated: bool
    last_complexity: float

    def to_dict(self) -> Dict[str, object]:
        return {
            "repo": self.repo,
            "streak": self.streak,
            "trust": self.trust,
            "threshold": self.threshold,
            "graduated": self.graduated,
            "last_complexity": self.last_complexity,
        }


# ---------------------------------------------------------------------------
# CrossRepoTrustLedger
# ---------------------------------------------------------------------------


@dataclass
class _RepoAccumulator:
    """In-memory reduction of the JSONL for one repo's CURRENT streak."""

    streak: int = 0
    trust: float = 0.0
    # Max complexity observed in the CURRENT (post-last-reset) streak.
    max_complexity_in_streak: float = 0.0
    last_complexity: float = 0.0


class CrossRepoTrustLedger:
    """Append-only durable trust ledger. Best-effort writes, fail-CLOSED
    graduation reads.

    State is reconstructed by replaying the log (the JSONL is the source of
    truth; mirrors GraduationLedger's reduce-the-log model).
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        # Preserve None so the singleton re-reads env on every access (so a
        # per-test JARVIS_CROSS_REPO_TRUST_PATH takes effect). An explicit
        # path pins the instance.
        self._path = path

    # ----- path (re-read env each call so test fixtures take effect) -----

    @property
    def path(self) -> Path:
        # When constructed without an explicit path, honour live env so a
        # per-test JARVIS_CROSS_REPO_TRUST_PATH always points the singleton
        # at the right file.
        return self._path if self._path is not None else trust_ledger_path()

    # ----- write -----

    def record_outcome(
        self,
        *,
        repo: str,
        pr_id: str,
        outcome: str,
        complexity: float,
    ) -> None:
        """Record one cross-repo integration outcome.

        ``outcome in {"clean_merge", "rollback", "fracture"}``.
          * clean_merge: ``streak += 1``, ``trust += complexity``.
          * rollback OR fracture: ``streak = 0``, ``trust = 0.0`` (ANY
            failure resets — consecutive, zero-rollback).

        Unique-PR dedup: a ``pr_id`` is recorded once (a duplicate is a
        no-op). Durable JSONL append; fail-soft (NEVER raises).
        """
        repo_clean = (repo or "").strip()
        pr_clean = (pr_id or "").strip()
        outcome_clean = (outcome or "").strip()
        if not repo_clean or not pr_clean:
            logger.debug("[CrossRepoTrust] empty repo/pr_id — skipping")
            return
        if outcome_clean not in _VALID_OUTCOMES:
            logger.debug(
                "[CrossRepoTrust] unknown outcome=%r — skipping", outcome_clean,
            )
            return
        # Unique-PR dedup — a pr_id already in the log is a no-op.
        try:
            if pr_clean in self._seen_pr_ids(repo_clean):
                return
        except Exception:  # noqa: BLE001 — never block the write on a read
            logger.debug("[CrossRepoTrust] dedup check failed", exc_info=True)
        try:
            comp = float(complexity)
            if comp != comp:  # NaN guard
                comp = 0.0
        except (TypeError, ValueError):
            comp = 0.0
        record = {
            "repo": repo_clean,
            "pr_id": pr_clean,
            "outcome": outcome_clean,
            "complexity": comp,
            "recorded_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ",
            ),
            "recorded_at_epoch": time.time(),
        }
        self._append(record)

    def _append(self, record: Dict[str, object]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("[CrossRepoTrust] mkdir failed: %s", exc)
            return
        try:
            line = json.dumps(record, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            logger.warning("[CrossRepoTrust] serialize failed: %s", exc)
            return
        # Reuse the canonical cross-process append (sibling .lock flock).
        try:
            from backend.core.ouroboros.governance.cross_process_jsonl import (
                flock_append_line,
            )
            if flock_append_line(self.path, line):
                return
        except ImportError:
            pass
        except Exception:  # noqa: BLE001 — fall through to legacy append
            logger.debug("[CrossRepoTrust] flock append failed", exc_info=True)
        # Legacy fallback — plain append (best-effort).
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line)
                f.write("\n")
                f.flush()
        except OSError as exc:
            logger.warning("[CrossRepoTrust] append failed: %s", exc)

    # ----- read -----

    def _read_all(self) -> List[Dict[str, object]]:
        """Replay every record. Bounded + fail-open (empty on any error)."""
        p = self.path
        if not p.exists():
            return []
        try:
            if p.stat().st_size > MAX_LEDGER_FILE_BYTES:
                logger.warning(
                    "[CrossRepoTrust] %s exceeds MAX_LEDGER_FILE_BYTES — "
                    "refusing to load", p,
                )
                return []
            text = p.read_text(encoding="utf-8")
        except OSError:
            return []
        out: List[Dict[str, object]] = []
        for line in text.splitlines():
            if len(out) >= MAX_RECORDS_LOADED:
                break
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out

    def _seen_pr_ids(self, repo: str) -> set:
        return {
            str(r.get("pr_id") or "")
            for r in self._read_all()
            if str(r.get("repo") or "") == repo
        }

    def _accumulate(self, repo: str) -> _RepoAccumulator:
        """Reduce the JSONL into the CURRENT streak accumulator for repo.

        Replays in file order; clean_merge accrues, rollback/fracture
        resets. Dedup is applied here too (defence-in-depth — the first
        occurrence of a pr_id wins, later duplicates are ignored).
        """
        acc = _RepoAccumulator()
        seen: set = set()
        for r in self._read_all():
            if str(r.get("repo") or "") != repo:
                continue
            pr = str(r.get("pr_id") or "")
            if not pr or pr in seen:
                continue
            seen.add(pr)
            outcome = str(r.get("outcome") or "")
            try:
                comp = float(r.get("complexity") or 0.0)
            except (TypeError, ValueError):
                comp = 0.0
            if outcome == _CLEAN:
                acc.streak += 1
                acc.trust += comp
                acc.last_complexity = comp
                if comp > acc.max_complexity_in_streak:
                    acc.max_complexity_in_streak = comp
            elif outcome in _RESET_OUTCOMES:
                # ANY failure resets streak + trust to zero. The current
                # streak begins fresh AFTER this point.
                acc.streak = 0
                acc.trust = 0.0
                acc.max_complexity_in_streak = 0.0
                acc.last_complexity = comp
        return acc

    def adaptive_threshold(self, repo: str) -> float:
        """``JARVIS_TRUST_BASE x max(max_observed_complexity_in_streak, 1.0)``.

        The bar SCALES with the complexity the system is currently
        attempting: to be trusted with complex cross-repo mutations it must
        have demonstrated clean streaks at comparable complexity. Dynamic,
        not a count. NEVER raises (returns the base on error)."""
        base = _env_float("JARVIS_TRUST_BASE", 3.0)
        try:
            acc = self._accumulate(repo)
            return base * max(acc.max_complexity_in_streak, 1.0)
        except Exception:  # noqa: BLE001
            logger.debug(
                "[CrossRepoTrust] adaptive_threshold failed", exc_info=True,
            )
            return base

    def trust_state(self, repo: str) -> TrustState:
        repo_clean = (repo or "").strip()
        acc = self._accumulate(repo_clean)
        threshold = _env_float("JARVIS_TRUST_BASE", 3.0) * max(
            acc.max_complexity_in_streak, 1.0,
        )
        min_streak = _env_int("JARVIS_TRUST_MIN_STREAK", 2)
        # Hard gate: the streak MUST contain at least one PR whose
        # complexity_weight >= JARVIS_TRUST_MIN_COMPLEXITY (default 1.0).
        # A trivial-only streak (all weights below the floor) can NEVER
        # graduate regardless of count or accumulated trust. Fail-CLOSED:
        # any error in reading the env leaves the gate in its default
        # (1.0), so a streak of only sub-1.0 PRs still cannot pass.
        min_complexity = _env_float("JARVIS_TRUST_MIN_COMPLEXITY", 1.0)
        has_nontrivial = acc.max_complexity_in_streak >= min_complexity
        graduated = (
            acc.trust >= threshold
            and acc.streak >= min_streak
            and has_nontrivial
        )
        return TrustState(
            repo=repo_clean,
            streak=acc.streak,
            trust=acc.trust,
            threshold=threshold,
            graduated=graduated,
            last_complexity=acc.last_complexity,
        )

    def is_graduated(self, repo: str) -> bool:
        """``trust >= adaptive_threshold`` AND ``streak >= MIN_STREAK``.

        Fail-CLOSED: returns ``False`` on ANY error — never graduate on
        error (a ledger failure can never relax the operator lock)."""
        try:
            return self.trust_state(repo).graduated
        except Exception:  # noqa: BLE001 — fail-CLOSED
            logger.debug("[CrossRepoTrust] is_graduated failed", exc_info=True)
            return False


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


_DEFAULT_LEDGER: Optional[CrossRepoTrustLedger] = None


def get_cross_repo_trust_ledger() -> CrossRepoTrustLedger:
    global _DEFAULT_LEDGER
    if _DEFAULT_LEDGER is None:
        # Construct with path=None so it honours live env per call.
        _DEFAULT_LEDGER = CrossRepoTrustLedger(path=None)
    return _DEFAULT_LEDGER


def reset_cross_repo_trust_ledger() -> None:
    """Test-only: reset the singleton."""
    global _DEFAULT_LEDGER
    _DEFAULT_LEDGER = None


__all__ = [
    "CrossRepoTrustLedger",
    "TrustState",
    "complexity_weight",
    "get_cross_repo_trust_ledger",
    "reset_cross_repo_trust_ledger",
    "trust_ledger_path",
]
