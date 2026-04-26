#!/usr/bin/env python3
"""P0 — PostmortemRecall live-fire smoke (PRD §11 Layer 3).

Formal in-process smoke that exercises the full P0 chain end-to-end
WITHOUT requiring an Anthropic API key, a running battle-test harness,
or any network call. Mirrors the W2(4) curiosity / W3(6) parallel-dispatch
live-fire pattern.

15 checks per PRD §11 P0 row Layer 3 target:

1. Master flag default OFF (graduation discipline pin)
2. Master flag explicit ON enables service
3. PostmortemRecallService construction (no crash)
4. Empty sessions dir → recall returns [] cleanly
5. Real-shape postmortem line parsed from synthesized debug.log
6. Session walker finds the postmortem
7. Embedder lazy-init via SemanticIndex._Embedder
8. End-to-end recall with embedder present + similarity above threshold
9. JSONL ledger written on match
10. Ledger schema_version is "postmortem_recall.1"
11. Time-decay halves match score at one halflife
12. Top-k cap respected
13. render_recall_section produces "## Lessons from prior similar ops"
14. Master-OFF makes recall return [] even with embedder + postmortems
15. Default-singleton accessor respects master-flag composition

Exit 0 on PASS; non-zero with failed-check summary on FAIL.

Usage::

    python3 scripts/livefire_p0_postmortem_recall.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

# Repo root on sys.path
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


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
        print("All checks passed — P0 PostmortemRecall live-fire smoke OK.")
        return 0


_REAL_POSTMORTEM_LINE = (
    "2026-04-25T01:08:13 [backend.core.ouroboros.governance.comm_protocol] "
    "INFO [CommProtocol] POSTMORTEM op=op-019dc3ac-8864-766b-84c8-5f36913654ee-cau "
    "seq=8 payload={'root_cause': 'all_providers_exhausted:fallback_failed', "
    "'failed_phase': 'GENERATE', 'next_safe_action': 'retry_with_smaller_seed', "
    "'target_files': ['backend/core/foo.py', 'backend/core/bar.py']}"
)


def main() -> int:
    j = Journal()
    print("=" * 64)
    print("P0 — PostmortemRecall live-fire smoke (PRD Phase 1)")
    print("=" * 64)

    # Reset all P0 envs to default
    for key in (
        "JARVIS_POSTMORTEM_RECALL_ENABLED",
        "JARVIS_POSTMORTEM_RECALL_TOP_K",
        "JARVIS_POSTMORTEM_RECALL_DECAY_DAYS",
        "JARVIS_POSTMORTEM_RECALL_SIM_THRESHOLD",
        "JARVIS_POSTMORTEM_RECALL_MAX_SCAN",
    ):
        os.environ.pop(key, None)

    from backend.core.ouroboros.governance.postmortem_recall import (
        PostmortemRecallService,
        PostmortemRecord,
        RecallMatch,
        _decay_factor,
        _gather_recent_postmortems,
        _parse_postmortem_line,
        decay_days,
        get_default_service,
        is_enabled,
        max_postmortems_to_scan,
        render_recall_section,
        reset_default_service,
        similarity_threshold,
        top_k,
    )

    # --- (1) Master flag default OFF (graduation discipline)
    j.check(
        "1. Master flag defaults False (PRD §17 default-off)",
        is_enabled() is False,
        f"got {is_enabled()}",
    )

    # --- (2) Master flag explicit ON
    os.environ["JARVIS_POSTMORTEM_RECALL_ENABLED"] = "true"
    j.check(
        "2. Master flag explicit True enables service",
        is_enabled() is True,
    )

    # --- (3) Construction smoke
    with tempfile.TemporaryDirectory() as td:
        sessions_dir = Path(td) / "sessions"
        sessions_dir.mkdir()
        ledger_path = Path(td) / "ledger.jsonl"

        try:
            svc = PostmortemRecallService(
                sessions_dir=sessions_dir, ledger_path=ledger_path,
            )
            j.check("3. PostmortemRecallService constructs without crash", True)
        except Exception as e:  # noqa: BLE001
            j.check("3. PostmortemRecallService constructs", False, repr(e))
            return j.summary()

        # --- (4) Empty sessions dir
        # Inject a stub embedder so the lazy-init path doesn't try to
        # download fastembed weights during smoke.
        fake_emb = MagicMock()
        fake_emb.disabled = False
        fake_emb.embed = MagicMock(return_value=[[1.0, 0.0]])
        svc._embedder = fake_emb

        result = svc.recall_for_op("any signature")
        j.check(
            "4. Empty sessions dir → recall returns [] cleanly",
            result == [],
            f"got {len(result)} matches",
        )

        # --- (5) Real-shape postmortem line parses
        rec = _parse_postmortem_line(_REAL_POSTMORTEM_LINE, session_id="bt-test")
        j.check(
            "5. Real postmortem line parses → PostmortemRecord",
            rec is not None and rec.failed_phase == "GENERATE",
            f"got {rec}",
        )

        # --- (6) Session walker finds postmortems on disk
        sess_dir = sessions_dir / "bt-2026-04-25-test"
        sess_dir.mkdir()
        (sess_dir / "debug.log").write_text(_REAL_POSTMORTEM_LINE + "\n")
        records = _gather_recent_postmortems(sessions_dir, max_total=10)
        j.check(
            "6. Session walker finds postmortem in synthesized debug.log",
            len(records) == 1 and records[0].failed_phase == "GENERATE",
            f"got {len(records)} records",
        )

        # --- (7) Embedder lazy-init (SemanticIndex._Embedder is the real thing,
        #         but smoke doesn't need to actually load weights — we just
        #         confirm the import path exists)
        try:
            from backend.core.ouroboros.governance.semantic_index import (
                _Embedder as _SE,
                _embedder_name,
            )
            j.check(
                "7. SemanticIndex._Embedder import path resolves (lazy-init substrate)",
                callable(_SE) and isinstance(_embedder_name(), str),
            )
        except Exception as e:  # noqa: BLE001
            j.check("7. SemanticIndex._Embedder import", False, repr(e))

        # --- (8) End-to-end recall with stub embedder
        os.environ["JARVIS_POSTMORTEM_RECALL_DECAY_DAYS"] = "365"
        os.environ["JARVIS_POSTMORTEM_RECALL_SIM_THRESHOLD"] = "0.0"
        # Stub embedder returns identity vectors so cosine = 1.0
        fake_emb.embed = MagicMock(
            return_value=[[1.0, 0.0, 0.0], [1.0, 0.0, 0.0]]
        )
        matches = svc.recall_for_op("test op signature")
        j.check(
            "8. End-to-end recall with stub embedder produces ≥1 match",
            len(matches) >= 1,
            f"got {len(matches)} matches",
        )

        # --- (9) Ledger written on match
        j.check(
            "9. JSONL ledger written on match",
            ledger_path.exists() and ledger_path.stat().st_size > 0,
        )

        # --- (10) Ledger schema version pinned
        if ledger_path.exists():
            import json as _json
            line = ledger_path.read_text(encoding="utf-8").splitlines()[0]
            try:
                rec_d = _json.loads(line)
                j.check(
                    "10. Ledger entry schema_version=postmortem_recall.1",
                    rec_d.get("schema_version") == "postmortem_recall.1",
                    f"got {rec_d.get('schema_version')}",
                )
            except Exception as e:  # noqa: BLE001
                j.check("10. Ledger entry parse", False, repr(e))

        # --- (11) Time-decay halves at one halflife
        decay_at_zero = _decay_factor(age_seconds=0, halflife_days=30.0)
        decay_at_one = _decay_factor(age_seconds=30 * 86400.0, halflife_days=30.0)
        j.check(
            "11. Time-decay: factor(age=0)=1.0, factor(age=halflife)≈0.5",
            abs(decay_at_zero - 1.0) < 0.001 and abs(decay_at_one - 0.5) < 0.001,
            f"got zero={decay_at_zero}, halflife={decay_at_one}",
        )

        # --- (12) Top-k cap respected
        # Create 5 postmortems then cap at 2
        sess2 = sessions_dir / "bt-test-many"
        sess2.mkdir()
        many_lines = []
        for i in range(5):
            many_lines.append(_REAL_POSTMORTEM_LINE.replace(
                "op-019dc3ac-8864-766b-84c8-5f36913654ee-cau", f"op-many-{i}",
            ))
        (sess2 / "debug.log").write_text("\n".join(many_lines) + "\n")
        # Reset singleton + use new svc
        svc2 = PostmortemRecallService(
            sessions_dir=sessions_dir,
            ledger_path=Path(td) / "ledger2.jsonl",
        )
        fake2 = MagicMock()
        fake2.disabled = False
        # query + 5 (from sess2) + 1 (from earlier sess1 still on disk)
        fake2.embed = MagicMock(return_value=[[1.0, 0.0]] * 7)
        svc2._embedder = fake2
        matches2 = svc2.recall_for_op("test", top_k_override=2)
        j.check(
            "12. Top-k override caps result list (override=2)",
            len(matches2) == 2,
            f"got {len(matches2)} matches",
        )

        # --- (13) render_recall_section
        if matches2:
            rendered = render_recall_section(matches2)
            j.check(
                "13. render_recall_section produces '## Lessons from prior similar ops'",
                rendered is not None and "## Lessons from prior similar ops" in rendered,
                f"got {rendered[:100] if rendered else 'None'}",
            )
            j.check(
                "13a. Each match rendered as a bullet line with op_id + phase",
                rendered is not None and rendered.count("- op=") == len(matches2),
                f"got {rendered.count('- op=') if rendered else 0} bullets",
            )

        # --- (14) Master-OFF returns [] even with embedder + postmortems
        os.environ["JARVIS_POSTMORTEM_RECALL_ENABLED"] = "false"
        result_off = svc2.recall_for_op("any signature")
        j.check(
            "14. Master-OFF returns [] even with embedder + postmortems",
            result_off == [],
            f"got {len(result_off)} matches with master off",
        )

        # --- (15) Default-singleton respects master-flag composition
        os.environ["JARVIS_POSTMORTEM_RECALL_ENABLED"] = "false"
        reset_default_service()
        j.check(
            "15. get_default_service() returns None when master OFF",
            get_default_service() is None,
        )

    return j.summary()


if __name__ == "__main__":
    sys.exit(main())
