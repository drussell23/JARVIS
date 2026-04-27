"""Wiring PR #2 — Phase 7.3 ScopedToolBackend per-Order budget caller wiring.

Phase 7.3 shipped `compute_effective_max_mutations(order, env_default,
adapted=None)` as the substrate (PR #23083). This wiring PR threads
that helper into `general_driver.py:dispatch_general()` — the single
inner site that constructs `ScopedToolBackend` from invocation
metadata. All upstream callers benefit automatically (single-point
enforcement = easier to reason about + easier to test).

Pinned cage:
  * Master-off byte-identical: when JARVIS_SCOPED_TOOL_BACKEND_LOAD_
    ADAPTED_BUDGETS=false (default) AND no adapted YAML present,
    effective_max_mutations == invocation["max_mutations"]. Zero
    behavior change in default flag state.
  * Master-on Order-1 lower budget: adapted YAML with order=1 entry
    lowers ScopedToolBackend's actual max_mutations.
  * Master-on Order-2 lower budget + MIN_ORDER2_BUDGET=1 floor.
  * Order is read from invocation["order"]; missing/invalid/unknown
    defaults to Order-1 (safer assumption).
  * read_only flag is derived from EFFECTIVE max_mutations (not the
    raw env value) — when adapted lowers to 0, scope becomes
    read-only.
  * Defense-in-depth: doctored YAML with HIGHER budget than env →
    min() clamps back to env_default → cage cannot be loosened.
  * Caller-grep: ScopedToolBackend constructed with
    effective_max_mutations (not raw max_mutations).
  * Caller authority: general_driver imports the helper directly
    (this IS the wiring; not a violation).
  * Subagent contract docstring updated with the new optional
    `order` field (positive grep).
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest import mock

import pytest

from backend.core.ouroboros.governance.adaptation.adapted_mutation_budget_loader import (  # noqa: E501
    AdaptedBudgetEntry,
    MIN_ORDER2_BUDGET,
    compute_effective_max_mutations,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]
_DRIVER_PATH = (
    _REPO_ROOT
    / "backend/core/ouroboros/governance/general_driver.py"
)


# ---------------------------------------------------------------------------
# Section A — helper-direct unit tests (substrate-level pins via wiring)
# ---------------------------------------------------------------------------


class TestHelperViaWiringEntryPoint:
    """The wiring is just `compute_effective_max_mutations(order,
    max_mutations)` at the top of dispatch_general's setup. Direct
    substrate pins prove the helper does what the wiring promises."""

    def test_master_off_returns_env_unchanged(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_SCOPED_TOOL_BACKEND_LOAD_ADAPTED_BUDGETS",
            raising=False,
        )
        # Pre-loaded empty adapted dict simulates master-off state.
        assert compute_effective_max_mutations(1, 5, adapted={}) == 5
        assert compute_effective_max_mutations(2, 3, adapted={}) == 3
        assert compute_effective_max_mutations(1, 0, adapted={}) == 0

    def test_master_on_order_1_lowers_budget(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_SCOPED_TOOL_BACKEND_LOAD_ADAPTED_BUDGETS", "1",
        )
        assert compute_effective_max_mutations(
            1, 10, adapted={1: 3},
        ) == 3

    def test_master_on_order_2_lowers_budget(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_SCOPED_TOOL_BACKEND_LOAD_ADAPTED_BUDGETS", "1",
        )
        assert compute_effective_max_mutations(
            2, 5, adapted={2: 1},
        ) == 1

    def test_doctored_higher_budget_clamped_to_env(self, monkeypatch):
        # Defense-in-depth: even if YAML somehow has budget > env,
        # min() ensures we never raise.
        monkeypatch.setenv(
            "JARVIS_SCOPED_TOOL_BACKEND_LOAD_ADAPTED_BUDGETS", "1",
        )
        assert compute_effective_max_mutations(
            1, 3, adapted={1: 99},
        ) == 3

    def test_other_order_unaffected(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_SCOPED_TOOL_BACKEND_LOAD_ADAPTED_BUDGETS", "1",
        )
        # Adapted entry only for Order-2; Order-1 caller unchanged.
        assert compute_effective_max_mutations(
            1, 10, adapted={2: 1},
        ) == 10

    def test_loader_exception_falls_back(self, monkeypatch):
        from backend.core.ouroboros.governance.adaptation import (
            adapted_mutation_budget_loader as loader,
        )
        monkeypatch.setenv(
            "JARVIS_SCOPED_TOOL_BACKEND_LOAD_ADAPTED_BUDGETS", "1",
        )
        with mock.patch.object(
            loader, "load_adapted_budgets",
            side_effect=RuntimeError("boom"),
        ):
            # adapted=None forces a load attempt; raise is caught;
            # falls back to env_default.
            assert compute_effective_max_mutations(1, 5) == 5


# ---------------------------------------------------------------------------
# Section B — caller-source invariants (general_driver.py)
# ---------------------------------------------------------------------------


class TestDriverSourceInvariants:
    """Source-level pins that prove the wiring is in place."""

    def test_driver_imports_helper(self):
        src = _DRIVER_PATH.read_text(encoding="utf-8")
        # Lazy import inside the function — confirm the import string
        # is present.
        assert (
            "from backend.core.ouroboros.governance.adaptation"
            ".adapted_mutation_budget_loader import" in src
            or "compute_effective_max_mutations" in src
        )
        assert "compute_effective_max_mutations" in src

    def test_driver_uses_effective_max_mutations_for_scope(self):
        src = _DRIVER_PATH.read_text(encoding="utf-8")
        # The ToolScope must derive read_only from the EFFECTIVE
        # value, not the raw env value.
        assert (
            "read_only=(effective_max_mutations == 0)" in src
        ), (
            "ToolScope.read_only must use effective_max_mutations "
            "(post-wiring); using raw max_mutations would create a "
            "scope/budget mismatch when adapted lowers to 0."
        )

    def test_driver_passes_effective_to_scoped_backend(self):
        src = _DRIVER_PATH.read_text(encoding="utf-8")
        # ScopedToolBackend(max_mutations=...) must receive the wired value.
        # Use a focused substring search to avoid false-positives.
        # Pattern: "max_mutations=effective_max_mutations" within a few
        # lines of "ScopedToolBackend(".
        idx = src.find("ScopedToolBackend(")
        assert idx > 0, "ScopedToolBackend constructor not found"
        # Search next 800 chars for the wired arg.
        window = src[idx: idx + 800]
        assert "max_mutations=effective_max_mutations" in window, (
            "ScopedToolBackend(max_mutations=...) must be passed the "
            "post-wiring effective_max_mutations, not the raw env value."
        )

    def test_driver_reads_order_from_invocation(self):
        src = _DRIVER_PATH.read_text(encoding="utf-8")
        assert 'invocation.get("order"' in src

    def test_driver_defaults_invalid_order_to_1(self):
        # The wiring snippet should include the safety fallback
        # `if order not in (1, 2): order = 1`.
        src = _DRIVER_PATH.read_text(encoding="utf-8")
        assert "if order not in (1, 2):" in src
        # The body of that if should set order = 1
        # (not a hard pattern but a documentation pin — tested below
        # behaviorally too).

    def test_no_other_live_caller_passes_raw_max_mutations_to_scope(
        self,
    ):
        # Caller-grep invariant: the ONLY ScopedToolBackend constructor
        # that gates max_mutations is in general_driver.py. If a future
        # PR adds a second one, it MUST also wire through the helper.
        violations = []
        backend_dir = _REPO_ROOT / "backend"
        ctor_pattern = re.compile(r"ScopedToolBackend\(")
        allowlist = {
            # The constructor definition itself in scoped_tool_backend.py
            "backend/core/ouroboros/governance/scoped_tool_backend.py",
            # The wired site (this PR).
            "backend/core/ouroboros/governance/general_driver.py",
            # The substrate's docstring example mentioning the call.
            "backend/core/ouroboros/governance/adaptation/adapted_mutation_budget_loader.py",
        }
        for path in backend_dir.rglob("*.py"):
            rel = str(path.relative_to(_REPO_ROOT))
            if "/test" in rel or path.name.startswith("test_"):
                continue
            if rel in allowlist:
                continue
            try:
                src = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for m in ctor_pattern.finditer(src):
                line_no = src[: m.start()].count("\n") + 1
                violations.append(f"{rel}:{line_no}")
        assert not violations, (
            f"New ScopedToolBackend(...) call sites detected outside "
            f"the wired allowlist — they MUST also wire through "
            f"compute_effective_max_mutations(order, env_default):\n  "
            + "\n  ".join(violations)
        )


# ---------------------------------------------------------------------------
# Section C — subagent contract docstring updated
# ---------------------------------------------------------------------------


class TestSubagentContractDocstring:
    def test_general_invocation_shape_documents_order(self):
        path = (
            _REPO_ROOT
            / "backend/core/ouroboros/governance/subagent_contracts.py"
        )
        src = path.read_text(encoding="utf-8")
        # The general_invocation shape comment must include the new
        # optional `order` field with Phase 7.3 reference.
        assert '"order": int' in src
        assert "Phase 7.3" in src


# ---------------------------------------------------------------------------
# Section D — behavioral end-to-end via dispatch_general (mocked provider)
# ---------------------------------------------------------------------------


def _build_payload(*, max_mutations: int, order=None, allowed_tools=("read_file",)):
    """Build the minimal valid payload for dispatch_general."""
    invocation = {
        "operation_scope": ("backend/",),
        "max_mutations": max_mutations,
        "allowed_tools": allowed_tools,
        "invocation_reason": "wiring test",
        "parent_op_risk_tier": "NOTIFY_APPLY",
        "goal": "test goal",
    }
    if order is not None:
        invocation["order"] = order
    return {
        "sub_id": "sub-test-7.3-wiring",
        "invocation": invocation,
        "primary_provider_name": "test-fake",
        "max_rounds": 1,
        "tool_timeout_s": 5.0,
    }


class _MutationCapturingBackend:
    """Test stub that captures the max_mutations actually passed to
    ScopedToolBackend. We patch ScopedToolBackend with this so we
    don't need to spin up the full async dispatch."""

    def __init__(self):
        self.last_max_mutations = None
        self.last_read_only = None

    def __call__(self, *, inner, gate, max_mutations, state_mirror=None):
        self.last_max_mutations = max_mutations
        self.last_read_only = gate.scope.read_only
        # Return a minimal mock so downstream code in dispatch_general
        # doesn't crash. We don't actually run a tool loop here.
        m = mock.MagicMock()
        m.max_mutations = max_mutations
        m._mutations_count = 0
        m._mutation_records = []
        return m


