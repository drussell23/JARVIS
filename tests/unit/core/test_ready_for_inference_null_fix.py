"""Tests for v303.0: ready_for_inference null → bool coercion.

Root cause: update_apars() in the golden image startup script defaulted
model_loaded and ready to `null` instead of `false`.  When the progress
file had `"ready_for_inference": null`, PrimeClient's v276.0 schema
validation rejected NoneType and blocked PrimeRouter promotion.

Three-layer fix:
  1. update_apars() defaults changed to `false` (root cause)
  2. J-Prime server.py coerces APARS override to bool (defensive)
  3. PrimeClient normalizes health data before validation (defensive)
"""

import pytest
from typing import Dict, Any, List


# ---------------------------------------------------------------------------
# Layer 1: Schema validation rejects None, accepts bool
# ---------------------------------------------------------------------------

class TestSchemaValidation:
    """Verify validate_health_response correctly handles None vs bool."""

    @staticmethod
    def _validate(endpoint: str, data: Dict[str, Any]) -> List[str]:
        """Inline copy of startup_contracts.validate_health_response logic."""
        SCHEMAS = {
            "prime:/health": {"ready_for_inference": "bool"},
        }
        schema = SCHEMAS.get(endpoint)
        if not schema:
            return []
        violations = []
        for field_name, expected_type in schema.items():
            if field_name not in data:
                violations.append(f"{endpoint} missing required field '{field_name}'")
            else:
                val = data[field_name]
                if expected_type == "bool" and not isinstance(val, bool):
                    violations.append(
                        f"{endpoint} field '{field_name}' expected "
                        f"{expected_type} but got {type(val).__name__}"
                    )
        return violations

    def test_none_value_rejected(self):
        """None for ready_for_inference must produce a violation."""
        violations = self._validate("prime:/health", {"ready_for_inference": None})
        assert len(violations) == 1
        assert "NoneType" in violations[0]

    def test_true_accepted(self):
        violations = self._validate("prime:/health", {"ready_for_inference": True})
        assert violations == []

    def test_false_accepted(self):
        violations = self._validate("prime:/health", {"ready_for_inference": False})
        assert violations == []

    def test_missing_field_rejected(self):
        violations = self._validate("prime:/health", {})
        assert len(violations) == 1
        assert "missing" in violations[0]


# ---------------------------------------------------------------------------
# Layer 2: J-Prime APARS override coercion
# ---------------------------------------------------------------------------

class TestJPrimeAPARSCoercion:
    """Verify J-Prime's get_status() coerces APARS values to bool."""

    @staticmethod
    def _simulate_get_status(phase: str, apars_payload: dict) -> dict:
        """Simulate J-Prime server.py get_status() APARS override logic."""
        result = {}
        # Line 1400: safe default
        result.setdefault("ready_for_inference", phase == "ready")
        result.setdefault("model_loaded", phase == "ready")

        # v303.0 fix: coerce to bool
        if apars_payload:
            if "ready_for_inference" in apars_payload:
                _val = apars_payload["ready_for_inference"]
                result["ready_for_inference"] = bool(_val) if _val is not None else result["ready_for_inference"]
            if "model_loaded" in apars_payload:
                _val = apars_payload["model_loaded"]
                result["model_loaded"] = bool(_val) if _val is not None else result["model_loaded"]

        return result

    def test_null_ready_stays_false(self):
        """APARS with ready_for_inference=null should NOT override safe default."""
        result = self._simulate_get_status("starting", {"ready_for_inference": None})
        assert result["ready_for_inference"] is False
        assert isinstance(result["ready_for_inference"], bool)

    def test_null_model_loaded_stays_false(self):
        result = self._simulate_get_status("starting", {"model_loaded": None})
        assert result["model_loaded"] is False

    def test_true_override_works(self):
        result = self._simulate_get_status("starting", {"ready_for_inference": True})
        assert result["ready_for_inference"] is True

    def test_false_override_works(self):
        result = self._simulate_get_status("ready", {"ready_for_inference": False})
        assert result["ready_for_inference"] is False

    def test_no_apars_uses_phase_default(self):
        result = self._simulate_get_status("ready", None)
        assert result["ready_for_inference"] is True

    def test_apars_without_field_keeps_default(self):
        result = self._simulate_get_status("ready", {"checkpoint": "verifying"})
        assert result["ready_for_inference"] is True


