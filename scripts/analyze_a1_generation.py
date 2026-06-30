#!/usr/bin/env python3
"""Autonomous Quality Diagnostic Extractor (A1 generation post-mortem).

Parses a soak ``debug.log`` + its ``chaos_manifest.json`` and, for every op that
terminated NON-applied (blocked / failed / rolled_back), extracts the structural
story of WHY -- so we can mathematically distinguish:

  * ORANGE_BLOCK_CORRECT   -- an Orange/APPROVAL_REQUIRED block on a chaos mutation
                              that is genuinely un-auto-fixable (AST-destroying).
                              The FSM is doing its job; NOT a defect.
  * RISK_MISCLASSIFICATION  -- Orange-blocked despite a trivially-fixable mutation.
  * VALIDATION_FAILURE      -- the model produced a candidate but it failed
                              VALIDATE/pytest (genuine "bad AI code").
  * PROVIDER_EXHAUSTION     -- the provider cascade exhausted BEFORE any code was
                              generated (no candidate ever produced). NOT the AI's
                              code quality -- a routing/availability gap.

It also reports the exact chaos mutation, the GENERATE provider cascade, and any
pytest traceback that tripped VALIDATE. Read-only; NEVER mutates anything.

Usage:
    python3 scripts/analyze_a1_generation.py \
        --debug-log .ouroboros/sessions/<sid>/debug.log \
        --chaos-manifest logs/a1_runs/<stamp>/autopsy/<run>/chaos_manifest.json
    # or point at an autopsy dir (finds both):
    python3 scripts/analyze_a1_generation.py --autopsy-dir logs/a1_runs/<stamp>/autopsy/<run>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Markers (matched against the real debug.log -- substring/regex, fail-soft)
# ---------------------------------------------------------------------------

_TERMINAL_RE = re.compile(r"LEDGER_TERMINAL op_id=(?P<op>[A-Za-z0-9-]+) state=(?P<state>[a-z_]+)")
_RISK_RE = re.compile(r"\[Advisor\][^\n]*?risk=(?P<risk>[0-9.]+)")
_MODEL_ATTEMPT_RE = re.compile(r"attempting model=(?P<model>[A-Za-z0-9/_.:-]+)")
_EXHAUSTION_RE = re.compile(r"EXHAUSTION event_n=(?P<n>\d+) cause=(?P<cause>[A-Za-z0-9_:]+)")
_A1TRACE_RE = re.compile(r"\[A1Trace\] (?P<hop>emit|ingest|dequeue|submit|accept) goal=(?P<op>[A-Za-z0-9-]+)")
_APPROVAL_RE = re.compile(r"APPROVAL_REQUIRED|risk_tier_floor_block|Immutable Orange|approval_required")
_CANDIDATE_RE = re.compile(r"edit_file|write_file|change_engine|candidate.*(?:generated|applied)|full_content")
_PYTEST_FAIL_RE = re.compile(r"(?:FAILED|AssertionError|Error:|Traceback|VALIDATE.*fail|tests? failed)")

# A mutation is trivially auto-fixable (a competent model SHOULD repair it) when it
# is a localized, AST-valid edit -- a single operator/constant/keyword flip. It is
# "fatal" (an Orange block would be CORRECT) when it structurally destroys the AST
# (deleted body, unbalanced syntax, removed signature).
_TRIVIAL_MUTATION_PREFIXES = ("binop:", "compare:", "boolop:", "unaryop:", "const:", "num:", "keyword:")


def classify_mutation_fixability(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Classify the injected chaos mutation as trivially-fixable vs AST-fatal."""
    kind = str(manifest.get("mutation_kind", "") or "")
    detail = manifest.get("mutation_detail", {}) or {}
    orig = str(detail.get("original_segment", ""))
    mutated = str(detail.get("mutated_segment", ""))
    # Single-token localized swap with both segments present + short == trivially fixable.
    single_token = bool(orig) and bool(mutated) and len(orig) <= 3 and len(mutated) <= 3
    trivially_fixable = single_token or kind.startswith(_TRIVIAL_MUTATION_PREFIXES)
    return {
        "mutation_kind": kind,
        "original_segment": orig,
        "mutated_segment": mutated,
        "function": manifest.get("function"),
        "line": manifest.get("line"),
        "target_file": manifest.get("target_file"),
        "test_node": manifest.get("test_node"),
        "trivially_fixable": trivially_fixable,
        "verdict": "trivially_fixable" if trivially_fixable else "ast_fatal_or_complex",
    }


def classify_op_failure(op: Dict[str, Any], mutation_fixable: bool) -> str:
    """Classify WHY a non-applied op terminated. Order matters (most-specific first)."""
    state = op.get("terminal_state", "")
    if state == "applied":
        return "APPLIED_OK"
    if not op.get("candidate_generated"):
        # No code ever produced -> a routing/availability gap, NOT AI code quality.
        if op.get("exhaustion_events"):
            return "PROVIDER_EXHAUSTION"
        if state == "blocked" or op.get("approval_required"):
            return ("ORANGE_BLOCK_CORRECT" if not mutation_fixable
                    else "RISK_MISCLASSIFICATION")
        return "FAILED_PRE_GENERATION"
    # A candidate WAS produced.
    if op.get("validate_failed"):
        return "VALIDATION_FAILURE"
    if state == "blocked" or op.get("approval_required"):
        return ("ORANGE_BLOCK_CORRECT" if not mutation_fixable
                else "RISK_MISCLASSIFICATION")
    return "FAILED_OTHER:%s" % (state or "unknown",)


