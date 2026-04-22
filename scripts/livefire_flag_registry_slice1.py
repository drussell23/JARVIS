#!/usr/bin/env python3
"""Slice 1 live-fire: FlagRegistry + seed registrations on real repo.

Proves end-to-end on real process state:
  1. Default registry is the same singleton across two get_default_registry() calls
  2. ensure_seeded() installs all SEED_SPECS
  3. All 9 DirectionInferrer flags registered (audit — the arc we just graduated)
  4. All 8 categories reached; all 4 postures referenced
  5. Typed accessor reads real env → returns expected type; no malformed log
  6. Inject a typo env var → suggest_similar returns correct nearest flag
  7. report_typos() emits when master flag is on, silent when off
  8. unregistered_env() identifies 2 injected typos + generates suggestions
  9. JSON export validates schema_version + total count
 10. Authority invariants hold (grep)

Exit 0 on success, 1 on any check failure.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
from typing import Any, Dict, List

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _scrub_registry_env():
    for key in list(os.environ):
        if key.startswith("JARVIS_FLAG_REGISTRY") or key.startswith("JARVIS_FLAG_TYPO"):
            del os.environ[key]


def main() -> int:
    print("=" * 72)
    print("FlagRegistry Slice 1 — Live-Fire on Real Process State")
    print("=" * 72)
    _scrub_registry_env()

    from backend.core.ouroboros.governance.flag_registry import (
        Category,
        FLAG_REGISTRY_SCHEMA_VERSION,
        FlagType,
        Relevance,
        ensure_seeded,
        get_default_registry,
        is_enabled,
        levenshtein_distance,
        reset_default_registry,
    )
    from backend.core.ouroboros.governance.flag_registry_seed import (
        SEED_SPECS,
    )

    checks: List[tuple] = []

    # --- (1) Singleton consistency ---
    reset_default_registry()
    r1 = get_default_registry()
    r2 = get_default_registry()
    checks.append(("get_default_registry returns singleton", r1 is r2))

    # --- (2) ensure_seeded installs specs ---
    reset_default_registry()
    registry = ensure_seeded()
    specs = registry.list_all()
    print(f"[seed] installed {len(specs)} flags")
    checks.append((f"ensure_seeded installs ≥40 flags (actual: {len(specs)})",
                   len(specs) >= 40))
    checks.append(("SEED_SPECS count matches registry count",
                   len(specs) == len(SEED_SPECS)))

    # --- (3) All 9 DirectionInferrer flags present ---
    direction_inferrer_flags = {
        "JARVIS_DIRECTION_INFERRER_ENABLED",
        "JARVIS_POSTURE_PROMPT_INJECTION_ENABLED",
        "JARVIS_POSTURE_OBSERVER_INTERVAL_S",
        "JARVIS_POSTURE_HYSTERESIS_WINDOW_S",
        "JARVIS_POSTURE_HIGH_CONFIDENCE_BYPASS",
        "JARVIS_POSTURE_CONFIDENCE_FLOOR",
        "JARVIS_POSTURE_OVERRIDE_MAX_H",
        "JARVIS_POSTURE_HISTORY_SIZE",
        "JARVIS_POSTURE_WEIGHTS_OVERRIDE",
    }
    registered_names = {s.name for s in specs}
    missing = direction_inferrer_flags - registered_names
    checks.append((
        f"All 9 DirectionInferrer flags registered (missing: {len(missing)})",
        not missing,
    ))

    # --- (4) Category + posture coverage ---
    categories_seen = {s.category for s in specs}
    checks.append((f"all 8 categories covered (seen: {len(categories_seen)})",
                   categories_seen == set(Category)))

    postures_seen: set = set()
    for s in specs:
        postures_seen.update(s.posture_relevance.keys())
    expected_postures = {"EXPLORE", "CONSOLIDATE", "HARDEN", "MAINTAIN"}
    checks.append((
        f"all 4 postures reached in relevance ({sorted(postures_seen)})",
        expected_postures.issubset(postures_seen),
    ))

    # --- (5) Typed accessor with real env ---
    os.environ["JARVIS_POSTURE_OBSERVER_INTERVAL_S"] = "600"
    got_int = registry.get_int("JARVIS_POSTURE_OBSERVER_INTERVAL_S")
    print(f"[accessor] get_int('JARVIS_POSTURE_OBSERVER_INTERVAL_S') = {got_int}")
    checks.append(("get_int reads real env", got_int == 600))

    # Spec default when env absent
    del os.environ["JARVIS_POSTURE_OBSERVER_INTERVAL_S"]
    got_default = registry.get_int("JARVIS_POSTURE_OBSERVER_INTERVAL_S")
    checks.append(("get_int falls back to spec default", got_default == 300))

    # Malformed int → fallback
    os.environ["JARVIS_POSTURE_OBSERVER_INTERVAL_S"] = "not-an-int"
    got_malformed = registry.get_int("JARVIS_POSTURE_OBSERVER_INTERVAL_S")
    checks.append(("get_int on malformed falls back to spec default",
                   got_malformed == 300))
    del os.environ["JARVIS_POSTURE_OBSERVER_INTERVAL_S"]

    # --- (6) Levenshtein + suggest_similar ---
    assert levenshtein_distance("POSTURE", "POSTUR") == 1
    suggestions = registry.suggest_similar("JARVIS_POSTUR_OBSERVER_INTERVAL_S")
    print(f"[typo] suggest_similar → {suggestions[:2]}")
    checks.append(("suggest_similar finds nearby registered flag",
                   len(suggestions) >= 1))
    if suggestions:
        checks.append(("nearest suggestion is POSTURE_OBSERVER_INTERVAL_S",
                       suggestions[0][0] == "JARVIS_POSTURE_OBSERVER_INTERVAL_S"))

    # --- (7) report_typos master-gated ---
    os.environ["JARVIS_POSTUR_OBSERVER_INTERVAL_S"] = "123"  # typo
    os.environ["JARVIS_POSTURE_OBSERVE_INTERVAL_S"] = "123"  # another typo

    # Master off → silent
    os.environ.pop("JARVIS_FLAG_REGISTRY_ENABLED", None)
    silent = registry.report_typos()
    checks.append(("report_typos silent when master off", silent == []))

    # Master on → emits
    os.environ["JARVIS_FLAG_REGISTRY_ENABLED"] = "true"
    emitted = registry.report_typos()
    print(f"[typo] emitted {len(emitted)} warnings: {[e[0] for e in emitted]}")
    checks.append(("report_typos emits ≥2 on injected typos with master on",
                   len(emitted) >= 2))

    # Deduplication within session
    second = registry.report_typos()
    checks.append(("report_typos deduplicates within session", second == []))

    # --- (8) unregistered_env includes suggestions ---
    unreg = dict(registry.unregistered_env())
    # Cleanup typos before suggestion check
    has_posture_typo = "JARVIS_POSTUR_OBSERVER_INTERVAL_S" in unreg
    checks.append(("unregistered_env identifies typo", has_posture_typo))
    if has_posture_typo:
        sugs = unreg["JARVIS_POSTUR_OBSERVER_INTERVAL_S"]
        checks.append(("unregistered_env includes suggestions", len(sugs) >= 1))

    # Cleanup
    for k in ("JARVIS_POSTUR_OBSERVER_INTERVAL_S",
              "JARVIS_POSTURE_OBSERVE_INTERVAL_S"):
        os.environ.pop(k, None)

    # --- (9) JSON export ---
    payload = json.loads(registry.to_json())
    checks.append(("JSON export has schema_version=1.0",
                   payload["schema_version"] == FLAG_REGISTRY_SCHEMA_VERSION))
    checks.append((f"JSON total matches count ({payload['total']})",
                   payload["total"] == len(specs)))
    # Spot-check one flag in export
    explore_entry = next(
        (f for f in payload["flags"]
         if f["name"] == "JARVIS_DIRECTION_INFERRER_ENABLED"), None,
    )
    checks.append(("JSON export contains master flag",
                   explore_entry is not None))
    if explore_entry:
        checks.append(("JSON flag has category",
                       explore_entry.get("category") == "safety"))
        checks.append(("JSON flag has posture_relevance",
                       "HARDEN" in explore_entry.get("posture_relevance", {})))

    # --- (10) is_enabled reflects Slice 1 default ---
    os.environ.pop("JARVIS_FLAG_REGISTRY_ENABLED", None)
    checks.append(("Slice 1 default: is_enabled() is False",
                   is_enabled() is False))

    # --- (11) Stats sanity ---
    stats = registry.stats()
    print(f"[stats] total={stats['total']} by_category={stats['by_category']}")
    checks.append(("stats total = count", stats["total"] == len(specs)))
    checks.append(("stats has 8 category rollups",
                   len(stats["by_category"]) == 8))

    # --- (12) Authority invariants ---
    authority_forbidden = (
        "orchestrator", "policy", "iron_gate", "risk_tier",
        "change_engine", "candidate_generator",
    )
    for relpath in (
        "backend/core/ouroboros/governance/flag_registry.py",
        "backend/core/ouroboros/governance/flag_registry_seed.py",
    ):
        src = (REPO_ROOT / relpath).read_text(encoding="utf-8")
        bad: List[str] = []
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

    pass_log = REPO_ROOT / "scripts" / "livefire_flag_registry_slice1_PASS.log"
    fail_log = REPO_ROOT / "scripts" / "livefire_flag_registry_slice1_FAIL.log"
    log_path = pass_log if all_pass else fail_log
    other = fail_log if all_pass else pass_log
    if other.exists():
        other.unlink()

    artifact = {
        "slice": 1,
        "feature": "FlagRegistry + seed registrations",
        "timestamp": time.time(),
        "all_pass": all_pass,
        "seed_count": len(specs),
        "categories_covered": sorted(c.value for c in categories_seen),
        "postures_reached": sorted(postures_seen),
        "checks": [{"name": n, "pass": ok} for n, ok in checks],
    }
    log_path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(f"Artifact: {log_path}")

    if all_pass:
        print(f"\n  RESULT: PASS  —  {len(checks)} / {len(checks)} checks green.")
        return 0
    print("\n  RESULT: FAIL  —  check the log artifact.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
