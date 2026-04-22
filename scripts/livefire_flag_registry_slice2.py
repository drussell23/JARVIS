#!/usr/bin/env python3
"""Slice 2 live-fire: /help dispatcher on real seeded registry.

Exercises all 9 subcommand shapes on the real seeded registry with the
master flag on. Injects 2 realistic typos and asserts Levenshtein
suggestions surface in /help unregistered. Verifies the posture-relevance
filter is the first real downstream consumer of Wave 1 #1 by querying
HARDEN + EXPLORE postures and counting critical flags each returns.

Exit 0 on success, 1 on any failure.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time
from typing import List

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _scrub_env():
    for key in list(os.environ):
        if key.startswith("JARVIS_FLAG_REGISTRY") or key.startswith("JARVIS_HELP_DISPATCHER") or key.startswith("JARVIS_FLAG_TYPO"):
            del os.environ[key]


def main() -> int:
    print("=" * 72)
    print("FlagRegistry + /help — Slice 2 Live-Fire")
    print("=" * 72)
    _scrub_env()
    os.environ["JARVIS_FLAG_REGISTRY_ENABLED"] = "true"

    from backend.core.ouroboros.governance.flag_registry import (
        ensure_seeded, reset_default_registry,
    )
    from backend.core.ouroboros.governance.help_dispatcher import (
        dispatch_help_command,
        dispatcher_enabled,
        get_default_verb_registry,
        reset_default_verb_registry,
    )

    reset_default_registry()
    reset_default_verb_registry()
    registry = ensure_seeded()
    verbs = get_default_verb_registry()

    checks: List[tuple] = []

    # --- (1) Master-on enables dispatcher ---
    checks.append(("dispatcher_enabled() True with master on",
                   dispatcher_enabled() is True))

    # --- (2) /help help always works ---
    r = dispatch_help_command("/help help")
    print(f"[help help] ok={r.ok}")
    checks.append(("/help help returns ok", r.ok))
    checks.append(("/help help contains subcommand list",
                   "/help flags" in r.text and "/help flag" in r.text))

    # --- (3) /help top-level index ---
    r = dispatch_help_command("/help")
    print(f"[top-index] {len(r.text.splitlines())} lines")
    checks.append(("/help top-index ok", r.ok))
    checks.append(("/help lists ≥7 REPL verbs",
                   r.ok and "/posture" in r.text and "/recover" in r.text
                   and "/session" in r.text and "/cost" in r.text
                   and "/plan" in r.text and "/layout" in r.text))
    checks.append(("/help shows flag total",
                   r.ok and "env flags" in r.text))

    # --- (4) /help verbs ---
    r = dispatch_help_command("/help verbs")
    verb_count = len(verbs.list_all())
    print(f"[verbs] listed {verb_count} verbs")
    checks.append((f"/help verbs lists {verb_count} verbs",
                   r.ok and f"{verb_count} REPL verbs" in r.text))

    # --- (5) /help <verb> delegation ---
    r = dispatch_help_command("/help /posture")
    checks.append(("/help /posture delegates",
                   r.ok and "posture" in r.text.lower()))
    r = dispatch_help_command("/help recover")
    checks.append(("/help recover (no slash) delegates",
                   r.ok and "recover" in r.text.lower()))

    # --- (6) /help flags + filters ---
    r = dispatch_help_command("/help flags")
    print(f"[flags] bare listing: {r.text.splitlines()[0] if r.text else ''}")
    checks.append(("/help flags bare ok", r.ok))
    checks.append(("/help flags lists 52 flags",
                   r.ok and "52" in r.text.splitlines()[0]))

    r = dispatch_help_command("/help flags --category safety")
    checks.append(("/help flags --category safety",
                   r.ok and "JARVIS_DIRECTION_INFERRER_ENABLED" in r.text))

    r = dispatch_help_command("/help flags --category made_up")
    checks.append(("/help flags --category made_up returns error",
                   not r.ok and "unknown category" in r.text.lower()))

    r = dispatch_help_command("/help flags --posture HARDEN")
    harden_count = r.text.splitlines()[0] if r.ok else ""
    print(f"[posture HARDEN] {harden_count}")
    checks.append(("/help flags --posture HARDEN surfaces flags",
                   r.ok and "JARVIS_" in r.text))

    r = dispatch_help_command("/help flags --posture EXPLORE")
    print(f"[posture EXPLORE] {r.text.splitlines()[0] if r.ok else 'ERR'}")
    checks.append(("/help flags --posture EXPLORE surfaces flags",
                   r.ok and "JARVIS_" in r.text))

    r = dispatch_help_command("/help flags --search observer")
    checks.append(("/help flags --search observer",
                   r.ok and "observer" in r.text.lower()))

    r = dispatch_help_command("/help flags --search xyzzy_nothing")
    checks.append(("/help flags --search xyzzy no match",
                   r.ok and "no flags match" in r.text.lower()))

    # --- (7) /help flag <NAME> ---
    r = dispatch_help_command("/help flag JARVIS_DIRECTION_INFERRER_ENABLED")
    print(f"[flag detail] ok={r.ok}")
    checks.append(("/help flag detail on known flag ok",
                   r.ok and "category" in r.text.lower()
                   and "default" in r.text.lower()))

    os.environ["JARVIS_DIRECTION_INFERRER_ENABLED"] = "true"
    r = dispatch_help_command("/help flag JARVIS_DIRECTION_INFERRER_ENABLED")
    checks.append(("/help flag detail shows 'currently set' when env set",
                   r.ok and "currently set in env" in r.text.lower()))
    del os.environ["JARVIS_DIRECTION_INFERRER_ENABLED"]

    # Unknown flag with suggestion
    r = dispatch_help_command("/help flag JARVIS_POSTURE_OBSERVR_INTERVAL_S")
    checks.append(("/help flag unknown shows Did-you-mean suggestion",
                   not r.ok and "Did you mean" in r.text))

    # --- (8) /help category + /help posture alias ---
    r = dispatch_help_command("/help category safety")
    checks.append(("/help category alias works",
                   r.ok and "JARVIS_DIRECTION_INFERRER_ENABLED" in r.text))

    r = dispatch_help_command("/help posture EXPLORE")
    checks.append(("/help posture EXPLORE alias works",
                   r.ok and "JARVIS_" in r.text))

    # --- (9) /help unregistered with 2 injected typos ---
    os.environ["JARVIS_POSTURE_OBSERVR_INTERVAL_S"] = "600"   # typo dist 1
    os.environ["JARVIS_POSTUR_HYSTERESIS_WINDOW_S"] = "900"   # typo dist 1
    r = dispatch_help_command("/help unregistered")
    print(f"[unregistered] {r.text.splitlines()[0] if r.ok else 'ERR'}")
    checks.append(("/help unregistered ok", r.ok))
    checks.append(("unregistered identifies JARVIS_POSTURE_OBSERVR",
                   r.ok and "JARVIS_POSTURE_OBSERVR_INTERVAL_S" in r.text))
    checks.append(("unregistered identifies JARVIS_POSTUR_HYSTERESIS",
                   r.ok and "JARVIS_POSTUR_HYSTERESIS_WINDOW_S" in r.text))
    checks.append(("unregistered shows closest matches",
                   r.ok and "closest match" in r.text.lower()))

    # Cleanup typo env vars
    del os.environ["JARVIS_POSTURE_OBSERVR_INTERVAL_S"]
    del os.environ["JARVIS_POSTUR_HYSTERESIS_WINDOW_S"]

    # --- (10) /help stats ---
    r = dispatch_help_command("/help stats")
    print(f"[stats] ok={r.ok}")
    checks.append(("/help stats ok", r.ok))
    for key in ("schema_version", "total_flags", "by_category",
                "verbs_registered"):
        checks.append((f"stats contains {key}",
                       r.ok and key in r.text))

    # --- (11) Master-off revert matrix ---
    os.environ["JARVIS_FLAG_REGISTRY_ENABLED"] = "false"
    r_off = dispatch_help_command("/help flags")
    checks.append(("master off: /help flags rejected",
                   not r_off.ok
                   and "JARVIS_FLAG_REGISTRY_ENABLED" in r_off.text))

    r_help_off = dispatch_help_command("/help help")
    checks.append(("master off: /help help still works",
                   r_help_off.ok and "/help" in r_help_off.text))

    # Re-enable
    os.environ["JARVIS_FLAG_REGISTRY_ENABLED"] = "true"
    r_back = dispatch_help_command("/help flags")
    checks.append(("re-enable: /help flags works again", r_back.ok))

    # --- (12) Posture-relevance filter is first real consumer of Wave 1 #1 ---
    r = dispatch_help_command("/help flags --posture HARDEN")
    if r.ok:
        lines = [
            ln for ln in r.text.splitlines()
            if ln.strip().startswith("JARVIS_")
        ]
        print(f"[posture HARDEN] {len(lines)} entries")
        checks.append((
            "posture HARDEN returns ≥5 relevant flags (consumer of Wave 1 #1)",
            len(lines) >= 5,
        ))

    # --- (13) Authority invariant ---
    authority_forbidden = (
        "orchestrator", "policy", "iron_gate", "risk_tier",
        "change_engine", "candidate_generator",
    )
    relpath = "backend/core/ouroboros/governance/help_dispatcher.py"
    src = (REPO_ROOT / relpath).read_text(encoding="utf-8")
    bad = []
    for line in src.splitlines():
        if line.startswith(("from ", "import ")):
            for forbidden in authority_forbidden:
                if f".{forbidden}" in line:
                    bad.append(line)
    checks.append((f"authority-free: {relpath}", not bad))

    # Report
    print()
    print("-" * 72)
    print(f"Checks ({len(checks)}):")
    all_pass = True
    for name, ok in checks:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            all_pass = False
        print(f"  [{mark}] {name}")
    print("-" * 72)

    pass_log = REPO_ROOT / "scripts" / "livefire_flag_registry_slice2_PASS.log"
    fail_log = REPO_ROOT / "scripts" / "livefire_flag_registry_slice2_FAIL.log"
    log_path = pass_log if all_pass else fail_log
    other = fail_log if all_pass else pass_log
    if other.exists():
        other.unlink()

    artifact = {
        "slice": 2,
        "feature": "/help dispatcher + VerbRegistry",
        "timestamp": time.time(),
        "all_pass": all_pass,
        "total_checks": len(checks),
        "checks": [{"name": n, "pass": ok} for n, ok in checks],
    }
    log_path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(f"Artifact: {log_path}")

    if all_pass:
        print(f"\n  RESULT: PASS  —  {len(checks)}/{len(checks)} checks green.")
        return 0
    print("\n  RESULT: FAIL  —  check the log artifact.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
