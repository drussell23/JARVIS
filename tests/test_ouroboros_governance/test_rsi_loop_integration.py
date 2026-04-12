"""Integration test for the RSI loop closure (Manifesto §6).

Proves the post-VERIFY Phase 8b2 hot-reload hook can hot-swap a real safe
module's bytecode in-process. The system under test is verify_gate.py — the
production safe module that ships in DEFAULT_SAFE_MODULES with a real probe
symbol (enforce_verify_thresholds). All disk mutations are restored in
fixture teardown so the test is idempotent across reruns and parallel
sessions.

The test exercises the EXACT call surface that the orchestrator's Phase 8b2
hook invokes (`reload_for_op`), so a green test here is direct evidence that
a successful APPLY+VERIFY against any safe module will close the RSI loop
in-process — no process restart required.

This is the deterministic counterpart to a live battle-test breakthrough:
- Battle test proves the autonomous side (sensors → ops → safe module).
- This integration test proves the deterministic side (the reload mechanism
  itself is correct against the real production module set).
"""

from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.module_hot_reloader import (
    DEFAULT_QUARANTINE,
    DEFAULT_SAFE_MODULES,
    PROBES,
    ModuleHotReloader,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
VERIFY_GATE_REL = "backend/core/ouroboros/governance/verify_gate.py"
VERIFY_GATE_MODULE = "backend.core.ouroboros.governance.verify_gate"
VERIFY_GATE_ABS = PROJECT_ROOT / VERIFY_GATE_REL
ORCHESTRATOR_REL = "backend/core/ouroboros/governance/orchestrator.py"


@pytest.fixture
def verify_gate_restoration():
    """Capture verify_gate.py content + restore on teardown.

    The teardown re-writes the original content AND reloads the module so
    that subsequent tests in the same session see the original
    enforce_verify_thresholds function object, not the mutated one.
    """
    original_content = VERIFY_GATE_ABS.read_text(encoding="utf-8")
    if VERIFY_GATE_MODULE not in sys.modules:
        importlib.import_module(VERIFY_GATE_MODULE)
    try:
        yield original_content
    finally:
        try:
            VERIFY_GATE_ABS.write_text(original_content, encoding="utf-8")
        finally:
            mod = sys.modules.get(VERIFY_GATE_MODULE)
            if mod is not None:
                try:
                    importlib.reload(mod)
                except Exception:
                    pass


def _mutate(original_content: str, marker: str) -> str:
    """Append a non-functional comment that perturbs the file's hash."""
    return original_content.rstrip() + f"\n\n# {marker}\n"


class TestSafeModuleSurface:
    """Sanity guards on the production safe-module set.

    These are not strictly RSI loop tests, but they catch the regression
    where someone removes verify_gate from the safe set or drops the probe.
    A failure here means the integration tests below would degrade to NO_OP
    and silently stop proving anything.
    """

    def test_verify_gate_is_in_default_safe_modules(self) -> None:
        assert VERIFY_GATE_MODULE in DEFAULT_SAFE_MODULES

    def test_verify_gate_has_probe_symbol(self) -> None:
        probe = PROBES.get(VERIFY_GATE_MODULE)
        assert probe == "enforce_verify_thresholds"

    def test_orchestrator_module_is_quarantined(self) -> None:
        # The orchestrator itself must NEVER be hot-reloaded (it holds FSM state).
        assert "backend.core.ouroboros.governance.orchestrator" in DEFAULT_QUARANTINE


class TestRSILoopClosureDirect:
    """Direct ModuleHotReloader → real verify_gate.py reload proof."""

    def test_hot_swap_replaces_in_process_function_object(
        self, verify_gate_restoration: str
    ) -> None:
        """The end-to-end RSI closure proof.

        Sequence:
          1. Capture id() of probe function (enforce_verify_thresholds).
          2. Construct reloader against real PROJECT_ROOT.
          3. Mutate verify_gate.py on disk (append a comment).
          4. Call reloader.reload_for_op directly — same call Phase 8b2 makes.
          5. Assert: HOT_RELOAD decision, success status, probe id changed.
          6. Re-resolve the function from sys.modules — confirm new id.
        """
        importlib.import_module(VERIFY_GATE_MODULE)
        before_func = sys.modules[VERIFY_GATE_MODULE].enforce_verify_thresholds
        before_id = id(before_func)

        reloader = ModuleHotReloader(project_root=PROJECT_ROOT)
        assert VERIFY_GATE_MODULE in reloader.safe_modules

        marker = f"RSI-loop-test-{time.time_ns()}"
        new_content = _mutate(verify_gate_restoration, marker)
        VERIFY_GATE_ABS.write_text(new_content, encoding="utf-8")

        batch = reloader.reload_for_op(
            op_id="rsi-loop-integration-test",
            target_files=[VERIFY_GATE_REL],
        )

        assert batch.decision.action == "HOT_RELOAD"
        assert VERIFY_GATE_MODULE in batch.decision.safe_modules
        assert batch.overall_status == "success", (
            f"expected success, got {batch.overall_status}: "
            f"{[(o.module_name, o.status, o.error) for o in batch.outcomes]}"
        )
        assert len(batch.outcomes) == 1
        outcome = batch.outcomes[0]
        assert outcome.module_name == VERIFY_GATE_MODULE
        assert outcome.status == "reloaded"
        assert outcome.probe_id_changed is True
        assert outcome.old_sha != outcome.new_sha
        assert reloader.reload_count == 1

        after_func = sys.modules[VERIFY_GATE_MODULE].enforce_verify_thresholds
        assert id(after_func) != before_id, (
            "probe function id did not change — module dict was not swapped"
        )

        on_disk = VERIFY_GATE_ABS.read_text(encoding="utf-8")
        assert marker in on_disk

    def test_no_change_when_disk_matches_imported_hash(
        self, verify_gate_restoration: str
    ) -> None:
        """If disk hash matches imported hash, status is no_change."""
        reloader = ModuleHotReloader(project_root=PROJECT_ROOT)

        batch = reloader.reload_for_op(
            op_id="rsi-no-change-test",
            target_files=[VERIFY_GATE_REL],
        )

        assert batch.decision.action == "HOT_RELOAD"
        assert batch.overall_status == "no_change"
        assert reloader.reload_count == 0
        outcome = batch.outcomes[0]
        assert outcome.status == "no_change"
        assert outcome.old_sha == outcome.new_sha

    def test_quarantined_module_triggers_restart_not_reload(
        self, verify_gate_restoration: str
    ) -> None:
        """Targeting a quarantined module routes to RESTART, not HOT_RELOAD."""
        reloader = ModuleHotReloader(project_root=PROJECT_ROOT)

        batch = reloader.reload_for_op(
            op_id="rsi-quarantine-test",
            target_files=[ORCHESTRATOR_REL],
        )

        assert batch.decision.action == "RESTART"
        assert batch.overall_status == "skipped"
        assert batch.restart_required is True
        assert reloader.reload_count == 0

    def test_two_consecutive_reloads_use_fresh_hash_baseline(
        self, verify_gate_restoration: str
    ) -> None:
        """Cache aliasing regression guard.

        After a successful reload, the imported-hash cache must hold the
        just-loaded disk hash. The next cycle's `old_sha` should therefore
        equal the previous cycle's `new_sha` — not the original baseline,
        and not a stale read. A second mutation must still be detected as
        change against this promoted baseline.
        """
        reloader = ModuleHotReloader(project_root=PROJECT_ROOT)

        marker_one = f"RSI-cycle-1-{time.time_ns()}"
        VERIFY_GATE_ABS.write_text(
            _mutate(verify_gate_restoration, marker_one), encoding="utf-8"
        )
        batch_one = reloader.reload_for_op(
            op_id="rsi-cycle-test-1",
            target_files=[VERIFY_GATE_REL],
        )
        assert batch_one.overall_status == "success"
        assert reloader.reload_count == 1
        outcome_one = batch_one.outcomes[0]
        assert outcome_one.old_sha != outcome_one.new_sha

        marker_two = f"RSI-cycle-2-{time.time_ns()}"
        VERIFY_GATE_ABS.write_text(
            _mutate(verify_gate_restoration, marker_two), encoding="utf-8"
        )
        batch_two = reloader.reload_for_op(
            op_id="rsi-cycle-test-2",
            target_files=[VERIFY_GATE_REL],
        )
        assert batch_two.overall_status == "success", (
            f"second cycle did not detect new mutation: {batch_two.overall_status}"
        )
        assert reloader.reload_count == 2
        outcome_two = batch_two.outcomes[0]
        # Cache promotion: cycle 2's pre-state == cycle 1's post-state.
        assert outcome_two.old_sha == outcome_one.new_sha
        # Mutation detection: cycle 2's post-state is the new file hash.
        assert outcome_two.new_sha != outcome_two.old_sha
        # Distinct mutations produce distinct hashes.
        assert outcome_one.new_sha != outcome_two.new_sha


class TestRSILoopClosureViaOrchestratorWiring:
    """Verify the orchestrator constructs a reloader and exposes the same surface.

    Doesn't run the full FSM (too many mock dependencies). Instead, confirms
    the construction path the live battle test takes (orchestrator __init__
    builds a real ModuleHotReloader against project_root) and that the call
    surface Phase 8b2 invokes is exactly what we test directly above.
    """

    def test_orchestrator_constructs_real_reloader_with_safe_set(self) -> None:
        from unittest.mock import MagicMock

        from backend.core.ouroboros.governance.orchestrator import (
            GovernedOrchestrator,
            OrchestratorConfig,
        )

        config = OrchestratorConfig(project_root=PROJECT_ROOT)
        stack = MagicMock()
        generator = MagicMock()

        orch = GovernedOrchestrator(
            stack=stack,
            generator=generator,
            approval_provider=None,
            config=config,
        )

        assert orch._hot_reloader is not None
        assert VERIFY_GATE_MODULE in orch._hot_reloader.safe_modules
        assert PROBES.get(VERIFY_GATE_MODULE) == "enforce_verify_thresholds"

    def test_orchestrator_reloader_can_hot_swap_verify_gate(
        self, verify_gate_restoration: str
    ) -> None:
        """Use the orchestrator-built reloader to drive the same closure.

        Same proof as the direct test, but the reloader is the one the live
        orchestrator constructs at boot, not a freshly-instantiated copy.
        Confirms there is no construction-path skew.
        """
        from unittest.mock import MagicMock

        from backend.core.ouroboros.governance.orchestrator import (
            GovernedOrchestrator,
            OrchestratorConfig,
        )

        before_id = id(
            sys.modules[VERIFY_GATE_MODULE].enforce_verify_thresholds
        )

        config = OrchestratorConfig(project_root=PROJECT_ROOT)
        orch = GovernedOrchestrator(
            stack=MagicMock(),
            generator=MagicMock(),
            approval_provider=None,
            config=config,
        )
        reloader = orch._hot_reloader
        assert reloader is not None

        marker = f"RSI-orch-wired-{time.time_ns()}"
        VERIFY_GATE_ABS.write_text(
            _mutate(verify_gate_restoration, marker), encoding="utf-8"
        )

        batch = reloader.reload_for_op(
            op_id="rsi-orch-wired-test",
            target_files=[VERIFY_GATE_REL],
        )

        assert batch.overall_status == "success"
        assert batch.outcomes[0].probe_id_changed is True
        after_id = id(
            sys.modules[VERIFY_GATE_MODULE].enforce_verify_thresholds
        )
        assert after_id != before_id
