"""Adapter productivity -- does the served adapter land more work per organism hour?

A training cycle's own gate (6 verification tasks, 4/6 vs 3/6 on 2026-10-07)
says an adapter is not WORSE at answering in schema. Whether it makes the
organism more productive is a different question with a different instrument:
what the organism actually landed while that adapter served it.

## What is measured, from records that already exist

* **Who served when** -- J-Prime's adapter registry: every version's
  ``published_at``, and ``rejected_at`` for one that was rejected. A version
  serves from its publish until the next version that was not rejected is
  published (or until its own rejection). ``origin`` -- the adapter that
  predates the registry -- serves everything before the first publish.
* **Organism hours** -- each session's own ``summary.json``: ``duration_s``
  ending at ``last_activity_ts``. A session belongs to the adapter serving
  when it started: a cycle cannot run beside a live organism (the lease and
  the organism lock), so no session spans an adapter change.
* **Work attempted** -- the summary's ``stats.attempted``.
* **Work landed** -- the landing-provenance ledger: commits proven on the
  promoted branch AND still there (``landed`` and ``surviving``), one per
  commit, attributed to the session whose window holds its AUTHOR time
  (``landing_provenance.surviving_commit_times(authored=True)``): a rebase
  rewrites committer time, never when the organism made the change.

## The comparison, and what it can honestly claim

The active adapter is compared with the INCUMBENT it replaced (the version
serving immediately before it) over a MATCHED exposure: the incumbent's most
recent sessions, as many hours as the active adapter has served, so a long
history does not swamp a short one. Landings are counts over exposure, so the
test is the exact conditional one for two Poisson rates: given n landings in
total, the active adapter's share is Binomial(n, h_active / (h_active +
h_incumbent)) when the rates are equal. The report gives the rate ratio and
the one-sided p that the active adapter is better.

It is NOT a controlled experiment: the work available differs between
sessions (the 2026-10-04 soak's bottleneck was the supply of landable work,
not the model). ``landed_per_attempted`` is reported beside the hourly rate
because it is less exposed to that, and the verdict stays
``insufficient_evidence`` until the active adapter has served the minimum
exposure. NEVER raises from :func:`measure`.
"""
from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.AdapterProductivity")

#: The least service an adapter needs before it is judged, in organism hours.
_ENV_MIN_HOURS = "JARVIS_ADAPTER_PRODUCTIVITY_MIN_HOURS"
#: The one-sided significance at which "better" is claimed.
_ENV_ALPHA = "JARVIS_ADAPTER_PRODUCTIVITY_ALPHA"

ORIGIN = "origin"


@dataclass(frozen=True)
class Session:
    session_id: str
    start: float
    end: float
    attempted: int

    @property
    def hours(self) -> float:
        return max(0.0, self.end - self.start) / 3600.0


@dataclass
class Exposure:
    """One adapter's record over a set of sessions."""
    version: str
    sessions: int = 0
    hours: float = 0.0
    attempted: int = 0
    landed: int = 0

    @property
    def landed_per_hour(self) -> Optional[float]:
        return self.landed / self.hours if self.hours > 0 else None

    @property
    def landed_per_attempted(self) -> Optional[float]:
        return self.landed / self.attempted if self.attempted > 0 else None

    def public(self) -> Dict[str, Any]:
        out = asdict(self)
        out["hours"] = round(self.hours, 2)
        for k in ("landed_per_hour", "landed_per_attempted"):
            v = getattr(self, k)
            out[k] = None if v is None else round(v, 4)
        return out


# ---------------------------------------------------------------------------
# Who served when
# ---------------------------------------------------------------------------

def service_intervals(versions_doc: Dict[str, Any]) -> List[Tuple[str, float, float]]:
    """``(version, from, until)`` from the registry's own records, oldest
    first. A rejected version serves from its publish to its rejection; any
    other serves until the next non-rejected publish. ``origin`` covers
    everything before the first publish. Pure."""
    rows = sorted((v for v in versions_doc.get("versions") or [] if v.get("published_at")),
                  key=lambda v: float(v["published_at"]))
    kept = [v for v in rows if v.get("status") != "rejected"]
    out: List[Tuple[str, float, float]] = []
    first = float(rows[0]["published_at"]) if rows else math.inf
    out.append((ORIGIN, -math.inf, first))
    for i, v in enumerate(kept):
        nxt = float(kept[i + 1]["published_at"]) if i + 1 < len(kept) else math.inf
        out.append((str(v["version"]), float(v["published_at"]), nxt))
    for v in rows:
        if v.get("status") == "rejected":
            out.append((str(v["version"]), float(v["published_at"]),
                        float(v.get("rejected_at") or v["published_at"])))
    return out


