"""Gap #2 Slice 3 — adapted_confidence_loader regression suite.

Covers:

  §1   master flag + path resolution
  §2   default-off behavior (every accessor returns None)
  §3   missing YAML, oversized YAML, malformed YAML
  §4   schema_version mismatch
  §5   per-knob tighten-only filter (defense-in-depth)
  §6   per-knob accessors round-trip
  §7   precedence integration with confidence_monitor accessors
  §8   baseline-vs-confidence-monitor pin (drift guard)
  §9   AST authority pins
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

# PyYAML is required for the loader — skip the suite cleanly if
# the dev environment doesn't have it.
yaml = pytest.importorskip("yaml")

from backend.core.ouroboros.governance.adaptation.adapted_confidence_loader import (
    ADAPTED_CONFIDENCE_LOADER_SCHEMA_VERSION,
    AdaptedConfidenceThresholds,
    MAX_YAML_BYTES,
    adapted_approaching_factor,
    adapted_enforce,
    adapted_floor,
    adapted_thresholds_path,
    adapted_window_k,
    baseline_approaching_factor,
    baseline_enforce,
    baseline_floor,
    baseline_window_k,
    is_loader_enabled,
    load_adapted_thresholds,
)


_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend"
    / "core"
    / "ouroboros"
    / "governance"
    / "adaptation"
    / "adapted_confidence_loader.py"
)


def _write_yaml(tmp_path: Path, doc: dict) -> Path:
    p = tmp_path / "adapted_confidence_thresholds.yaml"
    p.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return p


def _enable(monkeypatch, yaml_path: Path = None):
    """Helper: turn on the loader and optionally point it at a
    custom YAML path."""
    monkeypatch.setenv("JARVIS_CONFIDENCE_LOAD_ADAPTED", "true")
    if yaml_path is not None:
        monkeypatch.setenv(
            "JARVIS_ADAPTED_CONFIDENCE_THRESHOLDS_PATH",
            str(yaml_path),
        )


# ============================================================================
# §1 — Master flag + path resolution
# ============================================================================


class TestMasterFlagAndPath:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CONFIDENCE_LOAD_ADAPTED", raising=False,
        )
        assert is_loader_enabled() is False

    def test_explicit_true(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_LOAD_ADAPTED", "true",
        )
        assert is_loader_enabled() is True

    def test_garbage_value_treated_as_false(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_LOAD_ADAPTED", "maybe",
        )
        assert is_loader_enabled() is False

    def test_default_path(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_ADAPTED_CONFIDENCE_THRESHOLDS_PATH",
            raising=False,
        )
        p = adapted_thresholds_path()
        assert p.parts[-2:] == (
            ".jarvis", "adapted_confidence_thresholds.yaml",
        )

    def test_env_overrides_path(self, monkeypatch, tmp_path):
        monkeypatch.setenv(
            "JARVIS_ADAPTED_CONFIDENCE_THRESHOLDS_PATH",
            str(tmp_path / "custom.yaml"),
        )
        assert (
            adapted_thresholds_path()
            == tmp_path / "custom.yaml"
        )


# ============================================================================
# §2 — Default-off behavior
# ============================================================================


class TestDefaultOff:
    @pytest.fixture(autouse=True)
    def _disable(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CONFIDENCE_LOAD_ADAPTED", raising=False,
        )

    def test_load_returns_empty(self):
        result = load_adapted_thresholds()
        assert result.is_empty()

    def test_per_knob_accessors_return_none(self):
        assert adapted_floor() is None
        assert adapted_window_k() is None
        assert adapted_approaching_factor() is None
        assert adapted_enforce() is None


# ============================================================================
# §3 — Missing / oversized / malformed YAML
# ============================================================================


class TestPathologicalYAML:
    def test_missing_file_returns_empty(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path / "does_not_exist.yaml")
        result = load_adapted_thresholds()
        assert result.is_empty()

    def test_empty_file_returns_empty(
        self, monkeypatch, tmp_path,
    ):
        p = tmp_path / "empty.yaml"
        p.write_text("", encoding="utf-8")
        _enable(monkeypatch, p)
        result = load_adapted_thresholds()
        assert result.is_empty()

    def test_oversized_file_refused(
        self, monkeypatch, tmp_path,
    ):
        p = tmp_path / "oversized.yaml"
        # MAX_YAML_BYTES + 1 byte
        p.write_text("x" * (MAX_YAML_BYTES + 1), encoding="utf-8")
        _enable(monkeypatch, p)
        result = load_adapted_thresholds()
        assert result.is_empty()

    def test_malformed_yaml_returns_empty(
        self, monkeypatch, tmp_path,
    ):
        p = tmp_path / "bad.yaml"
        p.write_text("[: : :", encoding="utf-8")
        _enable(monkeypatch, p)
        result = load_adapted_thresholds()
        assert result.is_empty()

    def test_top_level_not_mapping_returns_empty(
        self, monkeypatch, tmp_path,
    ):
        p = tmp_path / "list.yaml"
        p.write_text("- a\n- b\n", encoding="utf-8")
        _enable(monkeypatch, p)
        result = load_adapted_thresholds()
        assert result.is_empty()

    def test_thresholds_key_missing_returns_empty(
        self, monkeypatch, tmp_path,
    ):
        p = _write_yaml(tmp_path, {
            "schema_version": ADAPTED_CONFIDENCE_LOADER_SCHEMA_VERSION,
        })
        _enable(monkeypatch, p)
        result = load_adapted_thresholds()
        assert result.is_empty()

    def test_thresholds_key_not_mapping_returns_empty(
        self, monkeypatch, tmp_path,
    ):
        p = _write_yaml(tmp_path, {
            "schema_version": ADAPTED_CONFIDENCE_LOADER_SCHEMA_VERSION,
            "thresholds": "not a dict",
        })
        _enable(monkeypatch, p)
        result = load_adapted_thresholds()
        assert result.is_empty()


# ============================================================================
# §4 — Schema version mismatch
# ============================================================================


class TestSchemaVersion:
    def test_wrong_version_returns_empty(
        self, monkeypatch, tmp_path,
    ):
        p = _write_yaml(tmp_path, {
            "schema_version": 999,
            "thresholds": {"floor": 0.10},
        })
        _enable(monkeypatch, p)
        result = load_adapted_thresholds()
        assert result.is_empty()

    def test_missing_version_returns_empty(
        self, monkeypatch, tmp_path,
    ):
        p = _write_yaml(tmp_path, {
            "thresholds": {"floor": 0.10},
        })
        _enable(monkeypatch, p)
        result = load_adapted_thresholds()
        assert result.is_empty()

    def test_correct_version_loads(
        self, monkeypatch, tmp_path,
    ):
        p = _write_yaml(tmp_path, {
            "schema_version": ADAPTED_CONFIDENCE_LOADER_SCHEMA_VERSION,
            "thresholds": {"floor": 0.10},
        })
        _enable(monkeypatch, p)
        result = load_adapted_thresholds()
        assert result.floor == 0.10


# ============================================================================
# §5 — Tighten-only filter (defense-in-depth)
# ============================================================================


class TestTightenFilter:
    def _load(self, monkeypatch, tmp_path, thresholds: dict):
        p = _write_yaml(tmp_path, {
            "schema_version": ADAPTED_CONFIDENCE_LOADER_SCHEMA_VERSION,
            "thresholds": thresholds,
        })
        _enable(monkeypatch, p)
        return load_adapted_thresholds()

    def test_floor_loosen_dropped(self, monkeypatch, tmp_path):
        # baseline 0.05; loosen attempt 0.01
        result = self._load(monkeypatch, tmp_path, {"floor": 0.01})
        assert result.floor is None

    def test_floor_at_baseline_accepted(self, monkeypatch, tmp_path):
        result = self._load(monkeypatch, tmp_path, {"floor": 0.05})
        assert result.floor == 0.05

    def test_floor_tighten_accepted(self, monkeypatch, tmp_path):
        result = self._load(monkeypatch, tmp_path, {"floor": 0.10})
        assert result.floor == 0.10

    def test_floor_outside_unit_interval_dropped(
        self, monkeypatch, tmp_path,
    ):
        result = self._load(monkeypatch, tmp_path, {"floor": 1.5})
        assert result.floor is None

    def test_floor_negative_dropped(self, monkeypatch, tmp_path):
        result = self._load(monkeypatch, tmp_path, {"floor": -0.1})
        assert result.floor is None

    def test_floor_non_numeric_dropped(self, monkeypatch, tmp_path):
        result = self._load(monkeypatch, tmp_path, {"floor": "high"})
        assert result.floor is None

    def test_window_k_loosen_dropped(self, monkeypatch, tmp_path):
        # baseline 16; loosen attempt 32
        result = self._load(monkeypatch, tmp_path, {"window_k": 32})
        assert result.window_k is None

    def test_window_k_tighten_accepted(self, monkeypatch, tmp_path):
        result = self._load(monkeypatch, tmp_path, {"window_k": 8})
        assert result.window_k == 8

    def test_window_k_zero_dropped(self, monkeypatch, tmp_path):
        result = self._load(monkeypatch, tmp_path, {"window_k": 0})
        assert result.window_k is None

    def test_approaching_factor_loosen_dropped(
        self, monkeypatch, tmp_path,
    ):
        # baseline 1.5; narrowing toward 1.0 = loosen
        result = self._load(
            monkeypatch, tmp_path,
            {"approaching_factor": 1.1},
        )
        assert result.approaching_factor is None

    def test_approaching_factor_tighten_accepted(
        self, monkeypatch, tmp_path,
    ):
        result = self._load(
            monkeypatch, tmp_path,
            {"approaching_factor": 2.0},
        )
        assert result.approaching_factor == 2.0

    def test_enforce_true_accepted(self, monkeypatch, tmp_path):
        result = self._load(monkeypatch, tmp_path, {"enforce": True})
        assert result.enforce is True

    def test_enforce_false_dropped(self, monkeypatch, tmp_path):
        # baseline is False; YAML enforce: false matches baseline,
        # no-op materialization → loader returns None
        result = self._load(monkeypatch, tmp_path, {"enforce": False})
        assert result.enforce is None

    def test_enforce_truthy_string_dropped(
        self, monkeypatch, tmp_path,
    ):
        # Strict: only literal True is accepted
        result = self._load(monkeypatch, tmp_path, {"enforce": "yes"})
        assert result.enforce is None


# ============================================================================
# §6 — Per-knob accessors round-trip
# ============================================================================


class TestAccessors:
    def test_full_record_round_trip(
        self, monkeypatch, tmp_path,
    ):
        p = _write_yaml(tmp_path, {
            "schema_version": ADAPTED_CONFIDENCE_LOADER_SCHEMA_VERSION,
            "proposal_id": "conf-abc",
            "approved_at": "2026-05-02T12:00:00Z",
            "approved_by": "alice",
            "thresholds": {
                "floor": 0.10,
                "window_k": 8,
                "approaching_factor": 2.0,
                "enforce": True,
            },
        })
        _enable(monkeypatch, p)
        assert adapted_floor() == 0.10
        assert adapted_window_k() == 8
        assert adapted_approaching_factor() == 2.0
        assert adapted_enforce() is True

    def test_partial_record_other_knobs_none(
        self, monkeypatch, tmp_path,
    ):
        # Only floor moved; the other three knobs stay at None
        # (consumer falls through to hardcoded default).
        p = _write_yaml(tmp_path, {
            "schema_version": ADAPTED_CONFIDENCE_LOADER_SCHEMA_VERSION,
            "thresholds": {"floor": 0.08},
        })
        _enable(monkeypatch, p)
        assert adapted_floor() == 0.08
        assert adapted_window_k() is None
        assert adapted_approaching_factor() is None
        assert adapted_enforce() is None

    def test_metadata_preserved(self, monkeypatch, tmp_path):
        p = _write_yaml(tmp_path, {
            "schema_version": ADAPTED_CONFIDENCE_LOADER_SCHEMA_VERSION,
            "proposal_id": "conf-xyz",
            "approved_at": "2026-05-02T13:00:00Z",
            "approved_by": "bob",
            "thresholds": {"floor": 0.10},
        })
        _enable(monkeypatch, p)
        result = load_adapted_thresholds()
        assert result.proposal_id == "conf-xyz"
        assert result.approved_at == "2026-05-02T13:00:00Z"
        assert result.approved_by == "bob"


# ============================================================================
# §7 — Precedence integration with confidence_monitor accessors
# ============================================================================


class TestPrecedenceIntegration:
    def _setup(self, monkeypatch, tmp_path, thresholds: dict):
        p = _write_yaml(tmp_path, {
            "schema_version": ADAPTED_CONFIDENCE_LOADER_SCHEMA_VERSION,
            "thresholds": thresholds,
        })
        _enable(monkeypatch, p)

    def test_env_explicit_wins_over_yaml_floor(
        self, monkeypatch, tmp_path,
    ):
        from backend.core.ouroboros.governance.verification.confidence_monitor import (  # noqa: E501
            confidence_floor,
        )
        self._setup(monkeypatch, tmp_path, {"floor": 0.10})
        # Env explicit: 0.07 — should override YAML 0.10
        monkeypatch.setenv("JARVIS_CONFIDENCE_FLOOR", "0.07")
        assert confidence_floor() == 0.07

    def test_yaml_used_when_env_unset_floor(
        self, monkeypatch, tmp_path,
    ):
        from backend.core.ouroboros.governance.verification.confidence_monitor import (  # noqa: E501
            confidence_floor,
        )
        monkeypatch.delenv("JARVIS_CONFIDENCE_FLOOR", raising=False)
        self._setup(monkeypatch, tmp_path, {"floor": 0.10})
        assert confidence_floor() == 0.10

    def test_default_used_when_env_unset_and_yaml_disabled(
        self, monkeypatch,
    ):
        from backend.core.ouroboros.governance.verification.confidence_monitor import (  # noqa: E501
            confidence_floor,
        )
        monkeypatch.delenv("JARVIS_CONFIDENCE_FLOOR", raising=False)
        monkeypatch.delenv(
            "JARVIS_CONFIDENCE_LOAD_ADAPTED", raising=False,
        )
        assert confidence_floor() == 0.05  # hardcoded default

    def test_env_explicit_wins_over_yaml_window_k(
        self, monkeypatch, tmp_path,
    ):
        from backend.core.ouroboros.governance.verification.confidence_monitor import (  # noqa: E501
            confidence_window_k,
        )
        self._setup(monkeypatch, tmp_path, {"window_k": 4})
        monkeypatch.setenv("JARVIS_CONFIDENCE_WINDOW_K", "12")
        assert confidence_window_k() == 12

    def test_yaml_used_when_env_unset_window_k(
        self, monkeypatch, tmp_path,
    ):
        from backend.core.ouroboros.governance.verification.confidence_monitor import (  # noqa: E501
            confidence_window_k,
        )
        monkeypatch.delenv(
            "JARVIS_CONFIDENCE_WINDOW_K", raising=False,
        )
        self._setup(monkeypatch, tmp_path, {"window_k": 4})
        assert confidence_window_k() == 4

    def test_env_explicit_wins_over_yaml_approaching(
        self, monkeypatch, tmp_path,
    ):
        from backend.core.ouroboros.governance.verification.confidence_monitor import (  # noqa: E501
            confidence_approaching_factor,
        )
        self._setup(
            monkeypatch, tmp_path, {"approaching_factor": 2.5},
        )
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_APPROACHING_FACTOR", "1.8",
        )
        assert confidence_approaching_factor() == 1.8

    def test_yaml_used_when_env_unset_approaching(
        self, monkeypatch, tmp_path,
    ):
        from backend.core.ouroboros.governance.verification.confidence_monitor import (  # noqa: E501
            confidence_approaching_factor,
        )
        monkeypatch.delenv(
            "JARVIS_CONFIDENCE_APPROACHING_FACTOR", raising=False,
        )
        self._setup(
            monkeypatch, tmp_path, {"approaching_factor": 2.5},
        )
        assert confidence_approaching_factor() == 2.5

    def test_loosen_yaml_floor_falls_through_to_default(
        self, monkeypatch, tmp_path,
    ):
        """A loosen value in YAML is filtered out by the loader,
        so confidence_floor falls through to hardcoded default
        (NOT the loosen value)."""
        from backend.core.ouroboros.governance.verification.confidence_monitor import (  # noqa: E501
            confidence_floor,
        )
        monkeypatch.delenv("JARVIS_CONFIDENCE_FLOOR", raising=False)
        # baseline floor = 0.05; loosen attempt = 0.01
        self._setup(monkeypatch, tmp_path, {"floor": 0.01})
        # Loader drops the 0.01 value → fall-through to default 0.05
        assert confidence_floor() == 0.05


# ============================================================================
# §8 — Baseline pin (drift guard)
# ============================================================================


class TestBaselinesMatchConfidenceMonitorDefaults:
    """The loader has its own copy of the four baseline constants
    to avoid a circular import. This test pins them to
    confidence_monitor's defaults — drift triggers a clear
    failure rather than a silent weakening of the tighten-only
    filter."""

    def test_baselines_match_confidence_monitor_defaults(self):
        from backend.core.ouroboros.governance.verification import (
            confidence_monitor,
        )
        assert (
            baseline_floor() == confidence_monitor._DEFAULT_FLOOR
        )
        assert (
            baseline_window_k()
            == confidence_monitor._DEFAULT_WINDOW_K
        )
        assert (
            baseline_approaching_factor()
            == confidence_monitor._DEFAULT_APPROACHING_FACTOR
        )
        # confidence_monitor_enforce returns True post-graduation;
        # baseline_enforce is False because the YAML can never
        # *introduce* the looser False — see _filter_enforce.
        assert baseline_enforce() is False


# ============================================================================
# §9 — AST authority pins
# ============================================================================


_FORBIDDEN_AUTH_TOKENS = (
    "orchestrator", "iron_gate", "policy_engine",
    "risk_engine", "change_engine", "tool_executor",
    "providers", "candidate_generator", "semantic_guardian",
    "semantic_firewall", "scoped_tool_backend",
    "subagent_scheduler",
    # Confidence_monitor must NOT be imported here — strict
    # one-way: monitor depends on loader.
    "confidence_monitor",
    # Substrate is upstream of the loader; loader only reads YAML
    # the substrate's predicate has already ratified.
    "confidence_policy",
    "confidence_threshold_tightener",
)
_SUBPROC_TOKENS = (
    "subprocess" + ".",
    "os." + "system",
    "popen",
)
_ENV_MUTATION_TOKENS = (
    "os.environ[", "os.environ.pop", "os.environ.update",
    "os.put" + "env", "os.set" + "env",
)


class TestAuthorityInvariants:
    @pytest.fixture(scope="class")
    def source(self):
        return _MODULE_PATH.read_text(encoding="utf-8")

    def test_no_forbidden_imports(self, source):
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in _FORBIDDEN_AUTH_TOKENS:
                    assert f not in module, (
                        f"forbidden import contains {f!r}: "
                        f"{module}"
                    )

    def test_no_governance_imports_outside_loader_surface(
        self, source,
    ):
        """The loader is intentionally stdlib-only at module
        level — PyYAML is lazy-imported inside
        load_adapted_thresholds. No governance-layer imports."""
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert "governance" not in module, (
                    f"loader must be stdlib-only, found "
                    f"governance import: {module}"
                )

    def test_no_eval_exec_compile(self, source):
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(
                node.func, ast.Name,
            ):
                assert node.func.id not in (
                    "eval", "exec", "compile",
                ), f"forbidden bare call: {node.func.id}"

    def test_no_subprocess(self, source):
        for token in _SUBPROC_TOKENS:
            assert token not in source, (
                f"forbidden subprocess token: {token}"
            )

    def test_no_env_mutation(self, source):
        for token in _ENV_MUTATION_TOKENS:
            assert token not in source, (
                f"forbidden env mutation token: {token}"
            )

    def test_yaml_lazy_imported(self, source):
        """PyYAML must NOT appear in module-level imports — it's
        imported lazily inside load_adapted_thresholds so the
        import surface stays clean when the loader is disabled."""
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = []
                if isinstance(node, ast.Import):
                    names = [n.name for n in node.names]
                else:
                    names = [node.module or ""]
                for n in names:
                    assert n != "yaml", (
                        "yaml must be lazy-imported inside "
                        "load_adapted_thresholds, not at module "
                        "level"
                    )

    def test_only_one_filesystem_read_path(self, source):
        """Loader reads ``adapted_thresholds_path()`` only — no
        other paths. Guard against accidental multi-file growth
        (defense against attacker tricking loader into reading
        other files via a doctored env knob)."""
        # Easy heuristic: count the number of `.read_text` and
        # `.exists` calls — should be ≤ 1 each (guarded by the
        # single path resolution).
        tree = ast.parse(source)
        read_text_calls = 0
        exists_calls = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                if node.attr == "read_text":
                    read_text_calls += 1
                if node.attr == "exists":
                    exists_calls += 1
        assert read_text_calls <= 1, (
            f"loader has {read_text_calls} read_text calls "
            f"(expected ≤ 1)"
        )
        assert exists_calls <= 1, (
            f"loader has {exists_calls} exists calls "
            f"(expected ≤ 1)"
        )
