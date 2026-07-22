"""OV Metrics Distillery — read-only global telemetry aggregation (2026-07-22).

A Principal-review committee will scrutinise the n-count behind any latency
percentile. This closes that reporting gap ARCHITECTURALLY: instead of a
hand-copied metric, a deterministic read-only query aggregates the recorded
telemetry across ALL campaign sessions and emits the global TTFT
distribution, total sample count, and aggregate cost.

Two telemetry sources, unified — canonical source of truth first, the GC
store second (DRY: the SAME schema the Context Distillation GC's
``compact_telemetry`` establishes — raw ``<table>`` rows + day-bucketed
``<table>_agg`` aggregates):

1. **Session archive (canonical).** Each battle-test session persists a
   ``debug.log`` carrying ``[Slice187] TTFT pure_network=<ms>`` lines (the
   DoubleWord real-time generation lane) and a ``summary.json`` carrying
   ``cost_total``. This is where the campaign's real per-op latency lives.

2. **GC-compacted SQLite (forward-compatible).** When a telemetry DB
   populated by the GC is supplied, its live ``<table>`` rows and
   ``<table>_agg`` day-buckets are folded in — so once production routes
   TTFT through the compacted store, the SAME distillery reports it with no
   change. Opened read-only (``mode=ro``), ``check_same_thread=False`` per
   the GC's cross-thread contract.

STRICTLY READ-ONLY: never writes, never mutates a session or a DB. Every
number is computed from ground truth at call time — nothing is hardcoded.
Fail-soft: an unreadable session/DB is skipped, never fatal.

Usage::

    python3 -m backend.core.ouroboros.governance.ov_metrics_distillery \
        --sessions-glob 'bt-2026-07-2*' [--telemetry-db path.db] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_TTFT_RE = re.compile(r"TTFT pure_network=(\d+)")
_DEFAULT_SESSIONS_ROOT = ".ouroboros/sessions"


def _percentile(sorted_vals: List[int], p: float) -> int:
    """Nearest-rank percentile (0..1). Returns 0 on empty. Pure."""
    if not sorted_vals:
        return 0
    idx = min(int(len(sorted_vals) * p), len(sorted_vals) - 1)
    return sorted_vals[idx]


@dataclass
class MetricsReport:
    sessions_scanned: int = 0
    cost_bearing_sessions: int = 0
    ttft_sample_count: int = 0
    ttft_p50_ms: int = 0
    ttft_p90_ms: int = 0
    ttft_p95_ms: int = 0
    ttft_p99_ms: int = 0
    ttft_min_ms: int = 0
    ttft_max_ms: int = 0
    ttft_mean_ms: float = 0.0
    aggregate_cost_usd: float = 0.0
    telemetry_db_rows: int = 0
    distribution: Dict[str, int] = field(default_factory=dict)

    @property
    def p50_s(self) -> float:
        return round(self.ttft_p50_ms / 1000.0, 1)

    @property
    def p95_s(self) -> float:
        return round(self.ttft_p95_ms / 1000.0, 1)


def _scan_sessions(
    sessions_root: str, sessions_glob: str,
) -> Tuple[List[int], float, int, int]:
    """Return ``(ttft_ms_samples, total_cost, sessions, cost_bearing)`` from
    the session archive. ``sessions_glob`` may be a comma-separated list of
    patterns (deduped) so a campaign spanning a date boundary can be scoped
    precisely. Read-only; fail-soft per session."""
    ttfts: List[int] = []
    total_cost = 0.0
    sessions = 0
    cost_bearing = 0
    root = Path(sessions_root)
    matched = set()
    for pat in (p.strip() for p in sessions_glob.split(",") if p.strip()):
        matched.update(root.glob(pat))
    for d in sorted(matched):
        log = d / "debug.log"
        if not log.is_file():
            continue
        sessions += 1
        try:
            with log.open("r", errors="replace") as fh:
                for line in fh:
                    m = _TTFT_RE.search(line)
                    if m:
                        try:
                            ttfts.append(int(m.group(1)))
                        except (TypeError, ValueError):
                            pass
        except OSError:
            pass
        summ = d / "summary.json"
        if summ.is_file():
            try:
                c = json.loads(summ.read_text(errors="replace")).get("cost_total")
                if isinstance(c, (int, float)):
                    total_cost += float(c)
                    cost_bearing += 1
            except Exception:  # noqa: BLE001 — fail-soft per session
                pass
    return ttfts, total_cost, sessions, cost_bearing


def _fold_telemetry_db(
    db_path: str,
    *,
    table: str = "telemetry",
) -> int:
    """Fold GC-store telemetry into the count (DRY: the same ``<table>`` /
    ``<table>_agg`` schema ``compact_telemetry`` produces). Opened READ-ONLY
    per the GC's cross-thread contract. Returns the raw-row count folded
    (0 if the DB / schema is absent). Never raises."""
    rows = 0
    try:
        uri = f"file:{os.path.abspath(db_path)}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        try:
            cur = conn.cursor()
            # Live raw rows.
            try:
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                rows += int(cur.fetchone()[0] or 0)
            except sqlite3.Error:
                pass
            # Day-bucketed aggregates the GC compacted (row_count column).
            try:
                cur.execute(f"SELECT COALESCE(SUM(row_count),0) FROM {table}_agg")
                rows += int(cur.fetchone()[0] or 0)
            except sqlite3.Error:
                pass
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — DB absent / unreadable → fold nothing
        pass
    return rows


def distill(
    *,
    sessions_root: str = _DEFAULT_SESSIONS_ROOT,
    sessions_glob: str = "bt-2026-07-2*",
    telemetry_db: Optional[str] = None,
) -> MetricsReport:
    """Compute the global campaign metrics report. Read-only; NEVER raises."""
    ttfts, cost, sessions, cost_bearing = _scan_sessions(
        sessions_root, sessions_glob,
    )
    ttfts.sort()
    n = len(ttfts)
    rep = MetricsReport(
        sessions_scanned=sessions,
        cost_bearing_sessions=cost_bearing,
        ttft_sample_count=n,
        aggregate_cost_usd=round(cost, 4),
    )
    if n:
        rep.ttft_p50_ms = _percentile(ttfts, 0.50)
        rep.ttft_p90_ms = _percentile(ttfts, 0.90)
        rep.ttft_p95_ms = _percentile(ttfts, 0.95)
        rep.ttft_p99_ms = _percentile(ttfts, 0.99)
        rep.ttft_min_ms = ttfts[0]
        rep.ttft_max_ms = ttfts[-1]
        rep.ttft_mean_ms = round(sum(ttfts) / n, 1)
        dist: Dict[str, int] = {"<1s": 0, "1-5s": 0, "5-15s": 0,
                                "15-30s": 0, ">30s": 0}
        for t in ttfts:
            if t < 1000:
                dist["<1s"] += 1
            elif t < 5000:
                dist["1-5s"] += 1
            elif t < 15000:
                dist["5-15s"] += 1
            elif t < 30000:
                dist["15-30s"] += 1
            else:
                dist[">30s"] += 1
        rep.distribution = dist
    if telemetry_db:
        rep.telemetry_db_rows = _fold_telemetry_db(telemetry_db)
    return rep


def _format_human(rep: MetricsReport) -> str:
    lines = [
        "OV Metrics Distillery — global campaign telemetry",
        "=" * 52,
        f"  sessions scanned      : {rep.sessions_scanned} "
        f"({rep.cost_bearing_sessions} cost-bearing)",
        f"  DW-RT TTFT samples (n): {rep.ttft_sample_count}",
        f"  TTFT p50 / p90 / p95  : {rep.p50_s}s / "
        f"{rep.ttft_p90_ms/1000:.1f}s / {rep.p95_s}s",
        f"  TTFT p99 / max / mean : {rep.ttft_p99_ms/1000:.1f}s / "
        f"{rep.ttft_max_ms/1000:.1f}s / {rep.ttft_mean_ms/1000:.1f}s",
        f"  aggregate cost (USD)  : ${rep.aggregate_cost_usd:.4f}",
    ]
    if rep.telemetry_db_rows:
        lines.append(f"  GC-store rows folded  : {rep.telemetry_db_rows}")
    if rep.distribution:
        d = rep.distribution
        lines.append(
            "  distribution          : "
            + "  ".join(f"{k}={v}" for k, v in d.items())
        )
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="OV Metrics Distillery")
    ap.add_argument("--sessions-root", default=_DEFAULT_SESSIONS_ROOT)
    ap.add_argument("--sessions-glob", default="bt-2026-07-2*")
    ap.add_argument("--telemetry-db", default=None)
    ap.add_argument("--json", action="store_true")
    ns = ap.parse_args(argv)
    rep = distill(
        sessions_root=ns.sessions_root,
        sessions_glob=ns.sessions_glob,
        telemetry_db=ns.telemetry_db,
    )
    if ns.json:
        print(json.dumps(asdict(rep), indent=2))
    else:
        print(_format_human(rep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
