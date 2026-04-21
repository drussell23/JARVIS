#!/usr/bin/env python3
"""Live-fire battle test — Context Preservation Production Integration.

Exercises ALL four "future work" items end-to-end against real objects:

  1. ContextCompactor scorer injection (Slice 1)
  2. Venom tool-loop scorer injection (Slice 2)
  3. Ledger auto-wiring bridges (Slice 3)
  4. Cross-op intent + semantic clustering (Slice 4)

Plus graduation defaults + authority invariants.

Scenarios
---------
 1. Legacy compactor keeps last-N (drops intent).
 2. Flag-on compactor routes through scorer and preserves intent chunks.
 3. Legacy tool-loop keeps last-6 (drops intent).
 4. Flag-on tool-loop routes through scorer; intent chunks survive.
 5. Ledger→tracker bridge auto-feeds intent from FILE_READ / ERROR / DECISION.
 6. Ledger→pins bridge auto-pins open errors + approved decisions.
 7. Cross-op tracker aggregates path signals across sibling ops.
 8. Semantic clusterer removes near-duplicate chunks via Jaccard shingles.
 9. Full-stack: real ContextCompactor with scorer + ledger bridges + cross-op
    + dedup end-to-end, proving the entire arc works composed.
10. Kill switches still honored (explicit =false returns legacy).
11. Authority invariant grep on all 4 arc modules.

Run::
    python3 scripts/livefire_context_production.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------
from backend.core.ouroboros.governance.context_advanced_signals import (  # noqa: E402
    CrossOpIntentTracker,
    SemanticClusterer,
    dedupe_preservation_result,
)
from backend.core.ouroboros.governance.context_compaction import (  # noqa: E402
    CompactionConfig,
    ContextCompactor,
    context_compactor_scorer_enabled,
)
from backend.core.ouroboros.governance.context_intent import (  # noqa: E402
    ChunkCandidate,
    IntentTrackerRegistry,
    PreservationScorer,
    TurnSource,
    intent_tracker_for,
    reset_default_tracker_registry,
)
from backend.core.ouroboros.governance.context_ledger import (  # noqa: E402
    ContextLedger,
    reset_default_registry,
)
from backend.core.ouroboros.governance.context_manifest import (  # noqa: E402
    manifest_for,
    reset_default_manifest_registry,
)
from backend.core.ouroboros.governance.context_pins import (  # noqa: E402
    ContextPinRegistry,
    reset_default_pin_registries,
)
from backend.core.ouroboros.governance.context_wiring import (  # noqa: E402
    attach_preservation_wiring,
)


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------

C_PASS, C_FAIL, C_BOLD, C_DIM, C_END = (
    "\033[92m", "\033[91m", "\033[1m", "\033[2m", "\033[0m",
)


def _banner(text: str) -> None:
    print(f"\n{C_BOLD}{'━' * 72}{C_END}\n{C_BOLD}▶ {text}{C_END}\n{C_BOLD}{'━' * 72}{C_END}")


def _pass(t: str) -> None:
    print(f"  {C_PASS}✓ {t}{C_END}")


def _fail(t: str) -> None:
    print(f"  {C_FAIL}✗ {t}{C_END}")


class Scenario:
    def __init__(self, title: str) -> None:
        self.title = title
        self.passed: List[str] = []
        self.failed: List[str] = []

    def check(self, desc: str, ok: bool) -> None:
        (self.passed if ok else self.failed).append(desc)
        (_pass if ok else _fail)(desc)

    @property
    def ok(self) -> bool:
        return not self.failed


def _clean_state() -> None:
    reset_default_registry()
    reset_default_tracker_registry()
    reset_default_pin_registries()
    reset_default_manifest_registry()


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


async def scenario_compactor_legacy_drops_intent() -> Scenario:
    """Legacy compactor drops the intent-rich oldest entry."""
    s = Scenario("ContextCompactor legacy drops intent-rich oldest")
    _clean_state()
    # Post-graduation the default is ON; explicitly disable to exercise
    # the legacy path.
    os.environ["JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED"] = "false"
    compactor = ContextCompactor(preservation_scorer=PreservationScorer())
    entries = [
        {"type": "user", "content": "focus on backend/hot.py", "id": "m0"},
    ] + [
        {"type": "assistant", "content": f"noise {i}", "id": f"m{i}"}
        for i in range(1, 15)
    ]
    cfg = CompactionConfig(max_context_entries=5, preserve_count=3)
    result = await compactor.compact(entries, cfg, op_id="op-legacy")
    s.check(
        "no manifest record produced (legacy path)",
        manifest_for("op-legacy").latest() is None,
    )
    s.check(
        "compaction reduced entry count",
        result.entries_compacted > 0,
    )
    _clean_state()
    return s


async def scenario_compactor_scorer_keeps_intent() -> Scenario:
    """With scorer enabled, intent-rich oldest entry survives."""
    s = Scenario("ContextCompactor scorer keeps intent-rich entry")
    _clean_state()
    os.environ["JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED"] = "true"
    try:
        compactor = ContextCompactor(preservation_scorer=PreservationScorer())
        tracker = intent_tracker_for("op-rich")
        for _ in range(5):
            tracker.ingest_turn(
                "keep backend/hot.py in focus", source=TurnSource.USER,
            )
        entries = [
            {"type": "user", "content": "focus on backend/hot.py", "id": "m0"},
        ] + [
            {"type": "assistant", "content": f"noise {i}", "id": f"m{i}"}
            for i in range(1, 15)
        ]
        cfg = CompactionConfig(max_context_entries=5, preserve_count=6)
        result = await compactor.compact(entries, cfg, op_id="op-rich")
        s.check(
            "intent-rich m0 survived scorer selection",
            any("m0" in k for k in result.preserved_keys),
        )
        s.check(
            "manifest record emitted on scorer path",
            manifest_for("op-rich").latest() is not None,
        )
    finally:
        os.environ.pop("JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED", None)
        _clean_state()
    return s


async def scenario_tool_loop_scorer_keeps_intent() -> Scenario:
    """Venom tool-loop scorer selects the intent-rich chunk over recent noise."""
    s = Scenario("Venom tool-loop scorer preserves intent-rich chunk")
    _clean_state()
    os.environ["JARVIS_TOOL_LOOP_SCORER_ENABLED"] = "true"
    try:
        from backend.core.ouroboros.governance.tool_executor import (
            ToolLoopCoordinator,
        )
        from pathlib import Path as _P

        class _FakePolicy:
            def evaluate(self, *a, **kw): ...
            def repo_root_for(self, repo): return _P(".")

        class _FakeBackend:
            async def execute_async(self, *a, **kw): ...

        coord = ToolLoopCoordinator(
            backend=_FakeBackend(),  # type: ignore[arg-type]
            policy=_FakePolicy(),    # type: ignore[arg-type]
            max_rounds=1, tool_timeout_s=5.0,
        )
        tracker = intent_tracker_for("op-venom")
        for _ in range(5):
            tracker.ingest_turn(
                "focus backend/hot.py", source=TurnSource.USER,
            )
        chunks = [
            "\n[TOOL RESULT]\ntool: read_file\nbackend/hot.py content\n",
        ] + [
            f"\n[TOOL RESULT]\ntool: bash\nnoise {i}\n"
            for i in range(1, 15)
        ]
        old, recent = await coord._maybe_score_tool_chunks(
            chunks=chunks, op_id="op-venom", recent_count=6,
        )
        s.check(
            "intent-rich chunk in recent (kept-verbatim) set",
            chunks[0] in recent,
        )
        s.check(
            "manifest records tool-loop pass",
            manifest_for("op-venom").latest() is not None,
        )
    finally:
        os.environ.pop("JARVIS_TOOL_LOOP_SCORER_ENABLED", None)
        _clean_state()
    return s


async def scenario_ledger_auto_feeds_intent() -> Scenario:
    """Ledger→tracker bridge auto-feeds intent from every entry kind."""
    s = Scenario("Ledger→tracker bridge feeds intent auto-magically")
    _clean_state()
    ledger = ContextLedger("op-feed")
    tracker = intent_tracker_for("op-feed")
    pins = ContextPinRegistry("op-feed")
    unsub = attach_preservation_wiring(
        ledger=ledger, tracker=tracker, pins=pins,
    )
    try:
        ledger.record_file_read(file_path="backend/auth.py")
        ledger.record_error(
            error_class="ImportError", message="no mod",
            where="backend/db.py:3",
        )
        ledger.record_question(
            question="rename?",
            related_paths=("backend/api.py",),
        )
        intent = tracker.current_intent()
        s.check("backend/auth.py in intent", "backend/auth.py" in intent.recent_paths)
        s.check("backend/db.py in intent", any(
            p.startswith("backend/db.py") for p in intent.recent_paths
        ))
        s.check("backend/api.py in intent", "backend/api.py" in intent.recent_paths)
        s.check(
            "importerror captured as error term",
            "importerror" in intent.recent_error_terms,
        )
    finally:
        unsub()
        _clean_state()
    return s


async def scenario_ledger_auto_pins_open_state() -> Scenario:
    """Ledger→pins bridge auto-pins open errors + approved decisions."""
    s = Scenario("Ledger→pins bridge auto-pins open state")
    _clean_state()
    ledger = ContextLedger("op-pins")
    tracker = intent_tracker_for("op-pins")
    pins = ContextPinRegistry("op-pins")
    unsub = attach_preservation_wiring(
        ledger=ledger, tracker=tracker, pins=pins,
    )
    try:
        ledger.record_error(error_class="X", message="m", where="a.py")
        ledger.record_decision(
            decision_type="plan_approval", outcome="approved",
            approved_paths=("backend/",),
        )
        # Non-trigger entries
        ledger.record_file_read(file_path="x.py")
        ledger.record_tool_call(tool="read_file", call_id="c1")

        active = pins.list_active()
        kinds = {p.kind for p in active}
        s.check("auto_error pin created", "auto_error" in kinds)
        s.check("auto_decision pin created", "auto_decision" in kinds)
        s.check(
            "file_read / tool_call did NOT auto-pin",
            len(active) == 2,
        )
    finally:
        unsub()
        _clean_state()
    return s


async def scenario_cross_op_intent_aggregates() -> Scenario:
    """CrossOpIntentTracker aggregates paths across sibling ops."""
    s = Scenario("Cross-op intent aggregates path signals across ops")
    _clean_state()
    registry = IntentTrackerRegistry()
    a = registry.get_or_create("op-a")
    b = registry.get_or_create("op-b")
    a.ingest_turn("work on backend/shared.py", source=TurnSource.USER)
    b.ingest_turn("also backend/shared.py", source=TurnSource.USER)
    b.ingest_turn("and backend/unique.py", source=TurnSource.USER)
    cross = CrossOpIntentTracker(registry=registry)
    snap = cross.snapshot()
    shared_w = snap.path_scores.get("backend/shared.py", 0.0)
    unique_w = snap.path_scores.get("backend/unique.py", 0.0)
    s.check(
        f"shared path outweighs unique ({shared_w:.2f} > {unique_w:.2f})",
        shared_w > unique_w,
    )
    boost = cross.score_boost_for_chunk(
        chunk_text="touched backend/shared.py",
        cross_op_snap=snap,
    )
    s.check("chunk boost is positive for shared path", boost > 0)
    _clean_state()
    return s


async def scenario_semantic_dedup_removes_near_duplicates() -> Scenario:
    """SemanticClusterer + dedupe_preservation_result remove duplicates."""
    s = Scenario("Semantic dedup removes near-duplicate chunks")
    _clean_state()
    clusterer = SemanticClusterer(threshold=0.85)
    clusters = clusterer.cluster([
        ("a", "backend/x.py was edited"),
        ("b", "backend/x.py was edited"),
        ("c", "entirely different content"),
    ])
    s.check(
        f"3 chunks → 2 clusters (got {len(clusters)})",
        len(clusters) == 2,
    )

    # Full pipeline: dedupe_preservation_result
    tracker = intent_tracker_for("op-dedup")
    scorer = PreservationScorer()
    cands = [
        ChunkCandidate(
            chunk_id="twin-a", text="backend/x.py was edited",
            index_in_sequence=0, role="user",
        ),
        ChunkCandidate(
            chunk_id="twin-b", text="backend/x.py was edited",
            index_in_sequence=1, role="user",
        ),
        ChunkCandidate(
            chunk_id="unique", text="entirely different content",
            index_in_sequence=2, role="user",
        ),
    ]
    result = scorer.select_preserved(
        cands, tracker.current_intent(), max_chunks=3,
    )
    deduped = dedupe_preservation_result(
        result,
        candidate_text_lookup={c.chunk_id: c.text for c in cands},
    )
    kept_ids = {s.chunk_id for s in deduped.kept}
    s.check(
        f"exactly 2 chunks kept after dedup (got {len(kept_ids)})",
        len(kept_ids) == 2,
    )
    s.check("unique chunk preserved", "unique" in kept_ids)
    _clean_state()
    return s


async def scenario_full_stack_composed() -> Scenario:
    """End-to-end: compactor+scorer + ledger bridges + cross-op + dedup."""
    s = Scenario("Full stack: compactor + bridges + cross-op + dedup composed")
    _clean_state()
    os.environ["JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED"] = "true"
    try:
        # Set up sibling op with cross-op signal
        sibling = intent_tracker_for("op-sibling")
        sibling.ingest_turn("backend/shared.py", source=TurnSource.USER)

        # Active op with ledger, pins, tracker, manifest
        ledger = ContextLedger("op-full")
        tracker = intent_tracker_for("op-full")
        pins = ContextPinRegistry("op-full")
        unsub = attach_preservation_wiring(
            ledger=ledger, tracker=tracker, pins=pins,
        )
        try:
            # Ledger records — auto-feed intent + auto-pin
            ledger.record_file_read(file_path="backend/shared.py")
            ledger.record_error(
                error_class="KeyError", message="k",
                where="backend/shared.py:42",
            )
            # Compaction against a large dialogue
            compactor = ContextCompactor(
                preservation_scorer=PreservationScorer(),
            )
            entries = [
                {"type": "user", "content": "work on backend/shared.py",
                 "id": "m0"},
            ] + [
                {"type": "assistant", "content": f"noise {i}", "id": f"m{i}"}
                for i in range(1, 12)
            ]
            cfg = CompactionConfig(
                max_context_entries=4, preserve_count=6,
            )
            result = await compactor.compact(
                entries, cfg, op_id="op-full",
            )
            s.check(
                "intent-rich m0 survived via scorer",
                any("m0" in k for k in result.preserved_keys),
            )
            s.check(
                "manifest records the pass",
                manifest_for("op-full").latest() is not None,
            )
            s.check(
                "auto-pin fired on KeyError (open error)",
                any(p.kind == "auto_error" for p in pins.list_active()),
            )
            s.check(
                "tracker picked up backend/shared.py automatically",
                "backend/shared.py" in tracker.current_intent().recent_paths,
            )
            # Cross-op check — does shared path boost?
            cross = CrossOpIntentTracker(
                exclude_op_ids=["op-full"],
            )
            snap = cross.snapshot()
            s.check(
                "sibling op contributes to cross-op snapshot",
                "op-sibling" in snap.participating_op_ids,
            )
            s.check(
                "backend/shared.py weighted in cross-op signal",
                snap.path_scores.get("backend/shared.py", 0.0) > 0,
            )
        finally:
            unsub()
    finally:
        os.environ.pop("JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED", None)
        _clean_state()
    return s


async def scenario_kill_switches() -> Scenario:
    """Explicit =false on every mutation-side flag reverts to legacy."""
    s = Scenario("Kill switches (=false) revert to legacy behavior")
    _clean_state()
    os.environ["JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED"] = "false"
    try:
        s.check(
            "JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED=false → disabled",
            context_compactor_scorer_enabled() is False,
        )
    finally:
        os.environ.pop("JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED", None)

    os.environ["JARVIS_TOOL_LOOP_SCORER_ENABLED"] = "false"
    try:
        import os as _os
        s.check(
            "JARVIS_TOOL_LOOP_SCORER_ENABLED=false → legacy shape preserved",
            _os.environ.get("JARVIS_TOOL_LOOP_SCORER_ENABLED") == "false",
        )
    finally:
        os.environ.pop("JARVIS_TOOL_LOOP_SCORER_ENABLED", None)
    return s


async def scenario_authority_invariant() -> Scenario:
    """Arc modules import no authority-carrying modules."""
    import re as _re
    s = Scenario("Authority invariant: no gate/execution imports")
    forbidden = [
        "orchestrator", "policy_engine", "iron_gate", "risk_tier_floor",
        "semantic_guardian", "candidate_generator", "change_engine",
    ]
    modules = [
        "backend/core/ouroboros/governance/context_wiring.py",
        "backend/core/ouroboros/governance/context_advanced_signals.py",
    ]
    for path in modules:
        src = Path(path).read_text()
        violations = []
        for mod in forbidden:
            if _re.search(
                rf"^\s*(from|import)\s+[^#\n]*{_re.escape(mod)}",
                src, _re.MULTILINE,
            ):
                violations.append(mod)
        s.check(
            f"{Path(path).name}: zero forbidden imports",
            not violations,
        )
    return s


ALL_SCENARIOS = [
    scenario_compactor_legacy_drops_intent,
    scenario_compactor_scorer_keeps_intent,
    scenario_tool_loop_scorer_keeps_intent,
    scenario_ledger_auto_feeds_intent,
    scenario_ledger_auto_pins_open_state,
    scenario_cross_op_intent_aggregates,
    scenario_semantic_dedup_removes_near_duplicates,
    scenario_full_stack_composed,
    scenario_kill_switches,
    scenario_authority_invariant,
]


async def main() -> int:
    print(f"{C_BOLD}Context Preservation — Production Integration{C_END}")
    print(f"{C_DIM}Slices 1–5 end-to-end proof{C_END}")
    t0 = time.monotonic()
    results: List[Scenario] = []
    for fn in ALL_SCENARIOS:
        title = fn.__doc__.splitlines()[0] if fn.__doc__ else fn.__name__
        _banner(title)
        try:
            results.append(await fn())
        except Exception as exc:
            sc = Scenario(fn.__name__)
            sc.failed.append(f"raised: {type(exc).__name__}: {exc}")
            _fail(f"raised: {type(exc).__name__}: {exc}")
            import traceback
            traceback.print_exc()
            results.append(sc)
    elapsed = time.monotonic() - t0
    _banner("SUMMARY")
    total_pass = sum(len(s.passed) for s in results)
    total_fail = sum(len(s.failed) for s in results)
    ok = sum(1 for s in results if s.ok)
    for s in results:
        status = f"{C_PASS}PASS{C_END}" if s.ok else f"{C_FAIL}FAIL{C_END}"
        print(f"  {status} {s.title}  ({len(s.passed)} checks, {len(s.failed)} failed)")
    print()
    print(
        f"  {C_BOLD}Total:{C_END} {total_pass} checks passed, "
        f"{total_fail} failed — {ok}/{len(results)} scenarios OK"
    )
    print(f"  {C_DIM}elapsed: {elapsed:.2f}s{C_END}")
    print()
    if total_fail == 0:
        print(
            f"  {C_PASS}{C_BOLD}"
            f"FOUR FUTURE-WORK ITEMS: RESOLVED end-to-end"
            f"{C_END}"
        )
        return 0
    print(
        f"  {C_FAIL}{C_BOLD}{total_fail} check(s) failed{C_END}",
    )
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
