#!/usr/bin/env python3
"""Real-session graduation harness — Context Preservation gap closure.

Simulates a realistic 50+ turn operator session with:
  * Multi-file focus drift (auth → db → api → back to auth)
  * Interleaved read/edit/bash/test tool calls (100+ chunks total)
  * Multiple compaction passes (triggered by size, not by arbitrary count)
  * Ledger state accumulation: errors raised + resolved, questions
    asked + answered, decisions approved/rejected
  * Cross-op pressure from a sibling op working on shared files
  * Auto-pin churn: bridges fire; operator manually pins; /pins clear
  * Mid-session kill-switch verification: flip flag off, confirm legacy
    behaviour resumes; flip back on, confirm scorer returns
  * Full manifest audit: every pass is recorded, every chunk has a
    decision trail + reason code

Only graduates the gap if EVERY assertion in EVERY scenario passes.
Exit 0 on success, 1 on failure — wire this script into CI if desired.

Run::
    python3 scripts/real_session_graduation.py
"""
from __future__ import annotations

import asyncio
import os
import random
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

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
    PreservationScorer,
    TurnSource,
    intent_tracker_for,
    reset_default_tracker_registry,
)
from backend.core.ouroboros.governance.context_ledger import (  # noqa: E402
    ContextLedger,
    LedgerEntryKind,
    reset_default_registry,
)
from backend.core.ouroboros.governance.context_manifest import (  # noqa: E402
    PreservationReason,
    manifest_for,
    reset_default_manifest_registry,
)
from backend.core.ouroboros.governance.context_pins import (  # noqa: E402
    ContextPinRegistry,
    PinSource,
    reset_default_pin_registries,
)
from backend.core.ouroboros.governance.context_wiring import (  # noqa: E402
    attach_preservation_wiring,
)

C_PASS, C_FAIL, C_INFO, C_BOLD, C_DIM, C_END = (
    "\033[92m", "\033[91m", "\033[94m", "\033[1m", "\033[2m", "\033[0m",
)


def _banner(text: str) -> None:
    print(f"\n{C_BOLD}{'━' * 72}{C_END}\n{C_BOLD}▶ {text}{C_END}\n{C_BOLD}{'━' * 72}{C_END}")


def _info(text: str) -> None:
    print(f"  {C_INFO}· {text}{C_END}")


def _pass(text: str) -> None:
    print(f"  {C_PASS}✓ {text}{C_END}")


def _fail(text: str) -> None:
    print(f"  {C_FAIL}✗ {text}{C_END}")


class Scenario:
    def __init__(self, title: str) -> None:
        self.title = title
        self.passed: List[str] = []
        self.failed: List[str] = []

    def check(self, desc: str, ok: bool) -> None:
        (self.passed if ok else self.failed).append(desc)
        (_pass if ok else _fail)(desc)

    def fail(self, desc: str) -> None:
        self.failed.append(desc)
        _fail(desc)

    @property
    def ok(self) -> bool:
        return not self.failed


# ---------------------------------------------------------------------------
# Shared state setup + cleanup
# ---------------------------------------------------------------------------


def _clean_state() -> None:
    reset_default_registry()
    reset_default_tracker_registry()
    reset_default_pin_registries()
    reset_default_manifest_registry()


# ---------------------------------------------------------------------------
# The session simulator
# ---------------------------------------------------------------------------


