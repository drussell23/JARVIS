#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sovereign Artifact Extractor -- end-game capture for the first autonomous PR.

When the live Ouroboros node pushes its first orange-tier (held-for-approval)
PR, this standalone tool hooks GitHub via the ``gh`` CLI and pulls the PR diff,
the commit message + metadata, AND the local Doubleword latency/lane telemetry
(harvested from the local Sentinel logs under ``autopsy_reports/``) into one
clean, timestamped Markdown artifact on the local machine.

Standalone by design -- it imports NOTHING from the JARVIS core codebase and
mutates no live FSM state. Pure subprocess(gh) + local log parsing. Fail-soft.

Usage:
    # auto-detect the latest ouroboros/review PR + write the artifact:
    python3 scripts/extract_sovereign_pr.py
    # or pin a PR number + a sentinel log:
    python3 scripts/extract_sovereign_pr.py --pr 69670 \
        --sentinel-log autopsy_reports/sentinel_<node>.log --out my_pr.md
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple


# --------------------------------------------------------------------------- #
# gh CLI (fail-soft subprocess wrappers).
# --------------------------------------------------------------------------- #
def _gh(args: List[str], *, timeout_s: float = 60.0) -> Tuple[int, str]:
    try:
        p = subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=timeout_s,
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:  # noqa: BLE001 -- extractor never crashes
        return 1, f"[gh failed: {exc!r}]"


def find_orange_pr(*, repo: Optional[str] = None) -> Optional[Dict]:
    """Find the most recent autonomous orange-tier PR (head branch
    ``ouroboros/review/*`` OR an Ouroboros/Crucible commit author). Returns the
    PR dict (number,title,headRefName,url,author) or None."""
    args = ["pr", "list", "--state", "all", "--limit", "30", "--json",
            "number,title,headRefName,url,author,createdAt,mergedAt,state"]
    if repo:
        args += ["--repo", repo]
    rc, out = _gh(args)
    if rc != 0:
        return None
    try:
        prs = json.loads(out)
    except Exception:  # noqa: BLE001
        return None
    # The autonomous orange PR is created by OrangePRReviewer on a
    # ``ouroboros/review/<op-id>`` branch, authored by the loop's own identity
    # (e.g. "Ouroboros Sovereign Crucible"). Key on those STRUCTURAL signals --
    # NOT a loose title match (which false-positives on docs PRs about Ouroboros).
    def _is_orange(p: Dict) -> bool:
        head = str(p.get("headRefName", ""))
        login = str((p.get("author") or {}).get("login", "")).lower()
        return head.startswith("ouroboros/review") or (
            "ouroboros" in login or "crucible" in login or "sovereign" in login
        )
    _orange = [p for p in prs if _is_orange(p)]
    _orange.sort(key=lambda p: str(p.get("createdAt", "")), reverse=True)
    return _orange[0] if _orange else None


def pull_pr_artifacts(number: int, *, repo: Optional[str] = None) -> Dict:
    """Pull diff + commit messages + metadata for a PR. Fail-soft per field."""
    base = ["--repo", repo] if repo else []
    _, view = _gh(["pr", "view", str(number), *base, "--json",
                   "number,title,body,headRefName,baseRefName,url,author,"
                   "createdAt,mergedAt,state,additions,deletions,changedFiles,commits"])
    meta: Dict = {}
    try:
        meta = json.loads(view)
    except Exception:  # noqa: BLE001
        meta = {}
    _, diff = _gh(["pr", "diff", str(number), *base], timeout_s=120.0)
    commits = meta.get("commits") or []
    commit_msgs = "\n".join(
        f"- {c.get('oid', '')[:10]} {c.get('messageHeadline', '')}"
        for c in commits
    ) or "(no commit metadata)"
    return {"meta": meta, "diff": diff, "commit_messages": commit_msgs}


# --------------------------------------------------------------------------- #
# Local Doubleword latency / lane telemetry (from the Sentinel logs).
# --------------------------------------------------------------------------- #
_LANE_MARKERS = {
    "lane_collapse": r"\[SOVEREIGN YIELD: LANE COLLAPSE\]",
    "unresolvable": r"\[SOVEREIGN YIELD: UNRESOLVABLE PATH\]",
    "batch_timeout": r"Batch retrieval|batch.*TIMEOUT|fsm_failure_mode=TIMEOUT",
    "realtime_rotation": r"select_lane|rotate.*realtime|batch.*OPEN|TransportBreaker",
    "dispatch_decompose": r"BLOCK decomposed.*dispatched_this_tick",
    "generation_ok": r"state=applied|emitting 2b|candidate generated",
    "real_drop": r"\[SovereignPropagation\] REAL DROP",
}


def harvest_dw_metrics(sentinel_logs: List[str]) -> Dict[str, object]:
    """Scan local Sentinel logs for DW lane/latency telemetry. Returns counts +
    sampled lines per marker. Fail-soft -> partial."""
    counts: Dict[str, int] = {k: 0 for k in _LANE_MARKERS}
    samples: Dict[str, List[str]] = {k: [] for k in _LANE_MARKERS}
    compiled = {k: re.compile(v, re.IGNORECASE) for k, v in _LANE_MARKERS.items()}
    last_transition = ""
    total_lines = 0
    for path in sentinel_logs:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    total_lines += 1
                    for k, rx in compiled.items():
                        if rx.search(line):
                            counts[k] += 1
                            if len(samples[k]) < 5:
                                samples[k].append(line.rstrip()[-200:])
                    if "transitions=" in line:
                        last_transition = line.rstrip()[-160:]
        except Exception:  # noqa: BLE001
            continue
    return {"counts": counts, "samples": samples,
            "last_heartbeat": last_transition, "lines_scanned": total_lines}


