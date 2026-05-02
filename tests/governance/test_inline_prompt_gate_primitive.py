"""InlinePromptGate Slice 1 primitive — regression spine.

Tests the pure-stdlib phase-boundary primitive that bridges the
existing ``InlinePromptController`` Future-registry substrate to
the orchestrator's phase-boundary decision points.

Coverage:
  * Closed 5-value taxonomy (J.A.R.M.A.T.R.I.X.)
  * Frozen dataclass mutation guards
  * to_dict / from_dict round-trip
  * Total mapping function — every controller state → expected
    verdict, garbage input → DISABLED, NEVER raises
  * Phase C tightening stamp outcome-aware (passed for
    DENY/PAUSE_OP, empty for ALLOW/EXPIRED/DISABLED)
  * Master flag asymmetric env semantics
  * Env-knob clamping (timeout / summary / fingerprint)
  * Deterministic prompt-id derivation (idempotency)
  * Byte-parity to the live controller's STATE_* exports
  * No governance imports at module top (Slice 1 pure-stdlib pin)
"""
from __future__ import annotations

import ast
import pathlib
from dataclasses import FrozenInstanceError

import pytest

from backend.core.ouroboros.governance.inline_prompt_gate import (
    INLINE_PROMPT_GATE_SCHEMA_VERSION,
    PhaseInlinePromptRequest,
    PhaseInlinePromptVerdict,
    PhaseInlineVerdict,
    compute_phase_inline_verdict,
    default_prompt_timeout_s,
    derive_prompt_id,
    fingerprint_hex_chars,
    inline_prompt_gate_enabled,
    summary_max_chars,
    truncate_fingerprint,
    truncate_summary,
)


# ---------------------------------------------------------------------------
# Closed-taxonomy invariants
# ---------------------------------------------------------------------------


class TestClosedTaxonomy:
    def test_verdict_has_exactly_five_values(self):
        """Closed-taxonomy invariant. New verdict values require a
        scope-doc update + Slice 4 bridge update."""
        assert len(list(PhaseInlineVerdict)) == 5

    def test_verdict_value_set_exact(self):
        expected = {"allow", "deny", "pause_op", "expired", "disabled"}
        actual = {v.value for v in PhaseInlineVerdict}
        assert actual == expected

    def test_verdict_string_enum(self):
        """Each verdict is a str (str-enum) for SSE-broker
        serialization without a custom encoder."""
        for v in PhaseInlineVerdict:
            assert isinstance(v.value, str)
            assert isinstance(v, str)


# ---------------------------------------------------------------------------
# Frozen dataclass guards
# ---------------------------------------------------------------------------


class TestFrozenRequest:
    def _req(self, **overrides) -> PhaseInlinePromptRequest:
        defaults: dict = {
            "prompt_id": "ipg-test-1",
            "op_id": "op-x",
            "phase_at_request": "GATE",
            "risk_tier": "NOTIFY_APPLY",
            "change_summary": "edit foo.py: rename helper",
            "change_fingerprint": "a" * 64,
            "target_paths": ("backend/foo.py",),
        }
        defaults.update(overrides)
        return PhaseInlinePromptRequest(**defaults)

    def test_request_is_frozen(self):
        r = self._req()
        with pytest.raises(FrozenInstanceError):
            r.prompt_id = "mutated"  # type: ignore[misc]

    def test_request_target_paths_is_tuple(self):
        """target_paths must be a tuple (frozen-friendly). Passing
        a list still constructs but field is preserved as given."""
        r = self._req(target_paths=("a.py", "b.py"))
        assert isinstance(r.target_paths, tuple)
        assert r.target_paths == ("a.py", "b.py")

    def test_request_default_schema_version(self):
        r = self._req()
        assert r.schema_version == INLINE_PROMPT_GATE_SCHEMA_VERSION
        assert r.schema_version == "inline_prompt_gate.1"

    def test_request_to_dict_round_trip(self):
        r = self._req(
            rationale="model says it's safe",
            route="background",
            created_ts=12345.6,
            timeout_s=60.0,
        )
        d = r.to_dict()
        r2 = PhaseInlinePromptRequest.from_dict(d)
        assert r2 == r

    def test_request_from_dict_tolerates_missing_fields(self):
        r = PhaseInlinePromptRequest.from_dict({})
        assert r.prompt_id == ""
        assert r.target_paths == ()
        assert r.schema_version == INLINE_PROMPT_GATE_SCHEMA_VERSION

    def test_request_from_dict_tolerates_garbage_paths(self):
        r = PhaseInlinePromptRequest.from_dict(
            {"target_paths": "not-a-list"},
        )
        assert r.target_paths == ()

    def test_request_from_dict_coerces_path_elements_to_str(self):
        r = PhaseInlinePromptRequest.from_dict(
            {"target_paths": [1, 2, 3]},
        )
        assert r.target_paths == ("1", "2", "3")

    def test_request_from_dict_never_raises(self):
        bad_inputs = [
            {"created_ts": "not-a-float"},
            {"timeout_s": object()},
            {"prompt_id": None, "op_id": None},
        ]
        for bad in bad_inputs:
            r = PhaseInlinePromptRequest.from_dict(bad)
            assert isinstance(r, PhaseInlinePromptRequest)