class SessionSimulator:
    """Drives a realistic op through the full preservation stack.

    The 'conversation' is a list of dialogue entries that grows as the
    session proceeds. Every few turns, the compactor runs and reshapes
    the list. Tool chunks accumulate in a parallel prompt tail that
    mimics Venom's tool-result accumulation. The ledger captures
    structured facts in parallel.
    """

    def __init__(
        self,
        op_id: str,
        *,
        scorer: Optional[PreservationScorer] = None,
    ) -> None:
        self.op_id = op_id
        self.scorer = scorer or PreservationScorer()
        self.compactor = ContextCompactor(preservation_scorer=self.scorer)
        self.ledger = ContextLedger(op_id)
        self.tracker = intent_tracker_for(op_id)
        self.pins = ContextPinRegistry(op_id)
        self.dialogue: List[Dict[str, Any]] = []
        self.tool_chunks: List[str] = []
        self._unsub = attach_preservation_wiring(
            ledger=self.ledger, tracker=self.tracker, pins=self.pins,
        )
        self._next_id = 0

    def close(self) -> None:
        self._unsub()

    # --- conversation ---------------------------------------------------

    def _nid(self) -> str:
        self._next_id += 1
        return f"m{self._next_id:03d}"

    def user(self, text: str) -> Dict[str, Any]:
        self.tracker.ingest_turn(text, source=TurnSource.USER)
        entry = {
            "type": "user", "role": "user",
            "content": text, "id": self._nid(),
        }
        self.dialogue.append(entry)
        return entry

    def assistant(self, text: str) -> Dict[str, Any]:
        entry = {
            "type": "assistant", "role": "assistant",
            "content": text, "id": self._nid(),
        }
        self.dialogue.append(entry)
        return entry

    # --- tool activity --------------------------------------------------

    def tool_read(self, path: str, *, round_index: int = 0) -> None:
        self.ledger.record_file_read(
            file_path=path, tool="read_file",
            round_index=round_index, content=b"...",
        )
        self.ledger.record_tool_call(
            tool="read_file", arguments={"file_path": path},
            round_index=round_index, call_id=f"c-r-{self._next_id}",
            status="success", duration_ms=12.0,
            output_bytes=512, result_preview=f"read {path}",
        )
        self.tool_chunks.append(
            f"\n[TOOL RESULT]\ntool: read_file\n{path}\ncontents (first 512 bytes)\n",
        )

    def tool_edit(self, path: str, *, round_index: int = 0) -> None:
        self.ledger.record_tool_call(
            tool="edit_file", arguments={"file_path": path},
            round_index=round_index, call_id=f"c-e-{self._next_id}",
            status="success", duration_ms=22.0,
        )
        self.tool_chunks.append(
            f"\n[TOOL RESULT]\ntool: edit_file\n{path}\npatched\n",
        )

    def tool_bash(self, cmd: str, *, round_index: int = 0) -> None:
        self.ledger.record_tool_call(
            tool="bash", arguments={"command": cmd},
            round_index=round_index, call_id=f"c-b-{self._next_id}",
            status="success", duration_ms=45.0,
        )
        self.tool_chunks.append(
            f"\n[TOOL RESULT]\ntool: bash\n$ {cmd}\noutput lines...\n",
        )

    def tool_error(self, msg: str, *, where: str) -> None:
        self.ledger.record_error(
            error_class="ImportError",
            message=msg,
            where=where,
            linked_tool_call_id=f"c-b-{self._next_id}",
        )
        self.tool_chunks.append(
            f"\n[TOOL ERROR]\ntool: bash\n{msg}\n  at {where}\n",
        )

    def ask(
        self, question: str, *, related: Tuple[str, ...] = (),
    ) -> Dict[str, Any]:
        e = self.ledger.record_question(
            question=question, related_paths=related,
        )
        return {"entry_id": e.entry_id, "question": question}

    def answer(
        self, q: Dict[str, Any], answer: str,
    ) -> None:
        self.ledger.record_question_answer(
            original_entry_id=q["entry_id"], answer=answer,
        )

    def approve(
        self, decision_type: str, *, paths: Tuple[str, ...] = (),
    ) -> None:
        self.ledger.record_decision(
            decision_type=decision_type, outcome="approved",
            reviewer="operator", approved_paths=paths,
        )

    # --- compaction triggers --------------------------------------------

    async def compact_if_large(
        self, *, max_entries: int, preserve_count: int,
    ) -> Optional[Any]:
        if len(self.dialogue) < max_entries:
            return None
        cfg = CompactionConfig(
            max_context_entries=max_entries - 1,
            preserve_count=preserve_count,
        )
        # Run compaction with op_id → scorer path if flag on
        result = await self.compactor.compact(
            self.dialogue, cfg, op_id=self.op_id,
        )
        # Take the preserved set back as the new dialogue (matches
        # real-world contract: caller replaces dialogue with preserved +
        # a summary marker entry).
        # We don't actually need to collapse the dialogue for the test;
        # we just need the result projection.
        return result


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


