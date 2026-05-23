"""Slice 12R — Absolute Telemetry Seal & Reflexive ASCII Healing.

Tests for the three-phase Slice 12R deliverable:

* **Phase 1 — Telemetry Seal.** The
  ``_bg_unregister_active`` callback inside
  :func:`GovernedLoopService.start` calls
  :func:`get_active_recorder` and (when a session recorder is
  registered) records a terminal entry with
  ``terminal_reason_code="cancelled_shutdown"``. This guarantees
  that even when an op is killed via the
  shutdown-cancellation cascade (Slice 12O cooldown → CancelledError
  → cleanup callback, bypassing ``_record_ledger``) it still
  appears in ``summary.json.operations[]``. Composes Slice 12Q
  idempotency (``SessionRecorder._recorded_op_ids``) so the seal is
  byte-for-byte a no-op when the orchestrator path already
  recorded the op with richer attribution (first-write-wins).

* **Phase 2 — Payload Sanitization.**
  :data:`ascii_strict_gate._UNICODE_REPAIR_MAP` covers all
  typographical Unicode the model emits in practice (smart
  quotes, em-dashes, ellipsis, NBSP, arrows, section signs,
  zero-width chars). The gate's :meth:`AsciiStrictGate.check`
  runs :meth:`AsciiStrictGate.repair` *before* the hard-reject
  scan, so a candidate carrying only repairable characters
  passes the gate transparently — no ``ascii_corruption``
  retry, no second handoff.

* **Phase 3 — Reflexive ASCII Healing.** When sanitization
  cannot fix the candidate (Unicode letters that *look* like
  ASCII identifiers — the rapid**ف**uzz class —
  intentionally stay rejected because changing a letter changes
  identity), the orchestrator's ``ascii_corruption`` retry
  branch now prepends the Slice 12P
  ``<DEVELOPER_FEEDBACK priority="CRITICAL_SYSTEM_OVERRIDE">``
  block produced by
  :func:`reflexive_healing.format_structural_rejection_feedback`,
  mirroring the existing Slice 12P prepend in the
  ``exploration_insufficient`` branch. The block surfaces the
  rejection class (``ascii_gate_failed``) + canonical
  remediation actions so the model's attention mechanism gives
  the override priority over front-loaded task text.

AST pins enforce the structural wiring so future refactors
cannot silently regress the telemetry seal or the reflexive
healing prepend.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test.session_recorder import (
    SessionRecorder,
    get_active_recorder,
    reset_active_recorder,
    set_active_recorder,
)
from backend.core.ouroboros.governance.ascii_strict_gate import (
    AsciiStrictGate,
    _UNICODE_REPAIR_MAP,
    repair_content,
)
from backend.core.ouroboros.governance.reflexive_healing import (
    format_structural_rejection_feedback,
)
from backend.core.ouroboros.governance.terminal_reason import (
    TerminalReasonClass,
    classify_terminal_reason,
)


# ──────────────────────────────────────────────────────────────────────
# Shared fixtures
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_active_recorder():
    """Each test gets a clean process-singleton."""
    reset_active_recorder()
    yield
    reset_active_recorder()


@pytest.fixture
def recorder():
    """A fresh SessionRecorder, not registered as active."""
    return SessionRecorder(session_id="test-slice12r")


# ──────────────────────────────────────────────────────────────────────
# Phase 1 — Telemetry seal
# ──────────────────────────────────────────────────────────────────────


class TestPhase1TelemetrySeal:
    """Cancellation-path telemetry wiring."""

    def test_set_and_get_active_recorder_roundtrip(self, recorder):
        """The process-singleton register/get pair is symmetric."""
        assert get_active_recorder() is None
        set_active_recorder(recorder)
        assert get_active_recorder() is recorder

    def test_seal_classifies_cancelled_shutdown(self, recorder):
        """The exact shape the seal block uses records a
        ``CANCELLED_SHUTDOWN`` terminal class on the recorder."""
        set_active_recorder(recorder)
        active = get_active_recorder()
        assert active is recorder

        # Mirror _bg_unregister_active's call shape verbatim so
        # test stays load-bearing if the call site changes. The
        # canonical code is ``cancelled_during_shutdown`` — that's
        # the substring Slice 12P's classifier matches against to
        # return CANCELLED_SHUTDOWN. The bare string
        # ``cancelled_shutdown`` would classify as OTHER.
        active.record_operation(
            op_id="op-cancel-test-1",
            status="cancelled",
            sensor="bg_pool",
            technique="cancelled_during_shutdown",
            composite_score=0.0,
            elapsed_s=0.0,
            terminal_reason_code="cancelled_during_shutdown",
        )

        ops = recorder._operations
        assert len(ops) == 1
        op = ops[0]
        assert op["op_id"] == "op-cancel-test-1"
        assert op["status"] == "cancelled"
        assert op["sensor"] == "bg_pool"
        assert op["technique"] == "cancelled_during_shutdown"
        assert (
            op["terminal_reason_code"] == "cancelled_during_shutdown"
        )
        assert op["terminal_reason_class"] == (
            TerminalReasonClass.CANCELLED_SHUTDOWN.value
        )

    def test_seal_idempotency_with_prior_record_ledger(self, recorder):
        """If ``_record_ledger`` already recorded the op with rich
        attribution (Slice 12Q first-write-wins via
        ``_recorded_op_ids``), the seal fallback is a complete
        no-op — no duplicate row, no overwrite of the earlier
        terminal_reason_code."""
        set_active_recorder(recorder)

        # Simulate the Slice 12Q normal-path record.
        recorder.record_operation(
            op_id="op-idempotent-1",
            status="completed",
            sensor="opportunity_miner",
            technique="claude-extended-thinking",
            composite_score=0.91,
            elapsed_s=42.5,
            provider="claude-api",
            cost_usd=0.0123,
            terminal_reason_code="apply_succeeded_verify_passed",
        )
        assert len(recorder._operations) == 1

        # Now fire the Slice 12R seal for the SAME op_id.
        get_active_recorder().record_operation(
            op_id="op-idempotent-1",
            status="cancelled",
            sensor="bg_pool",
            technique="cancelled_shutdown",
            composite_score=0.0,
            elapsed_s=0.0,
            terminal_reason_code="cancelled_shutdown",
        )

        # First write wins — exactly one row, original attribution
        # preserved.
        assert len(recorder._operations) == 1
        op = recorder._operations[0]
        assert op["status"] == "completed"
        assert op["sensor"] == "opportunity_miner"
        assert (
            op["terminal_reason_code"] == "apply_succeeded_verify_passed"
        )

    def test_seal_swallows_when_no_active_recorder(self):
        """``get_active_recorder()`` returning None must not crash
        the cleanup callback (no recorder = headless rig / unit
        test / pre-harness boot)."""
        assert get_active_recorder() is None
        # The seal's try/except wraps everything; the None check
        # is the inner guard. Both layers prove out here.
        active = get_active_recorder()
        if active is not None:  # pragma: no cover
            pytest.fail("Expected None recorder in isolated test")


class TestPhase1ASTPin:
    """AST pin: the seal block lives inside
    ``_bg_unregister_active``."""

    def test_seal_block_present_in_bg_unregister_active(self):
        """The literal markers — function name + the
        ``Slice 12R Phase 1`` comment + the call shape — must all
        live in ``governed_loop_service.py`` so a refactor can't
        silently drop the seal."""
        gls_path = Path(
            "backend/core/ouroboros/governance/"
            "governed_loop_service.py"
        )
        source = gls_path.read_text()

        # Function exists.
        assert "def _bg_unregister_active(" in source, (
            "_bg_unregister_active was renamed or removed — the "
            "Slice 12R seal must move with it"
        )

        # Phase 1 marker comment.
        assert "Slice 12R Phase 1" in source, (
            "Slice 12R Phase 1 marker missing — telemetry seal "
            "block was edited out"
        )

        # Critical wiring — composes the canonical accessor and
        # passes the closed-taxonomy terminal_reason_code.
        assert "get_active_recorder" in source, (
            "get_active_recorder import / call missing from "
            "governed_loop_service.py — seal is unwired"
        )
        # Canonical classifier-matched code — bare
        # ``cancelled_shutdown`` would downgrade to OTHER.
        assert "cancelled_during_shutdown" in source, (
            "Seal no longer carries the cancelled_during_shutdown "
            "terminal_reason_code — Slice 12P classifier will "
            "downgrade the op to OTHER"
        )

    def test_seal_block_walks_ast_inside_bg_unregister_active(self):
        """Stronger pin: parse the file and confirm the seal lives
        in the right nesting — call to ``get_active_recorder``
        appears inside the body of ``_bg_unregister_active`` (not
        just somewhere else in the module)."""
        gls_path = Path(
            "backend/core/ouroboros/governance/"
            "governed_loop_service.py"
        )
        tree = ast.parse(gls_path.read_text())

        target_fn = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "_bg_unregister_active"
            ):
                target_fn = node
                break

        assert target_fn is not None, (
            "_bg_unregister_active not found via AST walk"
        )

        # Walk inside the function body looking for a Name/Attribute
        # reference to get_active_recorder.
        seal_calls_found = 0
        for inner in ast.walk(target_fn):
            if isinstance(inner, ast.Call):
                fn = inner.func
                if (
                    isinstance(fn, ast.Name)
                    and fn.id == "get_active_recorder"
                ):
                    seal_calls_found += 1
                elif (
                    isinstance(fn, ast.Attribute)
                    and fn.attr == "get_active_recorder"
                ):
                    seal_calls_found += 1

        assert seal_calls_found >= 1, (
            "AST walk found no get_active_recorder() call inside "
            "_bg_unregister_active — seal block was moved or "
            "removed"
        )


# ──────────────────────────────────────────────────────────────────────
# Phase 2 — Payload sanitization (auto-repair)
# ──────────────────────────────────────────────────────────────────────


class TestPhase2AutoRepair:
    """Verifies the comprehensive Unicode→ASCII repair map and
    that ``AsciiStrictGate.check`` runs repair *before* the
    hard-reject scan."""

    @pytest.mark.parametrize(
        "raw, expected_repair, label",
        [
            ("requirements—.txt", "requirements-.txt", "em-dash"),
            ("hello–world", "hello-world", "en-dash"),
            ("“ASCII”", '"ASCII"', "smart double quotes"),
            ("don’t panic", "don't panic", "right single quote"),
            ("a…z", "a...z", "horizontal ellipsis"),
            ("foo bar", "foo bar", "no-break space"),
            ("​clean", "clean", "zero-width space"),
            ("§6", "S6", "section sign"),
            ("CLASSIFY→ROUTE", "CLASSIFY->ROUTE", "right arrow"),
            ("(c)©", "(c)(c)", "copyright sign"),
            ("100°F", "100degF", "degree sign"),
            ("3×4", "3x4", "multiplication sign"),
        ],
    )
    def test_typographical_chars_repair_to_ascii(
        self, raw, expected_repair, label
    ):
        """Each common typographical codepoint repairs to a
        deterministic ASCII substitute."""
        repaired, n = repair_content(raw)
        assert repaired == expected_repair, (
            f"{label}: expected {expected_repair!r}, got {repaired!r}"
        )
        assert n >= 1, f"{label}: repair counter not incremented"

    def test_letters_intentionally_excluded(self):
        """Cyrillic / Arabic look-alikes MUST NOT be in the repair
        map — they change identifier identity (the rapid**ف**uzz
        class). This is the load-bearing invariant that prevents
        the gate from silently healing a package-name typosquat."""
        forbidden_letters = (
            0x0641,  # Arabic FEH
            0x0430,  # Cyrillic 'а'
            0x0435,  # Cyrillic 'е'
            0x03BF,  # Greek omicron 'ο'
        )
        for cp in forbidden_letters:
            assert cp not in _UNICODE_REPAIR_MAP, (
                f"Codepoint U+{cp:04X} ({chr(cp)!r}) is a letter "
                "look-alike — must NOT be in _UNICODE_REPAIR_MAP "
                "(changing a letter changes identity)"
            )

    def test_check_runs_repair_before_scan(self):
        """End-to-end: a candidate carrying only repairable
        characters passes ``check`` with ``ok=True`` and is
        mutated in place to the repaired form."""
        gate = AsciiStrictGate(enabled=True, auto_repair=True)
        candidate = {
            "file_path": "requirements.txt",
            "full_content": (
                "# pinned deps - touched 2026-05-22—see PR\n"
                "rich>=13.0\n"
                "click’s_companion==1.2\n"
            ),
        }
        ok, reason, offenders = gate.check(candidate)
        assert ok is True, (
            f"Repairable-only candidate failed gate: "
            f"reason={reason}, offenders={offenders}"
        )
        assert reason is None
        assert offenders == []

        # In-place mutation — em-dash and curly apostrophe both
        # gone; rest preserved verbatim.
        assert "—" not in candidate["full_content"]
        assert "’" not in candidate["full_content"]
        assert "rich>=13.0" in candidate["full_content"]
        assert "click's_companion==1.2" in candidate["full_content"]
        # Observability annotation present.
        assert candidate.get("_ascii_repair_count", 0) >= 2

    def test_check_still_rejects_letter_lookalikes(self):
        """The repair pass does NOT mask the
        rapid**ف**uzz-class failure — Unicode letters survive
        repair and the gate hard-rejects them."""
        gate = AsciiStrictGate(enabled=True, auto_repair=True)
        candidate = {
            "file_path": "requirements.txt",
            # Arabic 'ف' inside what looks like "rapidfuzz"
            "full_content": "rapidfفuzz==3.5.2\n",
        }
        ok, reason, offenders = gate.check(candidate)
        assert ok is False, (
            "Letter look-alike must hard-fail the gate even with "
            "auto-repair enabled"
        )
        assert reason is not None
        assert len(offenders) >= 1


# ──────────────────────────────────────────────────────────────────────
# Phase 3 — Reflexive ASCII healing
# ──────────────────────────────────────────────────────────────────────


class TestPhase3ReflexiveHealing:
    """Validates the reflexive healing formatter is wired into
    the orchestrator's ``ascii_corruption`` retry branch."""

    def test_format_returns_nonempty_for_ascii_gate_failed(self):
        """``format_structural_rejection_feedback`` matches the
        ``ascii_gate_failed`` substring and returns a
        ``<DEVELOPER_FEEDBACK>`` block on the second attempt."""
        feedback = format_structural_rejection_feedback(
            "ascii_gate_failed: 3 offenders at lines 14, 22, 31",
            rejection_detail=(
                "U+0641 ARABIC LETTER FEH masquerading as ASCII 'f'"
            ),
            attempt_number=2,
            max_attempts=2,
        )
        assert feedback is not None
        assert "DEVELOPER_FEEDBACK" in feedback
        assert (
            'priority="CRITICAL_SYSTEM_OVERRIDE"' in feedback
            or "CRITICAL_SYSTEM_OVERRIDE" in feedback
        )
        # The remediation action list for ascii_gate_failed must
        # bubble through to the prompt.
        assert "ASCII" in feedback.upper()

    def test_format_returns_none_for_unrelated_error(self):
        """Non-structural errors (cost_budget_exhausted,
        wall_clock_cap, …) get None back — the orchestrator's
        guarded prepend then leaves legacy feedback intact."""
        assert (
            format_structural_rejection_feedback(
                "cost_budget_exhausted",
                rejection_detail="$0.50 cap hit",
                attempt_number=2,
                max_attempts=2,
            )
            is None
        )

    def test_terminal_reason_classifier_cancelled_shutdown(self):
        """Sanity pin on Slice 12P classifier — the exact code
        the seal emits must classify as CANCELLED_SHUTDOWN, not
        OTHER."""
        cls = classify_terminal_reason("cancelled_during_shutdown")
        assert cls is TerminalReasonClass.CANCELLED_SHUTDOWN