class TestFrozenVerdict:
    def test_verdict_is_frozen(self):
        v = PhaseInlinePromptVerdict(
            prompt_id="ipg-1", op_id="op-x",
            verdict=PhaseInlineVerdict.ALLOW,
        )
        with pytest.raises(FrozenInstanceError):
            v.verdict = PhaseInlineVerdict.DENY  # type: ignore[misc]

    def test_verdict_to_dict_round_trip(self):
        v = PhaseInlinePromptVerdict(
            prompt_id="ipg-1", op_id="op-x",
            verdict=PhaseInlineVerdict.DENY,
            elapsed_s=12.5, reviewer="repl",
            operator_reason="risky",
            monotonic_tightening_verdict="passed",
        )
        v2 = PhaseInlinePromptVerdict.from_dict(v.to_dict())
        assert v2 == v

    def test_verdict_from_dict_unknown_verdict_degrades_to_disabled(self):
        v = PhaseInlinePromptVerdict.from_dict(
            {"verdict": "not-a-real-verdict"},
        )
        assert v.verdict is PhaseInlineVerdict.DISABLED

    def test_verdict_from_dict_never_raises(self):
        bad_inputs: list = [
            {},
            {"verdict": None},
            {"elapsed_s": "not-a-float"},
            {"verdict": object()},
        ]
        for bad in bad_inputs:
            v = PhaseInlinePromptVerdict.from_dict(bad)
            assert isinstance(v, PhaseInlinePromptVerdict)

    def test_is_terminal_true_for_every_verdict(self):
        for verdict in PhaseInlineVerdict:
            v = PhaseInlinePromptVerdict(
                prompt_id="x", op_id="x", verdict=verdict,
            )
            assert v.is_terminal

    def test_is_tightening_true_only_for_deny_and_pause(self):
        tightening = {PhaseInlineVerdict.DENY, PhaseInlineVerdict.PAUSE_OP}
        for verdict in PhaseInlineVerdict:
            v = PhaseInlinePromptVerdict(
                prompt_id="x", op_id="x", verdict=verdict,
            )
            assert v.is_tightening == (verdict in tightening)

    def test_allowed_property_only_true_for_allow(self):
        for verdict in PhaseInlineVerdict:
            v = PhaseInlinePromptVerdict(
                prompt_id="x", op_id="x", verdict=verdict,
            )
            assert v.allowed == (verdict is PhaseInlineVerdict.ALLOW)


# ---------------------------------------------------------------------------
# Total mapping — controller state → verdict
# ---------------------------------------------------------------------------