async def scenario_long_session_with_intent_drift() -> Scenario:
    """50+ turn conversation with focus drifting across three files.

    Proves: ledger retains ALL touched files even as compactor runs
    multiple times; score-ordered preservation tracks the operator's
    current focus; kill-switch mid-session still works.
    """
    s = Scenario("50+ turn session with intent drift across 3 files")
    _clean_state()
    os.environ["JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED"] = "true"
    try:
        sim = SessionSimulator("op-session-1")
        try:
            # Phase A: working on backend/auth.py (turns 1-15)
            _info("Phase A: operator focuses on backend/auth.py")
            for i in range(5):
                sim.user(f"debugging login in backend/auth.py — step {i}")
                sim.tool_read("backend/auth.py", round_index=i)
                sim.assistant("I see the issue, running tests.")
            sim.tool_bash("pytest tests/test_auth.py")
            sim.tool_error(
                msg="ImportError: cannot import Session",
                where="backend/auth.py:12",
            )
            sim.user("fix that ImportError please")
            sim.tool_edit("backend/auth.py", round_index=6)

            # Compaction pass 1 (mid-phase)
            _info("  → compaction pass 1 (mid-phase A)")
            r1 = await sim.compact_if_large(
                max_entries=10, preserve_count=6,
            )
            s.check(
                "pass 1 produced scorer-path result",
                manifest_for("op-session-1").latest() is not None,
            )

            # Phase B: shift to backend/db.py (turns 16-30)
            _info("Phase B: focus shifts to backend/db.py")
            for i in range(6):
                sim.user(f"now let's look at backend/db.py — {i}")
                sim.tool_read("backend/db.py", round_index=i + 10)
            sim.tool_edit("backend/db.py", round_index=16)
            sim.tool_bash("pytest tests/test_db.py")
            q = sim.ask(
                "should we refactor Session.query into a property?",
                related=("backend/db.py",),
            )

            # Compaction pass 2
            _info("  → compaction pass 2 (end-of-phase B)")
            await sim.compact_if_large(
                max_entries=10, preserve_count=6,
            )

            # Phase C: final push on backend/api.py (turns 31-50)
            _info("Phase C: push to backend/api.py")
            for i in range(10):
                sim.user(f"final push on backend/api.py — {i}")
                sim.tool_read("backend/api.py", round_index=i + 20)
            sim.answer(q, "yes, refactor approved")
            sim.tool_edit("backend/api.py", round_index=30)
            sim.approve("plan_approval", paths=("backend/api.py",))

            # Compaction pass 3
            _info("  → compaction pass 3 (end of session)")
            await sim.compact_if_large(
                max_entries=10, preserve_count=6,
            )

            # Assertions
            s.check(
                f"dialogue reached {len(sim.dialogue)} turns (≥ 25)",
                len(sim.dialogue) >= 25,
            )
            all_files = sim.ledger.files_read()
            s.check(
                f"ledger retains all 3 touched files ({all_files})",
                {
                    "backend/auth.py", "backend/db.py", "backend/api.py",
                } == set(all_files),
            )
            s.check(
                "manifest recorded 3 compaction passes",
                len(manifest_for("op-session-1").all_records()) == 3,
            )
            intent = sim.tracker.current_intent()
            s.check(
                f"current intent foregrounds backend/api.py (got {intent.recent_paths[:1]})",
                intent.recent_paths and intent.recent_paths[0] == "backend/api.py",
            )

            # Open error is auto-pinned AND still visible
            active_pins = sim.pins.list_active()
            s.check(
                "ImportError auto-pinned throughout",
                any(p.kind == "auto_error" for p in active_pins),
            )
            s.check(
                "approved plan decision auto-pinned",
                any(p.kind == "auto_decision" for p in active_pins),
            )
            # Ledger open errors reports at least the original ImportError
            s.check(
                "open errors include the original ImportError",
                any(
                    e.error_class == "ImportError"
                    for e in sim.ledger.open_errors()
                ),
            )

            # Kill-switch mid-session: flip OFF, new compaction should fall back to legacy
            _info("Kill-switch: flipping flag OFF mid-session")
            os.environ["JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED"] = "false"
            for _ in range(6):
                sim.user("more noise")
            before_passes = len(manifest_for("op-session-1").all_records())
            await sim.compact_if_large(
                max_entries=10, preserve_count=6,
            )
            after_passes = len(manifest_for("op-session-1").all_records())
            s.check(
                "kill-switch prevents scorer path (manifest count unchanged)",
                before_passes == after_passes,
            )

            # Flip back ON, prove recovery
            os.environ["JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED"] = "true"
            for _ in range(6):
                sim.user("more noise")
            await sim.compact_if_large(
                max_entries=10, preserve_count=6,
            )
            s.check(
                "scorer path resumes after flag back ON",
                len(manifest_for("op-session-1").all_records()) > after_passes,
            )
        finally:
            sim.close()
    finally:
        os.environ.pop("JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED", None)
        _clean_state()
    return s


