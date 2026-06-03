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


def render(rows) -> str:
    items = sorted(rows.items())
    scored = [(k, v) for k, v in items if v is not None]
    total = len(items)
    resolved = sum(1 for _, v in scored if v.get("resolved") is True)
    rate = (resolved / total * 100.0) if total else 0.0
    out = []
    out.append("# O+V — SWE-Bench-Pro Report Card")
    out.append("")
    out.append(f"## Resolved: **{resolved} / {total} = {rate:.1f}%**")
    out.append("")
    out.append("| Repo | Instance | eval_outcome | score_outcome | resolved |")
    out.append("|---|---|---|---|:---:|")
    for iid, r in items:
        if r is None:
            out.append(f"| {_repo(iid)} | {_short(iid)} | _(no row)_ | _(no row)_ | — |")
            continue
        ev = r.get("evaluation") or {}
        sc = r.get("scoring") or {}
        mark = "✅" if r.get("resolved") is True else "❌"
        out.append(
            f"| {_repo(iid)} | {_short(iid)} | {ev.get('outcome')} | "
            f"{sc.get('outcome')} | {mark} |"
        )
    out.append("")
    out.append(
        "_Methodology: each instance is the latest durable verdict in "
        "results.jsonl; `resolved` = held-out container suite passed "
        "(eval=resolved AND score=pass). Provider: Claude. Platform: "
        "linux/amd64 via Apple-Silicon emulation._"
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