def extract_ops(log_lines: List[str]) -> Dict[str, Dict[str, Any]]:
    """Build a per-op record from the debug.log lines. Op id is keyed by the short
    prefix used in most log lines (op-XXXX-YYYY) for robust correlation."""
    ops: Dict[str, Dict[str, Any]] = {}

    def _rec(op_id: str) -> Dict[str, Any]:
        short = "-".join(op_id.split("-")[:3])  # op-019f1ab4-b7b1
        return ops.setdefault(short, {
            "op_id": short, "hops": [], "risk": None, "models_attempted": [],
            "exhaustion_events": [], "candidate_generated": False,
            "approval_required": False, "validate_failed": False,
            "terminal_state": None, "validate_evidence": [],
        })

    for line in log_lines:
        m = _TERMINAL_RE.search(line)
        if m:
            _rec(m.group("op"))["terminal_state"] = m.group("state")
        m = _A1TRACE_RE.search(line)
        if m:
            r = _rec(m.group("op"))
            if m.group("hop") not in r["hops"]:
                r["hops"].append(m.group("hop"))
        m = _RISK_RE.search(line)
        if m:
            # Attribute risk to the op id present on the same line, if any.
            opm = re.search(r"op(?:_id)?=(op-[A-Za-z0-9-]+)", line)
            if opm:
                _rec(opm.group(1))["risk"] = float(m.group("risk"))
        opm = re.search(r"op(?:_id)?=(op-[A-Za-z0-9-]+)", line)
        if opm:
            r = _rec(opm.group(1))
            mm = _MODEL_ATTEMPT_RE.search(line)
            if mm and mm.group("model") not in r["models_attempted"]:
                r["models_attempted"].append(mm.group("model"))
            em = _EXHAUSTION_RE.search(line)
            if em:
                r["exhaustion_events"].append(em.group("cause"))
            if _APPROVAL_RE.search(line):
                r["approval_required"] = True
            if _CANDIDATE_RE.search(line):
                r["candidate_generated"] = True
            if _PYTEST_FAIL_RE.search(line) and ("VALIDATE" in line or "pytest" in line.lower()):
                r["validate_failed"] = True
                r["validate_evidence"].append(line.strip()[:200])
    return ops


def _load(path: Optional[str]) -> List[str]:
    if not path or not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        return fh.readlines()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="A1 generation-quality diagnostic extractor")
    ap.add_argument("--debug-log", default=None)
    ap.add_argument("--chaos-manifest", default=None)
    ap.add_argument("--autopsy-dir", default=None,
                    help="An autopsy dir; --debug-log/--chaos-manifest are derived if omitted.")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = ap.parse_args(argv)

    debug_log = args.debug_log
    chaos_manifest = args.chaos_manifest
    if args.autopsy_dir:
        chaos_manifest = chaos_manifest or os.path.join(args.autopsy_dir, "chaos_manifest.json")

    manifest: Dict[str, Any] = {}
    if chaos_manifest and os.path.isfile(chaos_manifest):
        try:
            manifest = json.load(open(chaos_manifest, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            manifest = {}
    mut = classify_mutation_fixability(manifest)

    ops = extract_ops(_load(debug_log))
    non_applied = {k: v for k, v in ops.items()
                   if v["terminal_state"] and v["terminal_state"] != "applied"}
    for op in ops.values():
        op["failure_class"] = classify_op_failure(op, mut["trivially_fixable"])

    report = {
        "chaos_mutation": mut,
        "ops_total": len(ops),
        "ops_non_applied": len(non_applied),
        "ops": ops,
    }

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    # Human report.
    print("=" * 78)
    print("A1 GENERATION DIAGNOSTIC")
    print("=" * 78)
    print("\nCHAOS MUTATION:")
    print("  file=%s func=%s line=%s" % (mut["target_file"], mut["function"], mut["line"]))
    print("  kind=%s  '%s' -> '%s'" % (mut["mutation_kind"], mut["original_segment"], mut["mutated_segment"]))
    print("  test=%s" % (mut["test_node"],))
    print("  FIXABILITY: %s  (an Orange block here is %s)" % (
        mut["verdict"],
        "CORRECT" if not mut["trivially_fixable"] else "a MISCLASSIFICATION (the AI should fix it)"))
    # Only ops with real lifecycle THIS run (terminal state or generation activity);
    # the log also carries stale historical op references (state=None) from the
    # semantic index / replay that are not part of this session's FSM.
    active = {k: v for k, v in ops.items()
              if v["terminal_state"] or v["models_attempted"] or v["hops"]
              or v["exhaustion_events"]}
    print("\nOPS: %d referenced, %d active this run, %d terminated non-applied\n"
          % (len(ops), len(active), len(non_applied)))
    for short, op in sorted(active.items()):
        print("  op=%s state=%s class=%s" % (short, op["terminal_state"], op["failure_class"]))
        print("     risk=%s hops=%s candidate=%s approval=%s validate_failed=%s"
              % (op["risk"], ",".join(op["hops"]) or "-", op["candidate_generated"],
                 op["approval_required"], op["validate_failed"]))
        print("     models_attempted=%s" % (", ".join(op["models_attempted"]) or "-"))
        if op["exhaustion_events"]:
            print("     exhaustion=%s" % (", ".join(op["exhaustion_events"]),))
        for ev in op["validate_evidence"][:2]:
            print("     VALIDATE: %s" % (ev,))
    # Aggregate diagnosis.
    classes: Dict[str, int] = {}
    for op in non_applied.values():
        classes[op["failure_class"]] = classes.get(op["failure_class"], 0) + 1
    print("\nDIAGNOSIS (non-applied failure classes): %s" % (classes or "{}",))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