async def scenario_tool_loop_under_load() -> Scenario:
    """100+ tool chunks across interleaved read/edit/bash; scorer picks wisely."""
    s = Scenario("Tool-loop under 100+ chunk load")
    _clean_state()
    os.environ["JARVIS_TOOL_LOOP_SCORER_ENABLED"] = "true"
    try:
        from backend.core.ouroboros.governance.tool_executor import (
            ToolLoopCoordinator,
        )

        class _FakePolicy:
            def evaluate(self, *a, **kw): ...
            def repo_root_for(self, repo): return Path(".")

        class _FakeBackend:
            async def execute_async(self, *a, **kw): ...

        coord = ToolLoopCoordinator(
            backend=_FakeBackend(),  # type: ignore[arg-type]
            policy=_FakePolicy(),    # type: ignore[arg-type]
            max_rounds=50, tool_timeout_s=5.0,
        )
        tracker = intent_tracker_for("op-tl")
        for _ in range(5):
            tracker.ingest_turn(
                "focus backend/critical.py", source=TurnSource.USER,
            )
        rng = random.Random(42)
        chunks: List[str] = []
        # The CRITICAL chunk
        chunks.append(
            "\n[TOOL RESULT]\ntool: read_file\nbackend/critical.py content — OPERATOR-RELEVANT\n",
        )
        # 100 noise chunks
        for i in range(1, 101):
            tool = rng.choice(["read_file", "edit_file", "bash"])
            noise_path = rng.choice([
                "tests/foo.py", "docs/guide.md", "scripts/run.sh",
            ])
            chunks.append(
                f"\n[TOOL RESULT]\ntool: {tool}\n{noise_path}\nnoise {i}\n",
            )
        old, recent = await coord._maybe_score_tool_chunks(
            chunks=chunks, op_id="op-tl", recent_count=10,
        )
        s.check(
            "critical chunk survived 100-noise flood",
            chunks[0] in recent,
        )
        s.check(
            f"exactly 10 chunks kept verbatim (got {len(recent)})",
            len(recent) == 10,
        )
        s.check(
            "manifest recorded the pass",
            manifest_for("op-tl").latest() is not None,
        )
    finally:
        os.environ.pop("JARVIS_TOOL_LOOP_SCORER_ENABLED", None)
        _clean_state()
    return s