@pytest.fixture
def patch_scoped_backend(monkeypatch):
    """Patch ScopedToolBackend at its import source so the lazy
    `from ... import ScopedToolBackend` inside dispatch_general
    picks up the patched version."""
    capturing = _MutationCapturingBackend()
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.scoped_tool_backend.ScopedToolBackend",
        capturing,
    )
    return capturing


class TestDispatchBehavioralWiring:
    """Behavioral pins — invoke dispatch_general up to the point
    where ScopedToolBackend is constructed and verify the captured
    max_mutations matches the wiring expectation."""

    def _invoke_to_capture(self, monkeypatch, payload, capturing):
        """Run run_general_tool_loop just far enough to construct
        ScopedToolBackend. The capturing fixture records max_mutations
        at construction time; whatever fails downstream doesn't matter
        for the wiring assertion."""
        from backend.core.ouroboros.governance import general_driver
        import asyncio
        from pathlib import Path

        # Provider must be non-None so we proceed past the provider
        # null-check and reach backend construction. Use a stub.
        def fake_provider_registry(name):
            return mock.MagicMock(name=f"fake-provider-{name}")

        try:
            asyncio.run(general_driver.run_general_tool_loop(
                payload,
                project_root=Path("/tmp/_wiring_test_root"),
                provider_registry=fake_provider_registry,
            ))
        except Exception:
            pass  # we only care that capture happened

    def test_master_off_byte_identical(
        self, monkeypatch, patch_scoped_backend,
    ):
        # Default flag state → effective == env.
        monkeypatch.delenv(
            "JARVIS_SCOPED_TOOL_BACKEND_LOAD_ADAPTED_BUDGETS",
            raising=False,
        )
        payload = _build_payload(max_mutations=5, order=1)
        self._invoke_to_capture(monkeypatch, payload, patch_scoped_backend)
        assert patch_scoped_backend.last_max_mutations == 5

    def test_master_on_order_1_lowers(
        self, monkeypatch, tmp_path, patch_scoped_backend,
    ):
        yaml_path = tmp_path / "y.yaml"
        yaml_path.write_text(
            "schema_version: 1\n"
            "budgets:\n"
            "  - order: 1\n    budget: 2\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(
            "JARVIS_SCOPED_TOOL_BACKEND_LOAD_ADAPTED_BUDGETS", "1",
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTED_MUTATION_BUDGETS_PATH", str(yaml_path),
        )
        payload = _build_payload(max_mutations=10, order=1)
        self._invoke_to_capture(monkeypatch, payload, patch_scoped_backend)
        # Adapted budget=2; env=10; min=2.
        assert patch_scoped_backend.last_max_mutations == 2

    def test_master_on_order_2_lowers(
        self, monkeypatch, tmp_path, patch_scoped_backend,
    ):
        yaml_path = tmp_path / "y.yaml"
        yaml_path.write_text(
            "schema_version: 1\n"
            "budgets:\n"
            "  - order: 2\n    budget: 1\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(
            "JARVIS_SCOPED_TOOL_BACKEND_LOAD_ADAPTED_BUDGETS", "1",
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTED_MUTATION_BUDGETS_PATH", str(yaml_path),
        )
        payload = _build_payload(max_mutations=5, order=2)
        self._invoke_to_capture(monkeypatch, payload, patch_scoped_backend)
        assert patch_scoped_backend.last_max_mutations == 1

    def test_master_on_doctored_higher_clamped(
        self, monkeypatch, tmp_path, patch_scoped_backend,
    ):
        # YAML claims budget=99 (operator typo); env=3.
        # Helper must clamp to min(3, 99) == 3.
        yaml_path = tmp_path / "y.yaml"
        yaml_path.write_text(
            "schema_version: 1\n"
            "budgets:\n"
            "  - order: 1\n    budget: 99\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(
            "JARVIS_SCOPED_TOOL_BACKEND_LOAD_ADAPTED_BUDGETS", "1",
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTED_MUTATION_BUDGETS_PATH", str(yaml_path),
        )
        payload = _build_payload(max_mutations=3, order=1)
        self._invoke_to_capture(monkeypatch, payload, patch_scoped_backend)
        assert patch_scoped_backend.last_max_mutations == 3

    def test_missing_order_defaults_to_1(
        self, monkeypatch, tmp_path, patch_scoped_backend,
    ):
        # Order field absent → defaults to 1.
        yaml_path = tmp_path / "y.yaml"
        yaml_path.write_text(
            "schema_version: 1\n"
            "budgets:\n"
            "  - order: 1\n    budget: 2\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(
            "JARVIS_SCOPED_TOOL_BACKEND_LOAD_ADAPTED_BUDGETS", "1",
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTED_MUTATION_BUDGETS_PATH", str(yaml_path),
        )
        payload = _build_payload(max_mutations=5)  # no order
        self._invoke_to_capture(monkeypatch, payload, patch_scoped_backend)
        # Treated as Order-1 → adapted budget=2 applies.
        assert patch_scoped_backend.last_max_mutations == 2

    def test_invalid_order_defaults_to_1(
        self, monkeypatch, tmp_path, patch_scoped_backend,
    ):
        yaml_path = tmp_path / "y.yaml"
        yaml_path.write_text(
            "schema_version: 1\n"
            "budgets:\n"
            "  - order: 1\n    budget: 2\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(
            "JARVIS_SCOPED_TOOL_BACKEND_LOAD_ADAPTED_BUDGETS", "1",
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTED_MUTATION_BUDGETS_PATH", str(yaml_path),
        )
        # Order 99 → unknown → defaults to 1.
        payload = _build_payload(max_mutations=5, order=99)
        self._invoke_to_capture(monkeypatch, payload, patch_scoped_backend)
        assert patch_scoped_backend.last_max_mutations == 2

    def test_non_int_order_defaults_to_1(
        self, monkeypatch, tmp_path, patch_scoped_backend,
    ):
        yaml_path = tmp_path / "y.yaml"
        yaml_path.write_text(
            "schema_version: 1\n"
            "budgets:\n"
            "  - order: 1\n    budget: 2\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(
            "JARVIS_SCOPED_TOOL_BACKEND_LOAD_ADAPTED_BUDGETS", "1",
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTED_MUTATION_BUDGETS_PATH", str(yaml_path),
        )
        # Non-int "two" → defaults to 1.
        payload = _build_payload(max_mutations=5, order="two")
        self._invoke_to_capture(monkeypatch, payload, patch_scoped_backend)
        assert patch_scoped_backend.last_max_mutations == 2

    def test_read_only_flag_uses_effective_value(
        self, monkeypatch, tmp_path, patch_scoped_backend,
    ):
        # Env says max_mutations=5 (writable scope); adapted lowers to 0
        # (read-only). The ToolScope MUST become read-only — proves the
        # wiring threads the effective value through both the scope AND
        # the backend.
        yaml_path = tmp_path / "y.yaml"
        yaml_path.write_text(
            "schema_version: 1\n"
            "budgets:\n"
            "  - order: 1\n    budget: 0\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(
            "JARVIS_SCOPED_TOOL_BACKEND_LOAD_ADAPTED_BUDGETS", "1",
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTED_MUTATION_BUDGETS_PATH", str(yaml_path),
        )
        payload = _build_payload(max_mutations=5, order=1)
        self._invoke_to_capture(monkeypatch, payload, patch_scoped_backend)
        assert patch_scoped_backend.last_max_mutations == 0
        assert patch_scoped_backend.last_read_only is True

    def test_order_2_floor_preserved(
        self, monkeypatch, tmp_path, patch_scoped_backend,
    ):
        # Loader hard-floors Order-2 at MIN_ORDER2_BUDGET=1. A doctored
        # YAML with budget=0 for Order-2 should be raised to 1 at the
        # loader, then min(env=5, adapted=1) = 1 reaches the backend.
        yaml_path = tmp_path / "y.yaml"
        yaml_path.write_text(
            "schema_version: 1\n"
            "budgets:\n"
            "  - order: 2\n    budget: 0\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(
            "JARVIS_SCOPED_TOOL_BACKEND_LOAD_ADAPTED_BUDGETS", "1",
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTED_MUTATION_BUDGETS_PATH", str(yaml_path),
        )
        payload = _build_payload(max_mutations=5, order=2)
        self._invoke_to_capture(monkeypatch, payload, patch_scoped_backend)
        assert patch_scoped_backend.last_max_mutations == MIN_ORDER2_BUDGET
        assert patch_scoped_backend.last_max_mutations == 1