class TestComputeVerdictMapping:
    @pytest.mark.parametrize(
        "state, expected",
        [
            ("allowed", PhaseInlineVerdict.ALLOW),
            ("denied", PhaseInlineVerdict.DENY),
            ("expired", PhaseInlineVerdict.EXPIRED),
            ("paused", PhaseInlineVerdict.PAUSE_OP),
        ],
    )
    def test_known_state_maps_to_expected_verdict(
        self, state: str, expected: PhaseInlineVerdict,
    ):
        v = compute_phase_inline_verdict(
            prompt_id="ipg-1", op_id="op-x", state=state,
        )
        assert v.verdict is expected

    @pytest.mark.parametrize(
        "garbage",
        [
            None, "", "   ", "ALLOWED-typo", "unknown_state",
            "completed", "failed", "running",
        ],
    )
    def test_garbage_state_degrades_to_disabled(self, garbage):
        v = compute_phase_inline_verdict(
            prompt_id="ipg-1", op_id="op-x", state=garbage,
        )
        assert v.verdict is PhaseInlineVerdict.DISABLED

    def test_non_string_state_degrades_to_disabled(self):
        v = compute_phase_inline_verdict(
            prompt_id="ipg-1", op_id="op-x", state=42,  # type: ignore[arg-type]
        )
        assert v.verdict is PhaseInlineVerdict.DISABLED

    def test_master_flag_off_short_circuits_to_disabled(self):
        """enabled=False → DISABLED even with valid state. The
        master-flag-off path never reached the operator."""
        v = compute_phase_inline_verdict(
            prompt_id="ipg-1", op_id="op-x", state="allowed",
            enabled=False,
        )
        assert v.verdict is PhaseInlineVerdict.DISABLED

    def test_compute_never_raises_on_garbage(self):
        """Last-resort defensive: arbitrary garbage must not raise."""
        garbage_inputs: list = [
            {"prompt_id": None, "op_id": None, "state": object()},
            {"prompt_id": object(), "op_id": object(), "state": None},
            {
                "prompt_id": "x", "op_id": "x", "state": "allowed",
                "elapsed_s": object(),
            },
            {
                "prompt_id": "x", "op_id": "x", "state": "allowed",
                "reviewer": object(),
            },
        ]
        for kw in garbage_inputs:
            v = compute_phase_inline_verdict(**kw)
            assert isinstance(v, PhaseInlinePromptVerdict)

    def test_elapsed_negative_clamped_to_zero(self):
        v = compute_phase_inline_verdict(
            prompt_id="x", op_id="x", state="allowed", elapsed_s=-5.0,
        )
        assert v.elapsed_s == 0.0

    def test_compute_propagates_metadata(self):
        v = compute_phase_inline_verdict(
            prompt_id="ipg-1", op_id="op-x", state="denied",
            elapsed_s=33.5, reviewer="repl_operator",
            operator_reason="rejected — touches credentials",
        )
        assert v.prompt_id == "ipg-1"
        assert v.op_id == "op-x"
        assert v.elapsed_s == 33.5
        assert v.reviewer == "repl_operator"
        assert v.operator_reason == "rejected — touches credentials"


# ---------------------------------------------------------------------------
# Phase C MonotonicTighteningVerdict stamping
# ---------------------------------------------------------------------------


class TestPhaseCTighteningStamp:
    def test_deny_stamps_passed(self):
        v = compute_phase_inline_verdict(
            prompt_id="x", op_id="x", state="denied",
        )
        assert v.monotonic_tightening_verdict == "passed"

    def test_pause_op_stamps_passed(self):
        v = compute_phase_inline_verdict(
            prompt_id="x", op_id="x", state="paused",
        )
        assert v.monotonic_tightening_verdict == "passed"

    def test_allow_stamps_empty(self):
        """ALLOW is operator-confirmed continuation, not a tightening
        — auto-apply would have done the same thing absent the prompt."""
        v = compute_phase_inline_verdict(
            prompt_id="x", op_id="x", state="allowed",
        )
        assert v.monotonic_tightening_verdict == ""

    def test_expired_stamps_empty(self):
        """EXPIRED is fall-through to current behavior — no
        tightening signal."""
        v = compute_phase_inline_verdict(
            prompt_id="x", op_id="x", state="expired",
        )
        assert v.monotonic_tightening_verdict == ""

    def test_disabled_stamps_empty(self):
        v = compute_phase_inline_verdict(
            prompt_id="x", op_id="x", state="allowed", enabled=False,
        )
        assert v.monotonic_tightening_verdict == ""


# ---------------------------------------------------------------------------
# Master flag — asymmetric env semantics
# ---------------------------------------------------------------------------


class TestMasterFlagSemantics:
    def test_default_is_false_pre_graduation(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_INLINE_PROMPT_GATE_ENABLED", raising=False,
        )
        assert inline_prompt_gate_enabled() is False

    def test_empty_string_is_default_false(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INLINE_PROMPT_GATE_ENABLED", "",
        )
        assert inline_prompt_gate_enabled() is False

    def test_whitespace_is_default_false(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INLINE_PROMPT_GATE_ENABLED", "   ",
        )
        assert inline_prompt_gate_enabled() is False

    @pytest.mark.parametrize("truthy", ["1", "true", "yes", "on", "TRUE", "Yes"])
    def test_truthy_values_enable(self, monkeypatch, truthy: str):
        monkeypatch.setenv(
            "JARVIS_INLINE_PROMPT_GATE_ENABLED", truthy,
        )
        assert inline_prompt_gate_enabled() is True

    @pytest.mark.parametrize("falsy", ["0", "false", "no", "off", "False", "OFF"])
    def test_falsy_values_disable(self, monkeypatch, falsy: str):
        monkeypatch.setenv(
            "JARVIS_INLINE_PROMPT_GATE_ENABLED", falsy,
        )
        assert inline_prompt_gate_enabled() is False