async def scenario_cross_op_concurrent_sessions() -> Scenario:
    """Two ops share focus on the same file; cross-op boost is visible."""
    s = Scenario("Concurrent cross-op pressure on shared file")
    _clean_state()
    try:
        sim_a = SessionSimulator("op-concurrent-a")
        sim_b = SessionSimulator("op-concurrent-b")
        try:
            for _ in range(8):
                sim_a.user("debugging backend/shared.py in op A")
                sim_a.tool_read("backend/shared.py")
                sim_b.user("also backend/shared.py in op B")
                sim_b.tool_read("backend/shared.py")

            cross = CrossOpIntentTracker(exclude_op_ids=["op-concurrent-a"])
            snap = cross.snapshot()
            s.check(
                "sibling op-B contributes to cross-op snapshot",
                "op-concurrent-b" in snap.participating_op_ids,
            )
            s.check(
                "backend/shared.py dominates cross-op path scores",
                snap.path_scores.get("backend/shared.py", 0.0) > 0,
            )
            boost_a = cross.score_boost_for_chunk(
                chunk_text="we touched backend/shared.py",
                cross_op_snap=snap,
            )
            s.check("cross-op boost is nonzero for shared path", boost_a > 0)
        finally:
            sim_a.close()
            sim_b.close()
    finally:
        _clean_state()
    return s


async def scenario_pin_lifecycle_under_session() -> Scenario:
    """Auto-pins fire, operator manually pins, /pins clear preserves auto."""
    s = Scenario("Pin lifecycle: auto + manual + clear")
    _clean_state()
    try:
        sim = SessionSimulator("op-pinlife")
        try:
            # Auto-pin triggers
            sim.tool_error(msg="KeyError", where="backend/x.py")
            sim.approve("plan_approval", paths=("backend/",))
            q = sim.ask("refactor?", related=("backend/y.py",))

            # Operator adds a manual pin
            sim.pins.pin(
                chunk_id="m0-operator-intent",
                source=PinSource.OPERATOR,
                reason="keep the original plan context",
            )

            active = sim.pins.list_active()
            kinds = {p.kind for p in active}
            s.check(
                "3 auto-pin kinds + 1 operator pin = 4 active pins",
                len(active) == 4,
            )
            s.check(
                "auto_error + auto_decision + auto_question + operator all present",
                kinds == {"auto_error", "auto_decision",
                          "auto_question", "operator"},
            )

            # /pins clear preserves auto, removes operator
            cleared = sim.pins.clear_operator_pins()
            s.check(f"/pins clear evicted {cleared}==1 operator pin", cleared == 1)
            remaining = sim.pins.list_active()
            s.check(
                "3 auto-pins survived /pins clear",
                len(remaining) == 3,
            )

            # Answering a question ages its auto-pin eventually (TTL); we
            # verify the pin doesn't break if the underlying state shifts.
            sim.answer(q, "yes, proceed")
            # Pin for that question is still active until TTL — not
            # removed on answer by design (bridge is one-way fire).
            s.check(
                "answered question's auto-pin remains until TTL (by design)",
                any(p.kind == "auto_question" for p in sim.pins.list_active()),
            )
        finally:
            sim.close()
    finally:
        _clean_state()
    return s


