"""Slice 224 — Token-Economic Accounting Fabric (read-only spend analytics).

Answers the operator's question — "where is the money going?" — from the
authoritative Aegis spend WAL (``.jarvis/aegis/spend.jsonl`` + its rotation
backups). Only ``kind=reconcile`` records count (real, usage-reconciled
``actual_cost_usd``); admits/reserves are accounting holds, not spend.

PROVIDER ATTRIBUTION IS HONEST-HEURISTIC and labeled as such in every
output: the spend WAL does not carry an explicit provider field, so we
attribute ``dw-*`` op-ids to DoubleWord and the rest to Claude (the Aegis
daemon's two upstreams), rendered as ``claude(inferred)`` /
``doubleword(inferred)`` so a reader never mistakes the heuristic for
ground truth. The Anthropic/DW consoles remain the authoritative external
numbers — this fabric is for *attribution shape* (which day, which route,
which provider, how many calls), which consoles can't see per-op.

Read-only, stdlib-only, NEVER raises. Surfaces:
  * ``rollup_spend()`` / ``format_spend_report()`` — library
  * ``/spend`` REPL verb (serpent_flow) — live in-session
  * ``python3 -m backend.core.ouroboros.governance.accounting_ledger`` —
    host-side CLI over the bind-mounted ``.jarvis`` (works on a dead
    container too).
"""
from __future__ import annotations

import glob
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

__all__ = ["SpendRollup", "rollup_spend", "format_spend_report"]


@dataclass
class SpendRollup:
    total_usd: float = 0.0
    calls: int = 0
    by_day: Dict[str, float] = field(default_factory=dict)
    by_provider: Dict[str, float] = field(default_factory=dict)
    by_route: Dict[str, float] = field(default_factory=dict)
    by_day_provider: Dict[str, Dict[str, float]] = field(default_factory=dict)
    ledger_files: int = 0
    skipped_lines: int = 0


def _provider_of(op_id: str) -> str:
    """Honest heuristic — labeled '(inferred)' everywhere it surfaces."""
    if str(op_id).startswith("dw-"):
        return "doubleword(inferred)"
    return "claude(inferred)"


def _day_of(ts: float) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(float(ts)))
    except Exception:  # noqa: BLE001
        return "unknown"


def rollup_spend(*, jarvis_dir: Optional[Path] = None) -> SpendRollup:
    """Aggregate every reconcile record across the live spend WAL + all
    rotation backups. NEVER raises — garbage lines are counted+skipped."""
    r = SpendRollup()
    try:
        base = Path(jarvis_dir) if jarvis_dir else Path(
            os.environ.get("JARVIS_DIR", ".jarvis"),
        )
        pattern = str(base / "aegis" / "spend.jsonl*")
        for f in sorted(glob.glob(pattern)):
            r.ledger_files += 1
            try:
                lines = Path(f).read_text(encoding="utf-8").splitlines()
            except Exception:  # noqa: BLE001
                continue
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:  # noqa: BLE001
                    r.skipped_lines += 1
                    continue
                if d.get("kind") != "reconcile":
                    continue
                cost = d.get("actual_cost_usd")
                if not isinstance(cost, (int, float)) or cost <= 0:
                    continue
                cost = float(cost)
                day = _day_of(d.get("ts", 0))
                prov = _provider_of(d.get("op_id", ""))
                route = str(d.get("route") or "unknown")
                r.total_usd += cost
                r.calls += 1
                r.by_day[day] = r.by_day.get(day, 0.0) + cost
                r.by_provider[prov] = r.by_provider.get(prov, 0.0) + cost
                r.by_route[route] = r.by_route.get(route, 0.0) + cost
                r.by_day_provider.setdefault(day, {})
                r.by_day_provider[day][prov] = (
                    r.by_day_provider[day].get(prov, 0.0) + cost
                )
    except Exception:  # noqa: BLE001
        pass
    return r


def format_spend_report(r: SpendRollup) -> str:
    """Scannable day x provider x route matrix. NEVER raises."""
    try:
        out = [
            f"TOTAL reconciled spend: ${r.total_usd:.2f} across {r.calls} "
            f"billable calls ({r.ledger_files} ledger file(s), "
            f"{r.skipped_lines} garbage line(s) skipped)",
            "",
            "by provider (op-id heuristic — inferred, consoles are "
            "authoritative):",
        ]
        for k, v in sorted(r.by_provider.items(), key=lambda x: -x[1]):
            out.append(f"  {k:24} ${v:.2f}")
        out.append("")
        out.append("by route:")
        for k, v in sorted(r.by_route.items(), key=lambda x: -x[1]):
            out.append(f"  {k:24} ${v:.2f}")
        out.append("")
        out.append("by day x provider:")
        for day in sorted(r.by_day_provider):
            cell = "  ".join(
                f"{p.split('(')[0]}=${v:.2f}"
                for p, v in sorted(r.by_day_provider[day].items())
            )
            out.append(f"  {day}  {cell}  (day total ${r.by_day[day]:.2f})")
        return "\n".join(out)
    except Exception:  # noqa: BLE001
        return f"TOTAL: ${r.total_usd:.2f} ({r.calls} calls)"


def main() -> int:  # pragma: no cover — host-side CLI
    import argparse
    ap = argparse.ArgumentParser(
        description="O+V spend attribution over the Aegis spend WAL",
    )
    ap.add_argument("--jarvis-dir", default=".jarvis")
    args = ap.parse_args()
    print(format_spend_report(rollup_spend(jarvis_dir=Path(args.jarvis_dir))))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
