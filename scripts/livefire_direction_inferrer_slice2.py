#!/usr/bin/env python3
"""Slice 2 live-fire: PostureObserver + Store + StrategicDirection on real repo.

Proves end-to-end on real repo state:
  1. SignalCollector pulls 12 signals from real git log + real summary.json
  2. DirectionInferrer produces a reading
  3. PostureObserver.run_one_cycle() promotes it to PostureStore
  4. Atomic writes produce valid .jarvis/posture_current.json on disk
  5. History ring buffer grows across N cycles
  6. Hysteresis holds current under a stub-swapped differing bundle
  7. High-confidence bypass flips current when threshold met
  8. Override state masks + expires + writes audit records
  9. StrategicDirection.format_for_prompt() includes the posture section
     when flags are on, omits it when off
 10. Authority invariants hold (grep-pinned files stay authority-free)

Uses a tempdir for the ``.jarvis/`` base, so no production state is
touched. Reads real ``git log`` and real ``.ouroboros/sessions/*/summary.json``
from the actual repo root — these are read-only pulls.

Exit codes:
  0 — all checks passed, PASS log written
  1 — any check failed, FAIL log written
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
)
sys.path.insert(0, str(REPO_ROOT))

from backend.core.ouroboros.governance.direction_inferrer import (  # noqa: E402
    DirectionInferrer,
)
from backend.core.ouroboros.governance.posture import (  # noqa: E402
    Posture,
    baseline_bundle,
)
from backend.core.ouroboros.governance.posture_observer import (  # noqa: E402
    OverrideState,
    PostureObserver,
    SignalCollector,
)
from backend.core.ouroboros.governance.posture_prompt import (  # noqa: E402
    compose_posture_section,
)
from backend.core.ouroboros.governance.posture_store import (  # noqa: E402
    PostureStore,
)


async def amain() -> int:
    print("=" * 72)
    print("DirectionInferrer Slice 2 — Live-Fire on Real Repo State")
    print("=" * 72)
    checks = []

    with tempfile.TemporaryDirectory(prefix="livefire_posture_slice2_") as tmp:
        tmp_path = Path(tmp)
        store = PostureStore(tmp_path / ".jarvis")
        collector = SignalCollector(REPO_ROOT)

        # --- (1) Real signal collection ---
        bundle = collector.build_bundle()
        print(
            f"[collect] feat={bundle.feat_ratio:.2f} fix={bundle.fix_ratio:.2f} "
            f"refactor={bundle.refactor_ratio:.2f} test+docs={bundle.test_docs_ratio:.2f}"
        )
        print(
            f"[collect] postmortem_rate={bundle.postmortem_failure_rate:.2f} "
            f"iron_gate_reject={bundle.iron_gate_reject_rate:.2f} "
            f"l2_repair={bundle.l2_repair_rate:.2f}"
        )
        print(
            f"[collect] time_since_graduation_inv="
            f"{bundle.time_since_last_graduation_inv:.4f} "
            f"cost_burn={bundle.cost_burn_normalized:.2f} "
            f"orphans={bundle.worktree_orphan_count}"
        )
        checks.append((
            "SignalCollector.build_bundle produces schema v1.0",
            bundle.schema_version == "1.0",
        ))
        checks.append((
            "all ratios in [0,1]",
            all(
                0.0 <= getattr(bundle, k) <= 1.0
                for k in (
                    "feat_ratio", "fix_ratio", "refactor_ratio", "test_docs_ratio",
                    "postmortem_failure_rate", "iron_gate_reject_rate",
                    "l2_repair_rate", "open_ops_normalized",
                    "session_lessons_infra_ratio", "time_since_last_graduation_inv",
                    "cost_burn_normalized",
                )
            ),
        ))

        # --- (2) + (3) + (4) Observer cycle writes current ---
        observer = PostureObserver(REPO_ROOT, store, collector=collector)
        reading = await observer.run_one_cycle()
        if reading is not None:
            print(
                f"[cycle 1] posture={reading.posture.value} "
                f"confidence={reading.confidence:.3f}"
            )
        else:
            print("[cycle 1] no reading (collector timeout)")
        checks.append(("first cycle produced a reading", reading is not None))

        current = store.load_current()
        checks.append(("PostureStore.load_current returns reading", current is not None))
        checks.append((
            "posture_current.json exists on disk",
            store.current_path.exists(),
        ))
        # Validate JSON shape
        try:
            payload = json.loads(store.current_path.read_text(encoding="utf-8"))
            checks.append((
                "posture_current.json has schema_version='1.0'",
                payload.get("schema_version") == "1.0",
            ))
        except (OSError, json.JSONDecodeError):
            checks.append(("posture_current.json parseable", False))

        # --- (5) History grows across N cycles ---
        for _ in range(3):
            await observer.run_one_cycle()
        history = store.load_history()
        print(f"[history] {len(history)} readings in ring buffer")
        checks.append(("history accumulates across cycles", len(history) >= 4))

        # --- (6) Hysteresis holds current under differing bundle ---
        os.environ["JARVIS_POSTURE_HYSTERESIS_WINDOW_S"] = "3600"
        os.environ["JARVIS_POSTURE_HIGH_CONFIDENCE_BYPASS"] = "2.0"
        # Snapshot current before swap
        pre_swap = store.load_current()

        class _FlipCollector:
            def __init__(self, bundle):
                self._bundle = bundle

            def build_bundle(self):
                return self._bundle

        # Pick a bundle that infers a different posture than current
        alt_bundle = replace(
            baseline_bundle(),
            fix_ratio=0.75,
            postmortem_failure_rate=0.55,
            iron_gate_reject_rate=0.45,
            session_lessons_infra_ratio=0.80,
        )
        alt_posture = DirectionInferrer().infer(alt_bundle).posture
        print(
            f"[hysteresis] pre-swap posture={pre_swap.posture.value if pre_swap else None} "
            f"alt bundle would infer {alt_posture.value}"
        )
        if pre_swap is not None and alt_posture is not pre_swap.posture:
            observer._collector = _FlipCollector(alt_bundle)  # type: ignore[attr-defined]
            await observer.run_one_cycle()
            post_swap = store.load_current()
            checks.append((
                "hysteresis holds current under low-confidence swap",
                post_swap is not None and post_swap.posture is pre_swap.posture,
            ))
        else:
            # Alt bundle matches current — hysteresis trivially holds, skip
            checks.append(("hysteresis check skipped (posture matches)", True))

        # --- (7) High-confidence bypass flips current ---
        os.environ["JARVIS_POSTURE_HIGH_CONFIDENCE_BYPASS"] = "0.0"
        observer._collector = _FlipCollector(alt_bundle)  # type: ignore[attr-defined]
        await observer.run_one_cycle()
        bypass_current = store.load_current()
        print(
            f"[bypass] post-bypass posture="
            f"{bypass_current.posture.value if bypass_current else None}"
        )
        checks.append((
            "high-confidence bypass flips current",
            bypass_current is not None and bypass_current.posture is alt_posture,
        ))

        # --- (8) Override set → expire → audit record ---
        override = OverrideState()
        override.set(Posture.MAINTAIN, duration_s=0.01, reason="livefire brief")
        time.sleep(0.02)
        observer_with_override = PostureObserver(
            REPO_ROOT, store,
            collector=_FlipCollector(alt_bundle),
            override_state=override,
        )
        await observer_with_override.run_one_cycle()
        audit = store.load_audit()
        print(f"[audit] {len(audit)} records; latest event="
              f"{audit[-1].event if audit else '(none)'}")
        checks.append((
            "override expiry emits 'expired' audit record",
            any(r.event == "expired" for r in audit),
        ))

        # --- (9) StrategicDirection integration ---
        from backend.core.ouroboros.governance.posture_observer import (
            reset_default_store,
            get_default_store,
        )
        from backend.core.ouroboros.governance.strategic_direction import (
            StrategicDirectionService,
        )

        # Wire default store to our tmp dir
        reset_default_store()
        default_store = get_default_store(tmp_path / ".jarvis")
        default_store.write_current(bypass_current)  # ensure a reading is visible

        svc = StrategicDirectionService(REPO_ROOT)
        svc._digest = "(test digest for livefire)"  # type: ignore[attr-defined]
        svc._loaded = True  # type: ignore[attr-defined]

        # Flag off → no posture section
        os.environ.pop("JARVIS_DIRECTION_INFERRER_ENABLED", None)
        out_off = svc.format_for_prompt()
        checks.append((
            "StrategicDirection omits posture when master flag off",
            "Current Strategic Posture" not in out_off,
        ))

        # Flag on → posture section present
        os.environ["JARVIS_DIRECTION_INFERRER_ENABLED"] = "true"
        out_on = svc.format_for_prompt()
        checks.append((
            "StrategicDirection includes posture when master flag on",
            "Current Strategic Posture" in out_on,
        ))
        # Check the advisory line is there
        checks.append((
            "posture block contains advisory",
            "Advisory" in out_on,
        ))

        # --- (10) Authority invariants ---
        authority_forbidden = (
            "orchestrator", "policy", "iron_gate", "risk_tier",
            "change_engine", "candidate_generator",
        )
        for relpath in (
            "backend/core/ouroboros/governance/posture_store.py",
            "backend/core/ouroboros/governance/posture_prompt.py",
            "backend/core/ouroboros/governance/posture_observer.py",
        ):
            src = (REPO_ROOT / relpath).read_text(encoding="utf-8")
            bad = []
            for line in src.splitlines():
                if line.startswith(("from ", "import ")):
                    for forbidden in authority_forbidden:
                        if f".{forbidden}" in line:
                            bad.append((forbidden, line))
            checks.append((f"authority-import-free: {relpath}", not bad))

        # --- (11) Render posture block sample ---
        sample = compose_posture_section(bypass_current, force=True)
        print()
        print("Sample posture prompt section:")
        print("-" * 50)
        print(sample)
        print("-" * 50)
        checks.append(("rendered section under 600 chars", len(sample) < 600))

        reset_default_store()

    # --- Report ---
    print()
    print("-" * 72)
    print("Checks:")
    all_pass = True
    for name, ok in checks:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            all_pass = False
        print(f"  [{mark}] {name}")
    print("-" * 72)

    pass_log = REPO_ROOT / "scripts" / "livefire_direction_inferrer_slice2_PASS.log"
    fail_log = REPO_ROOT / "scripts" / "livefire_direction_inferrer_slice2_FAIL.log"
    log_path = pass_log if all_pass else fail_log
    other = fail_log if all_pass else pass_log
    if other.exists():
        other.unlink()

    artifact = {
        "slice": 2,
        "feature": "DirectionInferrer + PostureObserver + StrategicDirection",
        "timestamp": time.time(),
        "all_pass": all_pass,
        "checks": [{"name": n, "pass": ok} for n, ok in checks],
    }
    log_path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(f"Artifact: {log_path}")

    if all_pass:
        print("\n  RESULT: PASS  —  Slice 2 live-fire clean on real repo state.")
        return 0
    print("\n  RESULT: FAIL  —  check the log artifact.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