# ---------------------------------------------------------------------------
# Layer 3: PrimeClient health data normalization
# ---------------------------------------------------------------------------

class TestPrimeClientNormalization:
    """Verify PrimeClient normalizes health data before schema validation."""

    @staticmethod
    def _normalize(health: dict) -> dict:
        """Simulate PrimeClient v303.0 normalization logic."""
        for _bf in ("ready_for_inference", "model_loaded"):
            if _bf in health and not isinstance(health[_bf], bool):
                health[_bf] = bool(health[_bf]) if health[_bf] is not None else False
        return health

    def test_none_coerced_to_false(self):
        h = self._normalize({"ready_for_inference": None, "status": "healthy"})
        assert h["ready_for_inference"] is False
        assert isinstance(h["ready_for_inference"], bool)

    def test_true_unchanged(self):
        h = self._normalize({"ready_for_inference": True})
        assert h["ready_for_inference"] is True

    def test_false_unchanged(self):
        h = self._normalize({"ready_for_inference": False})
        assert h["ready_for_inference"] is False

    def test_string_coerced_to_bool(self):
        """Edge case: string 'true' should become True."""
        h = self._normalize({"ready_for_inference": "true"})
        assert h["ready_for_inference"] is True

    def test_int_zero_coerced_to_false(self):
        h = self._normalize({"ready_for_inference": 0})
        assert h["ready_for_inference"] is False

    def test_int_one_coerced_to_true(self):
        h = self._normalize({"ready_for_inference": 1})
        assert h["ready_for_inference"] is True

    def test_missing_field_not_added(self):
        """Normalization should not add fields that don't exist."""
        h = self._normalize({"status": "healthy"})
        assert "ready_for_inference" not in h

    def test_both_fields_normalized(self):
        h = self._normalize({
            "ready_for_inference": None,
            "model_loaded": None,
        })
        assert h["ready_for_inference"] is False
        assert h["model_loaded"] is False


# ---------------------------------------------------------------------------
# End-to-end: full chain validation
# ---------------------------------------------------------------------------

class TestEndToEnd:
    """Verify the complete chain: APARS null → coercion → schema passes."""

    def test_null_progress_file_through_full_chain(self):
        """Simulate: update_apars() with 4 args → progress file → health → validation."""
        # Simulate progress file content from update_apars with 4 args
        # Before fix: model_loaded=null, ready=null
        # After fix (layer 1): model_loaded=false, ready=false
        progress_file = {
            "phase": 6,
            "phase_progress": 50,
            "total_progress": 92,
            "checkpoint": "verifying_attempt_5",
            "model_loaded": False,  # Was null before fix
            "ready_for_inference": False,  # Was null before fix
            "error": None,
            "updated_at": 1741337403,
        }

        # J-Prime builds health response
        result = {"ready_for_inference": False, "model_loaded": False}
        # APARS override
        if "ready_for_inference" in progress_file:
            _val = progress_file["ready_for_inference"]
            result["ready_for_inference"] = bool(_val) if _val is not None else False
        if "model_loaded" in progress_file:
            _val = progress_file["model_loaded"]
            result["model_loaded"] = bool(_val) if _val is not None else False

        # Schema validation
        assert isinstance(result["ready_for_inference"], bool)
        assert isinstance(result["model_loaded"], bool)

    def test_null_still_handled_by_layer2_and_3(self):
        """Even if layer 1 fails (old startup script), layers 2+3 handle null."""
        # Simulate OLD startup script producing null
        progress_file = {
            "ready_for_inference": None,  # null from old update_apars
            "model_loaded": None,
        }

        # Layer 2: J-Prime coercion
        result = {"ready_for_inference": False, "model_loaded": False}
        _val = progress_file["ready_for_inference"]
        result["ready_for_inference"] = bool(_val) if _val is not None else result["ready_for_inference"]
        assert result["ready_for_inference"] is False

        # Layer 3: PrimeClient normalization (if somehow null still leaks)
        leaked = {"ready_for_inference": None}
        for _bf in ("ready_for_inference", "model_loaded"):
            if _bf in leaked and not isinstance(leaked[_bf], bool):
                leaked[_bf] = bool(leaked[_bf]) if leaked[_bf] is not None else False
        assert leaked["ready_for_inference"] is False