# ---------------------------------------------------------------------------
# Env-knob clamping
# ---------------------------------------------------------------------------


class TestEnvKnobClamping:
    def test_timeout_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_INLINE_PROMPT_GATE_TIMEOUT_S", raising=False,
        )
        assert default_prompt_timeout_s() == 60.0

    def test_timeout_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INLINE_PROMPT_GATE_TIMEOUT_S", "0.001",
        )
        assert default_prompt_timeout_s() == 1.0

    def test_timeout_ceiling(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INLINE_PROMPT_GATE_TIMEOUT_S", "999999",
        )
        assert default_prompt_timeout_s() == 3600.0

    def test_timeout_garbage_uses_default(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INLINE_PROMPT_GATE_TIMEOUT_S", "not-a-number",
        )
        assert default_prompt_timeout_s() == 60.0

    def test_summary_max_chars_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_INLINE_PROMPT_GATE_SUMMARY_MAX_CHARS", raising=False,
        )
        assert summary_max_chars() == 200

    def test_summary_max_chars_floor_and_ceiling(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INLINE_PROMPT_GATE_SUMMARY_MAX_CHARS", "1",
        )
        assert summary_max_chars() == 16
        monkeypatch.setenv(
            "JARVIS_INLINE_PROMPT_GATE_SUMMARY_MAX_CHARS", "99999",
        )
        assert summary_max_chars() == 1024

    def test_fingerprint_chars_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_INLINE_PROMPT_GATE_FINGERPRINT_HEX_CHARS",
            raising=False,
        )
        assert fingerprint_hex_chars() == 16

    def test_fingerprint_chars_floor_and_ceiling(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INLINE_PROMPT_GATE_FINGERPRINT_HEX_CHARS", "1",
        )
        assert fingerprint_hex_chars() == 8
        monkeypatch.setenv(
            "JARVIS_INLINE_PROMPT_GATE_FINGERPRINT_HEX_CHARS", "999",
        )
        assert fingerprint_hex_chars() == 64


# ---------------------------------------------------------------------------
# Truncation helpers
# ---------------------------------------------------------------------------


class TestTruncationHelpers:
    def test_short_summary_unchanged(self):
        assert truncate_summary("hello") == "hello"

    def test_long_summary_truncated_with_marker(self):
        long_text = "x" * 500
        out = truncate_summary(long_text, max_chars=50)
        assert out.endswith("...<truncated>")
        assert len(out) <= 50

    def test_truncate_summary_never_raises_on_none(self):
        assert truncate_summary(None) == ""  # type: ignore[arg-type]

    def test_truncate_fingerprint_default_length(self):
        full = "a" * 64
        assert truncate_fingerprint(full) == "a" * 16

    def test_truncate_fingerprint_custom_length(self):
        full = "a" * 64
        assert truncate_fingerprint(full, hex_chars=24) == "a" * 24

    def test_truncate_fingerprint_clamps_floor(self):
        full = "a" * 64
        assert len(truncate_fingerprint(full, hex_chars=2)) == 8

    def test_truncate_fingerprint_clamps_ceiling(self):
        full = "a" * 64
        assert len(truncate_fingerprint(full, hex_chars=999)) == 64


# ---------------------------------------------------------------------------
# Deterministic prompt-id derivation
# ---------------------------------------------------------------------------


class TestDerivePromptId:
    def test_idempotent_same_inputs_same_id(self):
        a = derive_prompt_id(
            op_id="op-x", change_fingerprint="a" * 64, phase="GATE",
        )
        b = derive_prompt_id(
            op_id="op-x", change_fingerprint="a" * 64, phase="GATE",
        )
        assert a == b

    def test_different_op_id_different_id(self):
        a = derive_prompt_id(
            op_id="op-x", change_fingerprint="a" * 64,
        )
        b = derive_prompt_id(
            op_id="op-y", change_fingerprint="a" * 64,
        )
        assert a != b

    def test_different_fingerprint_different_id(self):
        a = derive_prompt_id(
            op_id="op-x", change_fingerprint="a" * 64,
        )
        b = derive_prompt_id(
            op_id="op-x", change_fingerprint="b" * 64,
        )
        assert a != b

    def test_different_phase_different_id(self):
        a = derive_prompt_id(
            op_id="op-x", change_fingerprint="a" * 64, phase="GATE",
        )
        b = derive_prompt_id(
            op_id="op-x", change_fingerprint="a" * 64, phase="APPLY",
        )
        assert a != b

    def test_id_format_is_ipg_prefix_sha256(self):
        pid = derive_prompt_id(
            op_id="op-x", change_fingerprint="a" * 64,
        )
        assert pid.startswith("ipg-")
        # ipg- (4) + 24 hex chars
        assert len(pid) == 28

    def test_derive_never_raises_on_garbage(self):
        # All garbage degrades to coerced str() based hash.
        pid = derive_prompt_id(
            op_id=None,  # type: ignore[arg-type]
            change_fingerprint=object(),  # type: ignore[arg-type]
        )
        assert pid.startswith("ipg-")