def serving_at(intervals: Sequence[Tuple[str, float, float]], t: float) -> str:
    """The version serving at ``t``. A rejected version's own window (its
    verification) wins over the version it was being tested against. Pure."""
    hit = [(start, name) for name, start, until in intervals if start <= t < until]
    return max(hit)[1] if hit else ORIGIN


# ---------------------------------------------------------------------------
# Hours and landings
# ---------------------------------------------------------------------------

def _id_start(name: str) -> Optional[float]:
    """A session id is its start, in UTC: ``bt-YYYY-MM-DD-HHMMSS``."""
    import calendar
    import time as _time
    try:
        return float(calendar.timegm(_time.strptime(name[3:], "%Y-%m-%d-%H%M%S")))
    except ValueError:
        return None


def read_sessions(root: Path) -> Tuple[List[Session], int]:
    """Every session's window and work attempted, and how many sessions had
    no summary (their hours still count; their attempted ops are unknown).

    The window is NOT read from the summary alone. A summary written by the
    atexit fallback of a killed session carries whatever moment the fallback
    captured. The start is the session id (UTC, set at boot); the end is the
    latest evidence of activity -- the debug log's last write (the session's
    own running log), the summary's recorded last activity, or start +
    duration. NOT the summary FILE's mtime: summaries are rewritten after the
    fact (a batch on 2026-09-23 17:21 stretched September sessions to 400 h
    each)."""
    sessions: List[Session] = []
    missing = 0
    for d in sorted(Path(root).glob("bt-*")):
        s: Dict[str, Any] = {}
        try:
            s = json.loads((d / "summary.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            missing += 1
        try:
            dur = float(s.get("duration_s") or 0.0)
            last = float(s.get("last_activity_ts") or 0.0)
            start = _id_start(d.name) or (last - dur if last else None)
            if start is None:
                continue
            ends = [start + dur, last]
            log = d / "debug.log"
            if log.exists():
                ends.append(log.stat().st_mtime)
            sessions.append(Session(session_id=d.name, start=start, end=max(ends),
                                    attempted=int((s.get("stats") or {}).get("attempted") or 0)))
        except (OSError, ValueError, TypeError):
            continue
    return sessions, missing


def tally(sessions: Sequence[Session], landings: Dict[str, float],
          intervals: Sequence[Tuple[str, float, float]]) -> Tuple[Dict[str, Exposure], Dict[str, str], int]:
    """Per-version exposure; which version each session ran under; landings
    outside every session window (not attributable, so not counted)."""
    by: Dict[str, Exposure] = {}
    owner: Dict[str, str] = {}
    for s in sessions:
        v = serving_at(intervals, s.start)
        owner[s.session_id] = v
        e = by.setdefault(v, Exposure(version=v))
        e.sessions += 1
        e.hours += s.hours
        e.attempted += s.attempted
    unattributed = 0
    for t in landings.values():
        s = next((s for s in sessions if s.start <= t <= s.end), None)
        if s is None:
            unattributed += 1
            continue
        by[owner[s.session_id]].landed += 1
    return by, owner, unattributed


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------

def p_better(k_new: int, k_old: int, h_new: float, h_old: float) -> Optional[float]:
    """One-sided exact conditional test that the new rate exceeds the old:
    P(X >= k_new), X ~ Binomial(k_new + k_old, h_new / (h_new + h_old)).
    None when there is no exposure or no landing to test. Pure."""
    n = k_new + k_old
    if n == 0 or h_new <= 0 or h_old <= 0:
        return None
    p = h_new / (h_new + h_old)
    return min(1.0, sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k_new, n + 1)))