async def scenario_semantic_dedup_under_load() -> Scenario:
    """50 chunks including 10 near-duplicates; dedup keeps representatives only."""
    s = Scenario("Semantic dedup removes duplicates under load")
    _clean_state()
    try:
        tracker = intent_tracker_for("op-dd")
        for _ in range(3):
            tracker.ingest_turn("focus backend/hot.py", source=TurnSource.USER)
        scorer = PreservationScorer()
        cands: List[ChunkCandidate] = []
        # 10 near-duplicates of an intent-rich chunk
        for i in range(10):
            cands.append(ChunkCandidate(
                chunk_id=f"dup-{i}",
                text="backend/hot.py was read and analysed",
                index_in_sequence=i, role="user",
            ))
        # 40 distinct chunks
        for i in range(10, 50):
            cands.append(ChunkCandidate(
                chunk_id=f"distinct-{i}",
                text=f"unrelated content variant {i} lorem ipsum",
                index_in_sequence=i, role="tool",
            ))
        result = scorer.select_preserved(
            cands, tracker.current_intent(), max_chunks=15,
        )
        text_lookup = {c.chunk_id: c.text for c in cands}
        deduped = dedupe_preservation_result(
            result, candidate_text_lookup=text_lookup,
        )
        kept_ids = {s.chunk_id for s in deduped.kept}
        dup_kept = {i for i in kept_ids if i.startswith("dup-")}
        s.check(
            f"at most ONE duplicate representative kept (got {len(dup_kept)})",
            len(dup_kept) == 1,
        )
        compacted_dupes = {
            s.chunk_id for s in deduped.compacted
            if s.chunk_id.startswith("dup-")
        }
        s.check(
            f"duplicate twins demoted to compact pool (got {len(compacted_dupes)}≥5)",
            len(compacted_dupes) >= 5,
        )
    finally:
        _clean_state()
    return s


async def scenario_manifest_audit_completeness() -> Scenario:
    """Every pass has every row; reasons are from the known enum."""
    s = Scenario("Manifest audit trail is complete + well-formed")
    _clean_state()
    os.environ["JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED"] = "true"
    try:
        sim = SessionSimulator("op-audit")
        try:
            for _ in range(20):
                sim.user("turn")
                sim.assistant("reply")
            cfg = CompactionConfig(
                max_context_entries=10, preserve_count=5,
            )
            await sim.compactor.compact(
                sim.dialogue, cfg, op_id="op-audit",
            )
            rec = manifest_for("op-audit").latest()
            assert rec is not None
            total_rows = len(rec.rows)
            s.check(
                f"manifest rows cover every input entry ({total_rows}=={len(sim.dialogue)})",
                total_rows == len(sim.dialogue),
            )
            valid_reasons = {r.value for r in PreservationReason}
            bad_reasons = {r.reason for r in rec.rows} - valid_reasons
            s.check(
                f"every row reason from enum (bad={bad_reasons})",
                not bad_reasons,
            )
            decision_codes = {r.decision for r in rec.rows}
            s.check(
                "manifest rows use known decision codes",
                decision_codes.issubset({"keep", "compact", "drop"}),
            )
            kept_rows = [r for r in rec.rows if r.decision == "keep"]
            s.check(
                f"kept_count ({rec.kept_count}) matches row count ({len(kept_rows)})",
                rec.kept_count == len(kept_rows),
            )
        finally:
            sim.close()
    finally:
        os.environ.pop("JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED", None)
        _clean_state()
    return s


async def scenario_backward_compat_with_flags_explicit_false() -> Scenario:
    """Explicit =false kill switches force pure legacy behavior."""
    s = Scenario("Kill switches (=false) force pure legacy behavior")
    _clean_state()
    # Post-graduation: defaults are on. Must set =false explicitly to
    # exercise the kill-switch path.
    os.environ["JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED"] = "false"
    os.environ["JARVIS_TOOL_LOOP_SCORER_ENABLED"] = "false"
    try:
        compactor = ContextCompactor(
            preservation_scorer=PreservationScorer(),
        )
        entries = [
            {"type": "user", "content": f"t{i}", "id": f"m{i}"}
            for i in range(20)
        ]
        cfg = CompactionConfig(max_context_entries=5, preserve_count=3)
        result = await compactor.compact(entries, cfg, op_id="op-compat")
        s.check(
            "compactor result returned",
            result.entries_before == 20 and result.entries_compacted > 0,
        )
        s.check(
            "no manifest recorded (scorer path kill-switched off)",
            manifest_for("op-compat").latest() is None,
        )
    finally:
        os.environ.pop("JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED", None)
        os.environ.pop("JARVIS_TOOL_LOOP_SCORER_ENABLED", None)
        _clean_state()
    return s


