"""
Trust Calibration Engine — the earned-trust auto-apply envelope (autonomy Gap 4)
================================================================================

Run #24 needed an external graduation auditor for the organism to believe its
own repair. For UNATTENDED operation O+V must verify its work to a standard that
justifies landing it without a human — and today the trust envelope is STATIC
and conservatively hardcoded (Orange → human gate, always). That never adapts to
the organism's actual record: it stays maximally distrustful even after a long
clean streak, and stays permissive right after a regression.

This engine makes the envelope EARNED and ADAPTIVE, per category, from the
DURABLE GIT-PROVABLE track record — never a hardcoded trust value:

  * Per SCOPE (the conventional-commit ``type(scope):`` AutoCommitter writes,
    derivable identically from a past commit's subject and from a new op's
    ``target_files`` via ``auto_committer._infer_scope`` — so historical trust
    and the op being judged share one key), compose the O+V-signed landed
    commits (the Goal-Metric-Dashboard git substrate) into a recency-weighted,
    ASYMMETRIC held-up-vs-reverted rate.
  * ``held up`` = landed and NOT later reverted; ``regressed`` = a git revert of
    an O+V-signed commit (the same ground-truth oracle autonomy_metrics uses).
    Weights are ``_scoring_primitives.recency_weight`` (the house 14d halflife),
    so a recent regression dominates a stale success — trust narrows fast.
  * The trust LEVEL bands the rate through a volume-confidence floor (the
    ``schelling_consensus_prior`` pattern): below a minimum sample the scope is
    UNKNOWN (never trusted on thin evidence), so a 2/2 record cannot masquerade
    as earned trust.

The envelope moves asymmetrically and cage-safely (the seam invariant is
"widen before the floors, narrow as a floor"):

  * NARROW (always active, safety-forward): a scope with LOW trust or a FRESH
    regression contributes a tighter risk-tier FLOOR via ``recommended_floor``'s
    strictest-wins compose — the organism auto-distrusts a region it just broke.
    Because it is a floor, the immune cage re-clamps it for free.
  * WIDEN (double opt-in, DEFAULT-INERT): a scope with sustained HIGH trust over
    sufficient volume and NO fresh regression may relax a human-gated
    APPROVAL_REQUIRED op to auto-applied NOTIFY_APPLY — but ONLY when the
    operator has (a) enabled widening AND (b) raised the ceiling, AND the op
    does NOT touch the cage. Ships doing nothing until explicitly opted into.

Authority posture: read-only over git ground truth; the ONLY behavior it can
effect is through the two documented seams (a floor candidate + a bounded,
cage-excluded, ceiling-clamped relaxation applied BEFORE the immutable floor
stack). NEVER raises.
"""
from __future__ import annotations

import enum
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

TRUST_CALIBRATION_SCHEMA_VERSION = "trust_calibration.1"

# conventional-commit ``type(scope): ...`` — the per-category trust key.
_SCOPE_RE = re.compile(r"^\s*[a-zA-Z]+\(([^)]{1,60})\)\s*:")


class TrustLevel(str, enum.Enum):
    UNKNOWN = "unknown"   # below the volume floor — never earned trust yet
    LOW = "low"           # regressed / poor held-up rate → auto-NARROW
    MEDIUM = "medium"     # decent, not yet widen-worthy
    HIGH = "high"         # sustained clean record → widen-eligible


# ---------------------------------------------------------------------------
# Env knobs — additive, clamped; widening is DOUBLE opt-in + default-inert
# ---------------------------------------------------------------------------


def master_enabled() -> bool:
    """``JARVIS_TRUST_CALIBRATION_ENABLED`` (default true). The read/observe/
    NARROW engine. Off → no floor contribution, no widening, snapshot inert."""
    raw = os.environ.get("JARVIS_TRUST_CALIBRATION_ENABLED", "true")
    return raw.strip().lower() not in ("0", "false", "no", "off")


def widen_enabled() -> bool:
    """``JARVIS_TRUST_WIDEN_ENABLED`` (default FALSE). The cage-loosening half —
    the FIRST of the double opt-in. Off → the engine can only NARROW."""
    raw = os.environ.get("JARVIS_TRUST_WIDEN_ENABLED", "false")
    return raw.strip().lower() in ("1", "true", "yes", "on")