class TestPhase3OrchestratorASTPin:
    """AST pin: the Slice 12R Phase 3 prepend block lives inside
    the ``elif _err_str.startswith("ascii_corruption"):`` branch
    in ``orchestrator.py``."""

    def test_reflexive_healing_prepend_present_in_ascii_branch(self):
        """Source-string pins on the orchestrator file:
        (1) the ascii_corruption branch exists, (2) the Slice 12R
        Phase 3 marker comment appears, (3) the
        ``format_structural_rejection_feedback`` call appears."""
        orch_path = Path(
            "backend/core/ouroboros/governance/orchestrator.py"
        )
        source = orch_path.read_text()

        assert (
            'elif _err_str.startswith("ascii_corruption"):' in source
        ), (
            "ascii_corruption retry branch missing from orchestrator"
        )

        assert "Slice 12R Phase 3" in source, (
            "Slice 12R Phase 3 marker missing — prepend block "
            "was edited out of orchestrator.py"
        )

        assert (
            "format_structural_rejection_feedback" in source
        ), (
            "Slice 12P reflexive healing formatter no longer "
            "referenced from orchestrator — prepend is unwired"
        )

        # The prepend must mention the ascii_gate_failed class so
        # the reflexive_healing classifier picks the right action
        # list.
        assert "ascii_gate_failed" in source, (
            "ascii_gate_failed canonical code missing from "
            "orchestrator retry feedback — reflexive healing "
            "classifier will fall through to no-match"
        )

    def test_format_call_appears_in_ascii_branch_textually(self):
        """Targeted check: between the ascii_corruption branch
        header and the next ``elif`` boundary, the source must
        carry the reflexive healing call. Prevents the prepend
        from drifting out of the right retry branch on future
        edits."""
        orch_path = Path(
            "backend/core/ouroboros/governance/orchestrator.py"
        )
        source = orch_path.read_text()
        marker = 'elif _err_str.startswith("ascii_corruption"):'
        start = source.find(marker)
        assert start != -1
        # Find the next elif/else/end-of-chain after the branch
        # opens — the prepend must live before it.
        next_elif = source.find(
            'elif _err_str.startswith(',
            start + len(marker),
        )
        assert next_elif != -1, (
            "Could not find next elif boundary after "
            "ascii_corruption branch — orchestrator structure "
            "changed"
        )
        branch_body = source[start:next_elif]
        assert "Slice 12R Phase 3" in branch_body, (
            "Slice 12R Phase 3 prepend drifted out of the "
            "ascii_corruption retry branch"
        )
        assert (
            "format_structural_rejection_feedback" in branch_body
        ), (
            "format_structural_rejection_feedback call drifted "
            "out of the ascii_corruption retry branch"
        )


# ──────────────────────────────────────────────────────────────────────
# Cross-phase: process-singleton accessor module-level pins
# ──────────────────────────────────────────────────────────────────────


class TestSessionRecorderModuleAccessors:
    """Pins the three module-level accessors Slice 12R depends
    on. The seal block imports them by name; renames here cascade
    silently otherwise."""

    def test_accessors_are_module_level_callables(self):
        from backend.core.ouroboros.battle_test import session_recorder
        for name in (
            "set_active_recorder",
            "get_active_recorder",
            "reset_active_recorder",
        ):
            attr = getattr(session_recorder, name, None)
            assert attr is not None, (
                f"session_recorder.{name} missing — Slice 12R "
                "seal will fail to import"
            )
            assert callable(attr)

    def test_get_active_recorder_signature_takes_no_args(self):
        sig = inspect.signature(get_active_recorder)
        assert len(sig.parameters) == 0