# --------------------------------------------------------------------------- #
# Markdown rendering.
# --------------------------------------------------------------------------- #
def render_markdown(pr: Dict, metrics: Dict, *, stamp: str) -> str:
    meta = pr.get("meta", {}) or {}
    author = (meta.get("author") or {}).get("login", "unknown")
    out: List[str] = []
    out.append(f"# Sovereign Artifact - {meta.get('title', '(unknown PR)')}")
    out.append("")
    out.append(f"> Extracted {stamp} by `extract_sovereign_pr.py` "
               f"(standalone; no core-FSM access).")
    out.append("")
    out.append("## PR metadata")
    out.append(f"- **number:** #{meta.get('number', '?')}  **state:** {meta.get('state', '?')}")
    out.append(f"- **author (autonomous):** `{author}`")
    out.append(f"- **branch:** `{meta.get('headRefName', '?')}` -> `{meta.get('baseRefName', '?')}`")
    out.append(f"- **url:** {meta.get('url', '?')}")
    out.append(f"- **created:** {meta.get('createdAt', '?')}  **merged:** {meta.get('mergedAt') or '(held for approval)'}")
    out.append(f"- **size:** +{meta.get('additions', '?')} / -{meta.get('deletions', '?')} "
               f"across {meta.get('changedFiles', '?')} files")
    out.append("")
    out.append("## Commit message(s)")
    out.append(pr.get("commit_messages", "(none)"))
    out.append("")
    out.append("## PR body")
    out.append(str(meta.get("body") or "(no body)"))
    out.append("")
    out.append("## Doubleword lane / latency telemetry (local Sentinel logs)")
    counts = metrics.get("counts", {}) or {}
    out.append(f"- lines scanned: {metrics.get('lines_scanned', 0)}")
    for k, n in counts.items():
        out.append(f"- `{k}`: {n}")
    out.append(f"- last heartbeat: `{metrics.get('last_heartbeat', '(none)')}`")
    samples = metrics.get("samples", {}) or {}
    for k in ("lane_collapse", "unresolvable", "batch_timeout", "realtime_rotation"):
        ss = samples.get(k) or []
        if ss:
            out.append("")
            out.append(f"### sampled `{k}`")
            out.append("```")
            out.extend(ss)
            out.append("```")
    out.append("")
    out.append("## Diff")
    out.append("```diff")
    out.append(str(pr.get("diff") or "(no diff)")[:200000])
    out.append("```")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Entry point.
# --------------------------------------------------------------------------- #
def _default_sentinel_logs() -> List[str]:
    return sorted(glob.glob("autopsy_reports/sentinel_*.log"))


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Sovereign Artifact Extractor (gh + local FSM logs).")
    p.add_argument("--pr", type=int, default=0, help="PR number (default: auto-detect latest ouroboros/review).")
    p.add_argument("--repo", default="", help="owner/repo (default: gh's current).")
    p.add_argument("--sentinel-log", action="append", default=[], help="Sentinel log path(s) (repeatable).")
    p.add_argument("--out", default="", help="output markdown path (default: sovereign_pr_<n>_<ts>.md).")
    p.add_argument("--ingest", action="store_true", help="chain epistemic_memory_ingest.py to absorb the PR lesson into .cursorrules.")
    args = p.parse_args(argv)

    repo = args.repo or None
    number = args.pr
    if not number:
        pr = find_orange_pr(repo=repo)
        if not pr:
            print("[extractor] no ouroboros/review PR found yet (node hasn't pushed one).")
            return 2
        number = int(pr.get("number", 0))
        print(f"[extractor] auto-detected orange PR #{number}: {pr.get('title', '')}")

    artifacts = pull_pr_artifacts(number, repo=repo)
    logs = args.sentinel_log or _default_sentinel_logs()
    metrics = harvest_dw_metrics(logs)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    md = render_markdown(artifacts, metrics, stamp=stamp)

    out_path = args.out or f"sovereign_pr_{number}_{time.strftime('%Y%m%d-%H%M%S')}.md"
    try:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(md)
    except Exception as exc:  # noqa: BLE001
        print(f"[extractor] write failed: {exc!r}")
        return 1
    print(f"[extractor] wrote {out_path} ({len(md)} chars; PR #{number}; "
          f"{len(logs)} sentinel log(s) scanned).")

    # Hook: chain the Autonomous Epistemic Memory Matrix so the PR's structural
    # lesson is absorbed into .cursorrules. Best-effort, never fails the extract.
    if getattr(args, "ingest", False):
        try:
            _ing = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "epistemic_memory_ingest.py")
            _argv = [sys.executable, _ing, "--pr", str(number)]
            if repo:
                _argv += ["--repo", repo]
            print("[extractor] -> epistemic_memory_ingest (absorbing lesson into .cursorrules)")
            subprocess.run(_argv, timeout=120)
        except Exception as exc:  # noqa: BLE001
            print(f"[extractor] epistemic ingest hook failed (non-fatal): {exc!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