def max_auto_tier() -> str:
    """``JARVIS_TRUST_MAX_AUTO_TIER`` — the operator CEILING: the most permissive
    tier trust may EVER relax to (the SECOND opt-in). Default ``""`` = no
    widening. Set e.g. ``notify_apply`` to permit Orange→Yellow relaxation."""
    return os.environ.get("JARVIS_TRUST_MAX_AUTO_TIER", "").strip().lower()


def _clamped_int(env: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(os.environ.get(env, "").strip() or default)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _clamped_float(env: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float(os.environ.get(env, "").strip() or default)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def window_days() -> int:
    return _clamped_int("JARVIS_TRUST_WINDOW_DAYS", 60, 1, 3650)


def halflife_days() -> float:
    return _clamped_float("JARVIS_TRUST_HALFLIFE_DAYS", 14.0, 0.5, 3650.0)


def min_sample() -> int:
    """Volume floor: below this recency-weighted sample a scope is UNKNOWN."""
    return _clamped_int("JARVIS_TRUST_MIN_SAMPLE", 5, 1, 10000)


def widen_min_sample() -> int:
    """Higher volume floor required specifically to WIDEN (stricter than the
    banding floor — widening the cage demands more evidence than observing)."""
    return _clamped_int("JARVIS_TRUST_WIDEN_MIN_SAMPLE", 8, 1, 10000)


def low_threshold() -> float:
    return _clamped_float("JARVIS_TRUST_LOW_THRESHOLD", 0.5, 0.0, 1.0)


def high_threshold() -> float:
    """Held-up rate at/above which a scope is HIGH (widen-eligible). Deliberately
    strict — auto-applying formerly-human-gated changes needs a strong record."""
    return _clamped_float("JARVIS_TRUST_HIGH_THRESHOLD", 0.9, 0.0, 1.0)


def regression_window_s() -> float:
    """A revert within this window is a FRESH regression → forces LOW + a hard
    narrowing floor for the scope, regardless of the historical rate."""
    return _clamped_float("JARVIS_TRUST_REGRESSION_WINDOW_S", 7 * 86400.0, 60.0, 3650 * 86400.0)


# ---------------------------------------------------------------------------
# Per-scope track record — composed from the git ground truth
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScopeTrust:
    scope: str
    trust_level: str
    held_up_rate: Optional[float]      # recency-weighted; None on empty denom
    sample_count: int                  # landed commits (held + reverted)
    held_up: int
    reverted: int
    recent_regression: bool
    last_landed_unix: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scope": self.scope,
            "trust_level": self.trust_level,
            "held_up_rate": self.held_up_rate,
            "sample_count": self.sample_count,
            "held_up": self.held_up,
            "reverted": self.reverted,
            "recent_regression": self.recent_regression,
        }


def _extract_scope(body: str) -> str:
    if not body:
        return "unknown"
    subject = body.strip().split("\n", 1)[0]
    m = _SCOPE_RE.match(subject)
    return m.group(1).strip() if m else "unknown"


def _recency_weight(age_s: float, hl_days: float) -> float:
    """Compose the house recency primitive; degrade to a local halving on any
    fault (never let the trust read raise)."""
    try:
        from backend.core.ouroboros.governance._scoring_primitives import (
            recency_weight,
        )
        return recency_weight(age_s, hl_days)
    except Exception:  # noqa: BLE001
        age_days = max(0.0, age_s) / 86400.0
        try:
            return max(0.0, min(1.0, 0.5 ** (age_days / max(hl_days, 1e-6))))
        except Exception:  # noqa: BLE001
            return 0.0


def _collect_scope_records(
    repo: Path, now: float,
) -> Dict[str, List[Tuple[bool, float]]]:
    """Per scope → list of ``(held_up, commit_time_unix)`` for O+V-signed,
    non-Orange, non-empty landed commits in the window. ``held_up`` = the commit
    sha is not the target of any git revert in the window. NEVER raises."""
    out: Dict[str, List[Tuple[bool, float]]] = {}
    try:
        from backend.core.ouroboros.governance import autonomy_metrics as am
        trailer = am._ov_trailer()
        if not trailer:
            return out
        since = now - (window_days() * 86400.0)
        raw = am._run_git_log(repo, am.target_branch(), since, am.commit_scan_max())
        commits = am._parse_commits(raw)
        reverted_refs: set = set()
        for c in commits:
            for m in am._REVERT_RE.finditer(c.body):
                reverted_refs.add(m.group(1).lower())
        landed = [
            c for c in commits
            if am._is_ov_commit(c.body, trailer) and not am._is_orange(c.body) and c.files
        ]
        landed_shas = {c.commit_hash.lower() for c in landed}
        landed_shas |= {s[:12] for s in landed_shas}

        def _is_reverted(sha: str) -> bool:
            s = sha.lower()
            for ref in reverted_refs:
                if s == ref or s.startswith(ref) or ref.startswith(s):
                    return True
            return False

        for c in landed:
            scope = _extract_scope(c.body)
            held = not _is_reverted(c.commit_hash)
            out.setdefault(scope, []).append((held, float(c.commit_time_unix)))
    except Exception:  # noqa: BLE001
        return out
    return out


def _band(rate: Optional[float], sample: int, recent_regression: bool) -> TrustLevel:
    if recent_regression:
        return TrustLevel.LOW           # a fresh break is LOW regardless of rate
    if rate is None or sample < min_sample():
        return TrustLevel.UNKNOWN
    if rate < low_threshold():
        return TrustLevel.LOW
    if rate < high_threshold():
        return TrustLevel.MEDIUM
    return TrustLevel.HIGH


def _compute(scope: str, records: List[Tuple[bool, float]], now: float) -> ScopeTrust:
    hl = halflife_days()
    num = den = 0.0
    held = rev = 0
    last = 0.0
    recent = False
    rwin = regression_window_s()
    for held_up, ct in records:
        w = _recency_weight(max(0.0, now - ct), hl)
        den += w
        if held_up:
            num += w
            held += 1
        else:
            rev += 1
            if (now - ct) <= rwin:
                recent = True
        last = max(last, ct)
    rate = (num / den) if den > 0 else None
    sample = held + rev
    return ScopeTrust(
        scope=scope, trust_level=_band(rate, sample, recent).value,
        held_up_rate=(round(rate, 4) if rate is not None else None),
        sample_count=sample, held_up=held, reverted=rev,
        recent_regression=recent, last_landed_unix=last,
    )


# ---------------------------------------------------------------------------
# Cached trust report
# ---------------------------------------------------------------------------

_LOCK = threading.RLock()
_REPORT: Optional[Dict[str, ScopeTrust]] = None
_REPORT_TS: float = 0.0


def _ttl_s() -> float:
    return _clamped_float("JARVIS_TRUST_TTL_S", 300.0, 1.0, 86400.0)


def trust_report(
    *, repo_root: Optional[Path] = None, now: Optional[float] = None,
    force: bool = False,
) -> Dict[str, ScopeTrust]:
    """Per-scope ScopeTrust, TTL-cached (git walk amortized across GATE reads).
    NEVER raises."""
    global _REPORT, _REPORT_TS
    _now = float(now) if now is not None else time.time()
    with _LOCK:
        if not force and _REPORT is not None and (_now - _REPORT_TS) <= _ttl_s():
            return dict(_REPORT)
    try:
        from backend.core.ouroboros.governance.autonomy_metrics import (
            _resolve_repo_root,
        )
        repo = repo_root if repo_root is not None else _resolve_repo_root()
        records = _collect_scope_records(repo, _now)
        report = {sc: _compute(sc, recs, _now) for sc, recs in records.items()}
    except Exception:  # noqa: BLE001
        report = {}
    with _LOCK:
        _REPORT = report
        _REPORT_TS = _now
    return dict(report)


def scope_trust(
    scope: str, *, repo_root: Optional[Path] = None, now: Optional[float] = None,
) -> ScopeTrust:
    """ScopeTrust for one scope (UNKNOWN when absent). NEVER raises."""
    try:
        rep = trust_report(repo_root=repo_root, now=now)
        if scope in rep:
            return rep[scope]
    except Exception:  # noqa: BLE001
        pass
    return ScopeTrust(scope=scope, trust_level=TrustLevel.UNKNOWN.value,
                      held_up_rate=None, sample_count=0, held_up=0, reverted=0,
                      recent_regression=False)


def reset_cache_for_tests() -> None:
    global _REPORT, _REPORT_TS
    with _LOCK:
        _REPORT = None
        _REPORT_TS = 0.0


# ---------------------------------------------------------------------------
# Decision A — NARROWING floor candidate (always active, safety-forward)
# ---------------------------------------------------------------------------


def trust_narrowing_tier(
    scope: str, *, repo_root: Optional[Path] = None, now: Optional[float] = None,
) -> Optional[str]:
    """A tier-NAME floor for ``recommended_floor`` (strictest-wins), or None.

    A FRESH regression in the scope floors it to ``approval_required`` (human
    gate) until it recovers; a merely LOW historical record floors it to
    ``notify_apply`` (still auto, but surfaced). Composes cage-safely — it can
    only RAISE the effective tier. NEVER raises."""
    if not master_enabled():
        return None
    try:
        st = scope_trust(scope, repo_root=repo_root, now=now)
        if st.recent_regression:
            return "approval_required"
        if st.trust_level == TrustLevel.LOW.value:
            return "notify_apply"
    except Exception:  # noqa: BLE001
        return None
    return None


# ---------------------------------------------------------------------------
# Decision B — WIDENING relaxation (double opt-in, default-inert, cage-excluded)
# ---------------------------------------------------------------------------

def maybe_relax_tier(
    risk_tier: Any, *, scope: str, touches_cage: bool,
    repo_root: Optional[Path] = None, now: Optional[float] = None,
) -> Tuple[Any, Optional[str]]:
    """Return ``(possibly_relaxed_tier, rationale)``. Applied at GATE ENTRY,
    BEFORE the immutable floor stack (which re-clamps the cage), so this can
    only ever *propose* a relaxation the floors are free to override.

    Relaxes a human-gated ``APPROVAL_REQUIRED`` op to auto-applied
    ``NOTIFY_APPLY`` IFF ALL hold — else returns the tier UNCHANGED:
      * widening is opted in (``JARVIS_TRUST_WIDEN_ENABLED``) AND the operator
        ceiling (``JARVIS_TRUST_MAX_AUTO_TIER``) permits ``notify_apply``;
      * the op does NOT touch the cage;
      * the scope's trust is HIGH over ≥ ``widen_min_sample`` with NO fresh
        regression.
    DEFAULT-INERT: both opt-ins default off → always returns the tier unchanged.
    NEVER raises (any fault → unchanged tier, fail-closed to the human gate)."""
    try:
        if not (master_enabled() and widen_enabled()):
            return risk_tier, None
        if touches_cage:
            return risk_tier, "cage_excluded"
        ceiling = max_auto_tier()
        # Ceiling must explicitly permit notify_apply-level auto-apply.
        if ceiling not in ("notify_apply", "safe_auto"):
            return risk_tier, None
        name = _tier_name(risk_tier)
        if name != "approval_required":
            return risk_tier, None  # only relax the human-gated tier
        st = scope_trust(scope, repo_root=repo_root, now=now)
        if (
            st.trust_level == TrustLevel.HIGH.value
            and st.sample_count >= widen_min_sample()
            and not st.recent_regression
        ):
            relaxed = _tier_from_name("notify_apply", risk_tier)
            return relaxed, (
                f"widened approval_required->notify_apply "
                f"(scope={scope} rate={st.held_up_rate} n={st.sample_count})"
            )
        return risk_tier, None
    except Exception:  # noqa: BLE001 — fail closed to the human gate
        return risk_tier, None


def _op_touches_cage(target_files: List[str]) -> bool:
    """True iff the op touches the immune cage (kernel/security/self-mod). FAIL-
    CLOSED: any fault → True (treat as cage → never widen). Composes the
    canonical RiskEngine sentinels — the SAME predicate the cage uses."""
    if not target_files:
        return False
    try:
        from backend.core.ouroboros.governance.risk_engine import RiskEngine
        eng = RiskEngine()
        return bool(
            eng._matches_any(target_files, eng._kernel_sentinels())
            or eng._matches_any(target_files, eng._security_sentinels())
            or eng._matches_any(target_files, eng._self_mod_sentinels())
        )
    except Exception:  # noqa: BLE001 — fail closed to "is cage" (no widen)
        return True


def _infer_scope_for(target_files: List[str]) -> str:
    try:
        from backend.core.ouroboros.governance.auto_committer import AutoCommitter
        return AutoCommitter._infer_scope(tuple(target_files)) if target_files else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def relax_tier_for_op(
    risk_tier: Any, ctx: Any, *,
    repo_root: Optional[Path] = None, now: Optional[float] = None,
) -> Tuple[Any, Optional[str]]:
    """GATE-entry convenience: derive scope + cage from ``ctx.target_files`` and
    apply the bounded earned-trust widening. The single call the GATE seams make
    (keeps the load-bearing GATE diff to one guarded line). Default-inert; NEVER
    raises — any fault returns the tier unchanged (fail-closed to the gate)."""
    try:
        tf = [str(f) for f in (getattr(ctx, "target_files", None) or ())]
        return maybe_relax_tier(
            risk_tier, scope=_infer_scope_for(tf),
            touches_cage=_op_touches_cage(tf),
            repo_root=repo_root, now=now,
        )
    except Exception:  # noqa: BLE001
        return risk_tier, None


def _tier_name(risk_tier: Any) -> str:
    v = getattr(risk_tier, "name", None) or getattr(risk_tier, "value", None) or risk_tier
    return str(v).lower()


def _tier_from_name(name: str, like: Any) -> Any:
    """Return a RiskTier of ``name`` matching the enum type of ``like`` (so the
    GATE gets the same enum it passed). Falls back to the name string."""
    try:
        cls = type(like)
        for member in cls:  # type: ignore[call-overload]
            if str(member.name).lower() == name:
                return member
    except Exception:  # noqa: BLE001
        pass
    return name


# ---------------------------------------------------------------------------
# Observability snapshot
# ---------------------------------------------------------------------------


def snapshot(*, repo_root: Optional[Path] = None, force: bool = False) -> Dict[str, Any]:
    """Read-only trust snapshot for the observability GET. NEVER raises."""
    if not master_enabled():
        return {
            "schema_version": TRUST_CALIBRATION_SCHEMA_VERSION,
            "enabled": False, "reason_code": "disabled",
        }
    try:
        rep = trust_report(repo_root=repo_root, force=force)
        by_level: Dict[str, int] = {}
        for st in rep.values():
            by_level[st.trust_level] = by_level.get(st.trust_level, 0) + 1
        scopes = sorted((st.to_dict() for st in rep.values()),
                        key=lambda d: (d["trust_level"] != "low", d["scope"]))
        widen_eligible = sorted(
            st.scope for st in rep.values()
            if st.trust_level == TrustLevel.HIGH.value
            and st.sample_count >= widen_min_sample()
            and not st.recent_regression
        )
        narrowing = sorted(
            st.scope for st in rep.values()
            if st.recent_regression or st.trust_level == TrustLevel.LOW.value
        )
        return {
            "schema_version": TRUST_CALIBRATION_SCHEMA_VERSION,
            "enabled": True,
            "reason_code": "ok",
            "envelope": {
                # the operator opt-in state — widening does NOTHING until both.
                "widen_enabled": widen_enabled(),
                "max_auto_tier": max_auto_tier() or None,
                "active_narrowing_scopes": narrowing,
                "widen_eligible_scopes": widen_eligible,
            },
            "counts_by_level": by_level,
            "scopes": scopes,
            "interpretation": (
                "Trust is earned per scope from the git-provable held-up-vs-"
                "reverted record (recency-weighted, asymmetric). NARROWING "
                "(LOW/fresh-regression → tighter floor) is always active; "
                "WIDENING (HIGH → relax Orange to auto) is default-inert until "
                "the operator both enables it and raises the ceiling, and never "
                "touches the immune cage."
            ),
        }
    except Exception:  # noqa: BLE001
        return {
            "schema_version": TRUST_CALIBRATION_SCHEMA_VERSION,
            "enabled": True, "reason_code": "snapshot_error",
        }