async def scenario_authority_invariant_sanity() -> Scenario:
    """Post-real-session: arc modules still pass authority grep."""
    import re as _re
    s = Scenario("Authority invariant intact post-graduation")
    forbidden = [
        "orchestrator", "policy_engine", "iron_gate", "risk_tier_floor",
        "semantic_guardian", "candidate_generator", "change_engine",
    ]
    modules = [
        "backend/core/ouroboros/governance/context_ledger.py",
        "backend/core/ouroboros/governance/context_intent.py",
        "backend/core/ouroboros/governance/context_pins.py",
        "backend/core/ouroboros/governance/context_manifest.py",
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


async def scenario_repeated_compaction_stability() -> Scenario:
    """Running the scorer path 20 times in a row stays stable."""
    s = Scenario("20 repeated compaction passes stay stable")
    _clean_state()
    os.environ["JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED"] = "true"
    try:
        compactor = ContextCompactor(preservation_scorer=PreservationScorer())
        cfg = CompactionConfig(max_context_entries=1, preserve_count=1)
        for i in range(20):
            entries = [
                {"type": "x", "content": f"r{i}-c{j}", "id": f"r{i}m{j}"}
                for j in range(3)
            ]
            await compactor.compact(entries, cfg, op_id="op-rep")
        recs = manifest_for("op-rep").all_records()
        s.check(f"all 20 passes recorded (got {len(recs)})", len(recs) == 20)
        s.check(
            "all passes have valid reasons",
            all(
                r.reason in {pr.value for pr in PreservationReason}
                for rec in recs for r in rec.rows
            ),
        )
    finally:
        os.environ.pop("JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED", None)
        _clean_state()
    return s


ALL_SCENARIOS = [
    scenario_long_session_with_intent_drift,
    scenario_tool_loop_under_load,
    scenario_cross_op_concurrent_sessions,
    scenario_pin_lifecycle_under_session,
    scenario_semantic_dedup_under_load,
    scenario_manifest_audit_completeness,
    scenario_backward_compat_with_flags_explicit_false,
    scenario_repeated_compaction_stability,
    scenario_authority_invariant_sanity,
]


async def main() -> int:
    print(f"{C_BOLD}Context Preservation — Real-Session Graduation Harness{C_END}")
    print(f"{C_DIM}50+ turn sessions, multi-file drift, 100+ tool chunks, kill switches, etc.{C_END}")
    t0 = time.monotonic()
    results: List[Scenario] = []
    for fn in ALL_SCENARIOS:
        title = fn.__doc__.splitlines()[0] if fn.__doc__ else fn.__name__
        _banner(title)
        try:
            results.append(await fn())
        except Exception as exc:
            sc = Scenario(fn.__name__)
            sc.fail(f"raised: {type(exc).__name__}: {exc}")
            import traceback
            traceback.print_exc()
            results.append(sc)
    elapsed = time.monotonic() - t0
    _banner("GRADUATION SUMMARY")
    total_pass = sum(len(s.passed) for s in results)
    total_fail = sum(len(s.failed) for s in results)
    ok = sum(1 for s in results if s.ok)
    for s in results:
        status = f"{C_PASS}PASS{C_END}" if s.ok else f"{C_FAIL}FAIL{C_END}"
        print(f"  {status} {s.title}  ({len(s.passed)} ✓, {len(s.failed)} ✗)")
    print()
    print(
        f"  {C_BOLD}Total:{C_END} {total_pass} checks passed, "
        f"{total_fail} failed — {ok}/{len(results)} scenarios OK"
    )
    print(f"  {C_DIM}elapsed: {elapsed:.2f}s{C_END}")
    print()
    if total_fail == 0:
        print(
            f"  {C_PASS}{C_BOLD}REAL-SESSION GRADUATION PASSED — "
            f"safe to flip mutation-side defaults ON{C_END}"
        )
        return 0
    print(f"  {C_FAIL}{C_BOLD}{total_fail} check(s) failed — "
          f"DO NOT graduate until resolved{C_END}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
