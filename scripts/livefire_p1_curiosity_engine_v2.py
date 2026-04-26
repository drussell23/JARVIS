#!/usr/bin/env python3
"""P1 — Curiosity Engine v2 — in-process live-fire smoke (PRD §11 Layer 3).

Mirrors ``scripts/livefire_p0_postmortem_recall.py`` and
``scripts/livefire_p0_5_arc_context.py``. In-process exercise of the
full P1 chain WITHOUT requiring an Anthropic API key, a running
battle-test harness, or any network call. Validates the post-graduation
contract end-to-end.

18 checks:

 1.  Engine master flag default ON (post-graduation)
 2.  Sensor master flag default ON (post-graduation)
 3.  Engine hot-revert: explicit false disables
 4.  Sensor hot-revert: explicit false disables
 5.  Per-session cap default = 1 (PRD §9 P1)
 6.  Cost cap default = $0.10 (PRD §9 P1)
 7.  Min cluster size default = 3 ("3+ similar failures" per PRD)
 8.  ProposalDraft.schema_version frozen at "self_goal_formation.1"
 9.  IntentEnvelope source allowlist contains "auto_proposed"
 10. Clusterer happy path: 3 records → 1 cluster
 11. Engine end-to-end: cluster → ProposalDraft + ledger persist
 12. BacklogSensor reads ledger → IntentEnvelope (auto_proposed)
 13. Envelope carries requires_human_ack=True (operator-review tier)
 14. Envelope evidence has all 7 audit keys
 15. REPL approve writes decision + appends to backlog.json
 16. REPL idempotent (re-approve fails)
 17. Posture veto: HARDEN/MAINTAIN block engine
 18. Bounded: 50-proposal ledger emits ≤ MAX_PROPOSALS_PER_SCAN

Exit 0 on PASS; non-zero with failed-check summary on FAIL.

Usage::

    python3 scripts/livefire_p1_curiosity_engine_v2.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


class Journal:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed.append(name)
            print(f"  [PASS] {name}")
        else:
            self.failed.append((name, detail))
            print(f"  [FAIL] {name}  ({detail})")

    def summary(self) -> int:
        total = len(self.passed) + len(self.failed)
        print(f"\n{'=' * 64}")
        print(f"Result: {len(self.passed)}/{total} checks passed")
        if self.failed:
            print("\nFailures:")
            for n, d in self.failed:
                print(f"  - {n}: {d}")
            return 1
        print("All checks passed — P1 Curiosity Engine v2 live-fire smoke OK.")
        return 0


def _record(op_id, root_cause, failed_phase, ts):
    from backend.core.ouroboros.governance.postmortem_recall import (
        PostmortemRecord,
    )
    return PostmortemRecord(
        op_id=op_id, session_id="s1", root_cause=root_cause,
        failed_phase=failed_phase, next_safe_action="retry_with_smaller_seed",
        target_files=("a.py",), timestamp_iso="2026-04-26T10:00:00",
        timestamp_unix=ts,
    )


def main() -> int:
    j = Journal()
    print("=" * 64)
    print("P1 — Curiosity Engine v2 — live-fire smoke (PRD Phase 2)")
    print("=" * 64)

    # Reset env for deterministic defaults
    for k in (
        "JARVIS_SELF_GOAL_FORMATION_ENABLED",
        "JARVIS_BACKLOG_AUTO_PROPOSED_ENABLED",
        "JARVIS_SELF_GOAL_PER_SESSION_CAP",
        "JARVIS_SELF_GOAL_COST_CAP_USD",
    ):
        os.environ.pop(k, None)

    from backend.core.ouroboros.governance.self_goal_formation import (
        DEFAULT_COST_CAP_USD, DEFAULT_PER_SESSION_CAP,
        PROPOSAL_SCHEMA_VERSION, SelfGoalFormationEngine,
        cost_cap_usd, is_enabled as engine_enabled, per_session_cap,
        reset_default_engine,
    )
    from backend.core.ouroboros.governance.postmortem_clusterer import (
        DEFAULT_MIN_CLUSTER_SIZE, cluster_postmortems,
    )
    from backend.core.ouroboros.governance.intake.sensors.backlog_sensor import (
        BacklogSensor, _MAX_PROPOSALS_PER_SCAN, _auto_proposed_enabled,
    )
    from backend.core.ouroboros.governance.intake.intent_envelope import (
        _VALID_SOURCES,
    )
    from backend.core.ouroboros.governance.posture import Posture
    from backend.core.ouroboros.governance.backlog_auto_proposed_repl import (
        dispatch_backlog_auto_proposed_command as REPL,
    )

    reset_default_engine()

    # --- (1) Engine master flag default ON
    j.check(
        "1. Engine master flag defaults True (post-graduation)",
        engine_enabled() is True, f"got {engine_enabled()}",
    )

    # --- (2) Sensor master flag default ON
    j.check(
        "2. Sensor master flag defaults True (post-graduation)",
        _auto_proposed_enabled() is True, f"got {_auto_proposed_enabled()}",
    )

    # --- (3) Engine hot-revert
    os.environ["JARVIS_SELF_GOAL_FORMATION_ENABLED"] = "false"
    j.check(
        "3. Engine hot-revert: explicit false disables",
        engine_enabled() is False,
    )
    os.environ.pop("JARVIS_SELF_GOAL_FORMATION_ENABLED", None)

    # --- (4) Sensor hot-revert
    os.environ["JARVIS_BACKLOG_AUTO_PROPOSED_ENABLED"] = "false"
    j.check(
        "4. Sensor hot-revert: explicit false disables",
        _auto_proposed_enabled() is False,
    )
    os.environ.pop("JARVIS_BACKLOG_AUTO_PROPOSED_ENABLED", None)

    # --- (5/6/7) Default constants
    j.check("5. Per-session cap default = 1", per_session_cap() == 1
            and DEFAULT_PER_SESSION_CAP == 1)
    j.check("6. Cost cap default = $0.10", cost_cap_usd() == 0.10
            and DEFAULT_COST_CAP_USD == 0.10)
    j.check("7. Min cluster size default = 3", DEFAULT_MIN_CLUSTER_SIZE == 3)

    # --- (8/9) Schema invariants
    j.check(
        "8. ProposalDraft.schema_version = self_goal_formation.1",
        PROPOSAL_SCHEMA_VERSION == "self_goal_formation.1",
    )
    j.check(
        '9. IntentEnvelope source allowlist contains "auto_proposed"',
        "auto_proposed" in _VALID_SOURCES,
    )

    # --- (10) Clusterer happy path
    recs = [
        _record(f"op{i}", "all_providers_exhausted:fallback_failed",
                "GENERATE", 1_700_000_000.0 + i * 3600.0)
        for i in range(3)
    ]
    clusters = cluster_postmortems(recs)
    j.check(
        "10. Clusterer: 3 records → 1 cluster",
        len(clusters) == 1 and clusters[0].member_count == 3,
        f"got {len(clusters)} cluster(s)",
    )

    # End-to-end: persist + sensor + REPL
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        ledger = repo / ".jarvis" / "self_goal_formation_proposals.jsonl"
        eng = SelfGoalFormationEngine(project_root=repo, ledger_path=ledger)

        def stub_model(prompt, max_cost):
            return (
                json.dumps({
                    "description": "Investigate provider exhaustion",
                    "rationale": "5 ops failed at GENERATE",
                }),
                0.05,
            )

        # --- (11) Engine end-to-end
        draft = eng.evaluate(
            postmortems=recs, posture=Posture.EXPLORE,
            model_caller=stub_model,
        )
        j.check(
            "11. Engine: cluster → ProposalDraft + ledger persist",
            draft is not None and ledger.exists()
            and ledger.stat().st_size > 0,
            f"draft={draft}",
        )
        sig_hash = draft.signature_hash if draft else "?"

        # --- (12) Sensor reads ledger
        router = AsyncMock()
        router.ingest = AsyncMock(return_value="enqueued")
        sensor = BacklogSensor(
            backlog_path=repo / "backlog.json", repo_root=repo,
            router=router, proposals_ledger_path=ledger,
        )
        envs = asyncio.get_event_loop().run_until_complete(sensor.scan_once())
        j.check(
            "12. Sensor reads ledger → 1 IntentEnvelope (auto_proposed)",
            len(envs) == 1 and envs[0].source == "auto_proposed",
            f"got {len(envs)} envs, source={envs[0].source if envs else '?'}",
        )

        # --- (13) requires_human_ack=True
        j.check(
            "13. Envelope carries requires_human_ack=True",
            envs and envs[0].requires_human_ack is True,
        )

        # --- (14) Audit keys
        if envs:
            ev = envs[0].evidence
            required = {
                "auto_proposed", "signature_hash", "signature", "task_id",
                "cluster_member_count", "rationale", "posture_at_proposal",
                "schema_version",
            }
            present = set(ev.keys())
            j.check(
                "14. Envelope evidence has all 8 audit keys",
                required.issubset(present),
                f"missing: {required - present}",
            )
        else:
            j.check("14. Envelope evidence keys", False, "no envelope")

        # --- (15) REPL approve
        r = REPL(
            f"/backlog auto-proposed approve {sig_hash} --reason looks good",
            project_root=repo,
        )
        backlog = repo / ".jarvis" / "backlog.json"
        j.check(
            "15. REPL approve → decision + backlog.json append",
            r.ok is True and backlog.exists()
            and any(e.get("approved_signature_hash") == sig_hash
                    for e in json.loads(backlog.read_text())),
            f"ok={r.ok}",
        )

        # --- (16) REPL idempotent
        r2 = REPL(
            f"/backlog auto-proposed approve {sig_hash}",
            project_root=repo,
        )
        j.check(
            "16. REPL idempotent: re-approve returns ok=False",
            r2.ok is False and "already" in r2.text,
        )

    # --- (17) Posture veto
    with tempfile.TemporaryDirectory() as td:
        eng2 = SelfGoalFormationEngine(
            project_root=Path(td),
            ledger_path=Path(td) / "ledger.jsonl",
        )
        out_harden = eng2.evaluate(
            postmortems=recs, posture=Posture.HARDEN,
            model_caller=lambda p, m: ('{"description":"x","rationale":"y"}', 0.01),
        )
        out_maintain = eng2.evaluate(
            postmortems=recs, posture=Posture.MAINTAIN,
            model_caller=lambda p, m: ('{"description":"x","rationale":"y"}', 0.01),
        )
        j.check(
            "17. Posture veto: HARDEN + MAINTAIN both block engine",
            out_harden is None and out_maintain is None,
        )

    # --- (18) Bounded: ≤ MAX_PROPOSALS_PER_SCAN
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        (repo / ".jarvis").mkdir()
        ledger = repo / ".jarvis" / "self_goal_formation_proposals.jsonl"
        proposals = [
            {
                "schema_version": "self_goal_formation.1",
                "signature_hash": f"sig-{i:04d}",
                "cluster_member_count": 5,
                "target_files": ["a.py"],
                "description": "d",
                "rationale": "r",
                "posture_at_proposal": "EXPLORE",
                "auto_proposed": True,
            }
            for i in range(50)
        ]
        ledger.write_text(
            "\n".join(json.dumps(p) for p in proposals) + "\n"
        )
        router = AsyncMock()
        router.ingest = AsyncMock(return_value="enqueued")
        sensor = BacklogSensor(
            backlog_path=repo / "backlog.json", repo_root=repo,
            router=router, proposals_ledger_path=ledger,
        )
        envs = asyncio.get_event_loop().run_until_complete(sensor.scan_once())
        j.check(
            f"18. Bounded: 50-proposal ledger emits ≤ {_MAX_PROPOSALS_PER_SCAN}",
            len(envs) == _MAX_PROPOSALS_PER_SCAN,
            f"got {len(envs)}",
        )

    return j.summary()


if __name__ == "__main__":
    sys.exit(main())
