#!/usr/bin/env python3
"""EVAL-2 — macro report card from the durable SWE-Bench-Pro result ledger.

Reads ``.jarvis/swe_bench_pro/results.jsonl`` (the Slice 74/75 durable ledger),
takes the LATEST verdict per ``instance_id`` (the ledger accumulates a row per
solve-op across runs; for a sample percentage we want one verdict per problem),
and renders a Markdown report with the headline ``resolved / N = Y%`` over the
canonical Slice 75 ``resolved`` boolean.

Leverages the canonical EvaluationRecord schema (``evaluation``/``scoring``/
``resolved`` fields) — no new aggregation logic, no hardcoded instance ids. Scope
to a sample with a comma/space-separated id list as argv[1]; omit to report every
instance in the ledger.

Usage:
  python3 scripts/swe_bench_pro_report.py "id1,id2,..."   # scope to a sample
  python3 scripts/swe_bench_pro_report.py                  # all instances
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

LEDGER = Path(".jarvis/swe_bench_pro/results.jsonl")


def _short(instance_id: str) -> str:
    # "instance_qutebrowser__qutebrowser-…b3c171" -> "qutebrowser…b3c171"
    base = instance_id.split("__", 1)[1] if "__" in instance_id else instance_id
    return base[:46]


def _repo(instance_id: str) -> str:
    m = re.match(r"instance_([^_]+(?:-[^_]+)*)__", instance_id)
    return m.group(1) if m else instance_id.split("__", 1)[0]


def build(target_ids):
    if not LEDGER.exists():
        return None, "no results.jsonl ledger found"
    latest = {}  # instance_id -> row (file order is chronological; last wins)
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except (ValueError, TypeError):
            continue
        ev = r.get("evaluation") or {}
        iid = ev.get("problem_instance_id") or ""
        if iid:
            latest[iid] = r
    if target_ids:
        rows = {k: v for k, v in latest.items() if k in target_ids}
        # surface any requested instance that produced no row at all
        for tid in target_ids:
            rows.setdefault(tid, None)
    else:
        rows = latest
    return rows, None


_CATEGORY_MARK = {
    "resolved": "✅ resolved",
    "capability_miss": "❌ capability_miss",
    "infrastructure_exclusion": "⚠️ infra_exclusion",
}


def _category(row) -> str:
    """Slice 76 Phase 3 — read the persisted `category` if present, else derive
    it from the (eval, score) outcomes (back-compat for pre-Slice-76 rows).
    Mirrors result_store.EvaluationRecord.category exactly."""
    cat = row.get("category")
    if cat in _CATEGORY_MARK:
        return cat
    ev = ((row.get("evaluation") or {}).get("outcome")) or ""
    sc = ((row.get("scoring") or {}).get("outcome")) or ""
    if ev == "resolved" and sc == "pass":
        return "resolved"
    if ev in ("prepare_failed", "terminal_timeout"):
        return "infrastructure_exclusion"
    if ev == "resolved" and sc == "scoring_error":
        return "infrastructure_exclusion"
    return "capability_miss"


def render(rows) -> str:
    items = sorted(rows.items())
    cats = {iid: (_category(v) if v is not None else None) for iid, v in items}
    total = len(items)
    # a requested instance that produced NO row at all never got a fair attempt
    resolved = sum(1 for c in cats.values() if c == "resolved")
    excluded = sum(1 for c in cats.values()
                   if c == "infrastructure_exclusion" or c is None)
    cap_miss = sum(1 for c in cats.values() if c == "capability_miss")
    fairly = total - excluded
    strict = (resolved / total * 100.0) if total else 0.0
    operational = (resolved / fairly * 100.0) if fairly else 0.0
    out = []
    out.append("# O+V — SWE-Bench-Pro Report Card")
    out.append("")
    out.append(f"## Strict: **{resolved} / {total} = {strict:.1f}%**  ·  "
               f"Operational (fairly-attempted): **{resolved} / {fairly} = "
               f"{operational:.1f}%**")
    out.append("")
    out.append(f"- ✅ resolved: **{resolved}**  ·  ❌ capability_miss: "
               f"**{cap_miss}**  ·  ⚠️ infrastructure_exclusion: **{excluded}**")
    out.append("")
    out.append("| Repo | Instance | eval_outcome | score_outcome | category |")
    out.append("|---|---|---|---|:---:|")
    for iid, r in items:
        if r is None:
            out.append(f"| {_repo(iid)} | {_short(iid)} | _(no row)_ | "
                       f"_(no row)_ | ⚠️ infra_exclusion |")
            continue
        ev = r.get("evaluation") or {}
        sc = r.get("scoring") or {}
        out.append(
            f"| {_repo(iid)} | {_short(iid)} | {ev.get('outcome')} | "
            f"{sc.get('outcome')} | {_CATEGORY_MARK.get(cats[iid] or '', cats[iid] or '?')} |"
        )
    out.append("")
    out.append(
        "_Methodology: each instance is the latest durable verdict in "
        "results.jsonl. **Strict** counts every row; **Operational** excludes "
        "`infrastructure_exclusion` rows (prepare_failed / terminal_timeout / "
        "scoring_error — never a fair attempt), NOT the capability misses. "
        "`resolved` = held-out container suite passed (eval=resolved AND "
        "score=pass). Provider: Claude. Platform: linux/amd64 via Apple-Silicon "
        "emulation._"
    )
    return "\n".join(out)


def main():
    raw = sys.argv[1] if len(sys.argv) > 1 else ""
    target_ids = [s for s in re.split(r"[,\s]+", raw.strip()) if s] or None
    rows, err = build(target_ids)
    if err:
        print(err)
        sys.exit(1)
    print(render(rows))


if __name__ == "__main__":
    main()