def matched_incumbent(sessions: Sequence[Session], owner: Dict[str, str], incumbent: str,
                      hours: float, landings: Dict[str, float]) -> Exposure:
    """The incumbent's most recent sessions covering ``hours`` of service."""
    e = Exposure(version=incumbent)
    for s in sorted((s for s in sessions if owner.get(s.session_id) == incumbent),
                    key=lambda s: s.end, reverse=True):
        if e.hours >= hours:
            break
        e.sessions += 1
        e.hours += s.hours
        e.attempted += s.attempted
        e.landed += sum(1 for t in landings.values() if s.start <= t <= s.end)
    return e


def compare(versions_doc: Dict[str, Any], sessions: Sequence[Session],
            landings: Dict[str, float], *, min_hours: float, alpha: float) -> Dict[str, Any]:
    """The full report: every version's record, and the active adapter
    against its incumbent over matched exposure. Pure."""
    intervals = service_intervals(versions_doc)
    by, owner, unattributed = tally(sessions, landings, intervals)
    active = str(versions_doc.get("active") or ORIGIN)
    order = [name for name, _start, _until in intervals]
    served = [v for v in order if v in by and v != active and order.index(v) < order.index(active)] \
        if active in order else []
    incumbent = served[-1] if served else None
    cur = by.get(active, Exposure(version=active))
    out: Dict[str, Any] = {
        "active": active, "incumbent": incumbent,
        "by_version": {v: e.public() for v, e in by.items()},
        "unattributed_landings": unattributed, "min_hours": min_hours, "alpha": alpha,
    }
    if incumbent is None:
        out["verdict"] = "no_incumbent"
        return out
    old = matched_incumbent(sessions, owner, incumbent, cur.hours, landings)
    out["matched"] = {"active": cur.public(), "incumbent": old.public()}
    ratio = (cur.landed_per_hour / old.landed_per_hour
             if cur.landed_per_hour is not None and old.landed_per_hour else None)
    p = p_better(cur.landed, old.landed, cur.hours, old.hours)
    out["rate_ratio"] = None if ratio is None else round(ratio, 3)
    out["p_better"] = None if p is None else round(p, 4)
    if cur.hours < min_hours:
        out["verdict"] = "insufficient_evidence"
        out["detail"] = f"{cur.hours:.1f} of {min_hours:.1f} organism hours served"
    elif p is not None and p < alpha:
        out["verdict"] = "better"
    elif p is not None and p_better(old.landed, cur.landed, old.hours, cur.hours) is not None \
            and p_better(old.landed, cur.landed, old.hours, cur.hours) < alpha:
        out["verdict"] = "worse"
    else:
        out["verdict"] = "no_detectable_difference"
    return out


# ---------------------------------------------------------------------------
# Live
# ---------------------------------------------------------------------------

def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


async def measure(model: Optional[str] = None) -> Dict[str, Any]:
    """The report for the served model, from J-Prime's registry, the session
    summaries and the landing ledger. NEVER raises: an unreadable source is
    reported as ``error``."""
    from backend.core.ouroboros.governance.observability import training_handoff as th
    model = model or th._model()
    try:
        from backend.core.ouroboros.cli.thin_client import repo_root
        from backend.core.ouroboros.governance.observability.landing_provenance import (
            read_labels, surviving_commit_times)
        versions = await th._http("GET", f"/v1/adapters/{model}", timeout=30)
        sessions, missing = read_sessions(Path(repo_root()) / ".ouroboros" / "sessions")
        labels, _, _ = read_labels()
        landings = await surviving_commit_times(labels, authored=True)
        out = compare(versions, sessions, landings,
                      min_hours=_num(_ENV_MIN_HOURS, 6.0), alpha=_num(_ENV_ALPHA, 0.05))
        out.update({"model": model, "sessions_without_summary": missing})
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("[AdapterProductivity] unmeasurable: %s", exc, exc_info=True)
        return {"model": model, "verdict": "unmeasurable", "error": f"{type(exc).__name__}: {exc}"[:300]}


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import asyncio
    ap = argparse.ArgumentParser(prog="adapter_productivity")
    ap.add_argument("--model", default="")
    args = ap.parse_args(argv)
    from backend.core.env_bootstrap import load_env_once
    load_env_once()
    print(json.dumps(asyncio.run(measure(args.model or None)), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