# ---------------------------------------------------------------------------
# Byte-parity to the live controller's STATE_* constants
# ---------------------------------------------------------------------------


class TestControllerStateByteParity:
    """The Slice 1 module redefines the controller's STATE_*
    string constants verbatim to stay pure-stdlib (zero governance
    imports). This test asserts byte-parity to the live exports —
    if the controller renames a state, this pin fails BEFORE the
    Slice 2 producer ships out-of-sync."""

    def test_state_constants_byte_parity(self):
        from backend.core.ouroboros.governance import (
            inline_permission_prompt as live,
        )
        from backend.core.ouroboros.governance.inline_prompt_gate import (
            _CONTROLLER_STATE_ALLOWED,
            _CONTROLLER_STATE_DENIED,
            _CONTROLLER_STATE_EXPIRED,
            _CONTROLLER_STATE_PAUSED,
        )
        assert _CONTROLLER_STATE_ALLOWED == live.STATE_ALLOWED
        assert _CONTROLLER_STATE_DENIED == live.STATE_DENIED
        assert _CONTROLLER_STATE_EXPIRED == live.STATE_EXPIRED
        assert _CONTROLLER_STATE_PAUSED == live.STATE_PAUSED


# ---------------------------------------------------------------------------
# Slice 1 authority invariant — pure-stdlib at module top
# ---------------------------------------------------------------------------


class TestPureStdlibInvariant:
    """Slice 1 primitive must NOT import any governance module at
    module top — the bridge happens at Slice 2's producer.
    AST-walked pin (mirrors counterfactual_replay_pure_stdlib +
    Move 5/6/Priority #1/#2/#3/#4/#5 Slice 1 pattern)."""

    def test_no_governance_imports_at_module_top(self):
        path = (
            pathlib.Path(__file__).parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "inline_prompt_gate.py"
        )
        source = path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if "backend." in module or "governance" in module:
                    raise AssertionError(
                        f"Slice 1 must be pure-stdlib — found "
                        f"governance import {module!r} at line "
                        f"{getattr(node, 'lineno', '?')}"
                    )

    def test_no_async_def_in_module(self):
        """Slice 1 stays sync; Slice 2 wraps via the controller's
        existing async Future surface."""
        path = (
            pathlib.Path(__file__).parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "inline_prompt_gate.py"
        )
        source = path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                raise AssertionError(
                    f"Slice 1 must be sync — found async def "
                    f"{node.name!r} at line "
                    f"{getattr(node, 'lineno', '?')}"
                )

    def test_no_exec_eval_compile_calls(self):
        """Critical safety pin (mirrors Move 6 Slice 2 + Priority
        #1/#2/#3/#4/#5 Slice 1)."""
        path = (
            pathlib.Path(__file__).parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "inline_prompt_gate.py"
        )
        source = path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        raise AssertionError(
                            f"Slice 1 must NOT exec/eval/compile "
                            f"— found {node.func.id}() at line "
                            f"{getattr(node, 'lineno', '?')}"
                        )


# ---------------------------------------------------------------------------
# Schema version sanity
# ---------------------------------------------------------------------------


class TestSchemaVersion:
    def test_schema_version_constant(self):
        assert INLINE_PROMPT_GATE_SCHEMA_VERSION == "inline_prompt_gate.1"

    def test_schema_version_default_on_request(self):
        r = PhaseInlinePromptRequest(
            prompt_id="x", op_id="x",
            phase_at_request="GATE", risk_tier="NOTIFY_APPLY",
            change_summary="x", change_fingerprint="x",
            target_paths=(),
        )
        assert r.schema_version == "inline_prompt_gate.1"

    def test_schema_version_default_on_verdict(self):
        v = PhaseInlinePromptVerdict(
            prompt_id="x", op_id="x",
            verdict=PhaseInlineVerdict.DISABLED,
        )
        assert v.schema_version == "inline_prompt_gate.1"
